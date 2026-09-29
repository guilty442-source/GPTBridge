"""Golden Git baseline manifest — builder, integrity and freeze verdict.

Wave-F / codex articles 522-541.  ``gptbridge_git_baseline_v1.json`` is the
one machine-readable architectural contract of the Git v1 plane:

    architecture_version / governance_version / public_api_version
    Git executable + version, config digest, policy digest, hook digests
    schema versions, complete component list, public API list,
    state models, lock hierarchy, invariant list,
    test suite (gate) results, baseline commit

Integrity reuses the Wave1-A mechanism — ``governance_manifest.compute_digest``
over canonical JSON with the ``integrity`` field excluded — and adds the
``.sha256`` sidecar.  The main ``git_governance_manifest.json`` binds the
baseline digest in its ``baseline`` section (rewritten through
``governance_manifest.write_manifest`` so its own digest stays canonical).
That binding is excluded from the manifest's own digest by construction
(``governance_manifest.canonical_payload`` excludes ``integrity`` and
``baseline``), so the baseline's recorded ``governance_manifest.digest``
stays valid after binding — binding is idempotent with respect to the
manifest identity and cannot form a digest cycle.

Every start-up only needs to verify *baseline compatibility* (article 523):
the baseline describes the architecture and governance contract, not the
current HEAD; runtime state is never required to equal the baseline digest.

The freeze verdict is derived strictly from the sixteen conditions of
codex article 536.  A condition without measured evidence is UNVERIFIED —
never PASS.

Module layout (A185 source-size split):

    specs.py          identity constants + article-489/493/504-536 tables
    introspection.py  module inventory / evidence / component map / doc scan
    freeze.py         article-536 freeze-condition table + overall verdict
    payload.py        payload build / atomic write / verify / manifest bind
"""
from __future__ import annotations

from .specs import *  # noqa: F401,F403
from .introspection import *  # noqa: F401,F403
from .freeze import *  # noqa: F401,F403
from .payload import *  # noqa: F401,F403

__all__ = [
    "ARCHITECTURE_VERSION",
    "ARTICLE_493_API",
    "ARTICLE_522_FIELDS",
    "BASELINE_DIGEST_FILENAME",
    "BASELINE_DIR",
    "BASELINE_FILENAME",
    "BASELINE_ID",
    "BASELINE_VERSION",
    "DEFAULT_EVIDENCE_FILENAME",
    "EXPECTED_BRANCH_CLASSES",
    "EXPECTED_LOCKS",
    "EXPECTED_STATES",
    "FINAL_INVARIANTS",
    "FREEZE_CONDITIONS",
    "FROZEN_API",
    "MANDATED_RUNTIME_COMPONENTS",
    "PROJECT_ROOT",
    "PUBLIC_API_VERSION",
    "bind_to_governance_manifest",
    "build_baseline_payload",
    "build_freeze_table",
    "overall_verdict",
    "verify_baseline",
    "write_baseline",
]
