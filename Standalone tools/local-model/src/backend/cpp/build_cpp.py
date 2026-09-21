"""Build the Xingcheng formal C++ inference extension.

Layer contract:
- ``src/*.cpp`` is the C++ inference layer.
- ``native/include/gptbridge_native.h`` is the only C ABI header it consumes.
- ``native/{bridge,core}/*.c`` remain pure C and are compiled as C sources.
- Output is a separate ``_xingcheng_inference`` extension; it does not replace
  ``_sovereign_native`` or put C++ inside ``native/``.
"""

from __future__ import annotations

import pathlib
import shutil
import sys
import sysconfig

HERE = pathlib.Path(__file__).resolve().parent
LOCAL_MODEL_ROOT = HERE.parents[2]
PROJECT_ROOT = LOCAL_MODEL_ROOT.parents[1]
NATIVE_ROOT = PROJECT_ROOT / "native"
DIST_NATIVE = LOCAL_MODEL_ROOT / "dist-native"

CPP_SOURCES = (
    HERE / "src" / "binding.cpp",
    HERE / "src" / "engine.cpp",
)
C_SOURCES = (
    NATIVE_ROOT / "bridge" / "gptbridge_native.c",
    NATIVE_ROOT / "core" / "parser.c",
    NATIVE_ROOT / "core" / "vector.c",
    NATIVE_ROOT / "core" / "transformer.c",
    NATIVE_ROOT / "core" / "kv_pool.c",
)


def _extension():
    import pybind11
    from setuptools import Extension

    return Extension(
        "_xingcheng_inference",
        [str(path) for path in (*CPP_SOURCES, *C_SOURCES)],
        include_dirs=[
            pybind11.get_include(),
            str(HERE / "include"),
            str(NATIVE_ROOT / "include"),
        ],
        language="c++",
        extra_compile_args=(
            ["/std:c++17", "/utf-8"] if sys.platform == "win32" else ["-std=c++17"]
        ),
        optional=False,
    )


def main() -> int:
    from setuptools import setup

    DIST_NATIVE.mkdir(parents=True, exist_ok=True)
    sys.argv = [
        "build_cpp.py",
        "build_ext",
        "--build-lib",
        str(DIST_NATIVE),
        "--build-temp",
        str(DIST_NATIVE / ".cpp-build"),
    ]
    setup(name="xingcheng-inference", ext_modules=[_extension()])
    shutil.rmtree(DIST_NATIVE / ".cpp-build", ignore_errors=True)

    artifacts = sorted(DIST_NATIVE.glob("_xingcheng_inference*.pyd"))
    for artifact in artifacts:
        print(f"[build_cpp] built {artifact}")
        purelib = sysconfig.get_paths().get("purelib")
        if purelib:
            target = pathlib.Path(purelib) / artifact.name
            shutil.copy2(artifact, target)
            print(f"[build_cpp] installed {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
