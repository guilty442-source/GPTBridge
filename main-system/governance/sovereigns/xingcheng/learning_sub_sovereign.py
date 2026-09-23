"""Learning Sub-Sovereign — 學習子主宰（星澄專屬管理，無決策、無執行）。

法典依據 (A485 — learning-sub-sovereign-transfer-to-xingcheng):
- sovereign_id: learning-evidence-sync-sub-sovereign (identity preserved)
- display name: learning-sub-sovereign
- area: system-learning
- parent: 星澄 (xingcheng_sovereign)
- relation: privileged-institution-managed-sub-sovereign
- rank: child-of-星澄-no-decision-no-execution
- duties: manage learning-evidence modules, assign bounded learning work,
  coordinate evidence normalization/evaluation/retention, collect outcome proof
- boundary: no decision/execution power; cannot alter models, code, Codex,
  permissions, routing or active behavior
- information: 星澄-to-learning instructions/events/evidence/results use the
  information layer (including the 星澄 auxiliary private channel)

Module home: ``governance/sovereigns/xingcheng/`` (A485 module assignment to
the 星澄 owner).  The implementation was merged from the retired
``core_system.learning_system_sovereign.LearningSystemSovereign`` so the
active path keeps its persistent system-error learning behavior (fault
learning, repair outcomes, evidence feedback) while operating under the
codex ``learning-evidence-sync-sub-sovereign`` identity.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from governance.sub_sovereigns._base import SubSovereignBase
from governance_rule.execution.codex_official import official_self_declaration
from core_system.codex_decision import accepted_outcome, refusal_outcome
from .learning_reconciliation import LearningReconciliationMixin


_logger = logging.getLogger("gptbridge.sovereign.learning-evidence-sync")

_DECLARATION = official_self_declaration("learning-evidence-sync-sub-sovereign")
if _DECLARATION is None:
    raise RuntimeError("learning evidence sync sub-sovereign not found in Governance Codex")

# Bounded learning commands the codex parent (星澄) may delegate (A485).
# The child never self-arms its learning loop — arming, driving and
# disarming are all commanded through the governed delegation path.
_LEARNING_INTENTS: Final = frozenset(
    {
        "learn.auto-start",
        "learn.auto-stop",
        "learn.reconcile",
        "learn.outcome",
        "learn.evidence",
        "learn.analyze",
        "learn.teach",
    }
)

from .learning_constants import (
    RECONCILIATION_AUDIT_RELATIVE,
    NON_ACTIONABLE_REMEDY,
    DEFAULT_RECONCILE_INTERVAL_SECONDS,
    RECONCILE_EVENT_POLL_SECONDS,
    RECONCILE_INTERVAL_ENV,
)


class LearningEvidenceSyncSubSovereign(SubSovereignBase, LearningReconciliationMixin):
    """Learns verified error/remedy outcomes without gaining execution power."""

    sovereign_id = "learning-evidence-sync-sub-sovereign"
    parent_sovereign_id = "星澄"

    ROLE = sovereign_id

    # Base coordination intents plus the parent's bounded learn.* commands.
    _INTENT_ALLOWLIST = SubSovereignBase._INTENT_ALLOWLIST | _LEARNING_INTENTS

    def __init__(
        self,
        app: Any | None = None,
        parent: Any | None = None,
        *,
        reconcile_interval: float | None = None,
    ) -> None:
        super().__init__(app, parent)
        self._store: Any | None = None
        self._learner: Any | None = None
        self._sync_state: dict[str, Any] = {}
        self._reconcile_task: asyncio.Task[Any] | None = None
        self._reconcile_interval = reconcile_interval
        self._last_reconciliation: dict[str, Any] = {}
        self._auto_learning_armed = False
        self._fault_manual_catalog: tuple[dict[str, Any], ...] = ()
        self._fault_manual_catalog_hash = ""

    def _ingest_fault_manual_catalog(self) -> None:
        """Load the canonical fault manuals into the learning module.

        Fault identities remain owned by permission-sovereign.  This method
        transfers only maintenance-manual learning and management into the
        星澄 learning module and never mutates the canonical Codex database.
        """
        root = Path(getattr(self.app, "project_root", ".")).resolve()
        database = root / "governance_rule" / "codex" / "data" / "governance_codex.sqlite3"
        if not database.is_file():
            self._fault_manual_catalog = ()
            self._fault_manual_catalog_hash = ""
            return
        connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            records = tuple(
                dict(row)
                for row in connection.execute(  # sql-ok: catalog hash covers the full row
                    "SELECT * FROM maintenance_manual_directory "
                    "WHERE retired_version IS NULL ORDER BY manual_code"
                )
            )
        finally:
            connection.close()
        payload = json.dumps(records, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self._fault_manual_catalog = records
        self._fault_manual_catalog_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()

    async def start(self) -> dict[str, Any]:
        from tasks.repair_learning import RepairLearner, RepairLearningStore

        root = Path(getattr(self.app, "project_root", ".")).resolve()
        self._store = RepairLearningStore(root / "main-system" / "data" / "automatic-repair")
        self._learner = RepairLearner(self._store)
        self._ingest_fault_manual_catalog()
        self._started = True
        # E173: activation returns a light receipt — analyze_history()
        # runs on demand in status(), not on the startup critical path.
        # The reconcile loop is NOT self-armed: only a parent-delegated
        # ``learn.auto-start`` command may arm it (A485 commanded learning).
        return {
            "ok": True,
            "role": self.ROLE,
            "started": self._started,
            "duties": list(_DECLARATION.duties),
            "execution": "governed-executor-only",
            "persistence": "repair-learning-sqlite",
            "reconciliation": "commanded-by-parent",
            "fault_manuals": len(self._fault_manual_catalog),
        }

    async def stop(self) -> None:
        self._auto_learning_armed = False
        await self._stop_reconcile_loop()
        self._started = False

    def sync_learning_evidence(self, evidence: dict[str, Any]) -> None:
        """同步學習證據。"""
        self._sync_state = {
            "learning_evidence": evidence,
            "synced_at": self._iso_now(),
        }

    def status(self) -> dict[str, Any]:
        analysis = self._learner.analyze_history() if self._learner is not None else {}
        return {
            "role": self.ROLE,
            "started": self._started,
            "parent": self.parent_sovereign_id,
            "duties": list(_DECLARATION.duties),
            "execution": "governed-executor-only",
            "persistence": "repair-learning-sqlite",
            "analysis": analysis,
            "reconciliation": dict(self._last_reconciliation),
            "auto_learning": (
                "armed" if self._auto_learning_armed else "disarmed"
            ),
            "commanded_by": self.parent_sovereign_id,
            "reconcile_loop": bool(
                self._reconcile_task is not None and not self._reconcile_task.done()
            ),
            "fault_manual_catalog": {
                "owner": self.sovereign_id,
                "source": "governance-codex:maintenance_manual_directory",
                "mode": "read-only-learning-ingestion",
                "count": len(self._fault_manual_catalog),
                "catalog_hash": self._fault_manual_catalog_hash,
                "fault_directory_owner": "permission-sovereign",
            },
        }

    def live_status(self) -> dict[str, Any]:
        base = self.status()
        base["sync_state"] = self._sync_state
        return base

    # ------------------------------------------------------------------
    # Parent-commanded learning adjudication (A485)
    # ------------------------------------------------------------------

    async def _adjudicate(self, request: Any) -> Any:
        """Parent-authorized dispatch: learn.* commands + base coordination.

        ``_verify_parent_authorization`` runs exactly once here — the base
        coordination intents are dispatched to their handlers directly so
        the single-use delegation nonce is never consumed twice.
        """
        if not await self._verify_parent_authorization(request):
            return refusal_outcome(
                "PARENT_AUTHORIZATION_REQUIRED",
                self.verified_basis("A130", "A334"),
            )
        intent = request.intent
        if intent == "learn.auto-start":
            return await self._adjudicate_learn_auto_start(request)
        if intent == "learn.auto-stop":
            return await self._adjudicate_learn_auto_stop(request)
        if intent == "learn.reconcile":
            return await self._adjudicate_learn_reconcile(request)
        if intent == "learn.outcome":
            return self._adjudicate_learn_outcome(request)
        if intent == "learn.evidence":
            return self._adjudicate_learn_evidence(request)
        if intent == "learn.analyze":
            return self._adjudicate_learn_analyze(request)
        if intent == "learn.teach":
            return self._adjudicate_learn_teach(request)
        return await self._adjudicate_coordination(request)

    async def _adjudicate_coordination(self, request: Any) -> Any:
        """Base coordination intents (parent authorization already verified)."""
        intent = request.intent
        if intent == "coordinate":
            return await self._adjudicate_coordinate(request)
        if intent == "assign":
            return await self._adjudicate_assign(request)
        if intent == "manage":
            return await self._adjudicate_manage(request)
        if intent == "sync":
            return await self._adjudicate_sync(request)
        if intent == "status":
            return await self._adjudicate_status(request)
        return refusal_outcome(
            "UNKNOWN_INTENT", self.verified_basis("A130", "A284")
        )

    def _learn_basis(self) -> Any:
        return self.verified_basis("A130", "A334", "A485")

    async def _adjudicate_learn_auto_start(self, request: Any) -> Any:
        """Arm the reconcile loop — the only path that enables auto-learning."""
        self._ensure_learner()
        curriculum = self._apply_repair_curriculum()
        self._start_reconcile_loop()
        task = self._reconcile_task
        self._auto_learning_armed = task is not None and not task.done()
        return accepted_outcome(
            {
                "command": "learn.auto-start",
                "auto_learning": (
                    "armed" if self._auto_learning_armed else "arm-failed"
                ),
                "interval_seconds": self._interval_seconds(),
                "curriculum": curriculum,
                "commanded_by": self.parent_sovereign_id,
                "decision": "none",
                "execution": "delegated-to-governed-executor",
            },
            self._learn_basis(),
        )

    async def _adjudicate_learn_auto_stop(self, request: Any) -> Any:
        self._auto_learning_armed = False
        await self._stop_reconcile_loop()
        return accepted_outcome(
            {
                "command": "learn.auto-stop",
                "auto_learning": "disarmed",
                "commanded_by": self.parent_sovereign_id,
                "decision": "none",
                "execution": "delegated-to-governed-executor",
            },
            self._learn_basis(),
        )

    async def _adjudicate_learn_reconcile(self, request: Any) -> Any:
        """Run one bounded reconciliation pass under parent command."""
        self._ingest_fault_manual_catalog()
        receipt = await self.reconcile_once()
        return accepted_outcome(
            {
                "command": "learn.reconcile",
                "trigger": request.payload.get("trigger"),
                "reconciliation": receipt,
                "commanded_by": self.parent_sovereign_id,
                "decision": "none",
                "execution": "delegated-to-governed-executor",
            },
            self._learn_basis(),
        )

    def _adjudicate_learn_outcome(self, request: Any) -> Any:
        """Learn from one parent-pushed verified outcome."""
        self._ensure_learner()
        if self._learner is None:
            return refusal_outcome("LEARNER_UNAVAILABLE", self._learn_basis())
        signature = request.payload.get("signature")
        outcome = request.payload.get("outcome")
        if not isinstance(signature, dict) or not isinstance(outcome, dict):
            return refusal_outcome(
                "MISSING_LEARNING_PAYLOAD", self._learn_basis()
            )
        result = self._record_pushed_outcome(signature, outcome)
        if result is None:
            return refusal_outcome(
                "INVALID_LEARNING_PAYLOAD", self._learn_basis()
            )
        return accepted_outcome(
            {
                "command": "learn.outcome",
                "learning": result,
                "commanded_by": self.parent_sovereign_id,
                "decision": "none",
                "execution": "delegated-to-governed-executor",
            },
            self._learn_basis(),
        )

    def _adjudicate_learn_evidence(self, request: Any) -> Any:
        evidence = request.payload.get("evidence")
        if not isinstance(evidence, dict):
            return refusal_outcome(
                "MISSING_LEARNING_PAYLOAD", self._learn_basis()
            )
        self.sync_learning_evidence(evidence)
        return accepted_outcome(
            {
                "command": "learn.evidence",
                "synced": True,
                "commanded_by": self.parent_sovereign_id,
                "decision": "none",
                "execution": "none",
            },
            self._learn_basis(),
        )

    def _adjudicate_learn_analyze(self, request: Any) -> Any:
        self._ensure_learner()
        if self._learner is None:
            return refusal_outcome("LEARNER_UNAVAILABLE", self._learn_basis())
        return accepted_outcome(
            {
                "command": "learn.analyze",
                "analysis": self._learner.analyze_history(),
                "commanded_by": self.parent_sovereign_id,
                "decision": "none",
                "execution": "none",
            },
            self._learn_basis(),
        )

    def _adjudicate_learn_teach(self, request: Any) -> Any:
        """Store one parent-taught repair recipe (``learn.teach``).

        Taught knowledge is doctrine declared by the codex parent
        (星澄) — ``source="taught"`` with zero outcome counters, bounded
        to the runtime-safe remedy vocabulary so teaching can never arm
        a source mutation.  The accepted recipe is also forwarded to
        the model's governed teaching gate so repair doctrine settles
        into model capability, not only the evidence store.
        """
        self._ensure_learner()
        if self._learner is None:
            return refusal_outcome("LEARNER_UNAVAILABLE", self._learn_basis())
        signature = request.payload.get("signature") or {}
        if not isinstance(signature, dict):
            return refusal_outcome(
                "INVALID_LEARNING_PAYLOAD", self._learn_basis()
            )
        signatures = tuple(
            str(token).strip()
            for token in (
                signature.get("error_class"),
                signature.get("failure_code"),
                signature.get("message_pattern"),
            )
            if str(token or "").strip()
        )
        result = self._learner.teach_recipe(
            name=str(request.payload.get("name") or ""),
            failure_signatures=signatures,
            remedy=str(request.payload.get("remedy") or ""),
            verification=str(request.payload.get("verification") or ""),
            automatic=bool(request.payload.get("automatic", True)),
        )
        if not result.get("taught"):
            return refusal_outcome(
                str(result.get("reason") or "INVALID_TEACH_PAYLOAD"),
                self._learn_basis(),
            )
        recipe = result["recipe"]
        self._emit_repair_teaching_example(recipe)
        return accepted_outcome(
            {
                "command": "learn.teach",
                "recipe": recipe,
                "commanded_by": self.parent_sovereign_id,
                "decision": "none",
                "execution": "none",
            },
            self._learn_basis(),
        )

    def _apply_repair_curriculum(self) -> dict[str, Any]:
        """Apply the codified repair curriculum at arm time.

        The curriculum is 星澄's repair doctrine for the fault classes
        the system actually emits — stored as taught recipes so the
        learning module carries baseline knowledge instead of starting
        cold.  Idempotent: re-arming refreshes the same recipe ids.
        """
        if self._learner is None:
            return {"applied": 0, "reason": "learner-unavailable"}
        from .repair_curriculum import REPAIR_CURRICULUM

        applied: list[str] = []
        for entry in REPAIR_CURRICULUM:
            result = self._learner.teach_recipe(
                name=str(entry.get("name") or ""),
                failure_signatures=tuple(
                    entry.get("failure_signatures") or ()
                ),
                remedy=str(entry.get("remedy") or ""),
                verification=str(entry.get("verification") or ""),
                automatic=bool(entry.get("automatic", True)),
            )
            if result.get("taught"):
                applied.append(str(result["recipe"]["recipe_id"]))
                self._emit_repair_teaching_example(result["recipe"])
        return {"applied": len(applied), "recipe_ids": applied}

    def _emit_repair_teaching_example(self, recipe: dict[str, Any]) -> None:
        """Forward one repair recipe to the model's teaching gate.

        The governed bridge: learning-module doctrine becomes a repair
        teaching example submitted to ``xingcheng_submit_teaching``
        through the shared request layer under
        ``governance/main-system`` — repair experience settles into
        model capability.  Best-effort: a queued submission failure
        never fails the teach itself.
        """
        permission = getattr(self.app, "permission_sovereign", None)
        submit = getattr(permission, "submit_tool_execution_request", None)
        if submit is None:
            return
        signatures = "、".join(
            str(token) for token in (recipe.get("failure_signatures") or [])
        )
        name = str(recipe.get("name") or "")
        remedy = str(recipe.get("remedy") or "")
        verification = str(recipe.get("verification") or "").strip()
        doctrine = (
            "修復一律經受管鏈：分類→決策→權限→調度→獨立驗證→稽核，"
            "不得直接修改來源或略過驗證。"
        )
        # The teaching gate requires semantic grounding: every term in
        # target_text must be covered by input_text + reference_text, so
        # the full doctrine is declared in the reference.
        reference = (
            f"故障特徵：{signatures}。名稱：{name}。"
            f"修復動作：{remedy}。驗證條件：{verification}。{doctrine}"
        )
        payload = {
            "_governed_command": "xingcheng_submit_teaching",
            "training_intent": "repair",
            "input_text": (
                f"系統故障特徵「{signatures}」（{name}）："
                "對應的受管修復路徑與驗證條件是什麼？"
            ),
            "target_text": (
                f"「{signatures}」屬於{name}。受管修復動作：{remedy}。"
                f"驗證條件：{verification}。{doctrine}"
            ),
            "reference_text": reference,
            "recipe_id": str(recipe.get("recipe_id") or ""),
            "recipe_source": str(recipe.get("source") or ""),
            "source_type": "xingcheng-repair-doctrine",
            "received_via": "learn-teach-governed-bridge",
        }
        try:
            import uuid

            submit("xingcheng", f"learn-teach-{uuid.uuid4().hex[:20]}", payload)
        except Exception:
            _logger.info(
                "repair teaching example not queued for %s",
                recipe.get("recipe_id"),
            )

    def _record_pushed_outcome(
        self, signature: dict[str, Any], outcome: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Deserialize parent-pushed payload and record the learning outcome."""
        try:
            from tasks.repair_learning_types import (
                ErrorSignature,
                RepairOutcome,
            )

            sig = ErrorSignature(
                signature_hash=str(signature.get("signature_hash") or ""),
                error_class=str(signature.get("error_class") or ""),
                message_pattern=str(signature.get("message_pattern") or ""),
                failure_code=str(signature.get("failure_code") or ""),
                file_context=str(signature.get("file_context") or ""),
                target_tool_id=str(signature.get("target_tool_id") or ""),
            )
            out = RepairOutcome(
                run_id=str(outcome.get("run_id") or ""),
                signature_hash=str(
                    outcome.get("signature_hash") or sig.signature_hash
                ),
                remedy=str(outcome.get("remedy") or ""),
                ok=bool(outcome.get("ok")),
                detail=(
                    outcome.get("detail")
                    if isinstance(outcome.get("detail"), dict)
                    else {}
                ),
                recorded_at=str(outcome.get("recorded_at") or ""),
            )
        except (TypeError, ValueError):
            return None
        result = self._learner.learn_from_outcome(sig, out)
        recipe = (result or {}).get("recipe")
        if (result or {}).get("promoted") and isinstance(recipe, dict):
            self._emit_repair_teaching_example(recipe)
        return result

    def learn_outcome(self, signature: Any, outcome: Any) -> dict[str, Any]:
        if self._learner is None:
            return {"recorded": False, "reason": "learning-sovereign-not-ready"}
        result = self._learner.learn_from_outcome(signature, outcome)
        recipe = (result or {}).get("recipe")
        if (result or {}).get("promoted") and isinstance(recipe, dict):
            self._emit_repair_teaching_example(recipe)
        return result

    def suggest_remedy(self, signature: Any) -> dict[str, Any]:
        if self._learner is None:
            return {"suggested": False, "reason": "learning-sovereign-not-ready"}
        return self._learner.suggest_remedy(signature)

    # ------------------------------------------------------------------
    # Learning-driven fault-message reconciliation
    # ------------------------------------------------------------------

    def _project_root(self) -> Path:
        raw = getattr(self.app, "project_root", None)
        if raw:
            return Path(raw).resolve()
        # governance/sub-sovereigns/... -> main-system -> GPTBridge
        return Path(__file__).resolve().parents[3]

    def _ensure_learner(self) -> None:
        if self._learner is not None:
            return
        try:
            from tasks.repair_learning import RepairLearner, RepairLearningStore

            root = self._project_root()
            self._store = RepairLearningStore(
                root / "main-system" / "data" / "automatic-repair"
            )
            self._learner = RepairLearner(self._store)
        except Exception:
            self._store = None
            self._learner = None

    @staticmethod
    def _action_expired(action: dict[str, Any]) -> bool:
        raw = str(action.get("expires_at") or "").strip()
        if not raw:
            return False
        try:
            expiry = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return True
        return datetime.now(timezone.utc) > expiry

    def _interval_seconds(self) -> float:
        raw: Any = self._reconcile_interval
        if raw is None:
            raw = os.environ.get(RECONCILE_INTERVAL_ENV, "")
        try:
            value = (
                float(raw)
                if str(raw).strip()
                else DEFAULT_RECONCILE_INTERVAL_SECONDS
            )
        except (TypeError, ValueError):
            value = DEFAULT_RECONCILE_INTERVAL_SECONDS
        return max(0.0, float(value))

    def _start_reconcile_loop(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._reconcile_task is not None and not self._reconcile_task.done():
            return
        self._reconcile_task = loop.create_task(self._reconcile_loop())

    async def _stop_reconcile_loop(self) -> None:
        task = self._reconcile_task
        self._reconcile_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def reconcile_once(self) -> dict[str, Any]:
        """Run one reconciliation pass and record its receipt."""
        receipt = await asyncio.to_thread(self.reconcile_pending_fault_messages)
        self._last_reconciliation = {
            "at": self._iso_now(),
            "ok": bool(receipt.get("ok")),
            "removed": len(receipt.get("removed") or []),
            "learned": len(receipt.get("learned") or []),
            "remaining": receipt.get("remaining"),
        }
        if self._last_reconciliation["removed"]:
            _logger.info(
                "learning reconciliation removed %s pending message(s)",
                self._last_reconciliation["removed"],
            )
        return receipt

    async def _wait_for_cycle(self) -> None:
        """Wait one interval, waking early when the fault surface changes."""
        event = None
        try:
            from core_system.auto_action_policy import fault_change_event

            event = fault_change_event()
        except Exception:
            event = None
        remaining = self._interval_seconds()
        while True:
            if event is not None and event.is_set():
                event.clear()
                return
            if remaining <= 0:
                await asyncio.sleep(0.01)
                return
            nap = min(remaining, RECONCILE_EVENT_POLL_SECONDS)
            await asyncio.sleep(nap)
            remaining -= nap
            if remaining <= 0:
                return

    async def _reconcile_loop(self) -> None:
        """Boot pass + periodic passes; fault-change events wake it early."""
        while True:
            try:
                if self._started:
                    await self.reconcile_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # automation must never die
                _logger.warning("learning reconciliation pass failed: %s", error)
            await self._wait_for_cycle()


__all__ = ["LearningEvidenceSyncSubSovereign"]
