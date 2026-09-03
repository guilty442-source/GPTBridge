"""In-place build for the System Sovereign native kernel (hybrid Python/C++).

The extension is written next to this script so the Python adapters can import
it with a relative import. Requires a C++ compiler (MSVC via the VS/VS Build
Tools Developer environment) plus pybind11 installed in the active interpreter.
"""

from __future__ import annotations

import pathlib
import sys

import pybind11
from setuptools import Extension, setup

HERE = pathlib.Path(__file__).resolve().parent

extension = Extension(
    "_sovereign_native",
    [str(HERE / "_sovereign_native.cpp")],
    include_dirs=[pybind11.get_include()],
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
