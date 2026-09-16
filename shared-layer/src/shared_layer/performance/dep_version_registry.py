"""Dependency Version Registry — approved version/source/hash/consumer scope.

A file-based registry of approved third-party dependencies with:
    - approved version
    - source (PyPI, npm, etc.)
    - hash (integrity verification)
    - consumer scope (which components/languages consume this)
    - classification (RUNTIME_REQUIRED, BUILD_ONLY, etc.)

The registry is the single source of truth for approved dependency
versions.  No dependency upgrade proceeds without registry approval.
No blanket "upgrade to latest" — each version is explicitly approved.

Codex basis:
    A198 — third-party boundary
    A10/E10 — explicit-allowlist
    A46/E22 — Audit: mandatory-ledger
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .dep_classifier import DependencyClass


REGISTRY_VERSION = "1.0"


@dataclass(frozen=True)
class ApprovedDependency:
    """One approved third-party dependency."""
    dependency_id: str        # e.g. "python:psycopg" or "ts:react"
    name: str                  # package name
    language: str              # "python", "typescript", "c", "cpp", "sql"
    approved_version: str      # approved version (exact or range)
    version_spec: str          # version spec from requirements/package.json
    source: str                # "pypi", "npm", "system", "local"
    source_hash: str           # hash for integrity verification
    consumer_scope: str        # "runtime", "build", "test", "optional", "native"
    classification: str        # DependencyClass value
    approved_at: str           # ISO timestamp
    approved_by: str            # who approved
    notes: str = ""
    # Minimum release age in days (supply chain safety)
    min_release_age_days: int = 7


class DependencyVersionRegistry:
    """File-based third-party dependency version registry.

    The registry is a JSON file containing approved dependencies.
    Updates require explicit approval — no auto-upgrade to latest.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._save({})

    def _load(self) -> dict[str, Any]:
        data = self.path.read_text(encoding="utf-8")
        return json.loads(data) if data.strip() else {}

    def _save(self, data: dict[str, Any]) -> None:
        self.path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )

    def register(self, dep: ApprovedDependency) -> str:
        """Register or update an approved dependency."""
        data = self._load()
        data[dep.dependency_id] = asdict(dep)
        self._save(data)
        return dep.dependency_id

    def get(self, dependency_id: str) -> ApprovedDependency | None:
        """Get an approved dependency by ID."""
        data = self._load()
        d = data.get(dependency_id)
        if d is None:
            return None
        return ApprovedDependency(**d)

    def all_dependencies(self) -> list[ApprovedDependency]:
        """Get all registered dependencies."""
        data = self._load()
        return [ApprovedDependency(**d) for d in data.values()]

    def by_language(self, language: str) -> list[ApprovedDependency]:
        """Get all dependencies for a language."""
        return [d for d in self.all_dependencies() if d.language == language]

    def by_classification(self, classification: str) -> list[ApprovedDependency]:
        """Get all dependencies by classification."""
        return [
            d for d in self.all_dependencies()
            if d.classification == classification
        ]

    def is_approved(self, dependency_id: str, version: str) -> bool:
        """Check if a specific version is approved."""
        dep = self.get(dependency_id)
        if dep is None:
            return False
        # Check if version matches approved version or range
        return _version_matches(version, dep.approved_version)

    def find_duplicates(self) -> dict[str, list[ApprovedDependency]]:
        """Find dependencies with duplicate names (different versions/sources).

        Returns a dict mapping name to list of duplicate entries.
        """
        data = self._load()
        by_name: dict[str, list[ApprovedDependency]] = {}
        for d in data.values():
            dep = ApprovedDependency(**d)
            by_name.setdefault(dep.name, []).append(dep)

        return {
            name: deps for name, deps in by_name.items()
            if len(deps) > 1
        }

    def find_unregistered(
        self,
        declared_deps: dict[str, str],
        language: str,
    ) -> list[str]:
        """Find declared dependencies not in the registry.

        ``declared_deps`` maps dependency name to version spec.
        Returns a list of dependency names that are not registered.
        """
        data = self._load()
        registered_names = {
            d["name"] for d in data.values()
            if d.get("language") == language
        }
        return [
            name for name in declared_deps
            if name not in registered_names
        ]


def _version_matches(version: str, approved: str) -> bool:
    """Check if a version matches an approved version spec.

    Supports:
        - Exact version: "1.2.3" matches only "1.2.3"
        - Range: ">=1.2,<2" matches "1.5.0"
        - Prefix: "1.2" matches "1.2.0", "1.2.1"
    """
    # Exact match
    if version == approved:
        return True

    # Range match (simplified)
    if approved.startswith(">=") or approved.startswith("<") or approved.startswith("=="):
        # Parse range spec (simplified — doesn't handle all PEP 440)
        try:
            parts = approved.replace(" ", "").split(",")
            for part in parts:
                if part.startswith(">="):
                    min_ver = part[2:]
                    if not _ver_gte(version, min_ver):
                        return False
                elif part.startswith("<"):
                    max_ver = part[1:]
                    if not _ver_lt(version, max_ver):
                        return False
                elif part.startswith("=="):
                    if version != part[2:]:
                        return False
            return True
        except Exception:
            return False

    # Prefix match
    if version.startswith(approved):
        return True

    return False


def _parse_version(v: str) -> tuple[int, ...]:
    """Parse a version string into a tuple of ints."""
    parts = []
    for part in v.split("."):
        # Handle suffixes like "1.2.3rc1"
        num = ""
        for ch in part:
            if ch.isdigit():
                num += ch
            else:
                break
        parts.append(int(num) if num else 0)
    return tuple(parts)


def _ver_gte(a: str, b: str) -> bool:
    """Check if version a >= version b."""
    pa, pb = _parse_version(a), _parse_version(b)
    # Pad to same length
    while len(pa) < len(pb):
        pa = pa + (0,)
    while len(pb) < len(pa):
        pb = pb + (0,)
    return pa >= pb


def _ver_lt(a: str, b: str) -> bool:
    """Check if version a < version b."""
    pa, pb = _parse_version(a), _parse_version(b)
    while len(pa) < len(pb):
        pa = pa + (0,)
    while len(pb) < len(pa):
        pb = pb + (0,)
    return pa < pb


def compute_source_hash(content: bytes) -> str:
    """Compute SHA-256 hash of source content for integrity verification."""
    return hashlib.sha256(content).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Pre-approved dependencies (seeded from existing requirements)
# ---------------------------------------------------------------------------

def seed_default_registry(registry: DependencyVersionRegistry) -> None:
    """Seed the registry with pre-approved dependencies from existing config.

    These are the dependencies already declared in requirements.txt,
    pyproject.toml, and package.json.  They are pre-approved at their
    current version specs.
    """
    timestamp = now_iso()

    # Python runtime
    python_runtime = [
        ("psutil", ">=6.0,<8", "pypi", "runtime", DependencyClass.RUNTIME_REQUIRED.value),
        ("psycopg", ">=3.2,<4", "pypi", "runtime", DependencyClass.RUNTIME_REQUIRED.value),
        ("qdrant-client", ">=1.12,<2", "pypi", "runtime", DependencyClass.RUNTIME_REQUIRED.value),
        ("websockets", "==16.1", "pypi", "runtime", DependencyClass.RUNTIME_REQUIRED.value),
    ]

    # Python build
    python_build = [
        ("pybind11", ">=2.13,<3", "pypi", "native", DependencyClass.NATIVE_TOOLCHAIN.value),
        ("pyinstaller", "==6.22.2", "pypi", "build", DependencyClass.BUILD_ONLY.value),
        ("setuptools", ">=75,<80", "pypi", "build", DependencyClass.BUILD_ONLY.value),
    ]

    # Python test
    python_test = [
        ("pytest", "==9.1.1", "pypi", "test", DependencyClass.TEST_ONLY.value),
        ("pytest-asyncio", "==1.4.0", "pypi", "test", DependencyClass.TEST_ONLY.value),
    ]

    # Python optional (heavy, lazy-loaded)
    python_optional = [
        ("numpy", ">=1.26,<3", "pypi", "optional", DependencyClass.OPTIONAL.value),
        ("torch", ">=2.2,<3", "pypi", "optional", DependencyClass.OPTIONAL.value),
        ("sentence-transformers", ">=4.1.0", "pypi", "optional", DependencyClass.OPTIONAL.value),
        ("Pillow", ">=10.0,<14", "pypi", "optional", DependencyClass.OPTIONAL.value),
        ("beautifulsoup4", ">=4.12,<5", "pypi", "optional", DependencyClass.OPTIONAL.value),
        ("imageio-ffmpeg", "==0.6.0", "pypi", "optional", DependencyClass.OPTIONAL.value),
        ("tiktoken", "", "pypi", "optional", DependencyClass.OPTIONAL.value),
        ("openai", "", "pypi", "optional", DependencyClass.OPTIONAL.value),
        ("httpx", "", "pypi", "optional", DependencyClass.OPTIONAL.value),
    ]

    # TypeScript runtime
    ts_runtime = [
        ("react", "^19.2.0", "npm", "runtime", DependencyClass.RUNTIME_REQUIRED.value),
        ("react-dom", "^19.2.0", "npm", "runtime", DependencyClass.RUNTIME_REQUIRED.value),
    ]

    # TypeScript build
    ts_build = [
        ("electron", "^39.2.4", "npm", "build", DependencyClass.BUILD_ONLY.value),
        ("electron-builder", "^26.15.3", "npm", "build", DependencyClass.BUILD_ONLY.value),
        ("vite", "^7.3.3", "npm", "build", DependencyClass.BUILD_ONLY.value),
        ("@vitejs/plugin-react", "^5.1.1", "npm", "build", DependencyClass.BUILD_ONLY.value),
        ("typescript", "^5.9.3", "npm", "build", DependencyClass.BUILD_ONLY.value),
        ("ts-node", "^10.9.2", "npm", "build", DependencyClass.BUILD_ONLY.value),
        ("madge", "^8.0.0", "npm", "build", DependencyClass.BUILD_ONLY.value),
    ]

    # TypeScript type-only
    ts_types = [
        ("@types/node", "^22.7.7", "npm", "test", DependencyClass.TEST_ONLY.value),
        ("@types/react", "^19.2.7", "npm", "test", DependencyClass.TEST_ONLY.value),
        ("@types/react-dom", "^19.2.3", "npm", "test", DependencyClass.TEST_ONLY.value),
    ]

    all_deps = (
        [("python:" + name, name, "python", v, s, c) for name, v, s, _, c in python_runtime + python_build + python_test + python_optional]
        + [("typescript:" + name, name, "typescript", v, s, c) for name, v, s, _, c in ts_runtime + ts_build + ts_types]
    )

    for dep_id, name, lang, version_spec, source, classification in all_deps:
        registry.register(ApprovedDependency(
            dependency_id=dep_id,
            name=name,
            language=lang,
            approved_version=version_spec,
            version_spec=version_spec,
            source=source,
            source_hash="",  # to be filled when verifying
            consumer_scope=classification.lower(),
            classification=classification,
            approved_at=timestamp,
            approved_by="seed_default_registry",
            notes="pre-approved from existing requirements/package.json",
        ))


__all__ = [
    "REGISTRY_VERSION",
    "ApprovedDependency",
    "DependencyVersionRegistry",
    "compute_source_hash",
    "seed_default_registry",
    "now_iso",
]
