"""MiniCPM-V visual recognition client for automatic file management."""

from __future__ import annotations

import base64
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error, request


MODEL_NAME = "openbmb/minicpm-v4.6:q8_0"
MODEL_VERSION = "4.6"
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
MAX_IMAGE_BYTES = 32 * 1024 * 1024


class MiniCPMVisualServiceUnavailable(RuntimeError):
    """Raised when the governed local MiniCPM-V service cannot be used."""


@dataclass(frozen=True)
class VisualRecognitionResult:
    classification: str
    confidence: float
    width: int
    height: int
    reason: str
    tags: tuple[str, ...] = ()
    summary: str = ""
    model: str = MODEL_NAME
    model_version: str = MODEL_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "classification": self.classification,
            "confidence": self.confidence,
            "width": self.width,
            "height": self.height,
            "reason": self.reason,
            "tags": list(self.tags),
            "summary": self.summary,
            "model": self.model,
            "model_version": self.model_version,
        }


@dataclass(frozen=True)
class VisualSimilarityResult:
    similarity: int
    same_content: bool
    reason: str
    model: str = MODEL_NAME

    def to_dict(self) -> dict[str, Any]:
        return {
            "similarity": self.similarity,
            "same_content": self.same_content,
            "reason": self.reason,
            "model": self.model,
        }


class MiniCPMVisualRecognitionService:
    """Recognize local image content through MiniCPM-V over loopback Ollama."""

    def __init__(self, *, base_url: str | None = None, timeout: float = 120.0,
                 temperature: float = 0.0, top_p: float = 0.9,
                 context_window: int = 8192, max_output_tokens: int = 512) -> None:
        configured = base_url or os.environ.get("GPTBRIDGE_OLLAMA_URL") or DEFAULT_OLLAMA_URL
        self.base_url = configured.rstrip("/")
        if self.base_url not in {"http://127.0.0.1:11434", "http://localhost:11434"}:
            raise MiniCPMVisualServiceUnavailable("MiniCPM-V endpoint must be loopback-only")
        self.timeout = max(1.0, float(timeout))
        self.options = {
            "temperature": max(0.0, min(1.0, float(temperature))),
            "top_p": max(0.1, min(1.0, float(top_p))),
            "num_ctx": max(2048, min(16384, int(context_window))),
            "num_predict": max(128, min(1024, int(max_output_tokens))),
        }

    def predict(self, path: str | Path) -> VisualRecognitionResult:
        image_path = Path(path)
        try:
            payload = image_path.read_bytes()
        except OSError as exc:
            raise ValueError(f"Image cannot be read: {image_path.name}") from exc
        if not payload or len(payload) > MAX_IMAGE_BYTES:
            raise ValueError(f"Image size is unsupported: {image_path.name}")

        try:
            from PIL import Image, ImageOps

            with Image.open(image_path) as opened:
                normalized = ImageOps.exif_transpose(opened)
                width, height = normalized.size
                normalized.verify()
        except Exception as exc:
            raise ValueError(f"Image cannot be decoded: {image_path.name}") from exc

        body = {
            "model": MODEL_NAME,
            "keep_alive": "5m",
            "stream": False,
            "format": "json",
            "options": dict(self.options),
            "messages": [{
                "role": "user",
                "content": (
                    "你是自動檔案管理的視覺辨識服務。判斷畫面是否包含真人，並只輸出 JSON："
                    '{"classification":"person|non_person_candidate|indeterminate",'
                    '"confidence":0到1,"reason":"簡短原因","tags":["標籤"],"summary":"繁中摘要"}。'
                    "無法確定時必須使用 indeterminate。"
                ),
                "images": [base64.b64encode(payload).decode("ascii")],
            }],
        }
        response = self._post_json("/api/chat", body)
        content = str((response.get("message") or {}).get("content") or "")
        try:
            recognized = json.loads(content)
        except (json.JSONDecodeError, TypeError) as exc:
            raise MiniCPMVisualServiceUnavailable("MiniCPM-V returned invalid JSON") from exc
        if not isinstance(recognized, dict):
            raise MiniCPMVisualServiceUnavailable("MiniCPM-V returned an invalid result")

        classification = str(recognized.get("classification") or "indeterminate")
        if classification not in {"person", "non_person_candidate", "indeterminate"}:
            classification = "indeterminate"
        try:
            confidence = max(0.0, min(1.0, float(recognized.get("confidence") or 0.0)))
        except (TypeError, ValueError):
            confidence = 0.0
        raw_tags = recognized.get("tags")
        tags = tuple(str(item).strip()[:80] for item in raw_tags[:20] if str(item).strip()) if isinstance(raw_tags, list) else ()
        return VisualRecognitionResult(
            classification=classification,
            confidence=round(confidence, 4),
            width=int(width),
            height=int(height),
            reason=str(recognized.get("reason") or "")[:500],
            tags=tags,
            summary=str(recognized.get("summary") or "")[:2_000],
        )

    def compare(self, left: str | Path, right: str | Path, *, media_type: str,
                analysis_speed: int = 50) -> VisualSimilarityResult:
        """Compare two images or representative video contact sheets with MiniCPM-V."""
        if media_type not in {"image", "video"}:
            raise ValueError("media_type must be image or video")
        encoded_inputs = [
            base64.b64encode(self._visual_payload(Path(item), media_type, analysis_speed)).decode("ascii")
            for item in (left, right)
        ]
        body = {
            "model": MODEL_NAME,
            "keep_alive": "5m",
            "stream": False,
            "format": "json",
            "options": dict(self.options),
            "messages": [{
                "role": "user",
                "content": (
                    f"比較這兩個{('圖片' if media_type == 'image' else '影片代表影格')}的內容相似度。"
                    "忽略解析度、壓縮、裁切與輕微色彩差異，只輸出 JSON："
                    '{"similarity":0到100的整數,"same_content":true或false,"reason":"繁中簡短原因"}。'
                ),
                "images": encoded_inputs,
            }],
        }
        response = self._post_json("/api/chat", body)
        content = str((response.get("message") or {}).get("content") or "")
        try:
            result = json.loads(content)
            similarity = max(0, min(100, int(round(float(result.get("similarity", 0))))))
        except (json.JSONDecodeError, AttributeError, TypeError, ValueError) as exc:
            raise MiniCPMVisualServiceUnavailable("MiniCPM-V returned invalid similarity JSON") from exc
        return VisualSimilarityResult(
            similarity=similarity,
            same_content=bool(result.get("same_content")),
            reason=str(result.get("reason") or "")[:500],
        )

    def close(self) -> None:
        """Unload MiniCPM-V after the enabled scan finishes."""
        try:
            self._post_json("/api/generate", {
                "model": MODEL_NAME,
                "prompt": "",
                "stream": False,
                "keep_alive": 0,
            })
        except MiniCPMVisualServiceUnavailable:
            pass

    @staticmethod
    def _visual_payload(path: Path, media_type: str, analysis_speed: int) -> bytes:
        if media_type == "image":
            payload = path.read_bytes()
            if not payload or len(payload) > MAX_IMAGE_BYTES:
                raise ValueError(f"Image size is unsupported: {path.name}")
            return payload
        try:
            import imageio_ffmpeg

            executable = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception as exc:
            raise MiniCPMVisualServiceUnavailable("FFmpeg is required for video recognition") from exc
        sample_count = max(3, min(9, round(3 + (100 - max(1, min(100, analysis_speed))) * 6 / 99)))
        command = [
            str(executable), "-nostdin", "-hide_banner", "-loglevel", "error",
            "-i", str(path), "-an", "-vf",
            f"fps=1/20,scale=384:-1:flags=lanczos,tile={sample_count}x1",
            "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                check=False,
                timeout=90,
                creationflags=(
                    int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)
                    if os.name == "nt" else 0
                ),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise MiniCPMVisualServiceUnavailable(f"Video frames cannot be extracted: {path.name}") from exc
        if completed.returncode != 0 or not completed.stdout:
            raise MiniCPMVisualServiceUnavailable(f"Video frames cannot be extracted: {path.name}")
        return completed.stdout

    def _post_json(self, route: str, body: dict[str, Any]) -> dict[str, Any]:
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        call = request.Request(
            self.base_url + route,
            data=encoded,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(call, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            raise MiniCPMVisualServiceUnavailable(f"MiniCPM-V service unavailable: {exc}") from exc
        if not isinstance(result, dict) or result.get("error"):
            raise MiniCPMVisualServiceUnavailable(
                f"MiniCPM-V inference failed: {result.get('error', 'invalid response') if isinstance(result, dict) else 'invalid response'}"
            )
        return result


__all__ = [
    "MiniCPMVisualRecognitionService",
    "MiniCPMVisualServiceUnavailable",
    "MODEL_NAME",
    "MODEL_VERSION",
    "VisualRecognitionResult",
    "VisualSimilarityResult",
]
