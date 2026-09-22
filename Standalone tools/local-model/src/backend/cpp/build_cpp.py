"""Build the Xingcheng formal C++ inference extension.

Layer contract:
- ``src/*.cpp`` is the C++ inference layer.
- ``native/include/gptbridge_native.h`` is the only C ABI header it consumes.
- ``native/{bridge,core}/*.c`` remain pure C and are compiled as C sources.
- Output is a separate ``_xingcheng_inference`` extension; it does not replace
  ``_sovereign_native`` or put C++ inside ``native/``.
"""

from __future__ import annotations

import os
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
    # Host-API-only CUDA bridge (cuBLAS/cudart, no device kernels) — plain
    # C++ compilation works everywhere; guarded internally by XINGCHENG_CUDA.
    HERE / "src" / "cuda_bridge.cpp",
)
C_SOURCES = (
    NATIVE_ROOT / "bridge" / "gptbridge_native.c",
    NATIVE_ROOT / "core" / "parser.c",
    NATIVE_ROOT / "core" / "vector.c",
    NATIVE_ROOT / "core" / "transformer.c",
    NATIVE_ROOT / "core" / "kv_pool.c",
)


def _cuda_home() -> pathlib.Path | None:
    candidates = [
        os.environ.get("CUDA_PATH"),
        os.environ.get("CUDA_HOME"),
        r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.0",
    ]
    for raw in candidates:
        if not raw:
            continue
        home = pathlib.Path(raw)
        if (home / "include" / "cublas_v2.h").is_file() and (
            home / "lib" / "x64" / "cublas.lib"
        ).is_file():
            return home
    return None


def _extension():
    import pybind11
    from setuptools import Extension

    include_dirs = [
        pybind11.get_include(),
        str(HERE / "include"),
        str(NATIVE_ROOT / "include"),
    ]
    define_macros: list[tuple[str, str]] = []
    libraries: list[str] = []
    library_dirs: list[str] = []

    cuda_home = _cuda_home()
    if cuda_home is not None:
        libraries.extend(["cudart", "cublas"])
        library_dirs.append(str(cuda_home / "lib" / "x64"))
        include_dirs.append(str(cuda_home / "include"))
        define_macros.append(("XINGCHENG_CUDA", "1"))
        print(f"[build_cpp] CUDA bridge enabled ({cuda_home})")
    else:
        print("[build_cpp] CUDA toolkit not found — building CPU-only")

    return Extension(
        "_xingcheng_inference",
        [str(path) for path in (*CPP_SOURCES, *C_SOURCES)],
        include_dirs=include_dirs,
        define_macros=define_macros,
        libraries=libraries,
        library_dirs=library_dirs,
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
