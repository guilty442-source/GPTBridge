from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .cleanup_constants import (
    DEFAULT_PLAN_TTL_MINUTES,
    DEFAULT_QUARANTINE_TTL_HOURS,
    PLAN_SCHEMA_VERSION,
    _parse_iso,
)


class HealthPlanMixin:
    """Cleanup health scoring, preferences, and preview-plan management."""

    def _cleanup_health(
        self,
        scope: str,
        items: list[dict[str, Any]],
        skipped: list[dict[str, Any]],
        errors: list[dict[str, Any]],
    ) -> dict[str, Any]:
        high_count = self._risk_count(items, "high")
        medium_count = self._risk_count(items, "medium")
        low_count = self._risk_count(items, "low")
        total_bytes = sum(int(item.get("size_bytes") or 0) for item in items)
        error_count = len(errors)
        skipped_count = len(skipped)
        safety_score = max(
            0,
            100
            - min(60, high_count * 25)
            - min(35, medium_count * 8)
            - min(30, error_count * 12)
            - min(10, skipped_count // 25),
        )
        size_mib = total_bytes / (1024 * 1024)
        calculated_penalty = int(math.log2(size_mib + 1) * 12) + len(items) // 3
        cleanliness_penalty = min(80, max(1 if items else 0, calculated_penalty))
        cleanliness_score = max(0, 100 - cleanliness_penalty)
        confidence_score = max(0, 100 - error_count * 20 - min(40, skipped_count // 5))
        if not items and not errors:
            state = "clean"
            recommendation = "目前沒有可清理項目。"
            recommended_action = "none"
        elif errors:
            state = "attention"
            recommendation = "清理前先處理掃描錯誤。"
            recommended_action = "review"
        elif high_count or medium_count:
            state = "review"
            recommendation = "包含需確認項目，只允許隔離清理。"
            recommended_action = "quarantine"
        else:
            state = "ready"
            recommendation = "低風險項目可套用已驗證計畫，仍建議先隔離。"
            recommended_action = "quarantine"
        return {
            "state": state,
            "safety_level": "high" if high_count else "medium" if medium_count else "low",
            "score": safety_score,
            "safety_score": safety_score,
            "cleanliness_score": cleanliness_score,
            "confidence_score": confidence_score,
            "item_count": len(items),
            "low_count": low_count,
            "medium_count": medium_count,
            "high_count": high_count,
            "error_count": error_count,
            "skipped_count": skipped_count,
            "total_bytes": total_bytes,
            "recommended_action": recommended_action,
            "recommendation": recommendation,
            "requires_review": bool(error_count or high_count or medium_count),
            "direct_delete_allowed": bool(items and not error_count and not high_count and not medium_count),
            "quarantine_ttl_hours": self._quarantine_ttl_hours(),
            "scope": scope,
        }

    def _quarantine_ttl_hours(self) -> int:
        return max(1, int(self.rules.get("quarantine_ttl_hours") or DEFAULT_QUARANTINE_TTL_HOURS))

    def _plan_ttl_minutes(self) -> int:
        return max(1, int(self.rules.get("plan_ttl_minutes") or DEFAULT_PLAN_TTL_MINUTES))

    def update_preferences(
        self,
        *,
        quarantine_ttl_hours: int | None = None,
    ) -> dict[str, Any]:
        with self._mutation_guard("preferences") as lock:
            if not lock.get("acquired"):
                return {
                    "ok": False,
                    "busy": True,
                    "message": str(lock.get("message") or "project cleaner is busy"),
                }
            return self._update_preferences_unlocked(
                quarantine_ttl_hours=quarantine_ttl_hours,
            )

    def _update_preferences_unlocked(
        self,
        *,
        quarantine_ttl_hours: int | None = None,
    ) -> dict[str, Any]:
        override: dict[str, Any] = {}
        if self.rules_override_path.exists():
            try:
                loaded = json.loads(self.rules_override_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    override = loaded
            except (OSError, json.JSONDecodeError) as exc:
                return {"ok": False, "message": f"rule override is invalid: {exc}"}
        override.pop("automation", None)
        if quarantine_ttl_hours is not None:
            override["quarantine_ttl_hours"] = max(1, min(24 * 365, int(quarantine_ttl_hours)))
        override.setdefault("schema_version", PLAN_SCHEMA_VERSION)
        self._atomic_write_json(self.rules_override_path, override)
        self.rules, self.rule_warnings = self._load_rules()
        self._append_history(
            "preferences",
            ok=True,
            quarantine_ttl_hours=self._quarantine_ttl_hours(),
        )
        return {
            "ok": True,
            "override_path": str(self.rules_override_path),
            "quarantine_ttl_hours": self._quarantine_ttl_hours(),
            "message": "project cleaner preferences updated",
        }

    def _plan_key(self) -> bytes:
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self._harden_private_path(self.runtime_root)
        if self.key_path.exists():
            return self.key_path.read_bytes()
        key = secrets.token_bytes(32)
        try:
            with self.key_path.open("xb") as target:
                target.write(key)
            try:
                self.key_path.chmod(0o600)
            except OSError:
                pass
            return key
        except FileExistsError:
            return self.key_path.read_bytes()

    def _plan_signature(self, payload: dict[str, Any]) -> str:
        signable = {key: value for key, value in payload.items() if key != "plan_token"}
        encoded = json.dumps(
            signable,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hmac.new(self._plan_key(), encoded, hashlib.sha256).hexdigest()

    def _persist_plan(self, plan: dict[str, Any]) -> tuple[str, str]:
        plan_id = uuid.uuid4().hex
        created_at = datetime.now(timezone.utc)
        document = {
            "schema_version": PLAN_SCHEMA_VERSION,
            "plan_id": plan_id,
            "created_at": created_at.isoformat(),
            "expires_at": (created_at + timedelta(minutes=self._plan_ttl_minutes())).isoformat(),
            **plan,
        }
        token = self._plan_signature(document)
        document["plan_token"] = token
        self._atomic_write_json(self.plan_root / f"{plan_id}.json", document)
        return plan_id, token

    def _load_plan(self, plan_id: str, plan_token: str) -> tuple[dict[str, Any] | None, str]:
        clean_id = str(plan_id or "").strip().lower()
        if len(clean_id) != 32 or any(char not in "0123456789abcdef" for char in clean_id):
            return None, "valid preview plan is required"
        path = self.plan_root / f"{clean_id}.json"
        if not path.exists():
            return None, "preview plan not found"
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return None, f"preview plan is invalid: {exc}"
        if not isinstance(document, dict):
            return None, "preview plan is invalid"
        expected = self._plan_signature(document)
        stored = str(document.get("plan_token") or "")
        supplied = str(plan_token or "")
        if not stored or not supplied or not hmac.compare_digest(expected, stored) or not hmac.compare_digest(stored, supplied):
            return None, "preview plan signature mismatch"
        expires_at = _parse_iso(str(document.get("expires_at") or ""))
        if expires_at is None or expires_at <= datetime.now(timezone.utc):
            return None, "preview plan expired; run preview again"
        return document, ""

    def plan_cleanup(self, scope: str, *, now: float | None = None) -> dict[str, Any]:
        requested_scope = str(scope or "global").strip().lower()
        normalized_scope, candidate_roots = self._candidate_roots(requested_scope)
        if candidate_roots is None:
            return {
                "ok": False,
                "scope": requested_scope,
                "requested_scope": requested_scope,
                "items": [],
                "skipped": [],
                "errors": [],
                "message": f"unsupported cleanup scope: {requested_scope}",
            }
        self._emit_progress("scan", 2, "建立清理計畫", scope=normalized_scope)
        now = time.time() if now is None else now
        items: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        excluded_names = {
            str(item).casefold() for item in self.rules.get("excluded_directory_names", [])
        }
        _tracked, git_status = self._git_snapshot()
        if git_status.get("repository") and not git_status.get("available"):
            errors.append({"path": ".git", "type": "git", "message": git_status.get("error") or "git unavailable"})

        for root_index, raw_root in enumerate(candidate_roots):
            root = self._safe_resolve(raw_root)
            if not root.exists():
                continue
            if not self._inside_project(root) or self._is_protected(root):
                self._append_skip(skipped, {"path": str(raw_root), "reason": "protected or outside project"})
                continue
            for current_raw, dirnames, filenames in os.walk(root, followlinks=False):
                current = Path(current_raw)
                if self._is_protected(current):
                    dirnames[:] = []
                    continue
                kept: list[str] = []
                for dirname in sorted(dirnames):
                    child = current / dirname
                    rule = self._directory_rule(
                        dirname,
                        path=child,
                        scope=normalized_scope,
                    )
                    if (
                        rule is None
                        and dirname.casefold() in excluded_names
                    ) or self._is_protected(child):
                        continue
                    if self._is_link_or_reparse_point(child):
                        self._append_skip(skipped, {"path": self._relative_path(child), "type": "directory", "reason": "link or reparse point"})
                        continue
                    if rule is None:
                        kept.append(dirname)
                        continue
                    min_age_days = max(0.0, float(rule.get("min_age_days") or 0))
                    if self._age_days(child, now) < min_age_days:
                        continue
                    safe, reason = self._safe_candidate(child, "directory")
                    if safe and bool(rule.get("protect_user_content")) and self._directory_contains_user_content(child):
                        safe, reason = False, "directory contains source-like user content"
                    if not safe:
                        self._append_skip(skipped, {"path": self._relative_path(child), "type": "directory", "reason": reason})
                        continue
                    try:
                        snapshot = self._candidate_snapshot(child, "directory")
                        rel_path = self._relative_path(child)
                        item_id = hashlib.sha256(f"directory:{rel_path}:{snapshot['digest']}".encode("utf-8")).hexdigest()[:24]
                        contents_only = bool(
                            self.rules.get("delete_files_only", True)
                        ) or bool(rule.get("contents_only"))
                        if contents_only and int(
                            snapshot.get("file_count") or 0
                        ) == 0:
                            continue
                        items.append(
                            {
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
                                "allow_direct_delete": bool(
                                    rule.get("allow_direct_delete")
                                ),
                                "fingerprint": snapshot,
                            }
                        )
                    except OSError as exc:
                        errors.append({"path": self._relative_path(child), "type": "directory", "message": str(exc)})
                    continue
                dirnames[:] = kept

                for filename in sorted(filenames):
                    path = current / filename
                    if self._is_protected(path) or self._is_link_or_reparse_point(path):
                        continue
                    rule = self._file_rule(path, now)
                    if rule is None:
                        continue
                    safe, reason = self._safe_candidate(
                        path,
                        "file",
                        check_git=not bool(rule.get("allow_tracked")),
                    )
                    if not safe:
                        self._append_skip(skipped, {"path": self._relative_path(path), "type": "file", "reason": reason})
                        continue
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
                                "allow_direct_delete": bool(
                                    rule.get("allow_direct_delete")
                                ),
                                "age_days": round(self._age_days(path, now), 2),
                                "min_age_days": max(0.0, float(rule.get("min_age_days") or 0)),
                                "fingerprint": snapshot,
                            }
                        )
                    except OSError as exc:
                        errors.append({"path": self._relative_path(path), "type": "file", "message": str(exc)})
            self._emit_progress(
                "scan",
                15 + int((root_index + 1) / max(1, len(candidate_roots)) * 65),
                "掃描清理候選",
                item_count=len(items),
            )

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
