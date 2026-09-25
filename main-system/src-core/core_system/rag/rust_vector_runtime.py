"""Rust vector engine runtime — governed takeover of the canonical dense
vector index (codex A610 DATA-ARCHITECTURE-TARGET).

``RustVectorRuntime`` subclasses :class:`QdrantCanonicalRuntime` so the
entire pipeline surface (search / upsert / delete / counts / generation
aliases / health gate) keeps working unchanged — only the transport and
process ownership differ: instead of a Qdrant server the runtime drives
``vectord``, a loopback-only Rust service holding HNSW ANN indexes as
**rebuildable derived data**.  PostgreSQL remains the formal data
authority; scope/revision/tombstone resolution happens there on the
candidate IDs this engine returns (DATA-SAFETY clause).

Fail-closed rules mirror the Qdrant contract: non-loopback URLs are
BLOCKED, dimension mismatch is INDEX_MISMATCH, module scope stays
mandatory through :func:`require_scope`/`assert_payload_scoped`, and an
unreachable engine degrades rather than substituting another authority.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

from .rag_qdrant import QdrantCanonicalRuntime, RagPipelineConfig, is_loopback_url

_logger = logging.getLogger("gptbridge.rag.vectord")

VECTORD_CONTRACT = "vectord/v1"
DEFAULT_VECTORD_URL = "http://127.0.0.1:8092"


def _post(url: str, path: str, payload: Optional[dict] = None, timeout: float = 30.0) -> dict:
    """Synchronous JSON POST against the loopback vectord service."""
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{url.rstrip('/')}{path}",
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "GPTBridge/vectord-client"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return body


def _get(url: str, path: str, timeout: float = 5.0) -> dict:
    req = urllib.request.Request(
        f"{url.rstrip('/')}{path}",
        headers={"User-Agent": "GPTBridge/vectord-client"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _condition_to_json(condition: Any) -> dict:
    """Translate one Qdrant ``FieldCondition`` into the vectord filter DSL."""
    key = str(getattr(condition, "key", "") or "")
    match = getattr(condition, "match", None)
    if not key or match is None:
        raise ValueError(f"VECTOR_FILTER_UNSUPPORTED:{type(condition).__name__}")
    any_values = getattr(match, "any", None)
    if any_values is not None:
        return {"key": key, "match": {"any": list(any_values)}}
    value = getattr(match, "value", None)
    return {"key": key, "match": {"value": value}}


def _filter_to_json(query_filter: Any) -> dict:
    """Translate a Qdrant ``Filter`` into the vectord filter DSL."""
    if query_filter is None:
        return {}
    out: dict[str, Any] = {}
    for clause in ("must", "should", "must_not"):
        conditions = getattr(query_filter, clause, None) or []
        translated = [_condition_to_json(c) for c in conditions]
        if translated:
            out[clause] = translated
    return out


class VectordClient:
    """Duck-typed stand-in for ``qdrant_client.QdrantClient``.

    The pipeline, generation manager and health gate reach the vector
    store through the small QdrantClient surface used in this codebase;
    each method below translates that call into the vectord HTTP contract
    and returns ``SimpleNamespace`` objects shaped like the qdrant
    models the callers destructure (``.collections[].name``,
    ``.points_count``, ``.config.params.vectors.size``, ``.points[].id``
    and so on).
    """

    def __init__(self, url: str) -> None:
        self._url = url.rstrip("/")

    # -- internal ---------------------------------------------------------
    def _call(self, path: str, payload: Optional[dict] = None) -> dict:
        body = _post(self._url, path, payload)
        if body.get("ok") is not True:
            raise RuntimeError(str(body.get("error") or "VECTORD_ERROR"))
        return body

    # -- collections ------------------------------------------------------
    def get_collections(self) -> Any:
        body = self._call("/v1/collections/list")
        names = body.get("collections") or []
        return SimpleNamespace(
            collections=[SimpleNamespace(name=n) for n in names]
        )

    def create_collection(self, collection_name: str, vectors_config: Any = None, **_: Any) -> None:
        size = getattr(vectors_config, "size", None)
        if size is None and isinstance(vectors_config, dict):
            size = vectors_config.get("size")
        self._call(
            "/v1/collections/ensure",
            {"name": collection_name, "dimension": int(size or 0)},
        )

    def get_collection(self, collection_name: str) -> Any:
        body = self._call("/v1/collections/info", {"name": collection_name})
        return SimpleNamespace(
            points_count=int(body.get("points_count") or 0),
            config=SimpleNamespace(
                params=SimpleNamespace(
                    vectors=SimpleNamespace(size=int(body.get("dimension") or 0))
                )
            ),
        )

    def delete_collection(self, collection_name: str, **_: Any) -> bool:
        body = self._call("/v1/collections/delete", {"name": collection_name})
        return bool(body.get("existed"))

    def create_payload_index(self, **_: Any) -> None:
        # vectord evaluates payload filters natively; no secondary index
        # maintenance exists (and none is needed for correctness).
        return None

    # -- points -----------------------------------------------------------
    def upsert(self, collection_name: str, points: Any, wait: bool = True, **_: Any) -> None:
        del wait
        items = []
        for point in points:
            if isinstance(point, dict):
                pid = point.get("id")
                vector = point.get("vector")
                payload = point.get("payload") or {}
            else:
                pid = getattr(point, "id", None)
                vector = getattr(point, "vector", None)
                payload = getattr(point, "payload", None) or {}
            items.append(
                {"id": str(pid), "vector": [float(v) for v in vector], "payload": payload}
            )
        self._call("/v1/points/upsert", {"collection": collection_name, "points": items})

    def query_points(
        self,
        collection_name: str,
        query: Any,
        query_filter: Any = None,
        limit: int = 10,
        score_threshold: Optional[float] = None,
        with_payload: bool = True,
        with_vectors: bool = False,
        **_: Any,
    ) -> Any:
        del with_payload, with_vectors
        body = self._call(
            "/v1/search",
            {
                "collection": collection_name,
                "vector": [float(v) for v in query],
                "top_k": int(limit),
                "score_threshold": float(score_threshold or 0.0),
                "filter": _filter_to_json(query_filter),
            },
        )
        hits = [
            SimpleNamespace(id=h["id"], score=h["score"], payload=h.get("payload"))
            for h in (body.get("hits") or [])
        ]
        return SimpleNamespace(points=hits)

    def delete(self, collection_name: str, points_selector: Any, wait: bool = True, **_: Any) -> None:
        del wait
        self._call(
            "/v1/points/delete",
            {"collection": collection_name, "filter": _filter_to_json(points_selector)},
        )

    def count(self, collection_name: str, count_filter: Any = None, exact: bool = True, **_: Any) -> Any:
        del exact
        body = self._call(
            "/v1/points/count",
            {"collection": collection_name, "filter": _filter_to_json(count_filter)},
        )
        return SimpleNamespace(count=int(body.get("count") or 0))

    # -- aliases ----------------------------------------------------------
    def create_alias(self, alias_name: str, collection_name: str, **_: Any) -> None:
        self._call(
            "/v1/aliases/set", {"alias": alias_name, "collection": collection_name}
        )

    def delete_alias(self, alias_name: str, **_: Any) -> None:
        self._call("/v1/aliases/delete", {"alias": alias_name})

    def get_aliases(self) -> Any:
        body = self._call("/v1/aliases/list")
        return SimpleNamespace(
            aliases=[
                SimpleNamespace(
                    alias_name=a.get("alias_name"),
                    collection_name=a.get("collection_name"),
                )
                for a in (body.get("aliases") or [])
            ]
        )

    def update_aliases(self, change_aliases_operations: Any = None, **_: Any) -> None:
        """Batch alias ops (CreateAliasOperation / DeleteAliasOperation)."""
        for op in change_aliases_operations or []:
            create = getattr(op, "create_alias", None)
            delete = getattr(op, "delete_alias", None)
            if create is not None:
                self.create_alias(
                    alias_name=getattr(create, "alias_name"),
                    collection_name=getattr(create, "collection_name"),
                )
            elif delete is not None:
                self.delete_alias(alias_name=getattr(delete, "alias_name"))

    def close(self) -> None:  # pragma: no cover - symmetry with QdrantClient
        return None


class RustVectorRuntime(QdrantCanonicalRuntime):
    """Canonical vector runtime backed by the governed Rust engine.

    Behaviour contract is inherited from ``QdrantCanonicalRuntime``;
    ``initialize`` additionally enforces the loopback rule on the vectord
    URL and can lazily build/start the managed binary (same pattern as
    the governed ``searchd`` provider), remaining fail-closed throughout.
    """

    def __init__(self, config: RagPipelineConfig) -> None:
        super().__init__(config)
        self.vectord_url = str(
            getattr(config, "vectord_url", "") or DEFAULT_VECTORD_URL
        )
        self.auto_start = bool(getattr(config, "vectord_auto_start", True))

    def _probe(self) -> bool:
        try:
            body = _get(self.vectord_url, "/healthz", timeout=1.0)
            return body.get("ok") is True
        except Exception:
            return False

    def _binary_path(self) -> Path:
        repo_root = Path(__file__).resolve().parents[4]
        return repo_root / "Standalone tools" / "vectord-rs" / "bin" / "vectord.exe"

    def _store_dir(self) -> Path:
        configured = getattr(self.config, "vectord_store_dir", None) or os.environ.get(
            "VECTOR_STORE_DIR"
        )
        if configured:
            return Path(configured)
        repo_root = Path(__file__).resolve().parents[4]
        return repo_root / "Standalone tools" / "vectord-rs" / "runtime"

    def _build_binary(self, binary: Path) -> bool:
        """Lazy governed build — same shape as ``_build_searchd``.

        Uses the host rustup cargo (Rust 1.98.1, the codex-pinned
        toolchain).  A missing toolchain or failed build simply reports
        the runtime unavailable; the caller degrades per A374 instead of
        substituting another index.
        """
        cargo = Path.home() / ".cargo" / "bin" / "cargo.exe"
        module_dir = binary.parent.parent
        if not cargo.is_file() or not (module_dir / "Cargo.toml").is_file():
            return False
        try:
            proc = subprocess.run(
                [str(cargo), "build", "--release", "--locked"],
                cwd=str(module_dir),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=600,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        produced = module_dir / "target" / "release" / "vectord.exe"
        if proc.returncode != 0 or not produced.is_file():
            return False
        try:
            binary.parent.mkdir(parents=True, exist_ok=True)
            import shutil

            shutil.copy2(str(produced), str(binary))
        except OSError:
            return False
        return binary.is_file()

    def _ensure_daemon(self) -> bool:
        """Probe vectord; optionally build+start the managed binary."""
        if self._probe():
            return True
        if not self.auto_start:
            return False
        binary = self._binary_path()
        if not binary.is_file() and not self._build_binary(binary):
            return False
        try:
            subprocess.Popen(
                [
                    str(binary),
                    "--bind",
                    self.vectord_url.replace("http://", "").replace("https://", ""),
                    "--store-dir",
                    str(self._store_dir()),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "DETACHED_PROCESS", 0),
            )
        except OSError:
            return False
        for _ in range(50):  # 最長等 5 秒
            if self._probe():
                return True
            time.sleep(0.1)
        return False

    async def initialize(self) -> bool:
        """Loopback check, lazy daemon start, then bind the HTTP client."""
        if not is_loopback_url(self.vectord_url):
            self.last_error = (
                f"VECTOR_URL_NOT_LOOPBACK: {self.vectord_url} — the "
                "canonical vector index is local-owned and loopback-only"
            )
            _logger.error("RustVectorRuntime: %s", self.last_error)
            self._healthy = False
            return False
        if not self._ensure_daemon():
            self.last_error = "VECTORD_UNAVAILABLE"
            _logger.warning(
                "RustVectorRuntime: vectord unavailable at %s", self.vectord_url
            )
            self._healthy = False
            return False
        try:
            self.client = VectordClient(self.vectord_url)
            self.client.get_collections()
            self._healthy = True
            _logger.info("RustVectorRuntime: healthy at %s", self.vectord_url)
            return True
        except Exception as exc:
            _logger.warning("RustVectorRuntime: initialization failed: %s", exc)
            self.last_error = str(exc)
            self._healthy = False
            return False


def select_vector_runtime(config: RagPipelineConfig) -> QdrantCanonicalRuntime:
    """Canonical vector-runtime selector (A610 Rust-Vector-Engine=target-primary).

    ``vector_backend`` resolves to ``rust`` by default — the takeover
    state.  ``qdrant`` stays selectable through ``VECTOR_BACKEND`` for
    the bounded migration/verification window until the retire-after-
    cutover event completes.
    """
    backend = str(getattr(config, "vector_backend", "") or "rust").strip().lower()
    if backend == "qdrant":
        return QdrantCanonicalRuntime(config)
    return RustVectorRuntime(config)


__all__ = [
    "DEFAULT_VECTORD_URL",
    "VECTORD_CONTRACT",
    "RustVectorRuntime",
    "VectordClient",
    "select_vector_runtime",
]
