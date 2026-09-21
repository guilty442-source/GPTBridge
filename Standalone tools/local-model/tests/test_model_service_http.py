"""G30 acceptance tests: star-model-service/v1 loopback HTTP endpoint."""

from __future__ import annotations

import http.client
import json
import sys
from pathlib import Path

import pytest

SERVICE_ROOT = Path(__file__).resolve().parents[1] / "src" / "backend" / "services"
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from xingcheng.infrastructure import model_service_http  # noqa: E402
from xingcheng.infrastructure.model_service_http import (  # noqa: E402
    MAX_BODY_BYTES,
    SESSION_HEADER,
    ModelService,
)


@pytest.fixture()
def service(tmp_path: Path):
    svc = ModelService(root=tmp_path)
    info = svc.start()
    assert info["ok"] is True
    assert info["port"] > 0
    yield svc
    svc.stop()


def _token(service: ModelService) -> str:
    assert service._server is not None
    return service._server.session_token


def _request(service: ModelService, method: str, path: str,
             payload: dict | bytes | None = None, token: str | None = None):
    port = service.port
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    body = None
    headers = {}
    if payload is not None:
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers[SESSION_HEADER] = token
    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    return resp.status, json.loads(raw.decode("utf-8"))


def test_descriptor_written_and_removed(service: ModelService, tmp_path: Path):
    descriptor = tmp_path / "xingcheng" / "runtime" / "ipc" / "model-service.json"
    assert descriptor.is_file()
    data = json.loads(descriptor.read_text(encoding="utf-8"))
    assert data["schema"] == "star-model-service-descriptor/v1"
    assert data["port"] == service.port
    service.stop()
    assert not descriptor.exists()


def test_unauthorized_without_token(service: ModelService):
    status, body = _request(service, "POST", "/v1/infer", {"prompt": "hi"})
    assert status == 403
    assert body["ok"] is False
    assert body["error_code"] == "MODEL_SERVICE_UNAUTHORIZED"


def test_infer_contract_roundtrip(service: ModelService, monkeypatch):
    captured: dict = {}

    def fake_generate(request):
        captured.update(request)
        return {
            "ok": True,
            "text": "星澄回應",
            "model": "native-small",
            "latency_ms": 12.5,
            "decoder": "greedy",
            "cpp_runtime": True,
        }

    monkeypatch.setattr(
        "xingcheng.infrastructure.native_engine.generate_via_native_engine",
        fake_generate,
    )
    status, body = _request(
        service,
        "POST",
        "/v1/infer",
        {
            "prompt": "你好",
            "context": "ctx",
            "intent": "Conversation",
            "max_new_tokens": 16,
            "temperature": 0.7,
            "top_k": 50,
            "top_p": 0.9,
            "extra": {"seed": 42},
        },
        token=_token(service),
    )
    assert status == 200
    assert body["schema"] == "star-model-service/v1"
    assert body["ok"] is True
    assert body["text"] == "星澄回應"
    assert body["model_id"] == "native-small"
    assert body["cpp_runtime"] is True
    # 契約映射：C# max_new_tokens → engine max_tokens；extra.seed 提升為 seed
    assert captured["max_tokens"] == 16
    assert captured["seed"] == 42
    assert captured["intent"] == "Conversation"


def test_infer_engine_failure_is_fail_closed(service: ModelService, monkeypatch):
    monkeypatch.setattr(
        "xingcheng.infrastructure.native_engine.generate_via_native_engine",
        lambda request: {"ok": False, "error_code": "ENGINE_UNAVAILABLE", "message": "no engine"},
    )
    status, body = _request(
        service, "POST", "/v1/infer", {"prompt": "hi"}, token=_token(service)
    )
    assert status == 502
    assert body["ok"] is False
    assert body["error_code"] == "ENGINE_UNAVAILABLE"


def test_infer_rejects_bad_inputs(service: ModelService):
    token = _token(service)
    status, body = _request(service, "POST", "/v1/infer", b"{not json", token=token)
    assert status == 400
    assert body["error_code"] == "MODEL_SERVICE_BODY_INVALID"

    status, body = _request(service, "POST", "/v1/infer", {"prompt": "  "}, token=token)
    assert status == 400
    assert body["error_code"] == "PROMPT_REQUIRED"

    status, body = _request(
        service, "POST", "/v1/infer", b"x" * (MAX_BODY_BYTES + 1), token=token
    )
    assert status == 413
    assert body["error_code"] == "MODEL_SERVICE_BODY_TOO_LARGE"


def test_unknown_route_and_status(service: ModelService):
    token = _token(service)
    status, body = _request(service, "POST", "/v1/nope", {}, token=token)
    assert status == 404

    status, body = _request(service, "GET", "/v1/status", token=token)
    assert status == 200
    assert body["ok"] is True
    assert body["schema"] == "star-model-service/v1"
    assert "cpp_runtime" in body


def test_release_endpoint(service: ModelService):
    status, body = _request(service, "POST", "/v1/release", {}, token=_token(service))
    assert status == 200
    assert body["ok"] is True
    assert isinstance(body["released"], list)
