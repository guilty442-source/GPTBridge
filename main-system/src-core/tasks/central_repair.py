from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from .repair_learning import (
    ErrorSignature,
    RepairLearner,
    RepairLearningStore,
    RepairOutcome,
    _normalize_error_signature,
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
    _inside,
    _iso_now,
    database_integrity,
)
from .central_repair_learning import CentralRepairLearningMixin
from .central_repair_operations import CentralRepairOperationsMixin


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

    def _knowledge_file(self) -> Path:
        return self.repair_data_root / "knowledge" / "recipes.json"

    def known_recipes(self) -> list[dict[str, Any]]:
        knowledge_file = self._knowledge_file()
        raw = ""
        recorded: list[dict[str, Any]] = []
        try:
            raw = knowledge_file.read_text(encoding="utf-8")
            recorded = json.loads(raw)
        except (OSError, UnicodeError, json.JSONDecodeError):
            recorded = []
        if not isinstance(recorded, list):
            recorded = []
        merged: dict[str, dict[str, Any]] = {
            recipe["recipe_id"]: dict(recipe) for recipe in REPAIR_RECIPES
        }
        for recipe in recorded:
            if isinstance(recipe, dict) and recipe.get("recipe_id"):
                merged[str(recipe["recipe_id"])] = {**merged.get(str(recipe["recipe_id"]), {}), **recipe}
        # Merge learned recipes from the learning store.
        for learned in self.learning_store.get_learned_recipes():
            rid = str(learned.get("recipe_id") or "")
            if rid:
                merged[rid] = {**merged.get(rid, {}), **learned, "source": "learned"}
        recipes = list(merged.values())
        payload = json.dumps(recipes, ensure_ascii=False, indent=2)
        if raw.strip() != payload.strip():
            try:
                knowledge_file.parent.mkdir(parents=True, exist_ok=True)
                knowledge_file.write_text(payload, encoding="utf-8")
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
            "capability_id": "xingcheng-system-repair",
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
