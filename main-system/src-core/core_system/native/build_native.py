"""In-place build for the GPTBridge native kernel (A221/E186 canonical tree).

Compiles the pybind11 binding (_binding.cpp) and the C bridge
(native/bridge/gptbridge_native.c) into _sovereign_native.pyd placed next
to this script so the Python adapters can import it with a relative import.

The canonical native source lives in the project-root native/ tree:
  native/include/gptbridge_native.h  — sole public C header
  native/bridge/gptbridge_native.c   — sole C ABI thunk

Requires a C++ compiler (MSVC via the VS/VS Build Tools Developer
environment) plus pybind11 installed in the active interpreter.
"""

from __future__ import annotations

import pathlib
import sys

import pybind11
from setuptools import Extension, setup

HERE = pathlib.Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[3]
NATIVE_ROOT = PROJECT_ROOT / "native"

extension = Extension(
    "_sovereign_native",
    [
        str(HERE / "_binding.cpp"),
        str(NATIVE_ROOT / "bridge" / "gptbridge_native.c"),
    ],
    include_dirs=[
        pybind11.get_include(),
        str(NATIVE_ROOT / "include"),
    ],
    language="c++",
    optional=True,
)


def main() -> int:
    sys.argv = [
        "build_native.py",
        "build_ext",
        "--inplace",
        "--build-lib", str(HERE),
        "--build-temp", str(HERE / ".native-build"),
    ]
    setup(name="sovereign-native", ext_modules=[extension])
    import shutil

    shutil.rmtree(HERE / ".native-build", ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
