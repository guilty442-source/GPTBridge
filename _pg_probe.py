import psycopg

ADMIN = "postgresql://postgres:uUJiXqgO1oiTorXFuoAdYU2UoIvzPA0K@127.0.0.1:5432/gptbridge"
c = psycopg.connect(ADMIN, autocommit=True)
for r in c.execute("SELECT rolname FROM pg_roles WHERE rolname LIKE 'gptbridge%' ORDER BY 1"):
    print("role:", r[0])
print("--- members ---")
for r in c.execute(
    "SELECT m.rolname member, g.rolname grp FROM pg_auth_members am "
    "JOIN pg_roles m ON m.oid=am.member JOIN pg_roles g ON g.oid=am.roleid ORDER BY 1,2"
):
    print(r[0], "->", r[1])
print("--- dbs ---")
for r in c.execute("SELECT datname FROM pg_database WHERE datname LIKE 'gptbridge%'"):
    print(r[0])
