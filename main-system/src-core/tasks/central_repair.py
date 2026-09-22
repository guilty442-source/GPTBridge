from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .repair_learning import (
    LEARN_PROMOTION_MIN_SUCCESS_RATE,
    LEARN_PROMOTION_THRESHOLD,
    RepairLearner,
    RepairLearningStore,
)
from .repair_planning import (
    CENTRAL_REPAIR_VERSION,
    REPAIR_RECIPES,
    SOURCE_SELF_REPAIR_FAILURES,
    RepairPlan,
    plan_repair,
)
from .repair_inspection import (
    DatabaseRecoveryInspector,
    RepairRunStore,
    database_integrity,
)
from .central_repair_learning import CentralRepairLearningMixin
from .central_repair_operations import CentralRepairOperationsMixin


# Runtime-safe action tokens a learned recipe may promote.  Learned
# knowledge stays advisory and bounded to non-mutating stability
# recovery; ``repair-main-system-source`` is deliberately absent so a
# learned recipe can never self-authorize a source mutation.
_LEARNED_RUNTIME_ACTIONS: frozenset[str] = frozenset(
    {"inspect-owned-databases", "rebuild-tool-executable"}
)


class CentralRepairService(CentralRepairLearningMixin, CentralRepairOperationsMixin):
    """Central automatic-repair service, integrated into main-system.

    Supports self-upgrading repair knowledge:
    - Every repair outcome is recorded as an error signature + remedy pair.
    - The RepairLearner analyzes history and auto-promotes recurring
      error→remedy patterns into learned recipes.
    - Learned recipes are merged into the knowledge base alongside the
      static REPAIR_RECIPES, making them available for future dispatch.
    """

    VERSION = CENTRAL_REPAIR_VERSION
    MANAGEMENT_OWNER = "星澄"

    def __init__(self, project_root: Path, repair_data_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.repair_data_root = repair_data_root.resolve()
        self.store = RepairRunStore(self.repair_data_root)
        self.learning_store = RepairLearningStore(self.repair_data_root)
        self.learner = RepairLearner(self.learning_store)
        self._recipe_file_cache: tuple[int, int, list[dict[str, Any]]] | None = None

    def _knowledge_file(self) -> Path:
        return self.repair_data_root / "knowledge" / "recipes.json"

    def known_recipes(self) -> list[dict[str, Any]]:
        knowledge_file = self._knowledge_file()
        recorded: list[dict[str, Any]] = []
        try:
            stat = knowledge_file.stat()
            cached = self._recipe_file_cache
            if cached is not None and cached[:2] == (stat.st_mtime_ns, stat.st_size):
                recorded = [dict(recipe) for recipe in cached[2]]
            else:
                loaded = json.loads(knowledge_file.read_text(encoding="utf-8"))
                recorded = loaded if isinstance(loaded, list) else []
                recorded = [
                    dict(recipe) for recipe in recorded if isinstance(recipe, dict)
                ]
                self._recipe_file_cache = (stat.st_mtime_ns, stat.st_size, recorded)
        except (OSError, UnicodeError, json.JSONDecodeError):
            recorded = []
        raw = json.dumps(recorded, ensure_ascii=False, indent=2)
        merged: dict[str, dict[str, Any]] = {
            recipe["recipe_id"]: dict(recipe) for recipe in REPAIR_RECIPES
        }
        for recipe in recorded:
            if isinstance(recipe, dict) and recipe.get("recipe_id"):
                merged[str(recipe["recipe_id"])] = {**merged.get(str(recipe["recipe_id"]), {}), **recipe}
        # Merge learned recipes from the learning store.  A promoted
        # recipe's ``remedy`` records the executed action tokens, so it
        # maps back to structured plan actions — bounded to the
        # runtime-safe set: learned knowledge may never promote a source
        # mutation (``repair-main-system-source``) into an automatic
        # recipe.  Applicability is verified only while the recipe keeps
        # meeting the promotion thresholds AND its most recent outcomes
        # for the same signature have not turned negative; a remedy that
        # started failing is suppressed back to evidence collection.
        for learned in self.learning_store.get_learned_recipes():
            rid = str(learned.get("recipe_id") or "")
            if not rid:
                continue
            is_taught = str(learned.get("source") or "") == "taught"
            entry = {**merged.get(rid, {}), **learned}
            remedy_tokens = str(learned.get("remedy") or "")
            actions = [
                token.strip()
                for token in remedy_tokens.split(",")
                if token.strip() in _LEARNED_RUNTIME_ACTIONS
            ]
            entry["actions"] = actions or ["inspect-owned-databases"]
            if is_taught:
                # Taught doctrine carries a declared verification
                # statement — the same contract static REPAIR_RECIPES
                # use — instead of outcome-earned proof.  Its zero
                # occurrence count keeps it sorted below earned recipes.
                declared = str(learned.get("verification") or "").strip()
                entry["verified_applicability"] = bool(declared)
                entry["verification"] = declared
                merged[rid] = entry
                continue
            meets_threshold = (
                float(learned.get("success_rate") or 0.0)
                >= LEARN_PROMOTION_MIN_SUCCESS_RATE
                and int(learned.get("occurrence_count") or 0)
                >= LEARN_PROMOTION_THRESHOLD
            )
            suppressed = False
            if meets_threshold and rid.startswith("learned-"):
                try:
                    recent = self.learning_store.get_outcomes_for_signature(
                        rid[len("learned-"):], limit=3
                    )
                except Exception:
                    recent = []
                suppressed = len(recent) >= 2 and all(
                    not o.get("ok") for o in recent
                )
            entry["verified_applicability"] = bool(
                meets_threshold and not suppressed
            )
            entry["verification"] = (
                "promoted-by-verified-repair-outcomes"
                if entry["verified_applicability"]
                else ""
            )
            if suppressed:
                entry["automatic"] = False
                entry["suppressed"] = True
            merged[rid] = entry
        recipes = list(merged.values())
        payload = json.dumps(recipes, ensure_ascii=False, indent=2)
        if raw.strip() != payload.strip():
            try:
                knowledge_file.parent.mkdir(parents=True, exist_ok=True)
                knowledge_file.write_text(payload, encoding="utf-8")
                stat = knowledge_file.stat()
                self._recipe_file_cache = (stat.st_mtime_ns, stat.st_size, recipes)
            except OSError:
                pass
        return recipes

    def record_recipe(self, recipe: dict[str, Any]) -> dict[str, Any]:
        knowledge_file = self._knowledge_file()
        recipes = self.known_recipes()
        recipe_id = str(recipe.get("recipe_id") or "")
        if not recipe_id:
            return {"ok": False, "error": "recipe_id required"}
        for index, existing in enumerate(recipes):
            if existing.get("recipe_id") == recipe_id:
                recipes[index] = {**existing, **recipe}
                break
        else:
            recipes.append(recipe)
        try:
            knowledge_file.parent.mkdir(parents=True, exist_ok=True)
            knowledge_file.write_text(
                json.dumps(recipes, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as error:
            return {"ok": False, "error": error.__class__.__name__}
        return {"ok": True, "recipe_id": recipe_id, "recipes": recipes}

    def knowledge_summary(self) -> dict[str, Any]:
        recipes = self.known_recipes()
        analysis = self.learner.analyze_history()
        return {
            "recipe_count": len(recipes),
            "automatic_recipes": sum(
                1 for recipe in recipes if recipe.get("automatic") is True
            ),
            "learned_recipe_count": analysis.get("learned_recipes", 0),
            "total_error_types": analysis.get("total_error_types", 0),
            "recurring_errors": analysis.get("recurring_errors", 0),
            "recipes": [
                {
                    "recipe_id": recipe.get("recipe_id"),
                    "name": recipe.get("name"),
                    "automatic": recipe.get("automatic"),
                    "owner": recipe.get("owner"),
                    "source": recipe.get("source", "static"),
                }
                for recipe in recipes
            ],
        }

    def status(self) -> dict[str, Any]:
        return {
            "version": self.VERSION,
            "management_owner": self.MANAGEMENT_OWNER,
            "execution_owner": "governed-executor",
            "capability_id": "central-automatic-repair",
            "module_id": "xingcheng-auto-repair-module",
            "enabled": True,
            "delegation": "governed-executor-only",
            "source_self_repair": True,
            "self_upgrading": True,
            "learning_enabled": True,
            "knowledge_base": self.knowledge_summary(),
        }

    def self_repair_main_system_sources(self) -> dict[str, Any]:
        from .source_repair import self_repair_sources

        report = self_repair_sources(self.project_root)
        # Learn from each repaired file and each error.
        for problem in report.get("problems", []):
            self._learn_from_problem(problem, report)
        for repaired in report.get("repaired_files", []):
            self._learn_from_repair(repaired, report)
        return {"repair_service": self.VERSION, **report}


__all__ = [
    "CentralRepairService",
    "DatabaseRecoveryInspector",
    "REPAIR_RECIPES",
    "RepairPlan",
    "RepairRunStore",
    "SOURCE_SELF_REPAIR_FAILURES",
    "database_integrity",
    "plan_repair",
]
