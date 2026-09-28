"""Rust vectord-backed degraded vector store.

The degraded store is a rebuildable, non-canonical projection. PostgreSQL
remains authoritative; Rust owns persistence, filtering, ANN/scoring, and
snapshot recovery. This compatibility surface exists so callers can migrate
without retaining a Python vector implementation.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any

COLLECTION = "gptbridge_shared_knowledge"
DEFAULT_ENDPOINT = "http://127.0.0.1:8093"


class LocalVectorStore:
    COLLECTION = COLLECTION
    DEFAULT_ENDPOINT = DEFAULT_ENDPOINT

    def __init__(self, root: Path | str, *, dimension: int = 256, endpoint: str | None = None, schema: str = "") -> None:
        self.root = Path(root).resolve()
        self.endpoint = str(endpoint or os.environ.get("GPTBRIDGE_DEGRADED_VECTORD_URL", DEFAULT_ENDPOINT)).rstrip("/")
        if not self.endpoint.startswith(("http://127.0.0.1:", "http://localhost:")):
            raise ValueError("DEGRADED_VECTORD_URL_NOT_LOOPBACK")
        self._dimension = int(dimension)
        self.database_path = Path(f"vectord:{self.root}")
        self.location = str(self.database_path)
        self._process: subprocess.Popen[Any] | None = None

    def _call(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(f"{self.endpoint}{path}", data=data, headers={"Content-Type": "application/json"}, method="POST" if payload is not None else "GET")
        with urllib.request.urlopen(request, timeout=5.0) as response:
            body = json.loads(response.read().decode("utf-8"))
        if body.get("ok") is not True:
            raise RuntimeError(str(body.get("error") or "DEGRADED_VECTORD_ERROR"))
        return body

    def _ensure_daemon(self) -> bool:
        try:
            self._call("/healthz")
            return True
        except Exception:
            pass
        binary = Path(__file__).resolve().parents[4] / "Standalone tools" / "vectord-rs" / "bin" / "vectord.exe"
        if not binary.is_file():
            return False
        host_port = self.endpoint.removeprefix("http://")
        store_dir = self.root / "vectord-store"
        store_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._process = subprocess.Popen([str(binary), "--bind", host_port, "--store-dir", str(store_dir)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError:
            return False
        for _ in range(30):
            try:
                self._call("/healthz")
                return True
            except Exception:
                time.sleep(0.1)
        return False

    def ensure_collection(self, vector_size: int) -> None:
        if int(vector_size) <= 0:
            raise ValueError("RAG_VECTOR_DIMENSION_INVALID")
        if not self._ensure_daemon():
            raise RuntimeError("DEGRADED_VECTORD_UNAVAILABLE")
        self._call("/v1/collections/ensure", {"name": COLLECTION, "dimension": int(vector_size)})
        self._dimension = int(vector_size)

    def replace_document(self, document_id: str, points: list[dict[str, Any]], *, module_id: str | None = None) -> None:
        self.delete(document_id, module_id=module_id)
        items = []
        for point in points:
            point_id = str(point.get("id") or point.get("point_id") or "")
            if not point_id:
                raise ValueError("RAG_POINT_ID_REQUIRED")
            payload = dict(point.get("payload") or {})
            payload.update({"document_id": document_id, "module_id": str(module_id or payload.get("module_id") or "")})
            if point.get("text") is not None:
                items.append({"id": point_id, "text": str(point["text"]), "payload": payload})
            else:
                items.append({"id": point_id, "vector": [float(v) for v in point.get("vector") or []], "payload": payload})
        if items:
            self._call("/v1/points/upsert_text" if "text" in items[0] else "/v1/points/upsert", {"collection": COLLECTION, "points": items})

    def query(self, vector: list[float], *, limit: int, module_ids: tuple[str, ...] = ()) -> list[dict[str, Any]]:
        if not module_ids:
            raise ValueError("VECTOR_MODULE_SCOPE_REQUIRED")
        body = self._call("/v1/search", {"collection": COLLECTION, "vector": vector, "top_k": int(limit), "filter": {"must": [{"key": "module_id", "match": {"any": list(module_ids)}}]}})
        return [self._hit(hit) for hit in body.get("hits", [])]

    def query_text(self, text: str, *, limit: int, module_ids: tuple[str, ...] = ()) -> list[dict[str, Any]]:
        if not module_ids:
            raise ValueError("VECTOR_MODULE_SCOPE_REQUIRED")
        body = self._call("/v1/search_text", {"collection": COLLECTION, "text": text, "top_k": int(limit), "filter": {"must": [{"key": "module_id", "match": {"any": list(module_ids)}}]}})
        return [self._hit(hit) for hit in body.get("hits", [])]

    @staticmethod
    def _hit(hit: dict[str, Any]) -> dict[str, Any]:
        score = hit.get("score")
        return {**(hit.get("payload") or {}), "id": hit.get("id"), "point_id": hit.get("id"), "score": score, "vector_score": score}

    def delete(self, document_id: str, *, module_id: str | None = None) -> None:
        must = [{"key": "document_id", "match": {"value": document_id}}]
        if module_id:
            must.append({"key": "module_id", "match": {"value": module_id}})
        self._call("/v1/points/delete", {"collection": COLLECTION, "filter": {"must": must}})

    def delete_resource(self, resource_id: str, module_id: str | None = None, **_: Any) -> bool:
        self.delete(resource_id, module_id=module_id)
        return True

    def reindex(self, document_id: str, points: list[dict[str, Any]], *, module_id: str | None = None) -> None:
        self.replace_document(document_id, points, module_id=module_id)

    def status(self) -> dict[str, Any]:
        try:
            self._ensure_daemon()
            health = self._call("/healthz")
            info = self._call("/v1/collections/info", {"name": COLLECTION})
            return {"available": True, "engine": "rust-vectord-degraded", "canonical": False, "reconciliation_required": True, "endpoint": self.endpoint, "collection": COLLECTION, "points": int(info.get("points_count") or 0), "dimension": int(info.get("dimension") or self._dimension), "collections": health.get("collections"), "location": self.location}
        except Exception as error:
            return {"available": False, "engine": "rust-vectord-degraded", "canonical": False, "reconciliation_required": True, "endpoint": self.endpoint, "collection": COLLECTION, "points": 0, "dimension": self._dimension, "location": self.location, "last_error": str(error)[:500]}


__all__ = ["LocalVectorStore"]
