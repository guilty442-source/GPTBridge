"""AI Assistant standalone application entry."""

from __future__ import annotations

from pathlib import Path
import sys


SERVICES_ROOT = Path(__file__).resolve().parent / "backend" / "services"
if str(SERVICES_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICES_ROOT))

from investment_network_policy import install_investment_manager_network_policy

install_investment_manager_network_policy()

def main() -> None:
    workspace = Path(__file__).resolve().parent.parent
    print("AI Investment Manager is registered as a standalone GPTBridge application.")
    print(f"Project folder: {workspace}")
    print("Open AI投資管家 from the Applications screen.")


if __name__ == "__main__":
    main()
