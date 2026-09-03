"""In-place build for the GPTBridge native extension (hybrid Python/C++).

Run from any directory; the extension is written next to this script so that
the package can import it with a relative import.

Requires a C++ compiler on PATH (MSVC via the Developer environment) plus
pybind11 installed in the active interpreter.
"""

from __future__ import annotations

import pathlib
import sys

import pybind11
from setuptools import Extension, setup

HERE = pathlib.Path(__file__).resolve().parent

extension = Extension(
    "_gptbridge_native",
    [str(HERE / "_gptbridge_native.cpp")],
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
    setup(name="gptbridge-native", ext_modules=[extension])
    # remove the temp build tree
    import shutil
    shutil.rmtree(HERE / ".native-build", ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
