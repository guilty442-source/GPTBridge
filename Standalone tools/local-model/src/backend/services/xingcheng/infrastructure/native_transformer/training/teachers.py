"""星澄教師模型註冊表（Phase 6：本地教師蒸餾）。

定位：

- 星澄 = 學生與原生模型主體；
- 教師模型只產生**候選答案**——須經驗證／品質閘門篩選後才進入
  訓練資料集，且永遠標記為教師產出，不得冒充星澄訓練成果。

邊界：

- 教師端點只允許 loopback（``127.0.0.1`` / ``localhost`` / ``::1``），
  指向外部位址一律 fail-closed（``TEACHER_ENDPOINT_FORBIDDEN``）——
  模型核心不產生對外網路存取。
- 教師清單由治理設定提供；本表只定義角色與預設建議模型族。
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Iterable, Mapping
from urllib.parse import urlparse

TEACHER_REGISTRY_VERSION = "star-teacher-roles/v1"


@dataclass(frozen=True)
class TeacherRole:
    """單一教師角色。"""

    role: str
    duty: str
    preferred_families: tuple[str, ...]


TEACHER_ROLES: dict[str, TeacherRole] = {
    "language": TeacherRole(
        role="language",
        duty="語言及綜合能力教師",
        preferred_families=("qwen", "gemma"),
    ),
    "reasoning": TeacherRole(
        role="reasoning",
        duty="推理教師",
        preferred_families=("deepseek", "qwen"),
    ),
    "coding": TeacherRole(
        role="coding",
        duty="程式能力教師",
        preferred_families=("qwen-coder", "deepseek-coder", "codestral"),
    ),
}


def teacher_role(role: str) -> TeacherRole:
    key = str(role).strip().lower()
    if key not in TEACHER_ROLES:
        raise ValueError(f"TEACHER_ROLE_UNKNOWN:{role}")
    return TEACHER_ROLES[key]


def resolve_teacher_model(
    role: str,
    available_models: Iterable[str],
) -> str | None:
    """依角色偏好族序在已安裝模型清單中挑教師；找不到回 ``None``。"""
    spec = teacher_role(role)
    installed = [str(name) for name in available_models]
    lowered = [(name.casefold(), name) for name in installed]
    for family in spec.preferred_families:
        needle = family.casefold()
        for folded, original in lowered:
            if needle in folded:
                return original
    return None


def assert_loopback_endpoint(endpoint: str) -> str:
    """驗證教師端點為 loopback；回傳正規化 URL，否則拋例外。"""
    parsed = urlparse(str(endpoint).strip())
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"TEACHER_ENDPOINT_FORBIDDEN:{endpoint}")
    host = parsed.hostname or ""
    if host.casefold() == "localhost":
        return endpoint
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            address = ipaddress.ip_address(socket.gethostbyname(host))
        except (OSError, ValueError):
            raise ValueError(f"TEACHER_ENDPOINT_FORBIDDEN:{endpoint}")
    if not address.is_loopback:
        raise ValueError(f"TEACHER_ENDPOINT_FORBIDDEN:{endpoint}")
    return endpoint


def assign_teachers(
    available_models: Iterable[str],
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str | None]:
    """為每個教師角色挑模型；``overrides`` 可強制指定。"""
    overrides = overrides or {}
    assigned: dict[str, str | None] = {}
    for role in TEACHER_ROLES:
        override = overrides.get(role)
        assigned[role] = (
            str(override) if override else resolve_teacher_model(role, available_models)
        )
    return assigned


__all__ = [
    "TEACHER_REGISTRY_VERSION",
    "TEACHER_ROLES",
    "TeacherRole",
    "assign_teachers",
    "assert_loopback_endpoint",
    "resolve_teacher_model",
    "teacher_role",
]
