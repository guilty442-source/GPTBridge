from __future__ import annotations

import difflib
from pathlib import PurePosixPath
from typing import Any


class CodingExpertProcessMixin:
    """Test generation, upgrade-target resolution, and the main process entry point."""

    @classmethod
    def _generated_tests(cls, spec: dict[str, Any], source: str) -> dict[str, Any]:
        if str(spec.get("language")) == "python" and str(spec.get("kind")) == "api":
            test_source = (
                source.rstrip()
                + "\n\n"
                + "def test_api_contract():\n"
                + "    assert app.title\n"
                + "    assert callable(create_item)\n"
                + "    assert callable(read_item)\n"
            )
            return {
                "available": True,
                "source": test_source,
                "validation": cls._validate_source("python", test_source),
                "executed": False,
            }
        cases = spec.get("test_cases")
        if (
            str(spec.get("language")) != "python"
            or str(spec.get("kind")) != "function"
            or not isinstance(cases, list)
            or not cases
        ):
            return {"available": False, "source": "", "validation": None}
        test_spec = {
            "kind": "test",
            "subject": cls._identifier(spec.get("name")),
            "name": f"test_{cls._identifier(spec.get('name'))}",
            "test_cases": cases,
            "description": "星澄依規格產生的驗證案例",
        }
        test_source = source.rstrip() + "\n\n" + cls._python_test(test_spec, "")
        return {
            "available": True,
            "source": test_source,
            "validation": cls._validate_source("python", test_source),
            "executed": False,
        }

    @classmethod
    def _upgrade_target(
        cls,
        value: Any,
        language: str,
        *,
        existing_source: bool = False,
        selected_folder_scope: bool = False,
    ) -> dict[str, Any]:
        candidate = PurePosixPath(str(value or "").replace("\\", "/"))
        suffixes = {
            "python": {".py"},
            "typescript": {".ts", ".tsx"},
            "javascript": {".js", ".jsx", ".mjs"},
            "sql": {".sql"},
            "json": {".json"},
        }
        valid = (
            not candidate.is_absolute()
            and ".." not in candidate.parts
            and candidate.suffix.casefold() in suffixes.get(language, set())
            and bool(candidate.parts)
            and candidate.parts[0].casefold() not in cls.PROTECTED_PROJECT_ROOTS
            and (
                selected_folder_scope
                or len(candidate.parts) == 1
                or candidate.parts[0].casefold() in cls.PROJECT_SOURCE_ROOTS
                or tuple(part.casefold() for part in candidate.parts[:4])
                == ("src", "backend", "services", "xingcheng")
            )
        )
        return {
            "path": candidate.as_posix(),
            "within_project_source": valid,
            "programming_project_root": "user-selected-folder",
            "outside_project_access": False,
            "project_scope": "all-project-source-excluding-governance-rule",
            "governance_rule_excluded": True,
            # Retained for v1 proposal readers; the effective scope is now
            # the project-wide field above.
            "within_xingcheng_source": valid,
            "new_file_only": not existing_source,
        }

    @staticmethod
    def _unified_diff(original: str, proposed: str, target_path: str) -> str:
        return "".join(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                proposed.splitlines(keepends=True),
                fromfile=f"a/{target_path}",
                tofile=f"b/{target_path}",
            )
        )

    def process(self, payload: dict[str, Any], intent: str) -> dict[str, Any]:
        prompt = str(payload.get("instruction") or payload.get("prompt") or "").strip()
        spec = self._normalize_spec(payload, prompt, intent)
        language = str(spec["language"])
        action = str(spec["action"])
        if language not in self.ALLOWED_LANGUAGES:
            return {
                "ok": False,
                "error_code": "UNSUPPORTED_CODE_LANGUAGE",
                "supported_languages": sorted(self.ALLOWED_LANGUAGES),
            }
        if language == "python" and str(spec["kind"]) not in self.ALLOWED_PYTHON_KINDS:
            return {
                "ok": False,
                "error_code": "UNSUPPORTED_PYTHON_ARTIFACT",
                "supported_kinds": sorted(self.ALLOWED_PYTHON_KINDS),
            }
        if (
            language in {"typescript", "javascript"}
            and str(spec["kind"]) not in self.ALLOWED_SCRIPT_KINDS
        ):
            return {
                "ok": False,
                "error_code": "UNSUPPORTED_SCRIPT_ARTIFACT",
                "supported_kinds": sorted(self.ALLOWED_SCRIPT_KINDS),
            }
        supplied_source = str(payload.get("source_code") or spec.get("source_code") or "")
        if action in {"analyze", "refactor"} and not supplied_source.strip():
            return {"ok": False, "error_code": "SOURCE_CODE_REQUIRED", "action": action}
        if action == "analyze":
            source = supplied_source
        elif action == "refactor":
            source = (
                self._refactor_python(supplied_source)
                if language == "python"
                else supplied_source.strip() + "\n"
            )
        else:
            if language == "python":
                source = self._python_source(spec, prompt)
            elif language in {"typescript", "javascript"}:
                source = self._script_source(spec, prompt, language)
            elif language == "sql":
                source = self._sql_source(spec)
            else:
                source = self._json_document(spec, prompt)
        validation = self._validate_source(language, source)
        tests = self._generated_tests(spec, source)
        result: dict[str, Any] = {
            "ok": validation["ok"],
            "action": action,
            "artifact_kind": str(spec["kind"]),
            "language": language,
            "source": source,
            "validation": validation,
            "generated_tests": tests,
            "normalized_spec": spec,
            "synthesis": "star-constrained-ast-program-synthesis",
            "generated_by": "star-coding-native-model",
            "network_used": False,
            "external_model_used": False,
            "executed": False,
        }
        if intent == "self_upgrade" or action == "self_upgrade":
            original_source = str(
                payload.get("original_source") or spec.get("original_source") or ""
            )
            target = self._upgrade_target(
                spec.get("target_path")
                or {
                    "python": "xingcheng/src/backend/services/xingcheng/application/generated_extension.py",
                    "typescript": "xingcheng/src/backend/services/xingcheng/application/generated_extension.ts",
                    "javascript": "xingcheng/src/backend/services/xingcheng/application/generated_extension.js",
                    "sql": "xingcheng/src/backend/services/xingcheng/application/generated_query.sql",
                    "json": "xingcheng/src/backend/services/xingcheng/application/generated_extension.json",
                }[language],
                language,
                existing_source=bool(original_source),
                selected_folder_scope=bool(
                    str(payload.get("programming_folder") or "").strip()
                ),
            )
            tests_valid = tests["available"] is False or bool(
                isinstance(tests.get("validation"), dict)
                and tests["validation"].get("ok") is True
            )
            approved = validation["ok"] and tests_valid and target["within_project_source"]
            result["upgrade_proposal"] = {
                "schema": "star-self-upgrade-proposal/v1",
                "target": target,
                "self_authored": True,
                "proposal_ready": approved,
                "source_sha256": validation["sha256"],
                "test_source_sha256": (
                    tests["validation"]["sha256"]
                    if tests["available"] and isinstance(tests.get("validation"), dict)
                    else ""
                ),
                "change_type": "modify" if original_source else "create",
                "unified_diff": self._unified_diff(
                    original_source,
                    source,
                    str(target["path"]),
                ),
                "required_checks": [
                    "scope-validation",
                    "syntax-validation",
                    "static-security-scan",
                    "isolated-test-suite",
                    "governance-audit",
                    "recoverable-backup",
                ],
                "source_write_performed": False,
                "publish_authority": "governance-versioned-release-only",
                "rollback_required": True,
            }
        return result
