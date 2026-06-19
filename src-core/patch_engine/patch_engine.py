from __future__ import annotations

import re
from difflib import unified_diff
from pathlib import Path
from typing import Dict, List, Optional


class PatchEngine:
    def __init__(self, project_root: Path | None = None) -> None:
        self.project_root = project_root or Path(__file__).resolve().parent.parent

    def compute_patch(self, rel_path: str, old_text: str, new_text: str) -> str:
        return "".join(
            unified_diff(
                old_text.splitlines(keepends=True),
                new_text.splitlines(keepends=True),
                fromfile=rel_path,
                tofile=rel_path,
                lineterm="",
            )
        )

    def _strip_markdown_fence(self, text: str) -> str:
        text = text.strip()
        fenced = re.search(r"```(?:python|py)?\s*\n(.*?)\n```", text, re.DOTALL | re.IGNORECASE)
        if fenced:
            return fenced.group(1).strip() + "\n"
        return text

    def _looks_like_code(self, text: str) -> bool:
        stripped = text.strip()
        if not stripped:
            return False
        markers = ["import ", "from ", "class ", "def ", "if __name__", "function ", "const ", "let ", "var "]
        return any(marker in stripped for marker in markers)

    def parse_patch_text(self, text: str, default_path: str = "src-core/main.py") -> Dict[str, str]:
        text = text.strip()
        result: Dict[str, str] = {}

        block_matches = re.findall(r"=====\s*(.*?)\s*=====\n(.*?)(?=\n=====|\Z)", text, re.DOTALL)
        if block_matches:
            for rel_path, content in block_matches:
                rel_path = rel_path.strip()
                if not rel_path:
                    continue
                result[rel_path] = self._strip_markdown_fence(content)
            if result:
                return result

        code_blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)\n```", text, re.DOTALL | re.IGNORECASE)
        if code_blocks:
            content = code_blocks[-1].strip()
            if self._looks_like_code(content):
                result[default_path] = content + "\n"
                return result

        stripped = self._strip_markdown_fence(text)
        if self._looks_like_code(stripped):
            result[default_path] = stripped.strip() + "\n"
            return result

        raise ValueError(
            "No applicable patch format found.\n"
            "Include `===== path =====` blocks or Markdown code fences."
        )

    def apply_patches(self, patches: Dict[str, str], allowed_files: Optional[List[str]] = None) -> List[str]:
        applied: List[str] = []
        for rel_path, content in patches.items():
            if allowed_files is not None and rel_path not in allowed_files:
                continue
            target_path = self.project_root / rel_path
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(content, encoding="utf-8")
            applied.append(rel_path)
        return applied

    def get_file_content(self, rel_path: str) -> Optional[str]:
        target_path = self.project_root / rel_path
        if not target_path.exists():
            return None
        return target_path.read_text(encoding="utf-8", errors="ignore")

    def preview_patch(self, patches: Dict[str, str]) -> Dict[str, str]:
        diff_preview: Dict[str, str] = {}
        for rel_path, new_content in patches.items():
            old_content = self.get_file_content(rel_path) or ""
            diff_preview[rel_path] = self.compute_patch(rel_path, old_content, new_content)
        return diff_preview
