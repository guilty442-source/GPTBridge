import psycopg

RT = "dbname=gptbridge user=gptbridge_runtime password=3HOLcUi_fWAt44umB0UB5Y3_vS7Om8jc host=127.0.0.1 port=5432"
c = psycopg.connect(RT, autocommit=False)
# nonce consume
c.execute(
    "INSERT INTO gptbridge_transport.governance_used_nonces "
    "(namespace, actor, nonce, expires_at) VALUES ('t','a','n1',9999999999) ON CONFLICT DO NOTHING"
)
print("nonce insert ok")
row = c.execute(
    "INSERT INTO gptbridge_transport.governance_used_nonces "
    "(namespace, actor, nonce, expires_at) VALUES ('t','a','n1',9999999999) "
    "ON CONFLICT DO NOTHING RETURNING nonce"
).fetchone()
print("replay insert returned:", row)  # None = conflict = replay denied
# outbox
r = c.execute(
    "INSERT INTO gptbridge_transport.outbox_entity_revision (entity_id, revision) "
    "VALUES ('e1',1) ON CONFLICT (entity_id) DO UPDATE SET revision = outbox_entity_revision.revision + 1 "
    "RETURNING revision"
).fetchone()
print("revision:", r[0])
cur = c.execute(
    "INSERT INTO gptbridge_transport.outbox_event (entity_id, entity_type, operation, "
    "authoritative_revision, previous_revision, changed_field_allowlist, invalidation_keys, "
    "state_hash, backend_generation, release_id, contract_version, correlation_id, committed_at, recorded_at) "
    "VALUES ('e1','t','upsert',1,0,'[]','[]','h','g','r','v','c','now','now') RETURNING sequence"
)
print("outbox seq:", cur.fetchone())
c.rollback()
c.close()
print("RT_OK")
