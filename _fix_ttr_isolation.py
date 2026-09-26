from pathlib import Path

p = Path(r"Standalone tools/local-model/tests/test_transformer_training_repository.py")
d = p.read_text(encoding="utf-8")

# 1) imports: sqlite3 -> psycopg + locking helpers (dedupe double-import block)
d = d.replace("import sqlite3\nimport sys\nimport pytest", "import sys\nimport tempfile\nimport time\n\nimport psycopg\nimport pytest", 1)
d = d.replace("import sqlite3\nimport sys\n\nimport pytest", "import sys\nimport tempfile\nimport time\n\nimport psycopg\nimport pytest", 1)
# fallback: plain single-line replacement for any remaining sqlite3 import
d = d.replace("import sqlite3\n", "")

# 2) raise expectations: sqlite3.IntegrityError -> psycopg.Error
d = d.replace("pytest.raises(sqlite3.IntegrityError", "pytest.raises(psycopg.Error")
assert "sqlite3" not in d, "sqlite3 still referenced"

# 3) rename stale test ids (sqlite -> generic) for honest labels
d = d.replace(
    "def test_dataset_snapshot_links_are_sqlite_immutable(",
    "def test_dataset_snapshot_links_are_immutable(",
)

# 4) shared-schema isolation: file-level cross-process lock + per-test truncate
anchor = "def _sha(value: str) -> str:"
assert anchor in d
fixture = '''
# ---------------------------------------------------------------------------
# Shared-schema isolation (A610): the repository now lives in the governed
# ``gptbridge_xingcheng`` PostgreSQL schema, so every test previously isolated
# by a private sqlite file must serialize against sibling xdist workers and
# start from empty training tables.  A host-wide lock file serializes both the
# DDL (CREATE OR REPLACE FUNCTION deadlocked concurrent workers on pg_proc)
# and the data; TRUNCATE restores the sqlite-era clean-slate semantics.
# ---------------------------------------------------------------------------

_TEST_LOCK_PATH = Path(tempfile.gettempdir()) / "gptbridge-ttr-tests.lock"

_ISOLATED_TABLES = (
    "transformer_training_dataset_example",
    "transformer_training_dataset",
    "transformer_training_job",
    "transformer_adapter_candidate",
    "transformer_adapter_evaluation",
    "transformer_adapter_release",
    "transformer_runtime_model_state",
    "transformer_training_audit_event",
)


@pytest.fixture(autouse=True)
def _isolated_training_tables(tmp_path: Path):
    import msvcrt
    import os

    fd = os.open(_TEST_LOCK_PATH, os.O_RDWR | os.O_CREAT)
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        deadline = time.monotonic() + 120
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() > deadline:
                    os.close(fd)
                    raise
                time.sleep(0.05)
        # Ensure DDL exists, then wipe training rows for a clean slate.
        repository = TransformerTrainingRepository(tmp_path)
        with repository._connect() as connection:
            connection.execute(
                "TRUNCATE " + ", ".join(_ISOLATED_TABLES) + " RESTART IDENTITY CASCADE"
            )
        yield
    finally:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        os.close(fd)


'''
d = d.replace(anchor, fixture + anchor, 1)
p.write_text(d, encoding="utf-8")
print("patched")
