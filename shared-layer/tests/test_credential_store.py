"""G89: governed credential store + DSN resolution tests."""
from __future__ import annotations

import sys
from pathlib import Path

_p = str(Path(__file__).resolve().parents[1] / "src")
if _p not in sys.path:
    sys.path.insert(0, _p)

import pytest

from shared_layer.security import credential_store, dsn_policy
from shared_layer.security.dsn_policy import DsnPolicyError, DsnPurpose

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="Windows Credential Manager only"
)

_SUFFIX = "test/g89-probe"


@pytest.fixture()
def stored_secret():
    credential_store.delete_secret(_SUFFIX)
    yield
    credential_store.delete_secret(_SUFFIX)


def test_store_read_delete_roundtrip(stored_secret) -> None:
    secret = "postgresql://u:p@127.0.0.1:5432/db"
    target = credential_store.store_secret(_SUFFIX, secret)
    assert target == credential_store.CREDENTIAL_TARGET_PREFIX + _SUFFIX
    assert credential_store.read_secret(_SUFFIX) == secret
    assert credential_store.delete_secret(_SUFFIX) is True
    assert credential_store.read_secret(_SUFFIX) is None
    assert credential_store.delete_secret(_SUFFIX) is False


def test_credman_reference_resolution(stored_secret) -> None:
    secret = "postgresql://u:p@127.0.0.1:5432/db2"
    target = credential_store.store_secret(_SUFFIX, secret)
    ref = f"credman:{target}"
    assert credential_store.is_credential_reference(ref)
    assert credential_store.resolve_credential_reference(ref) == secret


def test_resolve_dsn_via_credman_reference(stored_secret) -> None:
    secret = "postgresql://u:p@127.0.0.1:5432/db3"
    target = credential_store.store_secret(_SUFFIX, secret)
    env = {"GPTBRIDGE_POSTGRES_DSN": f"credman:{target}"}
    binding = dsn_policy.resolve_dsn(DsnPurpose.RUNTIME, environ=env)
    assert binding.dsn == secret
    assert binding.env_name == "GPTBRIDGE_POSTGRES_DSN"


def test_resolve_dsn_canonical_store_lookup(stored_secret) -> None:
    secret = "postgresql://u:p@127.0.0.1:5432/db4"
    suffix = credential_store.DSN_TARGET_TEMPLATE.format(purpose="runtime")
    prior = credential_store.read_secret(suffix)
    try:
        credential_store.store_secret(suffix, secret)
        # empty environ disables the env-var path so the canonical store
        # lookup must answer.
        binding = dsn_policy.resolve_dsn(DsnPurpose.RUNTIME, environ={})
        assert binding.dsn == secret
        assert binding.env_name.startswith("credman:")
    finally:
        credential_store.delete_secret(suffix)
        if prior:
            credential_store.store_secret(suffix, prior)


def test_resolve_dsn_unresolved_reference_fails_closed() -> None:
    env = {"GPTBRIDGE_POSTGRES_DSN": "credman:GPTBridge/test/nonexistent"}
    with pytest.raises(DsnPolicyError, match="UNRESOLVED"):
        dsn_policy.resolve_dsn(DsnPurpose.RUNTIME, environ=env)


def test_resolve_dsn_plain_env_still_works() -> None:
    env = {"GPTBRIDGE_POSTGRES_DSN": "postgresql://u:p@h/db"}
    binding = dsn_policy.resolve_dsn(DsnPurpose.RUNTIME, environ=env)
    assert binding.dsn == "postgresql://u:p@h/db"
