"""Optimisation-invariant audit checks (cross-language perf pass).

Static guards pinning the contracts introduced by the cross-language
optimisation work (commits ``d0307ec2`` / ``e0d65d24`` / ``91959d4b``):

- ``check_gpu_coordinator_lazy_torch`` — ``gpu_coordinator.py`` must not
  import torch at module level (per-consumer import + CUDA-context cost);
- ``check_jax_sft_retrace_bound`` — the JAX SFT step must keep bucketed
  collation and a single fused ``jax.jit`` train step with ``lr`` as a
  traced scalar, plus a jitted eval loss;
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
_SFT = (
    "Standalone tools/local-model/src/backend/services/xingcheng/"
    "infrastructure/native_transformer/jax_backend/sft.py"
)
_RSM = "main-system/src-ui/renderer/services/RuntimeServiceManager.ts"
_SLO = "main-system/src-ui/renderer/ui/AppSloDrawer.tsx"
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


def check_gpu_coordinator_lazy_torch(root: Path, errors: list[str]) -> None:
    """torch must stay out of module scope — lazy probe only."""
    source = _read(root, _GPU_COORDINATOR)
    if source is None:
        errors.append(f"missing {_GPU_COORDINATOR}")
        return
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        errors.append(f"gpu_coordinator unparseable: {exc}")
        return
    # Direct top-level import statements only — the lazy probe lives
    # inside ``_torch()``'s function body, which is not module scope.
    for node in tree.body:
        if isinstance(node, ast.Import) and any(
            a.name == "torch" or a.name.startswith("torch.")
            for a in node.names
        ):
            errors.append(
                f"{_GPU_COORDINATOR}:{node.lineno} top-level "
                "import torch — must stay lazy (per-process cost)"
            )
        if isinstance(node, ast.ImportFrom) and (
            node.module or ""
        ).startswith("torch"):
            errors.append(
                f"{_GPU_COORDINATOR}:{node.lineno} top-level "
                "from-import of torch — must stay lazy"
            )
    # The lazy probe entry point must exist.
    if "def _torch()" not in source:
        errors.append(f"{_GPU_COORDINATOR}: lazy _torch() probe missing")
    # nvidia-smi-first ordering: query_gpu must call the smi probe before
    # the torch fallback.
    order = re.search(
        r"def query_gpu\(.*?return _query_via_torch\(\)",
        source,
        re.DOTALL,
    )
    if not order or "_query_via_nvidia_smi" not in order.group(0):
        errors.append(
            f"{_GPU_COORDINATOR}: query_gpu must try nvidia-smi before "
            "the torch fallback"
        )


def check_jax_sft_retrace_bound(root: Path, errors: list[str]) -> None:
    """Bucketed collation + one fused jitted step + jitted eval."""
    source = _read(root, _SFT)
    if source is None:
        errors.append(f"missing {_SFT}")
        return
    if not re.search(r"^_COLLATE_BUCKET\s*=\s*\d+", source, re.M):
        errors.append(f"{_SFT}: _COLLATE_BUCKET constant missing")
    if "train_step = jax.jit(" not in source:
        errors.append(
            f"{_SFT}: fused train step must be jax.jit-wrapped"
        )
    elif "donate_argnums" not in source.split("train_step = jax.jit(", 1)[1][:200]:
        errors.append(
            f"{_SFT}: fused train step must donate params/opt_state "
            "buffers (donate_argnums)"
        )
    if not re.search(
        r"def _train_step\(params, opt_state, input_ids, labels, lr\)",
        source,
    ):
        errors.append(
            f"{_SFT}: _train_step must take lr as a traced scalar "
            "argument (closure-baked lr forces per-step recompile)"
        )
    if "eval_loss = jax.jit(" not in source:
        errors.append(f"{_SFT}: eval loss must be jax.jit-wrapped")
    if "collate_bucket" not in source or "def _choose_bucket" not in source:
        errors.append(
            f"{_SFT}: adaptive bucket hook missing — JaxSFTConfig."
            "collate_bucket + _choose_bucket must stay wired"
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
_RENDERER_EXTS = (".ts", ".tsx")
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
    """Every renderer ``setInterval`` must be hidden-gated or carry an
    explicit ``idle-ok`` exemption (same auditable-suppression convention
    as ``sql-ok``).  The two known IPC timers additionally pin the gate
    BEFORE the IPC call."""
    gate_before_work = {
        _RSM: "invoke('app:get-status'",
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
    for marker in ("--prepare-only", "electron"):
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
_TOOL_HOST_RESCUE_MANIFEST = "Standalone tools/system-rescue/manifest.json"
_TOOL_HOST_RESCUE_NATIVE = (
    "Standalone tools/system-rescue/src-native/Program.cs"
)

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
    manifest = _read(root, _TOOL_HOST_RESCUE_MANIFEST)
    if manifest is None:
        errors.append(f"missing {_TOOL_HOST_RESCUE_MANIFEST}")
    else:
        try:
            native_entry = json.loads(manifest)["runtime"]["native_entry"]
        except (ValueError, KeyError, json.JSONDecodeError):
            native_entry = ""
        if (
            not native_entry.endswith(".exe")
            or native_entry.startswith("/")
            or ".." in Path(native_entry).parts
        ):
            errors.append(
                f"{_TOOL_HOST_RESCUE_MANIFEST}: native_entry must be a "
                "tool-root-relative .exe"
            )
    if not (root / _TOOL_HOST_RESCUE_NATIVE).is_file():
        errors.append(f"missing {_TOOL_HOST_RESCUE_NATIVE}")


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
        check_gpu_coordinator_lazy_torch,
        check_jax_sft_retrace_bound,
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
