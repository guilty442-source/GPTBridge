from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from managers.child_tool_workspace import ChildToolWorkspace


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a GPTBridge child tool.")
    parser.add_argument(
        "tool_name",
        nargs="?",
        default="ChildTool",
        help="Child tool name.",
    )
    args = parser.parse_args(argv)

    project_root = Path(__file__).resolve().parent.parent
    workspace = ChildToolWorkspace(project_root)
    result = workspace.create_project(args.tool_name, "python_desktop")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
