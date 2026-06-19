"""Vaultly formal-mode integration diagnostic."""

from pathlib import Path

def main() -> None:
    workspace = Path(__file__).resolve().parent.parent
    print("Vaultly 2.3.1 使用主程式常駐服務與專屬 Edge 登入工作階段。")
    print(f"專屬模組資料夾：{workspace}")
    print("請從程式庫啟動獨立下載中心，不需要開發模式或重新打包。")

if __name__ == "__main__":
    main()
