"""Governed build for the GPTBridge native kernel (A221/E186 canonical tree).

Build layers are explicit and auditable:

* ``native/include/gptbridge_native.h`` is the sole public C ABI header.
* ``native/bridge`` + ``native/core`` are pure C implementations.
* ``_binding.cpp`` is the only C++ source and only adapts Python objects to
  the public C ABI; it must not include private core headers.

Compiles the pybind11 binding and C core into ``main-system/dist-native/`` and
installs the artifact to this package directory plus the active interpreter's
site-packages for the shared-layer dispatcher.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import sys
import sysconfig
import time
from typing import Any

HERE = pathlib.Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[3]
NATIVE_ROOT = PROJECT_ROOT / "native"
DIST_NATIVE = HERE.parents[2] / "dist-native"

BINDING_SOURCES = (HERE / "_binding.cpp",)
PUBLIC_C_ABI_HEADERS = (
    NATIVE_ROOT / "include" / "gptbridge_native.h",
    NATIVE_ROOT / "include" / "watchdog.h",
    NATIVE_ROOT / "include" / "scheduler.h",
    NATIVE_ROOT / "include" / "outbox.h",
    NATIVE_ROOT / "include" / "maintenance.h",
)
C_CORE_SOURCES = (
    NATIVE_ROOT / "bridge" / "gptbridge_native.c",
    NATIVE_ROOT / "core" / "parser.c",
    NATIVE_ROOT / "core" / "vector.c",
    NATIVE_ROOT / "core" / "transformer.c",
    NATIVE_ROOT / "core" / "watchdog.c",
    NATIVE_ROOT / "core" / "scheduler.c",
    NATIVE_ROOT / "core" / "outbox.c",
    NATIVE_ROOT / "core" / "maintenance.c",
)
C_PRIVATE_HEADERS = (
    NATIVE_ROOT / "core" / "memory.h",
    NATIVE_ROOT / "core" / "parser.h",
    NATIVE_ROOT / "core" / "vector.h",
    NATIVE_ROOT / "core" / "transformer.h",
)
CPP_SUFFIXES = {".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx"}
# Explicit, auditable test-suite layer: the C test suites (and their C#
# orchestrator) live under native/test_suites and are NOT part of the pure-C
# core boundary; the layering validator excludes this declared layer.
TEST_SUITE_ROOT = NATIVE_ROOT / "test_suites"


def _relative(path: pathlib.Path) -> str:
    return path.resolve().relative_to(PROJECT_ROOT).as_posix()


def native_build_manifest() -> dict[str, Any]:
    """Return the governed native build layering manifest."""
    return {
        "schema_version": "star-native-build-layers/v1",
        "extension": "_sovereign_native",
        "binding_layer": [_relative(path) for path in BINDING_SOURCES],
        "public_c_abi": [_relative(path) for path in PUBLIC_C_ABI_HEADERS],
        "c_core_layer": [_relative(path) for path in C_CORE_SOURCES],
        "private_c_headers": [_relative(path) for path in C_PRIVATE_HEADERS],
        "test_suite_layer": _relative(TEST_SUITE_ROOT),
        "output_root": _relative(DIST_NATIVE),
        "link_language": "c++",
    }


def validate_layering() -> dict[str, Any]:
    """Validate the C core / public C ABI / C++ binding boundary."""
    errors: list[str] = []

    for path in BINDING_SOURCES:
        if path.suffix != ".cpp":
            errors.append(f"binding source is not C++: {_relative(path)}")
    for path in C_CORE_SOURCES:
        if path.suffix != ".c":
            errors.append(f"C core source is not C: {_relative(path)}")

    cpp_files = sorted(
        path for path in NATIVE_ROOT.rglob("*")
        if path.is_file()
        and path.suffix.lower() in CPP_SUFFIXES
        and TEST_SUITE_ROOT not in path.parents
    )
    for path in cpp_files:
        errors.append(f"C++ source inside pure-C native root: {_relative(path)}")

    public_header = PUBLIC_C_ABI_HEADERS[0].read_text(encoding="utf-8")
    if '#ifdef __cplusplus' not in public_header or 'extern "C"' not in public_header:
        errors.append("public C ABI header lacks extern \"C\" guards")
    binding_text = BINDING_SOURCES[0].read_text(encoding="utf-8")
    if '#include "gptbridge_native.h"' not in binding_text:
        errors.append("binding does not consume the sole public C ABI header")
    for header in C_PRIVATE_HEADERS:
        include = f'#include "{header.name}"'
        if include in binding_text:
            errors.append(f"binding bypasses public C ABI via {header.name}")
        header_text = header.read_text(encoding="utf-8")
        if 'extern "C"' not in header_text:
            errors.append(f"private C header lacks extern \"C\" guards: {header.name}")

    stale_roots = (
        HERE,
        DIST_NATIVE,
        PROJECT_ROOT / "shared-layer" / "src",
        PROJECT_ROOT / "Standalone tools" / "local-model",
    )
    stale = sorted(
        path
        for root in stale_roots
        if root.exists()
        for path in root.rglob("_gptbridge_native*.pyd")
    )
    for path in stale:
        errors.append(f"stale native artifact present: {path}")

    return {
        "ok": not errors,
        "errors": errors,
        "manifest": native_build_manifest(),
    }


def _make_extension() -> Any:
    import pybind11
    from setuptools import Extension

    return Extension(
        "_sovereign_native",
        [str(path) for path in (*BINDING_SOURCES, *C_CORE_SOURCES)],
        include_dirs=[
            pybind11.get_include(),
            str(NATIVE_ROOT / "include"),
        ],
        language="c++",
        optional=True,
    )


def _install(artifact: pathlib.Path) -> list[pathlib.Path]:
    targets = [HERE / artifact.name]
    purelib = sysconfig.get_paths().get("purelib")
    if purelib:
        targets.append(pathlib.Path(purelib) / artifact.name)
    installed: list[pathlib.Path] = []
    for target in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(artifact, target)
        except OSError as error:
            print(f"[build_native] WARNING: cannot install {target}: {error}")
            continue
        installed.append(target)
    return installed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate C/C++ build layering without compiling",
    )
    args = parser.parse_args(argv)

    report = validate_layering()
    if args.check:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ok"] else 1
    if not report["ok"]:
        for error in report["errors"]:
            print(f"[build_native] ERROR: {error}")
        return 1

    from setuptools import setup

    DIST_NATIVE.mkdir(parents=True, exist_ok=True)
    sys.argv = [
        "build_native.py",
        "build_ext",
        "--build-lib", str(DIST_NATIVE),
        "--build-temp", str(DIST_NATIVE / ".native-build"),
    ]
    build_started = time.time()
    setup(name="sovereign-native", ext_modules=[_make_extension()])
    shutil.rmtree(DIST_NATIVE / ".native-build", ignore_errors=True)

    artifacts = sorted(DIST_NATIVE.glob("_sovereign_native*.pyd"))
    if not artifacts:
        print(
            "[build_native] WARNING: no artifact produced (compiler or "
            "pybind11 unavailable); existing installs left untouched."
        )
        return 0
    fresh = [a for a in artifacts if a.stat().st_mtime >= build_started]
    if not fresh:
        print(
            "[build_native] WARNING: link produced no fresh artifact "
            "(existing .pyd likely locked by a running process); "
            "stale installs left untouched."
        )
        return 0
    for artifact in fresh:
        print(f"[build_native] built {artifact}")
        for target in _install(artifact):
            print(f"[build_native] installed {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BINDING_SOURCES",
    "C_CORE_SOURCES",
    "C_PRIVATE_HEADERS",
    "PUBLIC_C_ABI_HEADERS",
    "native_build_manifest",
    "validate_layering",
    "main",
]
