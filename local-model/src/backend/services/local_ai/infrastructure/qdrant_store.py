from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable


QdrantTransport = Callable[[str, str, dict[str, Any] | None, float], dict[str, Any]]


class QdrantStore:
    """Small loopback-only Qdrant REST adapter for the shared knowledge base."""

    DEFAULT_ENDPOINT = "http://127.0.0.1:6333"
    COLLECTION = "gptbridge_shared_knowledge"

    def __init__(
        self,
        *,
        endpoint: str = DEFAULT_ENDPOINT,
        transport: QdrantTransport | None = None,
        executable_path: str | None = None,
        working_directory: str | None = None,
    ) -> None:
        parsed = urllib.parse.urlparse(endpoint)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
            "127.0.0.1", "localhost", "::1",
        }:
            raise ValueError("QDRANT_ENDPOINT_MUST_BE_LOOPBACK")
        self.endpoint = endpoint.rstrip("/")
        self._transport = transport or self._request_json
        self.executable_path = str(executable_path or "")
        self.working_directory = str(working_directory or "")
        self._process: subprocess.Popen[bytes] | None = None

    def ensure_running(self) -> bool:
        if self.status().get("available") is True:
            return True
        if not self.executable_path or not self.working_directory:
            return False
        from pathlib import Path

        executable = Path(self.executable_path).resolve()
        working_directory = Path(self.working_directory).resolve()
        if not executable.is_file() or not working_directory.is_dir():
            return False
        environment = dict(os.environ)
        environment["QDRANT__SERVICE__HOST"] = "127.0.0.1"
        environment["QDRANT__STORAGE__STORAGE_PATH"] = str(
            working_directory / "storage"
        )
        creation_flags = 0
        if os.name == "nt":
            creation_flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
        self._process = subprocess.Popen(
            [str(executable)],
            cwd=str(working_directory),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
        )
        for _ in range(50):
            if self.status().get("available") is True:
                return True
            time.sleep(0.1)
        return False

    @staticmethod
    def _request_json(
        method: str, url: str, payload: dict[str, Any] | None, timeout: float
    ) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "Connection": "keep-alive"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                decoded = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return {"_not_found": True}
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"QDRANT_HTTP_{exc.code}: {detail}") from exc
        parsed = json.loads(decoded or "{}")
        if not isinstance(parsed, dict):
            raise RuntimeError("QDRANT_RESPONSE_INVALID")
        return parsed

    @property
    def _collection_url(self) -> str:
        name = urllib.parse.quote(self.COLLECTION, safe="")
        return f"{self.endpoint}/collections/{name}"

    def ensure_collection(self, vector_size: int) -> None:
        current = self._transport("GET", self._collection_url, None, 5.0)
        if current.get("_not_found") is True:
            created = self._transport(
                "PUT",
                self._collection_url,
                {
                    "vectors": {"size": int(vector_size), "distance": "Cosine"},
                    "on_disk_payload": True,
                },
                15.0,
            )
            if created.get("status") not in {"ok", None}:
                raise RuntimeError("QDRANT_COLLECTION_CREATE_FAILED")
            return
        configured = (
            current.get("result", {}).get("config", {}).get("params", {}).get("vectors")
            if isinstance(current.get("result"), dict)
            else None
        )
        size = configured.get("size") if isinstance(configured, dict) else None
        if size is not None and int(size) != int(vector_size):
            raise RuntimeError(
                f"QDRANT_VECTOR_SIZE_MISMATCH: existing={size}, requested={vector_size}"
            )

    def replace_document(
        self,
        document_id: str,
        points: list[dict[str, Any]],
        *,
        module_id: str | None = None,
    ) -> None:
        must = [{"key": "document_id", "match": {"value": document_id}}]
        if module_id:
            must.append({"key": "module_id", "match": {"value": module_id}})
        self._transport(
            "POST",
            f"{self._collection_url}/points/delete?wait=true",
            {
                "filter": {"must": must}
            },
            30.0,
        )
        if points:
            response = self._transport(
                "PUT",
                f"{self._collection_url}/points?wait=true",
                {"points": points},
                60.0,
            )
            status = response.get("status")
            operation = response.get("result")
            if status not in {"ok", None} or (
                isinstance(operation, dict)
                and operation.get("status") not in {"completed", "acknowledged", None}
            ):
                raise RuntimeError("QDRANT_UPSERT_FAILED")

    def query(
        self,
        vector: list[float],
        *,
        limit: int,
        module_ids: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "query": vector,
            "limit": limit,
            "with_payload": True,
        }
        if module_ids:
            payload["filter"] = {
                "must": [
                    {"key": "module_id", "match": {"any": list(module_ids)}}
                ]
            }
        response = self._transport(
            "POST",
            f"{self._collection_url}/points/query",
            payload,
            30.0,
        )
        result = response.get("result")
        points = result.get("points") if isinstance(result, dict) else None
        if not isinstance(points, list):
            return []
        records: list[dict[str, Any]] = []
        for point in points:
            if not isinstance(point, dict) or not isinstance(point.get("payload"), dict):
                continue
            records.append(
                {
                    **point["payload"],
                    "point_id": point.get("id"),
                    "vector_score": float(point.get("score") or 0.0),
                }
            )
        return records

    def status(self) -> dict[str, Any]:
        try:
            response = self._transport("GET", self._collection_url, None, 5.0)
            if response.get("_not_found") is True:
                return {
                    "available": True,
                    "collection_exists": False,
                    "endpoint": self.endpoint,
                    "collection": self.COLLECTION,
                    "point_count": 0,
                }
            result = response.get("result") if isinstance(response.get("result"), dict) else {}
            return {
                "available": True,
                "collection_exists": True,
                "endpoint": self.endpoint,
                "collection": self.COLLECTION,
                "point_count": int(result.get("points_count") or 0),
                "status": result.get("status"),
            }
        except (OSError, RuntimeError, ValueError, urllib.error.URLError) as exc:
            return {
                "available": False,
                "collection_exists": False,
                "endpoint": self.endpoint,
                "collection": self.COLLECTION,
                "point_count": 0,
                "last_error": str(exc)[:500],
            }


__all__ = ["QdrantStore"]
