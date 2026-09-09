from __future__ import annotations

import importlib
import hashlib
import json
import os
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

RELOADABLE_SRC_ROOTS: Final[tuple[str, ...]] = (
    "main-system/src-core",
    "shared-layer/src",
    "ai-collaboration/src",
    "ai-assistant/src",
    "global-cleaner/src",
    "system-rescue/src",
    "file-sorter/src",
    "vaultly/src",
    "investment-mobile/src",
)

PROTECTED_MODULE_PREFIXES: Final[tuple[str, ...]] = (
    "governance_rule.codex.",
    "governance_rule.permission_directory.",
    "core_system.hot_update_service",
    "core_system.maintenance_sovereign",
)


def _is_protected(module_name: str) -> bool:
    return any(module_name == prefix or module_name.startswith(prefix + ".") for prefix in PROTECTED_MODULE_PREFIXES)


class HotUpdateService:
    """Version-gated main-system hot-update boundary and system-wide hot-reload.

    Hot-update remains a frozen, version-gated boundary.  Hot-reload is a
    separate maintenance operation: it re-executes already-loaded governed
    backend modules in place so source edits take effect without a full process
    restart, after governance authorization.
    """

    def __init__(self, app: Any, interval_seconds: float = 1.0) -> None:
        self.app = app
        self.interval_seconds = max(0.5, interval_seconds)
        fallback_root = Path(__file__).resolve().parents[3]
        self.project_root = Path(
            getattr(app, "project_root", str(fallback_root))
        ).resolve()

    def _resolve_src_roots(self) -> list[Path]:
        roots: list[Path] = []
        for relative in RELOADABLE_SRC_ROOTS:
            root = (self.project_root / relative).resolve()
            if root.is_dir():
                roots.append(root)
        return roots

    def _is_in_src_root(self, module: types.ModuleType, roots: list[Path]) -> bool:
        file_path = getattr(module, "__file__", None)
        if not file_path:
            return False
        try:
            resolved = Path(file_path).resolve()
        except (OSError, ValueError):
            return False
        return any(_is_inside(resolved, root) for root in roots)

    def _authenticate(self, governance: Any, approval_token: str | None) -> tuple[bool, str]:
        if governance is None:
            return False, "governance-unavailable"
        if not approval_token:
            return False, "missing-approval-token"
        auth = getattr(governance, "_authentication", None)
        if auth is None or not hasattr(auth, "authenticate_token"):
            return False, "governance-authentication-unavailable"
        try:
            claims = auth.authenticate_token(approval_token)
        except Exception as error:
            return False, f"permission-denied: {error}"
        if getattr(claims, "capability", "") != "hot-update" and getattr(claims, "capability", "") != "hot-reload":
            return False, "capability-mismatch"
        return True, ""

    def _persist_reload_protection(self, module_names: list[str]) -> None:
        """Pin successfully reloaded source revisions against auto-repair."""
        protected: dict[str, str] = {}
        for module_name in module_names:
            module = sys.modules.get(module_name)
            file_path = getattr(module, "__file__", None)
            if not file_path:
                continue
            try:
                path = Path(file_path).resolve()
                relative = path.relative_to(self.project_root).as_posix()
                protected[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
            except (OSError, ValueError):
                continue
        if not protected:
            return
        target = (
            self.project_root / "main-system" / "runtime" / "state"
            / "hot-reload-protection.json"
        )
        payload = {
            "version": 1,
            "protected_sources": protected,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, target)
        except OSError:
            pass

    def reload_modules(
        self,
        *,
        governance: Any = None,
        approval_token: str | None = None,
        modules: Any = None,
    ) -> types.SimpleNamespace:
        """Reload governed backend modules after governance authorization.

        ``modules`` may be a list of dotted module names, or ``None`` to reload
        every loaded module that lives in the configured backend src roots.
        Returns a SimpleNamespace with ``ok``, ``reloaded``, ``skipped``,
        ``errors`` and ``error`` attributes.
        """
        authorized, auth_message = self._authenticate(governance, approval_token)
        if not authorized:
            return types.SimpleNamespace(
                ok=False,
                reloaded=[],
                skipped=[],
                errors=[auth_message],
                error=auth_message,
            )

        roots = self._resolve_src_roots()
        if not roots:
            return types.SimpleNamespace(
                ok=False,
                reloaded=[],
                skipped=[],
                errors=["no-reloadable-src-roots"],
                error="no-reloadable-src-roots",
            )

        requested: set[str] | None = None
        if modules is not None:
            try:
                requested = set(str(item).strip() for item in modules if item)
            except Exception:
                return types.SimpleNamespace(
                    ok=False,
                    reloaded=[],
                    skipped=[],
                    errors=["invalid-modules-argument"],
                    error="invalid-modules-argument",
                )

        reloaded: list[str] = []
        skipped: list[str] = []
        errors: list[str] = []

        for module_name in list(sys.modules.keys()):
            if requested is not None and module_name not in requested:
                continue
            module = sys.modules[module_name]
            if not isinstance(module, types.ModuleType):
                continue
            if _is_protected(module_name):
                skipped.append(module_name)
                continue
            if not self._is_in_src_root(module, roots):
                if requested is not None and module_name in requested:
                    skipped.append(module_name)
                continue
            try:
                importlib.reload(module)
                reloaded.append(module_name)
            except Exception as error:
                errors.append(f"{module_name}: {error}")
                skipped.append(module_name)

        if reloaded:
            self._persist_reload_protection(reloaded)

        return types.SimpleNamespace(
            ok=len(errors) == 0,
            reloaded=reloaded,
            skipped=skipped,
            errors=errors,
            error=errors[0] if errors else "",
        )

    def start(self) -> None:
        """No in-process update loop; hot-reload is triggered explicitly."""
        return None

    async def stop(self) -> None:
        return None

    async def _apply(
        self,
        _plan: dict[str, Any],
        *,
        repairs_completed: bool = False,
    ) -> None:
        del repairs_completed
        raise PermissionError("PERMISSION_DENIED")


def _is_inside(candidate: Path, root: Path) -> bool:
    try:
        candidate.resolve(strict=False).relative_to(root.resolve())
        return True
    except (OSError, RuntimeError, ValueError):
        return False


__all__ = ["HotUpdateService"]
