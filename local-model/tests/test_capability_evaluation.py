from __future__ import annotations

import asyncio
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "local-model" / "src" / "backend" / "services"))

from local_ai.application.capability_evaluation import evaluate_star_capabilities
from local_ai.application.service import LocalAiService


def test_held_out_capability_evaluation_passes_without_training_writeback() -> None:
    evaluation = evaluate_star_capabilities()

    assert evaluation["ok"] is True
    assert evaluation["held_out"] is True
    assert evaluation["training_writeback"] is False
    assert evaluation["passed_count"] == evaluation["case_count"] == 13
    assert set(evaluation["categories"]) == {
        "coding",
        "language",
        "reading",
        "training",
        "understanding",
    }


def test_status_separates_capability_evaluation_from_governance_tests(
    tmp_path: Path,
) -> None:
    service = LocalAiService(tmp_path)
    _, status = asyncio.run(service.handle("local_ai_status", {}))

    capability = status["capability_evaluation"]
    assert capability["schema"] == "star-capability-evaluation/v1"
    assert capability["ok"] is True
    assert status["upgrade_optimization"]["checks"][
        "held_out_capability_evaluation_passed"
    ] is True
