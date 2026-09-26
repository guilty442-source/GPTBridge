"""Package version sync driver — governed detect/upgrade/codex-amend loop.

A624 + §1.1：自動化核心擁有套件版本收斂。每個 tick：

1. **detect** — 依 ``config/package-version-sync.json`` 的技術表收集已
   安裝版本（vendored 工具鏈、manifest 宣告、執行期查詢、pip show）。
2. **upgrade** — mode=upgrade 的技術執行其套件管理器升級命令（bounded
   timeout、env 政策注入）；離線／vendored 生態 fail-closed 記帳，不
   fallback 網路抓取（A622 禁止有 vendored 副本仍走網路）。
3. **amend** — 偵測值高於法典條文宣告值時（升級已落地），產生
   ``codex-amendment-request`` JSON 投遞 convergence intake；升版文案
   只替換條文 rule 內的版本 token。法典版本是 canonical 需求釘——
   偵測值低於釘定值屬 pending-upgrade，記入狀態檔而不產生降級修正案。
   落地一律經既有
   ``codex-amendment-intake`` → 五主權稽核 → ``auto_execute`` 管線——
   本 driver 永不直接寫權威庫。

Kill switch：flow manifest ``enabled=false``（A10）或政策檔
``enabled=false``。只經 ``AutomationCore.register_flow`` 掛共享
PeriodicScheduler；被拒絕時不回落私有迴圈。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

_logger = logging.getLogger("gptbridge.package_version_sync")

FLOW_ID = "package-version-sync"

_MAIN_SYSTEM_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_POLICY = _MAIN_SYSTEM_ROOT / "config" / "package-version-sync.json"
_DEFAULT_STATE = (
    _MAIN_SYSTEM_ROOT / "runtime" / "state" / "package-version-sync.json"
)
_DEFAULT_LOG = (
    _MAIN_SYSTEM_ROOT / "runtime" / "logs" / "package-version-sync.jsonl"
)


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _version_gt(actual: str, declared: str) -> bool:
    """Numeric dotted-version compare; non-numeric tails ignored."""
    def parts(v: str) -> tuple[int, ...]:
        return tuple(int(x) for x in re.findall(r"\d+", v))

    a, d = parts(actual), parts(declared)
    return a > d


class PackageVersionDriver:
    """Periodic governed package-version reconciliation (A624)."""

    def __init__(
        self,
        app: Any,
        toolbox_service: Any = None,
        *,
        project_root: Path | None = None,
        policy_path: Path | None = None,
        state_path: Path | None = None,
    ) -> None:
        self.app = app
        self.toolbox = toolbox_service
        self._project_root = Path(
            project_root or getattr(app, "project_root", "")
        ).resolve()
        self._policy_path = policy_path or _DEFAULT_POLICY
        self._state_path = state_path or _DEFAULT_STATE
        self._registered = False
        self._last_decision = ""
        self._last_error = ""
        self._last_drift: list[dict[str, Any]] = []
        self._last_upgrades: list[dict[str, Any]] = []
        self._last_pending: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # lifecycle — the AutomationCore owns the schedule (§1.1)
    # ------------------------------------------------------------------

    async def start(self) -> dict[str, Any]:
        core = getattr(self.app, "automation_core", None)
        if core is None:
            _logger.warning(
                "package-version-sync: no automation core — not starting "
                "(no private loop fallback)"
            )
            return {"status": "no-automation-core"}
        self._registered = core.register_flow(FLOW_ID, self.run_once)
        return {
            "status": "registered" if self._registered else "denied",
            "flow": FLOW_ID,
        }

    async def stop(self) -> None:
        core = getattr(self.app, "automation_core", None)
        if core is not None and self._registered:
            try:
                core.unregister(FLOW_ID)
            except Exception:
                pass
        self._registered = False

    # ------------------------------------------------------------------
    # tick
    # ------------------------------------------------------------------

    async def run_once(self) -> None:
        """One scheduler tick — never raises into the shared loop."""
        try:
            decision = await self._tick_inner()
            self._last_error = ""
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 — audit then isolate
            decision = "error"
            self._last_error = f"{type(error).__name__}: {error}"
            _logger.warning("package-version-sync tick failed: %s", error)
        self._last_decision = decision
        self._write_state()

    async def _tick_inner(self) -> str:
        policy = self._load_policy()
        if not policy.get("enabled"):
            return "disabled"
        technologies = policy.get("technologies") or {}
        if not isinstance(technologies, dict) or not technologies:
            return "no-technologies"
        detected, upgrades = await asyncio.to_thread(
            self._detect_and_upgrade, policy, technologies
        )
        self._last_upgrades = upgrades
        drift = await asyncio.to_thread(
            self._drift_vs_codex, technologies, detected
        )
        self._last_drift = drift
        emitted = ""
        if drift:
            emitted = await asyncio.to_thread(
                self._emit_amendment, policy, drift, detected
            )
        return (
            f"detected:{len([v for v in detected.values() if v])} "
            f"upgraded:{sum(1 for u in upgrades if u.get('changed'))} "
            f"drift:{len(drift)} {emitted or 'no-amendment'}"
        )

    # ------------------------------------------------------------------
    # policy
    # ------------------------------------------------------------------

    def _load_policy(self) -> dict[str, Any]:
        try:
            payload = json.loads(
                self._policy_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as error:
            _logger.warning("package-version policy unreadable: %s", error)
            return {"enabled": False}
        return payload if isinstance(payload, dict) else {"enabled": False}

    # ------------------------------------------------------------------
    # detect + upgrade (worker thread)
    # ------------------------------------------------------------------

    def _detect_and_upgrade(
        self, policy: dict[str, Any], technologies: dict[str, Any]
    ) -> tuple[dict[str, str], list[dict[str, Any]]]:
        detected: dict[str, str] = {}
        upgrades: list[dict[str, Any]] = []
        detect_timeout = float(policy.get("detect_timeout_s") or 30)
        for name, spec in technologies.items():
            if not isinstance(spec, dict):
                continue
            mode = str(spec.get("mode") or "detect")
            if mode == "off":
                continue
            version = self._detect(name, spec, detect_timeout)
            if version:
                detected[name] = version
            if mode == "upgrade":
                upgrades.append(self._upgrade(name, spec, policy))
                # re-detect after upgrade so codex follows actuals
                version = self._detect(name, spec, detect_timeout)
                if version:
                    detected[name] = version
        return detected, upgrades

    def _resolve(self, arg: str) -> str:
        p = Path(arg)
        if not p.is_absolute():
            p = self._project_root / arg
        return str(p)

    def _run_argv(
        self,
        argv: list[str],
        *,
        cwd: Path,
        timeout: float,
        extra_env: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        resolved = [self._resolve(argv[0]), *argv[1:]]
        env = os.environ.copy()
        env.update(extra_env or {})
        try:
            proc = subprocess.run(
                resolved,
                cwd=str(cwd),
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return -1, f"{type(error).__name__}: {error}"
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    def _detect(
        self, name: str, spec: dict[str, Any], timeout: float
    ) -> str:
        detect = spec.get("detect")
        if not isinstance(detect, dict):
            return ""
        kind = detect.get("kind")
        pattern = detect.get("pattern") or r"(\d+\.\d+(?:\.\d+)?)"
        try:
            if kind == "command":
                rc, out = self._run_argv(
                    list(detect.get("argv") or []),
                    cwd=self._project_root,
                    timeout=timeout,
                )
                if rc != 0 or not out:
                    return ""
                if detect.get("extract") == "major":
                    majors = [
                        int(m.group(1))
                        for m in re.finditer(r"(\d+)\.", out)
                    ]
                    return str(max(majors)) if majors else ""
                m = re.search(pattern, out)
                return m.group(1) if m else ""
            if kind == "postgres":
                from governance_rule.execution.codex_repository import (
                    codex_readonly_connection,
                )

                with codex_readonly_connection() as conn:
                    row = conn.execute(
                        str(detect.get("query") or "SHOW server_version")
                    ).fetchone()
                text = str(row[0]) if row else ""
                m = re.search(pattern, text)
                return m.group(1) if m else ""
            if kind == "pip":
                rc, out = self._run_argv(
                    [
                        str(
                            self._project_root
                            / "main-system/.venv/Scripts/python.exe"
                        ),
                        "-m",
                        "pip",
                        "show",
                        str(detect.get("package") or ""),
                    ],
                    cwd=self._project_root,
                    timeout=timeout,
                )
                if rc != 0:
                    return ""
                m = re.search(r"^Version:\s*(\S+)", out, re.M)
                if not m:
                    return ""
                mm = re.search(pattern, m.group(1))
                return mm.group(1) if mm else m.group(1)
            if kind == "manifest-json":
                data = json.loads(
                    (self._project_root / str(detect.get("path")))
                    .read_text(encoding="utf-8")
                )
                node: Any = data
                for part in str(detect.get("field") or "").split("."):
                    node = node.get(part) if isinstance(node, dict) else None
                if not isinstance(node, str):
                    return ""
                node = node.lstrip("^~>=< ")
                m = re.search(pattern, node)
                return m.group(1) if m else ""
        except Exception as error:  # noqa: BLE001 — detection never breaks
            _logger.debug("detect %s failed: %s", name, error)
        return ""

    def _upgrade(
        self, name: str, spec: dict[str, Any], policy: dict[str, Any]
    ) -> dict[str, Any]:
        upgrade = spec.get("upgrade")
        if not isinstance(upgrade, dict):
            return {"name": name, "changed": False, "reason": "no-upgrade"}
        cwd = self._project_root / str(upgrade.get("cwd") or ".")
        argv = list(upgrade.get("argv") or [])
        if not argv:
            return {"name": name, "changed": False, "reason": "empty-argv"}
        rc, out = self._run_argv(
            argv,
            cwd=cwd,
            timeout=float(policy.get("upgrade_timeout_s") or 600),
            extra_env=upgrade.get("env") or None,
        )
        record = {
            "name": name,
            "argv": argv,
            "returncode": rc,
            "changed": rc == 0,
            "tail": out.strip().splitlines()[-1][:200] if out.strip() else "",
        }
        self._audit({"op": "upgrade", **record})
        return record

    # ------------------------------------------------------------------
    # codex drift → amendment emission (never direct writes)
    # ------------------------------------------------------------------

    def _drift_vs_codex(
        self, technologies: dict[str, Any], detected: dict[str, str]
    ) -> list[dict[str, Any]]:
        refs: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for name, spec in technologies.items():
            actual = detected.get(name)
            if not actual or not isinstance(spec, dict):
                continue
            for ref in spec.get("codex_refs") or []:
                if not isinstance(ref, dict):
                    continue
                refs.setdefault(
                    (str(ref.get("provision_id")), str(ref.get("field") or "rule")),
                    [],
                ).append({"tech": name, "actual": actual, **ref})
        if not refs:
            return []
        from governance_rule.execution.codex_repository import (
            codex_readonly_connection,
        )

        drift: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        with codex_readonly_connection() as conn:
            for (pid, field), reflist in refs.items():
                row = conn.execute(  # sql-ok: column name from manifest field whitelist, value parameterized
                    f"SELECT {field} FROM articles WHERE provision_id=?",
                    (pid,),
                ).fetchone()
                if not row:
                    continue
                text = str(row[0])
                new_text = text
                touched: list[dict[str, str]] = []
                for ref in reflist:
                    pat = ref.get("pattern")
                    if not pat:
                        continue
                    m = re.search(pat, new_text)
                    if not m:
                        continue
                    declared = m.group(1)
                    if declared == ref["actual"]:
                        continue
                    # A624: codex pins are canonical *requirements*; the
                    # driver restamps only upgrades (actual > declared).
                    # An actual below the pin is a pending upgrade — recorded
                    # in state, never emitted as a downgrade amendment.
                    if not _version_gt(ref["actual"], declared):
                        pending.append(
                            {
                                "tech": ref["tech"],
                                "provision_id": pid,
                                "declared": declared,
                                "actual": ref["actual"],
                            }
                        )
                        continue
                    new_text = new_text.replace(
                        m.group(0),
                        m.group(0).replace(declared, ref["actual"], 1),
                        1,
                    )
                    touched.append(
                        {
                            "tech": ref["tech"],
                            "declared": declared,
                            "actual": ref["actual"],
                        }
                    )
                if touched and new_text != text:
                    drift.append(
                        {
                            "provision_id": pid,
                            "field": field,
                            "proposed": new_text,
                            "touched": touched,
                        }
                    )
        self._last_pending = pending
        return drift

    def _emit_amendment(
        self,
        policy: dict[str, Any],
        drift: list[dict[str, Any]],
        detected: dict[str, str],
    ) -> str:
        from governance_rule.execution.codex_repository import (
            codex_readonly_connection,
        )

        with codex_readonly_connection() as conn:
            meta = {
                r[0]: r[1]
                for r in conn.execute(
                    "SELECT key, value FROM metadata WHERE key IN "
                    "('codex_version','current_version_identity',"
                    "'current_version_epoch')"
                )
            }
            seq, head = conn.execute(
                "SELECT sequence, entry_hash FROM revision_history "
                "ORDER BY sequence DESC LIMIT 1"
            ).fetchone()

        date = time.strftime("%Y%m%d", time.gmtime())
        request_id = f"package-version-sync-{date}"
        intake = self._project_root / str(
            policy.get("intake_dir")
            or "governance_rule/execution/audit/convergence"
        )
        intake.mkdir(parents=True, exist_ok=True)
        # in-flight dedupe: identical drift already queued → skip
        suffix = ""
        for n in range(1, 9):
            candidate = intake / (
                f"codex-amendment-request-{request_id}{suffix}.json"
            )
            if not candidate.exists():
                break
            try:
                prior = json.loads(candidate.read_text(encoding="utf-8"))
                if [c.get("proposed") for c in prior.get("changes") or []] == [
                    d["proposed"] for d in drift
                ]:
                    return "amendment-already-queued"
            except (OSError, json.JSONDecodeError):
                pass
            suffix = f"-r{n + 1}"
        else:
            return "amendment-queue-saturated"

        touched = sorted(
            {t["tech"] for d in drift for t in d["touched"]}
        )
        request = {
            "artifact": "codex-amendment-request",
            "authority": "request-only",
            "schema": "codex-amendment-request/v1",
            "request_id": f"{request_id}{suffix}",
            "title": "Package version drift convergence (A624)",
            "summary": (
                "Automation package-version-sync detected installed "
                "versions diverging from codex-declared pins: "
                + "; ".join(
                    f"{t['tech']} {t['declared']}→{t['actual']}"
                    for d in drift
                    for t in d["touched"]
                )
            ),
            "requested_by": "automation-sovereign",
            "origin": "package-version-sync driver drift detection (A624)",
            "change_class": "clarification",
            "required_review": "five-sovereign-audit-unanimous-pass",
            "flow": "A382/A488-non-disruptive-amendment-flow",
            "not_executed": True,
            "auto_execute": True,
            "predecessor": {
                "codex_version": meta.get("codex_version"),
                "version_identity": meta.get("current_version_identity"),
                "version_epoch": int(meta.get("current_version_epoch") or 0),
                "history_head": head,
                "revision_sequence": seq,
            },
            "problem": {
                "summary": (
                    "codex-declared versions stale relative to installed "
                    "toolchain/package actuals"
                )
            },
            "changes": [
                {
                    "table": "articles",
                    "key": {"provision_id": d["provision_id"]},
                    "field": d["field"],
                    "proposed": d["proposed"],
                }
                for d in drift
            ],
            "verification": {
                "requested_at": _iso_now(),
                "detected_versions": detected,
                "expected_audit_result": (
                    "declared versions match installed actuals"
                ),
            },
        }
        candidate.write_text(
            json.dumps(request, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self._audit(
            {
                "op": "emit-amendment",
                "request_id": f"{request_id}{suffix}",
                "provisions": [d["provision_id"] for d in drift],
                "technologies": touched,
            }
        )
        _logger.info(
            "package-version drift amendment emitted: %s (%s)",
            f"{request_id}{suffix}",
            ",".join(touched),
        )
        return f"amendment:{request_id}{suffix}"

    # ------------------------------------------------------------------
    # observability
    # ------------------------------------------------------------------

    def _audit(self, record: dict[str, Any]) -> None:
        try:
            _DEFAULT_LOG.parent.mkdir(parents=True, exist_ok=True)
            with _DEFAULT_LOG.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {"at": _iso_now(), **record}, ensure_ascii=False
                    )
                    + "\n"
                )
        except OSError:
            pass

    def _write_state(self) -> None:
        # Perf/low-IO: ticks usually repeat the same decision — skip the
        # tmp-write + replace when content (excluding ``written_at``)
        # matches the last write.
        fingerprint = json.dumps(
            {
                "flow": FLOW_ID,
                "last_decision": self._last_decision,
                "last_error": self._last_error,
                "drift": self._last_drift,
                "upgrades": self._last_upgrades,
                "pending_upgrade": self._last_pending,
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        if getattr(self, "_last_state_json", None) == fingerprint:
            return
        payload = {
            "flow": FLOW_ID,
            "last_decision": self._last_decision,
            "last_error": self._last_error,
            "drift": self._last_drift,
            "upgrades": self._last_upgrades,
            "pending_upgrade": self._last_pending,
            "written_at": _iso_now(),
        }
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self._state_path)
            self._last_state_json = fingerprint
        except OSError:
            pass

    def status(self) -> dict[str, Any]:
        return {
            "flow": FLOW_ID,
            "registered": self._registered,
            "last_decision": self._last_decision,
            "last_error": self._last_error,
            "drift": list(self._last_drift),
        }


__all__ = ["PackageVersionDriver", "FLOW_ID"]
