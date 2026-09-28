"""Session chokepoint for the Python-retirement transition (B171).

A bare ``pytest`` invocation is a Python runner outside the
``LegacyPythonVerificationAdapter`` — forbidden
(FORBID:Python-runner-outside-adapter).  The adapter sets
``GPTBRIDGE_LEGACY_VERIFICATION_ADAPTER=1`` in the child environment
before spawning its bounded, inventory-gated scope; collection exits
immediately when the marker is absent.  This file contains no tests —
it is the fail-closed enforcement seam for the transition law.
"""

from __future__ import annotations

import os

import pytest

_ADAPTER_ENV = "GPTBRIDGE_LEGACY_VERIFICATION_ADAPTER"


def pytest_sessionstart(session: pytest.Session) -> None:
    if os.environ.get(_ADAPTER_ENV) != "1":
        pytest.exit(
            "bare pytest is forbidden "
            "(FORBID:Python-runner-outside-adapter); run through "
            "governance_rule.execution.legacy_python_verification_adapter",
            returncode=2,
        )
