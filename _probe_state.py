import json
from pathlib import Path

src = Path(
    r"E:\GPTBridge\governance_rule\execution\audit\convergence"
    r"\codex-amendment-request-cfamily-primary-stack-one-format-20260925.json"
)
dst = src.with_name(
    "codex-amendment-request-cfamily-primary-stack-one-format-20260925-r4.json"
)

d = json.loads(src.read_text(encoding="utf-8-sig"))
d["request_id"] = "cfamily-primary-stack-one-format-20260925-r4"
d["predecessor"] = {
    "codex_version": "2026-09-25T08:10:00Z",
    "version_identity": "E2:2026-09-23T03:13:43Z",
    "version_epoch": 2,
    "history_head": (
        "bedd83c81bcc3dc3d2171bac92b6ace4b9acfa8b65e56d5ab7e6e2ac94717590"
    ),
    "revision_sequence": 75,
}
d["origin"] = (
    "human-governor directive 2026-09-25: unify file formats "
    "(one language one format), optimize for high performance, high "
    "execution speed, low consumption and low resource footprint, reduce "
    "Python dependency, C/C++/C# primary on .NET 10; r4 restamp after "
    "main-system-csharp14-migration-20260925 execution and r3 "
    "REQUEST_HASH_MISMATCH rejection"
)
d.setdefault("verification", {})["requested_at"] = "2026-09-25T08:50:00Z"

dst.write_text(
    json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
print("wrote", dst, dst.stat().st_size)
