import json
import re
import sys
from pathlib import Path

# Directories and paths
ROOT_DIR = Path(__file__).parent.parent
PACKAGE_JSON = ROOT_DIR / "package.json"
VERSION_FILE = ROOT_DIR / "VERSION"
GOVERNANCE_VERSION_MD = ROOT_DIR / "resources" / "governance" / "GOVERNANCE_VERSION.md"

def get_central_version():
    try:
        with open(PACKAGE_JSON, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data.get("version", "0.0.0")
    except Exception as e:
        print(f"Error reading package.json: {e}")
        sys.exit(1)

def update_version_file(version):
    # E.g. GPTBridge_W11_Stable_Baseline_v7.0.0
    # Keep the prefix but update the version part.
    prefix = "GPTBridge_W11_Stable_Baseline_v"
    try:
        with open(VERSION_FILE, 'w', encoding='utf-8') as f:
            f.write(f"{prefix}{version}\n")
        print(f"Updated {VERSION_FILE} to {prefix}{version}")
    except Exception as e:
        print(f"Error updating VERSION file: {e}")

def update_governance_version(version):
    if not GOVERNANCE_VERSION_MD.exists():
        print(f"Skipping {GOVERNANCE_VERSION_MD}, not found.")
        return
    try:
        with open(GOVERNANCE_VERSION_MD, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Replace "**Current Version**: v..." with the new version
        new_content = re.sub(
            r'\*\*Current Version\*\*: v[0-9a-zA-Z.-]+',
            f'**Current Version**: v{version}',
            content
        )
        
        with open(GOVERNANCE_VERSION_MD, 'w', encoding='utf-8') as f:
            f.write(new_content)
        print(f"Updated {GOVERNANCE_VERSION_MD} to v{version}")
    except Exception as e:
        print(f"Error updating GOVERNANCE_VERSION.md: {e}")

def main():
    if len(sys.argv) > 1 and sys.argv[1] == "sync":
        central_version = get_central_version()
        print(f"Central version found in package.json: {central_version}")
        update_version_file(central_version)
        update_governance_version(central_version)
        print("Version synchronization complete.")
    elif len(sys.argv) > 2 and sys.argv[1] == "set":
        new_version = sys.argv[2]
        # Update package.json
        try:
            with open(PACKAGE_JSON, 'r', encoding='utf-8') as f:
                data = json.load(f)
            data["version"] = new_version
            with open(PACKAGE_JSON, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
                f.write("\n")
            print(f"Updated package.json to {new_version}")
            
            update_version_file(new_version)
            update_governance_version(new_version)
            print("Version update and synchronization complete.")
        except Exception as e:
            print(f"Error updating version: {e}")
            sys.exit(1)
    else:
        print("Usage:")
        print("  python sync_version.py sync      # Sync versions across files using package.json as source")
        print("  python sync_version.py set <ver> # Set a new version across all files")

if __name__ == "__main__":
    main()
