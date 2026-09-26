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
  test project must exist (``information-channel-gateway`` target).

All checks are delegated (source/AST scans); none are reducible to the
native engine's file-kinds.
"""
from __future__ import annotations

import ast
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


def _interval_body(source: str, errors: list[str], rel: str) -> str | None:
    match = re.search(r"setInterval\((?:async )?\(\)\s*=>\s*\{", source)
    if not match:
        errors.append(f"{rel}: no arrow-callback setInterval found")
        return None
    start = match.end()
    depth = 1
    i = start
    while i < len(source) and depth:
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
        i += 1
    if depth:
        errors.append(f"{rel}: unbalanced braces in setInterval callback")
        return None
    return source[start:i]


def check_renderer_idle_gating(root: Path, errors: list[str]) -> None:
    """Hidden-window gate must precede the IPC call in both timers."""
    cases = (
        (_RSM, "invoke('app:get-status'"),
        (_SLO, "void fetchReport()"),
    )
    for rel, work_marker in cases:
        source = _read(root, rel)
        if source is None:
            errors.append(f"missing {rel}")
            continue
        body = _interval_body(source, errors, rel)
        if body is None:
            continue
        gate = _HIDDEN_GATE.search(body)
        work = body.find(work_marker)
        if not gate:
            errors.append(f"{rel}: timer lost its hidden-window gate")
        elif work < 0:
            errors.append(f"{rel}: expected IPC marker {work_marker!r} gone")
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
    ):
        check(args.root, errors)
    for error in errors:
        print(error)
    print(f"optimization-invariants: {len(errors)} error(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
