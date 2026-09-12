"""Vaultly formal-mode integration diagnostic."""

from pathlib import Path

def main() -> None:
    workspace = Path(__file__).resolve().parent.parent
    print("Vaultly 2.3.1 使用主程式常駐服務與專屬 Edge 登入工作階段。")
    print(f"專屬模組資料夾：{workspace}")
    print("請從主系統啟動獨立下載中心。")

if __name__ == "__main__":
    main()
