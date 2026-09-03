"""executors — 受治理執行器（決策層以外之實際執行處）。

一切執行委派受治理執行器（A5/E2）。本層每個執行器皆持有一個
「權限目標」token；唯有權限主宰明示準予該目標後才可執行（A10）。
任何執行器不得自我準予（A7）。

全部實作僅使用 Python 標準函式庫（A35/A37）。
"""

from __future__ import annotations

from ..governance.delegation import delegation_registry


def register_all() -> tuple[str, ...]:
    from .data_executors import bindings as data_bindings
    from .integration_executors import bindings as integration_bindings
    from .maintenance_executors import bindings as maintenance_bindings
    from .permission_executors import bindings as permission_bindings
    from .resource_executors import bindings as resource_bindings
    from .runtime_executors import bindings as runtime_bindings
    from .system_executors import bindings as system_bindings

    groups = (
        system_bindings,
        permission_bindings,
        runtime_bindings,
        maintenance_bindings,
        resource_bindings,
        data_bindings,
        integration_bindings,
    )
    for builder in groups:
        for binding in builder():
            delegation_registry.register(binding)
    return delegation_registry.list()


__all__ = ["register_all"]