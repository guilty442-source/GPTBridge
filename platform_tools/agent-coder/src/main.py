"""System Rescue Tool standalone application entry."""

from __future__ import annotations

from pathlib import Path


def main() -> None:
    workspace = Path(__file__).resolve().parent.parent
    print("System Rescue Tool is registered as a standalone GPTBridge application.")
    print(f"Project folder: {workspace}")
    print("Open System Rescue Tool from the Applications screen.")


if __name__ == "__main__":
    main()
