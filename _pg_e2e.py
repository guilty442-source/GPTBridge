import sys, os, secrets, time
sys.path.insert(0, "main-system/src-core")
sys.path.insert(0, "shared-layer/src")
from pathlib import Path
os.environ.pop("GPTBRIDGE_NONCE_ENGINE", None)
os.environ.pop("GPTBRIDGE_OUTBOX_ENGINE", None)

from governance_rule.execution.authentication.auth_signing import _build_nonce_store
from governance_rule.permission_directory.directory_authority import directory_authority_snapshot
policy = directory_authority_snapshot().authenticator_policy if hasattr(directory_authority_snapshot(), "authenticator_policy") else None
