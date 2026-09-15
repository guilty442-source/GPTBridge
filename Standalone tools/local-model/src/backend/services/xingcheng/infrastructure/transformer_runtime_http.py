from __future__ import annotations

import http.client
import json
import time
import urllib.parse
from typing import Any, Callable, Mapping


class TransformerRuntimeHttpMixin:
    """Loopback-only HTTP transport helpers for StarTransformerRuntime."""

    @staticmethod
    def _validated_endpoint(value: str) -> str:
        parsed = urllib.parse.urlparse(str(value or "").strip())
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("TRANSFORMER_ENDPOINT_MUST_BE_LOOPBACK")
        port = parsed.port or 11434
        if not 1 <= port <= 65535:
            raise ValueError("TRANSFORMER_ENDPOINT_PORT_INVALID")
        host = "127.0.0.1" if parsed.hostname in {"127.0.0.1", "localhost"} else "[::1]"
        return f"http://{host}:{port}"

    @staticmethod
    def _http_json(
        method: str,
        url: str,
        payload: dict[str, Any] | None,
        timeout: float,
    ) -> dict[str, Any]:
        body = None
        headers = {
            "Accept": "application/json",
            "Connection": "keep-alive",
            "Content-Type": "application/json",
        }
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        parsed = urllib.parse.urlparse(str(url))
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 11434
        path = parsed.path or "/"
        connection = http.client.HTTPConnection(host, port, timeout=timeout)
        try:
            connection.request(
                method,
                path,
                body=body,
                headers={
                    **headers,
                    "Content-Length": str(len(body)) if body else "0",
                },
            )
            response = connection.getresponse()
            if response.status != 200:
                detail = response.read(4_001).decode("utf-8", errors="replace")[:4_000]
                raise RuntimeError(f"TRANSFORMER_HTTP_{response.status}: {detail}")
            raw = response.read(2_000_001)
        except http.client.HTTPException as error:
            detail = str(error)[:500]
            raise RuntimeError(f"TRANSFORMER_HTTP_ERROR: {detail}") from error
        finally:
            try:
                connection.close()
            except Exception:
                pass
        if len(raw) > 2_000_000:
            raise RuntimeError("TRANSFORMER_RESPONSE_TOO_LARGE")
        decoded = json.loads(raw.decode("utf-8"))
        if not isinstance(decoded, dict):
            raise RuntimeError("TRANSFORMER_RESPONSE_INVALID")
        return decoded

    @staticmethod
    def _http_chat_stream(
        url: str,
        payload: dict[str, Any],
        timeout: float,
        cancel_event: Any = None,
        progress_callback: Callable[[dict[str, Any]], Any] | None = None,
    ) -> dict[str, Any]:
        streaming_payload = {**payload, "stream": True}
        body = json.dumps(streaming_payload, ensure_ascii=False).encode("utf-8")
        parsed = urllib.parse.urlparse(str(url))
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 11434
        deadline = time.monotonic() + max(1.0, float(timeout))
        connection: http.client.HTTPConnection | None = None
        try:
            connection = http.client.HTTPConnection(host, port, timeout=timeout)
            connection.request(
                "POST",
                "/api/chat",
                body=body,
                headers={
                    "Accept": "application/x-ndjson",
                    "Content-Type": "application/json",
                    "Content-Length": str(len(body)),
                    "Connection": "keep-alive",
                },
            )
            response = connection.getresponse()
            if response.status != 200:
                detail = response.read(4_001).decode("utf-8", errors="replace")[:4_000]
                raise RuntimeError(f"TRANSFORMER_HTTP_{response.status}: {detail}")
            final, chunks = TransformerRuntimeHttpMixin._read_chat_stream(
                response, payload, deadline, cancel_event, progress_callback
            )
        except http.client.HTTPException as error:
            detail = str(error)[:500]
            raise RuntimeError(f"TRANSFORMER_HTTP_ERROR: {detail}") from error
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("TRANSFORMER_REQUEST_CANCELLED")
        return {**final, "message": {"content": "".join(chunks)}}

    @staticmethod
    def _read_chat_stream(
        response: Any,
        payload: dict[str, Any],
        deadline: float,
        cancel_event: Any,
        progress_callback: Callable[[dict[str, Any]], Any] | None,
    ) -> tuple[dict[str, Any], list[str]]:
        chunks: list[str] = []
        total_characters = 0
        sequence = 0
        final: dict[str, Any] = {}
        for raw_line in response:
            if time.monotonic() >= deadline:
                raise TimeoutError("TRANSFORMER_DEADLINE_EXCEEDED")
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError("TRANSFORMER_REQUEST_CANCELLED")
            if not raw_line.strip():
                continue
            decoded = json.loads(raw_line.decode("utf-8"))
            if not isinstance(decoded, dict):
                raise RuntimeError("TRANSFORMER_STREAM_CHUNK_INVALID")
            content = TransformerRuntimeHttpMixin._chat_stream_content(decoded)
            if content:
                total_characters += len(content)
                if total_characters > 64_000:
                    raise RuntimeError("TRANSFORMER_RESPONSE_TOO_LARGE")
                chunks.append(content)
                sequence += 1
                if progress_callback is not None:
                    try:
                        progress_callback(
                            {
                                "sequence": sequence,
                                "text": "".join(chunks),
                                "model": str(payload.get("model") or ""),
                            }
                        )
                    except Exception:
                        pass
            if decoded.get("done") is True:
                final = decoded
        return final, chunks

    @staticmethod
    def _chat_stream_content(decoded: dict[str, Any]) -> str:
        message = decoded.get("message")
        return str(
            message.get("content")
            if isinstance(message, Mapping)
            else ""
        )
