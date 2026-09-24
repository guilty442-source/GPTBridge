"""Xingcheng Learning Capability — 星澄的學習能力（單一個體，無決策、無執行）。

法典依據 (A485 — learning-transfer-to-xingcheng; A592/A604 — layer
eliminated): 星澄 is the single-entity native-model sovereign — learning
is part of the entity itself, expressed as this mixin on
``XingchengSovereign``.  The retired codex identity
``learning-evidence-sync-sub-sovereign`` survives only as lineage (its
declaration still supplies the duty list below); it is never used as an
active route, registry key or parent edge.

- owner: 星澄 (xingcheng_sovereign) — all actions stamp this identity
- duties (lineage): manage learning-evidence, assign bounded learning
  work, coordinate evidence normalization/evaluation/retention, collect
  outcome proof
- boundary: no decision/execution power; cannot alter models, code,
  Codex, permissions, routing or active behavior

The implementation was merged from the retired
``core_system.learning_system_sovereign.LearningSystemSovereign`` so the
active path keeps its persistent system-error learning behavior (fault
learning, repair outcomes, evidence feedback).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from governance_rule.execution.codex_official import official_self_declaration
from core_system.codex_decision import accepted_outcome, refusal_outcome
from .learning_reconciliation import LearningReconciliationMixin


_logger = logging.getLogger("gptbridge.sovereign.xingcheng.learning")

# Lineage read only: the retired codex row supplies the descriptive duty
# list; it is not an active identity.
_DECLARATION = official_self_declaration("learning-evidence-sync-sub-sovereign")
if _DECLARATION is None:
    raise RuntimeError("learning evidence sync declaration not found in Governance Codex")

# Bounded learning commands the owning 星澄 sovereign may issue (A485).
# The engine never self-arms its learning loop — arming, driving and
# disarming are all commanded in-process by the owner.
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


class XingchengLearningCapabilityMixin(LearningReconciliationMixin):
    """星澄's learning capability — part of the single native-model entity.

    A485/A604: 星澄 is one indivisible individual (the native model's
    own-domain sovereign).  Learning is a set of methods and state on the
    sovereign itself — there is no separate engine/module object, no
    second codex identity, and no external request surface.
    """

    _store: Any | None
    _learner: Any | None
    _sync_state: dict[str, Any]
    _reconcile_task: asyncio.Task[Any] | None
    _reconcile_interval: float | None
    _last_reconciliation: dict[str, Any]
    _auto_learning_armed: bool
    _learning_active: bool
    _fault_manual_catalog: tuple[dict[str, Any], ...]
    _fault_manual_catalog_hash: str

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._store = None
        self._learner = None
        self._sync_state = {}
        self._reconcile_task = None
        self._reconcile_interval = None
        self._last_reconciliation = {}
        self._auto_learning_armed = False
        self._learning_active = False
        self._fault_manual_catalog = ()
        self._fault_manual_catalog_hash = ""

    def _ingest_fault_manual_catalog(self) -> None:
        """Load the canonical fault manuals into the learning module.

        Fault identities remain owned by permission-sovereign.  This method
        transfers only maintenance-manual learning and management into the
        星澄 learning module and never mutates the canonical Codex database.
        """
        from governance_rule.execution.codex_repository import codex_readonly_connection

        with codex_readonly_connection() as connection:
            columns = [row[1] for row in connection.execute("PRAGMA table_info(maintenance_manual_directory)")]
            records = tuple(
                dict(zip(columns, row))
                for row in connection.execute(  # sql-ok: catalog hash covers the full row
                    "SELECT * FROM maintenance_manual_directory "
                    "WHERE retired_version IS NULL ORDER BY manual_code"
                )
            )
        payload = json.dumps(records, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self._fault_manual_catalog = records
        self._fault_manual_catalog_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()

    async def learning_activate(self) -> dict[str, Any]:
        """Activate the learning capability with the sovereign's lifecycle."""
        from tasks.repair_learning import RepairLearner, RepairLearningStore

        root = Path(getattr(self.app, "project_root", ".")).resolve()
        self._store = RepairLearningStore(root / "main-system" / "data" / "automatic-repair")
        self._learner = RepairLearner(self._store)
        self._ingest_fault_manual_catalog()
        self._learning_active = True
        # E173: activation returns a light receipt — analyze_history()
        # runs on demand in learning_capability_status(), not on the
        # startup critical path.  The reconcile loop is NOT self-armed:
        # only an owner-issued ``learn.auto-start`` command may arm it
        # (A485 commanded learning).
        return {
            "ok": True,
            "capability": "xingcheng-learning",
            "active": self._learning_active,
            "duties": list(_DECLARATION.duties),
            "execution": "governed-executor-only",
            "persistence": "repair-learning-sqlite",
            "reconciliation": "commanded-by-owner",
            "fault_manuals": len(self._fault_manual_catalog),
        }

    async def learning_deactivate(self) -> None:
        self._auto_learning_armed = False
        await self._stop_reconcile_loop()
        self._learning_active = False

    def sync_learning_evidence(self, evidence: dict[str, Any]) -> None:
        """同步學習證據。"""
        self._sync_state = {
            "learning_evidence": evidence,
            "synced_at": self._iso_now(),
        }

    def learning_capability_status(self) -> dict[str, Any]:
        analysis = self._learner.analyze_history() if self._learner is not None else {}
        return {
            "capability": "xingcheng-learning",
            "active": self._learning_active,
            "owner": "星澄",
            "duties": list(_DECLARATION.duties),
            "execution": "governed-executor-only",
            "persistence": "repair-learning-sqlite",
            "analysis": analysis,
            "reconciliation": dict(self._last_reconciliation),
            "auto_learning": (
                "armed" if self._auto_learning_armed else "disarmed"
            ),
            "commanded_by": "星澄",
            "reconcile_loop": bool(
                self._reconcile_task is not None and not self._reconcile_task.done()
            ),
            "sync_state": self._sync_state,
            "fault_manual_catalog": {
                "owner": "星澄",
                "source": "governance-codex:maintenance_manual_directory",
                "mode": "read-only-learning-ingestion",
                "count": len(self._fault_manual_catalog),
                "catalog_hash": self._fault_manual_catalog_hash,
                "fault_directory_owner": "permission-sovereign",
            },
        }

    # ------------------------------------------------------------------
    # Owner-commanded learning dispatch (A485)
    # ------------------------------------------------------------------

    async def _learn_dispatch(self, request: Any) -> Any:
        """Dispatch one bounded learn.* command on this entity (A485)."""
        intent = request.intent
        if intent not in _LEARNING_INTENTS:
            return refusal_outcome(
                "UNKNOWN_INTENT", self.verified_basis("A485")
            )
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
        return refusal_outcome(
            "UNKNOWN_INTENT", self.verified_basis("A485")
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
                "commanded_by": "星澄",
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
                "commanded_by": "星澄",
                "decision": "none",
                "execution": "delegated-to-governed-executor",
            },
            self._learn_basis(),
        )

    async def _adjudicate_learn_reconcile(self, request: Any) -> Any:
        """Run one bounded reconciliation pass under owner command."""
        self._ingest_fault_manual_catalog()
        receipt = await self.reconcile_once()
        return accepted_outcome(
            {
                "command": "learn.reconcile",
                "trigger": request.payload.get("trigger"),
                "reconciliation": receipt,
                "commanded_by": "星澄",
                "decision": "none",
                "execution": "delegated-to-governed-executor",
            },
            self._learn_basis(),
        )

    def _adjudicate_learn_outcome(self, request: Any) -> Any:
        """Learn from one owner-pushed verified outcome."""
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
                "commanded_by": "星澄",
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
                "commanded_by": "星澄",
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
                "commanded_by": "星澄",
                "decision": "none",
                "execution": "none",
            },
            self._learn_basis(),
        )

    def _adjudicate_learn_teach(self, request: Any) -> Any:
        """Store one owner-taught repair recipe (``learn.teach``).

        Taught knowledge is doctrine declared by the owning sovereign
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
                "commanded_by": "星澄",
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

        The governed bridge: learning-engine doctrine becomes a repair
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
        """Deserialize owner-pushed payload and record the learning outcome."""
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
            return {"recorded": False, "reason": "learning-not-active"}
        result = self._learner.learn_from_outcome(signature, outcome)
        recipe = (result or {}).get("recipe")
        if (result or {}).get("promoted") and isinstance(recipe, dict):
            self._emit_repair_teaching_example(recipe)
        return result

    def suggest_remedy(self, signature: Any) -> dict[str, Any]:
        if self._learner is None:
            return {"suggested": False, "reason": "learning-not-active"}
        return self._learner.suggest_remedy(signature)

    # ------------------------------------------------------------------
    # Learning-driven fault-message reconciliation
    # ------------------------------------------------------------------

    def _project_root(self) -> Path:
        raw = getattr(self.app, "project_root", None)
        if raw:
            return Path(raw).resolve()
        # governance/sovereigns/xingcheng/... -> main-system -> GPTBridge
        return Path(__file__).resolve().parents[4]

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
                if self._learning_active:
                    await self.reconcile_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # automation must never die
                _logger.warning("learning reconciliation pass failed: %s", error)
            await self._wait_for_cycle()


__all__ = ["XingchengLearningCapabilityMixin"]
