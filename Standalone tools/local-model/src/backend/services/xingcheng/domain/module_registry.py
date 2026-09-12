from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class StarModule:
    module_id: str
    name: str
    layer: str
    responsibility: str
    required_modules: tuple[str, ...] = ()

    def status(self) -> dict[str, Any]:
        return {**asdict(self), "enabled": True, "isolation": "role-bound"}


class StarModuleRegistry:
    """Declares the real modules used by Star's governed inference pipeline."""

    UNDERSTANDING = StarModule(
        "language-understanding",
        "語意理解",
        "cognition",
        "tokenization, intent classification and entity extraction",
    )
    CONTEXT = StarModule(
        "context-retrieval",
        "記憶檢索",
        "memory",
        "retrieve reviewed local memory and database evidence",
        (UNDERSTANDING.module_id,),
    )
    MARKET = StarModule(
        "market-research",
        "市場資料搜尋",
        "tool",
        "retrieve source-attributed public market observations",
        (UNDERSTANDING.module_id,),
    )
    INVESTMENT = StarModule(
        "investment-analysis",
        "投資分析",
        "specialist",
        "execute deterministic portfolio and risk models",
        (CONTEXT.module_id,),
    )
    MATHEMATICAL = StarModule(
        "mathematical-reasoning",
        "數理推理",
        "specialist",
        "execute reproducible calculations and statistics",
        (UNDERSTANDING.module_id,),
    )
    CODING = StarModule(
        "program-synthesis",
        "程式碼編成",
        "specialist",
        "generate AST-validated code and self-upgrade proposals",
        (UNDERSTANDING.module_id, CONTEXT.module_id),
    )
    READING = StarModule(
        "document-reading",
        "文件閱讀理解",
        "specialist",
        "chunk supplied documents, extract structure and answer with source offsets",
        (UNDERSTANDING.module_id, CONTEXT.module_id),
    )
    GENERATION = StarModule(
        "response-generation",
        "生成式回覆",
        "generation",
        "decode a grounded response from learned next-token probabilities",
        (UNDERSTANDING.module_id, CONTEXT.module_id),
    )
    QUALITY = StarModule(
        "quality-governance",
        "品質治理",
        "governance",
        "verify factual preservation, semantic coverage and output bounds",
        (GENERATION.module_id,),
    )
    TRAINING = StarModule(
        "self-training",
        "受控自我訓練",
        "learning",
        "persist Star-verified self-distilled or GPT-coached examples and rebuild role-isolated local weights",
        (QUALITY.module_id,),
    )
    MAINTENANCE = StarModule(
        "model-maintenance",
        "模型自我維護",
        "maintenance",
        "audit training data, optimize isolated databases and rebuild local weights",
        (QUALITY.module_id, TRAINING.module_id),
    )

    MODULES = (
        UNDERSTANDING,
        CONTEXT,
        MARKET,
        INVESTMENT,
        MATHEMATICAL,
        CODING,
        READING,
        GENERATION,
        QUALITY,
        TRAINING,
        MAINTENANCE,
    )

    def catalog(self) -> list[dict[str, Any]]:
        return [module.status() for module in self.MODULES]

    @classmethod
    def _selected_modules(cls, intents: Iterable[str]) -> list[StarModule]:
        normalized = {str(intent).strip() for intent in intents}
        selected = [cls.UNDERSTANDING, cls.CONTEXT]
        if normalized & {"search", "distribution", "quote"}:
            selected.append(cls.MARKET)
        if normalized & {"risk", "analysis", "distribution", "quote"}:
            selected.append(cls.INVESTMENT)
        if normalized & {"calculation", "reasoning", "statistics", "data_organization"}:
            selected.append(cls.MATHEMATICAL)
        if normalized & {"coding", "self_upgrade"}:
            selected.append(cls.CODING)
        if "reading" in normalized:
            selected.append(cls.READING)
        selected.extend((cls.GENERATION, cls.QUALITY, cls.TRAINING))
        if normalized & {"status", "self_upgrade"}:
            selected.append(cls.MAINTENANCE)
        return list(dict.fromkeys(selected))

    def plan(self, intents: Iterable[str], *, coordinator_model: str) -> dict[str, Any]:
        selected = self._selected_modules(intents)
        active_ids = {module.module_id for module in selected}
        return {
            "mode": "automatic-composable-modules",
            "coordinator_model": coordinator_model,
            "module_count": len(selected),
            "modules": [
                {
                    **module.status(),
                    "sequence": index,
                    "active_dependencies": [
                        dependency
                        for dependency in module.required_modules
                        if dependency in active_ids
                    ],
                    "status": "planned",
                }
                for index, module in enumerate(selected, start=1)
            ],
        }

    def execution_report(
        self,
        intents: Iterable[str],
        *,
        coordinator_model: str,
        output: dict[str, Any],
    ) -> dict[str, Any]:
        report = self.plan(intents, coordinator_model=coordinator_model)
        self_training = output.get("self_training")
        for module in report["modules"]:
            module_id = module["module_id"]
            status = "completed"
            if module_id == self.MARKET.module_id and not isinstance(
                output.get("market_research"), dict
            ):
                status = "input-required"
            elif module_id == self.INVESTMENT.module_id and not isinstance(
                output.get("analysis"), dict
            ):
                status = "input-required"
            elif module_id == self.MATHEMATICAL.module_id:
                mathematical = output.get("mathematical_result")
                status = "completed" if isinstance(mathematical, dict) else "input-required"
            elif module_id == self.CODING.module_id:
                coding = output.get("coding_result")
                status = (
                    "completed"
                    if isinstance(coding, dict) and coding.get("ok") is True
                    else "input-required"
                )
            elif module_id == self.READING.module_id:
                reading = output.get("reading_result")
                status = (
                    "completed"
                    if isinstance(reading, dict) and reading.get("ok") is True
                    else "input-required"
                )
            elif module_id == self.GENERATION.module_id and not isinstance(
                output.get("generation"), dict
            ):
                status = "failed"
            elif module_id == self.QUALITY.module_id:
                candidate = output.get("generation")
                status = (
                    "completed"
                    if isinstance(candidate, dict) and candidate.get("facts_preserved") is True
                    else "failed"
                )
            elif module_id == self.TRAINING.module_id:
                status = (
                    "completed"
                    if isinstance(self_training, dict)
                    and self_training.get("accepted") is True
                    else "quality-gate-rejected"
                )
            elif module_id == self.MAINTENANCE.module_id:
                coding = output.get("coding_result")
                proposal = coding.get("upgrade_proposal") if isinstance(coding, dict) else None
                status = (
                    "upgrade-proposal-ready"
                    if isinstance(proposal, dict) and proposal.get("proposal_ready") is True
                    else "scheduled-idle-maintenance"
                )
            module["status"] = status
        report["completed_count"] = sum(
            module["status"] == "completed" for module in report["modules"]
        )
        return report


__all__ = ["StarModule", "StarModuleRegistry"]
