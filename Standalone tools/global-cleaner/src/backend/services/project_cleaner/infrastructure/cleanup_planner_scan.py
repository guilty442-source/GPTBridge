from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any


class CleanupPlannerScanMixin:

    def _finalize_plan(
        self,
        normalized_scope: str,
        requested_scope: str,
        items: list[dict[str, Any]],
        skipped: list[dict[str, Any]],
        errors: list[dict[str, Any]],
        git_status: dict[str, Any],
    ) -> dict[str, Any]:
        total_bytes = sum(int(item.get("size_bytes") or 0) for item in items)
        file_count = sum(1 for item in items if item.get("type") == "file")
        dir_count = sum(1 for item in items if item.get("type") == "directory")
        health = self._cleanup_health(normalized_scope, items, skipped, errors)
        plan_payload = {
            "scope": normalized_scope,
            "requested_scope": requested_scope,
            "items": items,
            "item_count": len(items),
            "file_count": file_count,
            "dir_count": dir_count,
            "total_bytes": total_bytes,
            "summary": self._summarize_items(items),
            "health": health,
            "skipped": skipped,
            "skipped_count": len(skipped),
            "errors": errors,
            "error_count": len(errors),
            "git": git_status,
            "rule_schema_version": self.rules.get("schema_version"),
        }
        plan_id, plan_token = self._persist_plan(plan_payload)
        self._append_history(
            "preview",
            ok=True,
            scope=normalized_scope,
            plan_id=plan_id,
            item_count=len(items),
            bytes=total_bytes,
        )
        self._emit_progress("scan", 100, "清理計畫完成", plan_id=plan_id, item_count=len(items))
        return {
            "ok": True,
            **plan_payload,
            "plan_id": plan_id,
            "plan_token": plan_token,
            "plan_expires_in_minutes": self._plan_ttl_minutes(),
            "message": f"{normalized_scope} cleanup plan completed (items={len(items)})",
        }
    def _plan_scan_root(
        self,
        raw_root: Any,
        *,
        normalized_scope: str,
        now: float,
        excluded_names: set[str],
        items: list[dict[str, Any]],
        skipped: list[dict[str, Any]],
        errors: list[dict[str, Any]],
    ) -> None:
        root = self._safe_resolve(raw_root)
        if not root.exists():
            return
        if not self._inside_project(root) or self._is_protected(root):
            self._append_skip(skipped, {"path": str(raw_root), "reason": "protected or outside project"})
            return
        for current_raw, dirnames, filenames in os.walk(root, followlinks=False):
            current = Path(current_raw)
            if self._is_protected(current):
                dirnames[:] = []
                continue
            kept: list[str] = []
            for dirname in sorted(dirnames):
                if self._plan_directory_candidate(
                    current / dirname,
                    dirname,
                    normalized_scope=normalized_scope,
                    now=now,
                    excluded_names=excluded_names,
                    items=items,
                    skipped=skipped,
                    errors=errors,
                ):
                    kept.append(dirname)
            dirnames[:] = kept
            for filename in sorted(filenames):
                self._plan_file_candidate(
                    current / filename,
                    now=now,
                    items=items,
                    skipped=skipped,
                    errors=errors,
                )
    def _plan_directory_candidate(
        self,
        child: Path,
        dirname: str,
        *,
        normalized_scope: str,
        now: float,
        excluded_names: set[str],
        items: list[dict[str, Any]],
        skipped: list[dict[str, Any]],
        errors: list[dict[str, Any]],
    ) -> bool:
        rule = self._directory_rule(dirname, path=child, scope=normalized_scope)
        if (
            rule is None and dirname.casefold() in excluded_names
        ) or self._is_protected(child):
            return False
        if self._is_link_or_reparse_point(child):
            self._append_skip(skipped, {"path": self._relative_path(child), "type": "directory", "reason": "link or reparse point"})
            return False
        if rule is None:
            return True
        min_age_days = max(0.0, float(rule.get("min_age_days") or 0))
        if not self._directory_candidate_allowed(
            child, rule, now=now, min_age_days=min_age_days, skipped=skipped
        ):
            return False
        try:
            snapshot = self._candidate_snapshot(child, "directory")
            rel_path = self._relative_path(child)
            contents_only = bool(
                self.rules.get("delete_files_only", True)
            ) or bool(rule.get("contents_only"))
            if contents_only and int(snapshot.get("file_count") or 0) == 0:
                return False
            items.append(
                self._directory_plan_item(
                    child,
                    rel_path,
                    snapshot,
                    rule,
                    now=now,
                    min_age_days=min_age_days,
                    contents_only=contents_only,
                )
            )
        except OSError as exc:
            errors.append({"path": self._relative_path(child), "type": "directory", "message": str(exc)})
        return False
    def _directory_candidate_allowed(
        self,
        child: Path,
        rule: dict[str, Any],
        *,
        now: float,
        min_age_days: float,
        skipped: list[dict[str, Any]],
    ) -> bool:
        if self._age_days(child, now) < min_age_days:
            return False
        safe, reason = self._safe_candidate(child, "directory")
        if safe and bool(rule.get("protect_user_content")) and self._directory_contains_user_content(child):
            safe, reason = False, "directory contains source-like user content"
        if not safe:
            self._append_skip(skipped, {"path": self._relative_path(child), "type": "directory", "reason": reason})
            return False
        return True
    def _directory_plan_item(
        self,
        child: Path,
        rel_path: str,
        snapshot: dict[str, Any],
        rule: dict[str, Any],
        *,
        now: float,
        min_age_days: float,
        contents_only: bool,
    ) -> dict[str, Any]:
        item_id = hashlib.sha256(
            f"directory:{rel_path}:{snapshot['digest']}".encode("utf-8")
        ).hexdigest()[:24]
        return {
            "item_id": item_id,
            "path": rel_path,
            "type": "directory",
            "size_bytes": snapshot["size_bytes"],
            "entry_count": snapshot["entry_count"],
            "reason": str(rule.get("reason") or "generated directory"),
            "rule_id": str(rule.get("id") or "directory-rule"),
            "risk": str(rule.get("risk") or "low"),
            "age_days": round(self._age_days(child, now), 2),
            "min_age_days": min_age_days,
            "contents_only": contents_only,
            "allow_direct_delete": bool(rule.get("allow_direct_delete")),
            "fingerprint": snapshot,
        }
    def _plan_file_candidate(
        self,
        path: Path,
        *,
        now: float,
        items: list[dict[str, Any]],
        skipped: list[dict[str, Any]],
        errors: list[dict[str, Any]],
    ) -> None:
        if self._is_protected(path) or self._is_link_or_reparse_point(path):
            return
        rule = self._file_rule(path, now)
        if rule is None:
            return
        safe, reason = self._safe_candidate(
            path,
            "file",
            check_git=not bool(rule.get("allow_tracked")),
        )
        if not safe:
            self._append_skip(skipped, {"path": self._relative_path(path), "type": "file", "reason": reason})
            return
        try:
            snapshot = self._candidate_snapshot(path, "file")
            rel_path = self._relative_path(path)
            item_id = hashlib.sha256(f"file:{rel_path}:{snapshot['digest']}".encode("utf-8")).hexdigest()[:24]
            items.append(
                {
                    "item_id": item_id,
                    "path": rel_path,
                    "type": "file",
                    "size_bytes": snapshot["size_bytes"],
                    "entry_count": 1,
                    "reason": str(rule.get("reason") or "generated file"),
                    "rule_id": str(rule.get("id") or "file-rule"),
                    "risk": str(rule.get("risk") or "low"),
                    "allow_tracked": bool(rule.get("allow_tracked")),
                    "allow_direct_delete": bool(rule.get("allow_direct_delete")),
                    "age_days": round(self._age_days(path, now), 2),
                    "min_age_days": max(0.0, float(rule.get("min_age_days") or 0)),
                    "fingerprint": snapshot,
                }
            )
        except OSError as exc:
            errors.append({"path": self._relative_path(path), "type": "file", "message": str(exc)})
