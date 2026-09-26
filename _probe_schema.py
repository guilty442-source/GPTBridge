import sys, uuid
sys.path.insert(0, "shared-layer/src")
from shared_layer.security.dsn_policy import resolve_dsn, DsnPurpose
import psycopg
conn = psycopg.connect(resolve_dsn(DsnPurpose.RUNTIME).dsn, autocommit=True)
name = "xtest_probe_" + uuid.uuid4().hex[:8]
try:
    conn.execute('CREATE SCHEMA "' + name + '"')
    conn.execute('CREATE TABLE "' + name + '".probe (id int primary key)')
    print("create schema OK:", name)
finally:
    conn.execute('DROP SCHEMA IF EXISTS "' + name + '" CASCADE')
    print("dropped")
