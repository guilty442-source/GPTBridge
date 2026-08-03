import json
import re
import sys
from pathlib import Path

# Directories and paths
ROOT_DIR = Path(__file__).parent.parent
PACKAGE_JSON = ROOT_DIR / "package.json"
VERSION_FILE = ROOT_DIR / "VERSION"
GOVERNANCE_MANIFEST = ROOT_DIR.parent / "governance_rule" / "manifest.json"
SEMVER_PATTERN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
LOCKED_VERSION = "1.0.0"

def get_central_version():
    try:
        with open(PACKAGE_JSON, 'r', encoding='utf-8') as f:
            data = json.load(f)
            version = str(data.get("version", "")).strip()
            if not SEMVER_PATTERN.fullmatch(version):
                raise ValueError(f"package.json version is not valid SemVer: {version}")
            if version != LOCKED_VERSION:
                raise ValueError(
                    f"product version is locked to {LOCKED_VERSION}; found {version}"
                )
            return version
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
    try:
        with open(GOVERNANCE_MANIFEST, 'r', encoding='utf-8') as f:
            governance_version = str(json.load(f).get("version", "")).strip()
        if governance_version != version:
            raise ValueError(
                "governance_rule is the highest authority and cannot be "
                "implicitly rewritten by product version synchronization"
            )
        print(f"Verified governance authority version: {governance_version}")
    except Exception as e:
        print(f"Error verifying governance manifest: {e}")
        raise

def check_versions(version):
    expected_version_file = f"GPTBridge_W11_Stable_Baseline_v{version}"
    actual_version_file = VERSION_FILE.read_text(encoding="utf-8").strip()
    governance_manifest = json.loads(
        GOVERNANCE_MANIFEST.read_text(encoding="utf-8")
    )
    governance_version = str(governance_manifest.get("version", "")).strip()
    errors = []
    if actual_version_file != expected_version_file:
        errors.append(f"VERSION mismatch: {actual_version_file}")
    if governance_version != version:
        errors.append(f"governance version mismatch: {governance_version}")
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return False
    print(f"Version check passed: {version}")
    return True

def main():
    if len(sys.argv) > 1 and sys.argv[1] == "check":
        if not check_versions(get_central_version()):
            sys.exit(1)
    elif len(sys.argv) > 1 and sys.argv[1] == "sync":
        central_version = get_central_version()
        print(f"Central version found in package.json: {central_version}")
        update_version_file(central_version)
        update_governance_version(central_version)
        print("Version synchronization complete.")
    elif len(sys.argv) > 2 and sys.argv[1] == "set":
        new_version = sys.argv[2]
        if not SEMVER_PATTERN.fullmatch(new_version):
            print(f"Error: version must use MAJOR.MINOR.PATCH SemVer: {new_version}")
            sys.exit(1)
        if new_version != LOCKED_VERSION:
            print(f"Error: product version is locked to {LOCKED_VERSION}")
            sys.exit(1)
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
        print("  python sync_version.py check     # Verify all version authorities")
        print("  python sync_version.py sync      # Sync versions across files using package.json as source")
        print("  python sync_version.py set <ver> # Set a new version across all files")

if __name__ == "__main__":
    main()
