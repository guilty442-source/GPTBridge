from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from packager_metadata import iter_companion_renderer_tools, iter_tools
from packager_orchestration import package_tool
from packager_renderer import build_platform_renderer
from packager_verification import verify_tool_package, verify_upgrade_result


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Package platform tools as standalone EXEs.")
    parser.add_argument("tool_ids", nargs="*", help="Optional tool ids to package.")
    parser.add_argument(
        "--all",
        action="store_true",
        dest="all_tools",
        help="Explicitly operate on every platform tool.",
    )
    parser.add_argument(
        "--changed",
        action="store_true",
        help="Package only missing, stale, or invalid platform tool EXEs.",
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument(
        "--build-renderers-only",
        action="store_true",
        help="Only build per-tool renderer bundles without assembling Electron EXEs.",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Verify packaged EXEs and confirm their source snapshots are current.",
    )
    args = parser.parse_args()

    scope_count = int(args.all_tools) + int(args.changed) + int(bool(args.tool_ids))
    if scope_count != 1:
        parser.error("Specify tool ids, --changed, or --all (exactly one scope)")
    if args.changed and (args.verify or args.build_renderers_only):
        parser.error("--changed is only available for package assembly")
    selected_ids = None if (args.all_tools or args.changed) else set(args.tool_ids)
    tools = iter_tools(
        selected_ids,
        include_special_unpacked=args.build_renderers_only,
    )
    if args.build_renderers_only:
        tools = [
            item
            for item in tools
            if item[0] not in {"governance_rule", "shared-layer", "xingcheng"}
            and item[2].get("has_custom_ui") is not False
        ]
        known_ids = {tool_id for tool_id, _, _ in tools}
        tools.extend(
            item
            for item in iter_companion_renderer_tools(selected_ids)
            if item[0] not in known_ids
        )
    auto_repair: dict[str, Any] | None = None
    post_repair: dict[str, Any] | None = None
    if args.changed:
        tools = [
            item
            for item in tools
            if not verify_tool_package(*item).get("ok")
        ]
    if args.verify:
        results = [
            verify_tool_package(tool_id, tool_dir, manifest)
            for tool_id, tool_dir, manifest in tools
        ]
    elif args.build_renderers_only:
        results = [
            build_platform_renderer(tool_id, tool_dir)
            for tool_id, tool_dir, _ in tools
        ]
    else:
        results = [
            package_tool(tool_id, tool_dir, manifest)
            for tool_id, tool_dir, manifest in tools
        ]
        results = [
            verify_upgrade_result(tool_id, tool_dir, manifest, result)
            for (tool_id, tool_dir, manifest), result in zip(
                tools,
                results,
                strict=True,
            )
        ]
    ok = (
        all(result.get("ok") for result in results)
        and (auto_repair is None or bool(auto_repair.get("ok")))
        and (post_repair is None or bool(post_repair.get("ok")))
    )

    if args.as_json:
        print(
            json.dumps(
                {
                    "ok": ok,
                    "auto_repair": auto_repair,
                    "post_repair": post_repair,
                    "results": results,
                },
                ensure_ascii=False,
            )
        )
    else:
        for result in results:
            status = "OK" if result.get("ok") else "FAIL"
            target = result.get("exe_path") or result.get("renderer_path") or result.get("message")
            print(f"[{status}] {result.get('tool_id')}: {target}")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
