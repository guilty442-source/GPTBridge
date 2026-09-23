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
    # Flat C ABI (P11/MS6): lets C#/ToolHost host the engine in-process —
    # exported from the same .pyd/DLL image; no Python hop on infer path.
    HERE / "src" / "engine_c_abi.cpp",
    # Host-API-only CUDA bridge (cuBLAS/cudart, no device kernels) — plain
    # C++ compilation works everywhere; guarded internally by XINGCHENG_CUDA.
    HERE / "src" / "cuda_bridge.cpp",
)
# nvcc-built device kernels (P1-1③). CUDA 12.x rejects the VS18 default
# toolset (v145 STL hard-fails), but the installed v142/v143 toolsets work
# via ``vcvars64.bat -vcvars_ver=<v>`` — probed at build time; absent a
# compatible pair the extension still builds, just without
# ``XINGCHENG_CUDA_KERNELS`` (bf16 request path then fails closed).
CU_SOURCES = (
    HERE / "src" / "kernels" / "matmul_bf16.cu",
    # P1-1③ residual: fp8 e4m3 weight-storage GEMM.
    HERE / "src" / "kernels" / "matmul_fp8.cu",
    # P1-1② device-resident KV + online-softmax attention.
    HERE / "src" / "kernels" / "kv_attention.cu",
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


def _nvcc(cuda_home: pathlib.Path) -> pathlib.Path | None:
    nvcc = cuda_home / "bin" / "nvcc.exe"
    return nvcc if nvcc.is_file() else None


def _vcvars() -> pathlib.Path | None:
    candidates = [
        r"E:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat",
        r"C:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat",
        r"C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat",
    ]
    for raw in candidates:
        path = pathlib.Path(raw)
        if path.is_file():
            return path
    return None


def _compile_cuda_kernels(
    cuda_home: pathlib.Path, build_dir: pathlib.Path
) -> list[pathlib.Path]:
    """Compile CU_SOURCES with nvcc under a compatible MSVC toolset.

    Returns the produced objects, or [] when no nvcc/MSVC pair works —
    callers then simply omit the kernels TU and XINGCHENG_CUDA_KERNELS.
    """
    import subprocess

    nvcc = _nvcc(cuda_home)
    vcvars = _vcvars()
    if nvcc is None or vcvars is None:
        return []
    msvc_root = vcvars.parents[2] / "Tools" / "MSVC"
    toolsets = (
        sorted(
            (d.name for d in msvc_root.iterdir() if d.is_dir()),
            key=lambda v: tuple(int(x) for x in v.split(".")[:2]),
        )
        if msvc_root.is_dir()
        else []
    )
    if not toolsets:
        return []
    build_dir.mkdir(parents=True, exist_ok=True)
    for toolset in toolsets:
        extra = (
            "-allow-unsupported-compiler "
            if int(toolset.split(".")[1]) >= 40
            else ""
        )
        objects: list[pathlib.Path] = []
        ok = True
        for src in CU_SOURCES:
            obj = build_dir / f"{src.stem}.obj"
            bat = build_dir / f"_nvcc_{src.stem}.bat"
            bat.write_text(
                "@echo off\r\n"
                f'call "{vcvars}" -vcvars_ver={toolset} >nul || exit /b 1\r\n'
                f'"{nvcc}" -std=c++17 {extra}-Xcompiler /EHsc,/MD '
                "-gencode=arch=compute_80,code=sm_86 "
                "-gencode=arch=compute_80,code=compute_80 "
                f'-c "{src}" -o "{obj}"\r\n',
                encoding="ascii",
            )
            proc = subprocess.run(
                ["cmd", "/c", str(bat)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=600,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if proc.returncode != 0 or not obj.is_file():
                # Surface the failure: silently omitting the kernels TU
                # would degrade a broken kernel to "toolchain absent",
                # hiding real compile errors from the evidence trail.
                tail = (proc.stdout + proc.stderr).strip().splitlines()
                print(
                    f"[build_cpp] nvcc failed for {src.name} "
                    f"(toolset {toolset}, rc={proc.returncode}): "
                    + "\n".join(tail[-15:])
                )
                ok = False
                break
            objects.append(obj)
        if ok:
            print(
                f"[build_cpp] CUDA kernels built "
                f"(nvcc + MSVC toolset {toolset})"
            )
            return objects
    return []


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
    extra_objects: list[str] = []
    if cuda_home is not None:
        libraries.extend(["cudart", "cublas"])
        library_dirs.append(str(cuda_home / "lib" / "x64"))
        include_dirs.append(str(cuda_home / "include"))
        define_macros.append(("XINGCHENG_CUDA", "1"))
        print(f"[build_cpp] CUDA bridge enabled ({cuda_home})")
        kernel_objs = _compile_cuda_kernels(
            cuda_home, DIST_NATIVE / ".cu-build"
        )
        if kernel_objs:
            extra_objects.extend(str(o) for o in kernel_objs)
            define_macros.append(("XINGCHENG_CUDA_KERNELS", "1"))
    else:
        print("[build_cpp] CUDA toolkit not found — building CPU-only")

    return Extension(
        "_xingcheng_inference",
        [str(path) for path in (*CPP_SOURCES, *C_SOURCES)],
        include_dirs=include_dirs,
        define_macros=define_macros,
        libraries=libraries,
        library_dirs=library_dirs,
        extra_objects=extra_objects,
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
