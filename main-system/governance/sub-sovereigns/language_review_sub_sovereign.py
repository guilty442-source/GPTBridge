"""Language Review Sub-Sovereign — ABOLISHED (A334 sovereign_hierarchy_registry).

法典依據:
- A308: language-review-sovereign retired → language-review-sub-sovereign
  under permission-sovereign (historical).
- A334 sovereign_hierarchy_registry: language-review-sub-sovereign is
  ABOLISHED; domain = historical-capability-transferred-to-星澄.
  The programming-language-review capability has been transferred to
  星澄 (the independent-privileged-institution global reviewer).

This module is retained as a HISTORICAL ARTIFACT only.  It must NOT be
imported by active paths, materialized by the sovereign-stack executor,
or started by any parent.  The class definition below is kept for
lineage traceability; the ``__init__`` raises ``RuntimeError`` so any
accidental reactivation fails closed.
"""

from __future__ import annotations

from typing import Any


class LanguageReviewSubSovereign:
    """ABOLISHED sub-sovereign — do not materialize (A334).

    Historical identity: language-review-sub-sovereign (child of
    permission-sovereign).  Capability transferred to 星澄.
    """

    sovereign_id = "language-review-sub-sovereign"
    parent_sovereign_id = "permission-sovereign"
    STATUS = "abolished"
    SUCCESSOR = "星澄"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError(
            "language-review-sub-sovereign is ABOLISHED per A334 "
            "sovereign_hierarchy_registry; capability transferred to 星澄. "
            "Do not materialize this sub-sovereign."
        )


__all__ = ["LanguageReviewSubSovereign"]
