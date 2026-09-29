"""Optimisation-invariant audit checks (cross-language perf pass).

Static guards pinning the contracts introduced by the cross-language
optimisation work (commits ``d0307ec2`` / ``e0d65d24`` / ``91959d4b``):

- ``check_gpu_coordinator_lazy_torch`` — ``gpu_coordinator.py`` must not
  import torch at module level (per-consumer import + CUDA-context cost);
- ``check_renderer_idle_gating`` — renderer timers must stay gated on
  ``document.visibilityState`` (hidden window = no heartbeat/SLO IPC);
- ``check_bootstrap_native_entry`` — the C# bootstrap entry project must
  exist (A341 orchestration-layer migration target ``bootstrap-entry``);
- ``check_channel_gateway_csharp`` — the C# A263 channel library and its
  test project must exist (``information-channel-gateway`` target);
- ``check_tool_host_native_boundary`` — the C# governed tool host must
  keep token issuance / transport-store access in the Python sidecar
  (E4), and the main-system spawn path must keep the ``native_entry``
  branch for tool-root ``.exe`` hosts.

All checks are delegated (source/AST scans); none are reducible to the
native engine's file-kinds.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

_GPU_COORDINATOR = (
    "shared-layer/src/shared_layer/adaptive/gpu_coordinator.py"
)
_RSM = "main-system/src-ui/renderer/services/RuntimeServiceManager.js"
_SLO = "main-system/src-ui/renderer/ui/AppSloDrawer.jsx"
_BOOTSTRAP_CSPROJ = (
    "main-system/launcher/src/GPTBridge.Bootstrap/GPTBridge.Bootstrap.csproj"
)
_CHANNEL_CSPROJ = (
    "shared-layer/csharp/GPTBridge.Channels/GPTBridge.Channels/"
    "GPTBridge.Channels.csproj"
)
_CHANNEL_TEST_CSPROJ = (
    "shared-layer/csharp/GPTBridge.Channels/GPTBridge.Channels.Tests/"
    "GPTBridge.Channels.Tests.csproj"
)

_HIDDEN_GATE = re.compile(
    r"document\.visibilityState\s*===\s*['\"]hidden['\"]"
)


def _read(root: Path, rel: str) -> str | None:
    path = root / rel
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8", errors="replace")


def check_gpu_coordinator_torch_free(root: Path, errors: list[str]) -> None:
    """PyTorch is retired (B167/B38) — the coordinator must not touch it."""
    source = _read(root, _GPU_COORDINATOR)
    if source is None:
        errors.append(f"missing {_GPU_COORDINATOR}")
        return
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        errors.append(f"gpu_coordinator unparseable: {exc}")
        return

    def _imports_torch(node: ast.AST) -> bool:
        if isinstance(node, ast.Import):
            return any(
                a.name == "torch" or a.name.startswith("torch.")
                for a in node.names
            )
        if isinstance(node, ast.ImportFrom):
            return (node.module or "").startswith("torch")
        return False

    for node in ast.walk(tree):
        if _imports_torch(node):
            errors.append(
                f"{_GPU_COORDINATOR}:{getattr(node, 'lineno', '?')} "
                "imports retired framework torch (B167/B38)"
            )
    for marker in ("torch.cuda", "_query_via_torch", "_TORCH"):
        if marker in source:
            errors.append(
                f"{_GPU_COORDINATOR}: retired torch reference {marker!r} remains"
            )
    # VRAM evidence must come from the native probe only (fail-closed).
    order = re.search(
        r"def query_gpu\(.*$",
        source,
        re.DOTALL | re.MULTILINE,
    )
    if not order or "_query_via_nvidia_smi" not in order.group(0):
        errors.append(
            f"{_GPU_COORDINATOR}: query_gpu must be served by "
            "nvidia-smi only"
        )


_INTERVAL_CB = re.compile(r"setInterval\((?:async )?\(\)\s*=>\s*\{")


def _interval_bodies(source: str) -> dict[int, str]:
    """``{lineno_of_setInterval: callback_body}`` for every arrow-body
    ``setInterval`` in the source."""
    bodies: dict[int, str] = {}
    for match in _INTERVAL_CB.finditer(source):
        start = match.end()
        depth = 1
        i = start
        while i < len(source) and depth:
            if source[i] == "{":
                depth += 1
            elif source[i] == "}":
                depth -= 1
            i += 1
        lineno = source.count("\n", 0, match.start()) + 1
        if depth:
            bodies[lineno] = ""
            continue
        bodies[lineno] = source[start:i]
    return bodies


_RENDERER_GLOB_ROOT = "main-system/src-ui/renderer"
_RENDERER_EXTS = (".js", ".jsx", ".mjs")
_IDLE_OK = "idle-ok"


def _renderer_intervals(root: Path) -> list[tuple[str, str, int]]:
    """Every ``setInterval`` site under renderer/ as
    ``(relpath, source, lineno)``."""
    base = root / _RENDERER_GLOB_ROOT
    if not base.is_dir():
        return []
    sites: list[tuple[str, str, int]] = []
    for path in sorted(base.rglob("*")):
        if path.suffix not in _RENDERER_EXTS or not path.is_file():
            continue
        if "node_modules" in path.parts or "dist" in path.parts:
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        rel = path.relative_to(root).as_posix()
        for lineno, line in enumerate(source.splitlines(), start=1):
            if "setInterval(" in line and "typeof setInterval" not in line:
                sites.append((rel, source, lineno))
    return sites


def _idle_ok_marked(source: str, lineno: int) -> bool:
    """``// idle-ok:`` marker on the setInterval line or the three
    comment lines above it (same convention as ``# sql-ok``)."""
    lines = source.splitlines()
    lo = max(0, lineno - 4)
    return any(_IDLE_OK in lines[i] for i in range(lo, lineno))


def check_renderer_idle_gating(root: Path, errors: list[str]) -> None:
    """Every active renderer interval must be hidden-gated or exempted.

    The legacy main-system renderer is retired when the Tauri shell is active;
    in that mode there is no main renderer interval surface to inspect.
    """
    if (
        (root / "main-system/src-tauri/src/tool_window.rs").is_file()
        and not (root / "main-system/src-ui/renderer/index.html").is_file()
    ):
        return
    gate_before_work = {
        _RSM: "app:get-status",
        _SLO: "void fetchReport()",
    }
    bodies_cache: dict[str, dict[int, str]] = {}
    for rel, source, lineno in _renderer_intervals(root):
        if rel not in bodies_cache:
            bodies_cache[rel] = _interval_bodies(source)
        bodies = bodies_cache[rel]
        body = bodies.get(lineno)
        if body is None:
            # Non arrow-callback form (e.g. function ref) — can still be
            # exempted by marker but cannot be statically gated.
            if not _idle_ok_marked(source, lineno):
                errors.append(
                    f"{rel}:{lineno} setInterval is not an inline arrow "
                    "callback — verify idle gating manually or mark "
                    "// idle-ok: <reason>"
                )
            continue
        if body == "":
            errors.append(f"{rel}:{lineno} unbalanced setInterval callback")
            continue
        gate = _HIDDEN_GATE.search(body)
        if gate is None:
            if not _idle_ok_marked(source, lineno):
                errors.append(
                    f"{rel}:{lineno} setInterval lacks a "
                    "document.visibilityState hidden gate — gate it or "
                    "mark the site with // idle-ok: <reason>"
                )
            continue
        work_marker = gate_before_work.get(rel)
        if work_marker is not None:
            work = body.find(work_marker)
            if work < 0:
                errors.append(
                    f"{rel}: expected IPC marker {work_marker!r} gone"
                )
            elif gate.start() > work:
                errors.append(
                    f"{rel}: visibility check must precede the IPC call"
                )


def check_bootstrap_native_entry(root: Path, errors: list[str]) -> None:
    """bootstrap-entry migrated to C# (A341/A610): project + entrypoint."""
    csproj = root / _BOOTSTRAP_CSPROJ
    if not csproj.is_file():
        errors.append(f"missing {_BOOTSTRAP_CSPROJ}")
        return
    program = csproj.parent / "Program.cs"
    if not program.is_file():
        errors.append(f"missing {program.relative_to(root).as_posix()}")
        return
    source = program.read_text(encoding="utf-8", errors="replace")
    for marker in ("--prepare-only",):
        if marker not in source:
            errors.append(
                f"{program.name}: bootstrap contract marker "
                f"{marker!r} missing"
            )


def check_channel_gateway_csharp(root: Path, errors: list[str]) -> None:
    """information-channel-gateway C# port: library + test project."""
    for rel in (_CHANNEL_CSPROJ, _CHANNEL_TEST_CSPROJ):
        if not (root / rel).is_file():
            errors.append(f"missing {rel}")
    channel_src = (
        root / _CHANNEL_CSPROJ
    ).parent / "A263Channel.cs"
    if channel_src.is_file():
        text = channel_src.read_text(encoding="utf-8", errors="replace")
        for marker in ("Stopwatch.GetTimestamp", "ReconnectAsync"):
            if marker not in text:
                errors.append(
                    f"A263Channel.cs: {marker!r} missing — high-resolution "
                    "timing and reconnect lifecycle are port invariants"
                )


_TOOL_HOST_DIR = "shared-layer/csharp/GPTBridge.ToolHost/GPTBridge.ToolHost"
_TOOL_HOST_TEST_DIR = (
    "shared-layer/csharp/GPTBridge.ToolHost/GPTBridge.ToolHost.Tests"
)
_TOOL_HOST_SPAWN = "main-system/src-core/tasks/toolbox_start_spawn_process.py"
_TOOL_HOST_RESOLVER = "main-system/src-core/tasks/tool_path_resolver.py"

# Token issuance, transport-store access and the governance bootstrap all
# stay in the Python plane (E4); the native host must never implement them.
_NATIVE_FORBIDDEN = (
    "HMACSHA",
    "issue_token",
    "launcher_key",
    "integrity_manifest",
    "identity_attestation",
    "gptbridge_transport",
    "Npgsql",
    "pg_notify",
)


def check_tool_host_native_boundary(root: Path, errors: list[str]) -> None:
    """migrate-csharp tool host: governed boundary + spawn-path wiring.

    The C# host may only speak star-governed-transport-proxy/v1 ops to
    the Python sidecar; it must never embed token-issuance or store
    access primitives.  The spawn path must branch on a tool-root .exe.
    """
    host_dir = root / _TOOL_HOST_DIR
    if not host_dir.is_dir():
        errors.append(f"missing {_TOOL_HOST_DIR}")
        return
    if not (root / _TOOL_HOST_TEST_DIR).is_dir():
        errors.append(f"missing {_TOOL_HOST_TEST_DIR}")
    for path in sorted(host_dir.glob("*.cs")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for marker in _NATIVE_FORBIDDEN:
            if marker in text:
                errors.append(
                    f"{path.relative_to(root).as_posix()}: forbidden "
                    f"governance primitive {marker!r} in native host — "
                    "token issuance/store access stays in the sidecar"
                )
    # Ops surface must remain the proxy's declared process-side set.
    client = host_dir / "TransportProxyClient.cs"
    if client.is_file():
        text = client.read_text(encoding="utf-8", errors="replace")
        for op in (
            '"hello"',
            '"claim"',
            '"respond"',
            '"request_cancelled"',
            '"notification_stamp"',
        ):
            if op not in text:
                errors.append(
                    f"TransportProxyClient.cs: proxy op {op} missing"
                )
    else:
        errors.append("missing TransportProxyClient.cs")

    spawn = _read(root, _TOOL_HOST_SPAWN)
    if spawn is None or 'source_entry.suffix.lower() == ".exe"' not in spawn:
        errors.append(
            f"{_TOOL_HOST_SPAWN}: native (.exe) spawn branch missing"
        )
    resolver = _read(root, _TOOL_HOST_RESOLVER)
    if resolver is None or "native_entry" not in resolver:
        errors.append(
            f"{_TOOL_HOST_RESOLVER}: runtime.native_entry branch missing"
        )
def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[3],
    )
    args = parser.parse_args()
    errors: list[str] = []
    for check in (
        check_gpu_coordinator_torch_free,
        check_renderer_idle_gating,
        check_bootstrap_native_entry,
        check_channel_gateway_csharp,
        check_tool_host_native_boundary,
    ):
        check(args.root, errors)
    for error in errors:
        print(error)
    print(f"optimization-invariants: {len(errors)} error(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
