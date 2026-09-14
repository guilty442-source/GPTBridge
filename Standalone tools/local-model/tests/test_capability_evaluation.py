"""Split from consolidated test_xingcheng.py (local-model/tests/test_capability_evaluation.py)."""
from __future__ import annotations

import _xingcheng_test_support as _support  # noqa: F401
from _xingcheng_test_support import ROOT

import asyncio
import sys
from pathlib import Path
from xingcheng.application.capability_evaluation import evaluate_star_capabilities
from xingcheng.application.service import LocalAiService

import asyncio
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]

from xingcheng.application.capability_evaluation import evaluate_star_capabilities
from xingcheng.application.service import LocalAiService


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
    _, status = asyncio.run(service.handle("xingcheng_status", {}))

    capability = status["capability_evaluation"]
    assert capability["schema"] == "star-capability-evaluation/v1"
    assert capability["ok"] is True
    assert status["upgrade_optimization"]["checks"][
        "held_out_capability_evaluation_passed"
    ] is True



########################################################################
