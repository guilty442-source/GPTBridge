from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any


WORKSPACE = Path(__file__).resolve().parents[2]
TOOL_ROOT = WORKSPACE / "local-model"
sys.path.insert(0, str(WORKSPACE))
sys.path.insert(0, str(WORKSPACE / "shared-layer" / "src"))
sys.path.insert(0, str(TOOL_ROOT / "src" / "backend" / "services"))
sys.path.insert(
    0,
    str(TOOL_ROOT / "model-dialogue" / "src" / "backend" / "services"),
)

from local_ai.application.service import LocalAiService  # noqa: E402
from star_chat.application.service import StarChatService  # noqa: E402


def response_text(result: dict[str, Any]) -> str:
    generation = result.get("generation")
    if isinstance(generation, dict):
        text = str(generation.get("text") or generation.get("response") or "").strip()
        if text:
            return text
    return str(result.get("response") or result.get("answer") or "").strip()


async def main() -> int:
    local_ai = LocalAiService(TOOL_ROOT, enable_transformer=True)
    dialogue = StarChatService()
    dialogue.bind_local_service(local_ai)
    report: dict[str, Any] = {}
    await local_ai.start()
    try:
        _, status = await dialogue.handle("star_chat_status", {})
        runtime = status.get("transformer_runtime")
        report["status"] = {
            "ok": status.get("ok") is True,
            "connected": dialogue.connected,
            "model_ready": isinstance(runtime, dict)
            and runtime.get("available") is True
            and runtime.get("model_installed") is True,
            "selectable_model_count": len(runtime.get("selectable_models") or [])
            if isinstance(runtime, dict)
            else 0,
        }

        _, chat_preparation = await dialogue.handle(
            "star_chat_status", {"prepare_mode": "chat"}
        )
        report["chat_preload"] = chat_preparation.get("mode_preparation")

        _, chat = await dialogue.handle(
            "star_chat_send_message",
            {
                "message": "請只用繁體中文回答，並回覆：聊天模式正常。",
                "conversation_mode": "chat",
                "runtime_model": "gemma4:e2b-it-qat",
                "reasoning_level": "light",
                "generation_speed": "high",
                "task_intensity": "simple",
                "max_output_tokens": 96,
            },
            request_id="smoke-chat-zh-tw",
        )
        chat_text = response_text(chat)
        report["chat"] = {
            "ok": chat.get("ok") is True and bool(chat_text),
            "model": chat.get("model") or (chat.get("generation") or {}).get("model"),
            "response": chat_text[:240],
        }

        _, coding_preparation = await dialogue.handle(
            "star_chat_status", {"prepare_mode": "coding"}
        )
        report["coding_preload"] = coding_preparation.get("mode_preparation")

        _, coding = await dialogue.handle(
            "star_chat_send_message",
            {
                "message": (
                    "請只用繁體中文回答，不要修改任何檔案。"
                    "請用一句話說明 Python 函式的用途。"
                ),
                "conversation_mode": "coding",
                "previous_conversation_mode": "chat",
                "programming_folder": str(WORKSPACE),
                "runtime_model": "gemma4:e2b-it-qat",
                "reasoning_level": "light",
                "generation_speed": "high",
                "task_intensity": "simple",
                "max_output_tokens": 128,
            },
            request_id="smoke-coding-zh-tw",
        )
        coding_text = response_text(coding)
        report["coding"] = {
            "ok": coding.get("ok") is True and bool(coding_text),
            "model": coding.get("model")
            or (coding.get("generation") or {}).get("model"),
            "response": coding_text[:240],
        }

        _, rag = await local_ai.handle("local_ai_rag_status", {})
        report["rag"] = {
            "enabled": rag.get("enabled") is True,
            "qdrant": (rag.get("vector_database") or {}).get("available") is True,
            "postgresql": (rag.get("keyword_index") or {}).get("engine")
            == "postgresql",
        }
        report["cancel_unknown_request"] = (
            await dialogue.cancel_request("smoke-not-running") is False
        )
    finally:
        await local_ai.shutdown()

    report["ok"] = all(
        (
            report["status"]["ok"],
            report["status"]["model_ready"],
            report["chat"]["ok"],
            report["coding"]["ok"],
            report["rag"]["enabled"],
            report["rag"]["qdrant"],
            report["rag"]["postgresql"],
            report["cancel_unknown_request"],
        )
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
