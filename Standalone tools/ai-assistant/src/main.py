"""AI Assistant standalone application entry."""

from __future__ import annotations

from pathlib import Path
import sys


SERVICES_ROOT = Path(__file__).resolve().parent / "backend" / "services"
if str(SERVICES_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICES_ROOT))

from investment_network_policy import (
    install_investment_manager_network_policy,
    uninstall_investment_manager_network_policy,
)


def _with_network_policy(function):
    def wrapped() -> None:
        install_investment_manager_network_policy()
        try:
            function()
        finally:
            uninstall_investment_manager_network_policy()

    return wrapped


@_with_network_policy
def main() -> None:
    install_investment_manager_network_policy()
    workspace = Path(__file__).resolve().parent.parent
    print("AI Investment Manager is registered as a standalone GPTBridge application.")
    print(f"Project folder: {workspace}")
    print("Open 投資管家 from the Applications screen.")
    uninstall_investment_manager_network_policy()


if __name__ == "__main__":
    main()
