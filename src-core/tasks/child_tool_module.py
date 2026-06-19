from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType


def _module_key(package_name: str, source: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z_]+", "_", package_name).strip("_")
    if not normalized:
        normalized = "tool"
    if normalized[0].isdigit():
        normalized = f"tool_{normalized}"
    return f"_gptbridge_{source}_{normalized.lower()}"


def _ensure_package(package_key: str, package_dir: Path) -> None:
    package = sys.modules.get(package_key)
    if package is None:
        package = ModuleType(package_key)
        package.__path__ = [str(package_dir)]  # type: ignore[attr-defined]
        sys.modules[package_key] = package
        return

    package_path = getattr(package, "__path__", None)
    if package_path is not None and str(package_dir) not in package_path:
        package_path.append(str(package_dir))


def _load_module_from_package_dir(
    package_dir: Path,
    package_name: str,
    module_name: str,
    public_module_name: str | None,
    source: str,
) -> ModuleType:
    module_file = package_dir / f"{module_name}.py"
    if not module_file.exists():
        raise ModuleNotFoundError(f"tool module not found: {module_file}")

    package_key = _module_key(package_name, source)
    _ensure_package(package_key, package_dir)

    module_key = f"{package_key}.{module_name}"
    cached = sys.modules.get(module_key)
    if cached is not None:
        if public_module_name:
            sys.modules[public_module_name] = cached
        return cached

    spec = importlib.util.spec_from_file_location(module_key, module_file)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load platform tool module: {module_file}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_key] = module
    spec.loader.exec_module(module)

    if public_module_name:
        sys.modules[public_module_name] = module
    return module


def load_platform_tool_module(
    project_root: Path,
    tool_dir_name: str,
    package_name: str,
    module_name: str,
    public_module_name: str | None = None,
) -> ModuleType:
    package_dir = (
        project_root
        / "platform_tools"
        / tool_dir_name
        / "src"
        / "backend"
        / "services"
        / package_name
    )
    return _load_module_from_package_dir(
        package_dir,
        package_name,
        module_name,
        public_module_name,
        "platform",
    )


def load_sibling_platform_tool_module(
    tool_dir_name: str,
    package_name: str,
    public_module_name: str,
    current_file: str,
) -> ModuleType:
    project_root = Path(current_file).resolve().parents[3]
    module_name = Path(current_file).stem
    module = load_platform_tool_module(
        project_root,
        tool_dir_name,
        package_name,
        module_name,
        public_module_name,
    )
    return module
