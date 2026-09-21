"""Governed build for the GPTBridge native kernel (A221/E186 canonical tree).

Compiles the pybind11 binding (_binding.cpp) and the C bridge
(native/bridge/gptbridge_native.c) into the governed output directory
``main-system/dist-native/`` and installs the artifact to:

  1. this package directory (``core_system/native/``), where the Python
     adapters import it with a relative import, and
  2. the active interpreter's site-packages, so consumers that import the
     top-level ``_sovereign_native`` module (the shared-layer performance
     dispatcher) resolve the same governed artifact.

The build never writes into the process working directory: the former
``--inplace`` invocation copied the extension into whatever directory the
build happened to run from and polluted the repository root.

The canonical native source lives in the project-root native/ tree:
  native/include/gptbridge_native.h       — sole public C header
  native/bridge/gptbridge_native.c        — sole C ABI thunk
  native/core/{parser,vector,transformer}.c — pure-C compute cores

Requires a C++ compiler (MSVC via the VS/VS Build Tools Developer
environment) plus pybind11 installed in the active interpreter.
"""

from __future__ import annotations

import pathlib
import shutil
import sys
import sysconfig

import pybind11
from setuptools import Extension, setup

HERE = pathlib.Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[3]
NATIVE_ROOT = PROJECT_ROOT / "native"
DIST_NATIVE = HERE.parents[2] / "dist-native"

extension = Extension(
    "_sovereign_native",
    [
        str(HERE / "_binding.cpp"),
        str(NATIVE_ROOT / "bridge" / "gptbridge_native.c"),
        str(NATIVE_ROOT / "core" / "parser.c"),
        str(NATIVE_ROOT / "core" / "vector.c"),
        str(NATIVE_ROOT / "core" / "transformer.c"),
    ],
    include_dirs=[
        pybind11.get_include(),
        str(NATIVE_ROOT / "include"),
        str(NATIVE_ROOT / "core"),
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


def main() -> int:
    DIST_NATIVE.mkdir(parents=True, exist_ok=True)
    sys.argv = [
        "build_native.py",
        "build_ext",
        "--build-lib", str(DIST_NATIVE),
        "--build-temp", str(DIST_NATIVE / ".native-build"),
    ]
    setup(name="sovereign-native", ext_modules=[extension])
    shutil.rmtree(DIST_NATIVE / ".native-build", ignore_errors=True)

    artifacts = sorted(DIST_NATIVE.glob("_sovereign_native*.pyd"))
    if not artifacts:
        print(
            "[build_native] WARNING: no artifact produced (compiler or "
            "pybind11 unavailable); existing installs left untouched."
        )
        return 0
    for artifact in artifacts:
        print(f"[build_native] built {artifact}")
        for target in _install(artifact):
            print(f"[build_native] installed {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
