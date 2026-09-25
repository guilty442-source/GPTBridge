import os, psycopg
con = psycopg.connect(os.environ["GPTBRIDGE_POSTGRES_DSN"])
print("epoch:", con.execute(
    "select epoch, baseline_version, version_identity, history_head, status from gptbridge_codex.codex_version_epochs order by epoch desc limit 2").fetchall())
print("seal:", con.execute(
    "select version, version_identity, version_epoch, certification_state, history_head from gptbridge_codex.epoch_seal_manifest order by version_epoch desc limit 2").fetchall())
print("rev:", con.execute(
    "select sequence, version, entry_hash from gptbridge_codex.revision_history order by sequence desc limit 2").fetchall())
