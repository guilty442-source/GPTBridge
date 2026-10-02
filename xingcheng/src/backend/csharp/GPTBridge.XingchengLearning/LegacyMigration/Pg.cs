// LEGACY_MIGRATION_ONLY: historical comparison source; excluded from production build.
// Pg.cs — governed PostgreSQL access for the xingcheng training repository.
//
// Port of ``shared_layer.local.pg_adapter.connect`` semantics:
//   - runtime DSN from ``GPTBRIDGE_POSTGRES_DSN`` (fail-closed when absent —
//     never a silent local fallback);
//   - every connection is scoped ``SET search_path TO "<schema>", public``;
//   - autocommit=False callers get an explicit NpgsqlTransaction whose
//     commit/rollback maps to the Python context-manager semantics.

using Npgsql;

namespace GPTBridge.XingchengLearning;

internal sealed class PgUnavailable : Exception
{
    public PgUnavailable(string message) : base(message) { }
}

internal static class Pg
{
    public const string DefaultSchema = "gptbridge_xingcheng";

    public static string Schema =>
        Environment.GetEnvironmentVariable("XINGCHENG_SHARED_PG_SCHEMA") is { Length: > 0 } s
            ? s
            : DefaultSchema;

    public static string RuntimeDsn()
    {
        string? dsn = Environment.GetEnvironmentVariable("GPTBRIDGE_POSTGRES_DSN");
        if (string.IsNullOrWhiteSpace(dsn))
            throw new PgUnavailable("runtime DSN unavailable: GPTBRIDGE_POSTGRES_DSN is not set");
        return dsn;
    }

    /// <summary>Open a connection scoped to <paramref name="schema"/>.
    /// When <paramref name="autocommit"/> is false a transaction is opened;
    /// <see cref="PgScope.Commit"/> / <see cref="PgScope.Rollback"/> finish it
    /// and dispose returns the connection.</summary>
    /// <summary>Translate libpq-style keywords (the DSN format the retired
    /// pg_adapter consumed: ``dbname=… user=… host=…``) into Npgsql
    /// connection-string keywords. Already-Npgsql DSNs pass through
    /// unchanged.</summary>
    internal static string NormalizeDsn(string dsn)
    {
        var builder = new NpgsqlConnectionStringBuilder();
        foreach (string part in dsn.Split(' ',
                     StringSplitOptions.RemoveEmptyEntries))
        {
            int eq = part.IndexOf('=');
            if (eq <= 0) continue;
            string key = part[..eq].Trim().ToLowerInvariant();
            string value = part[(eq + 1)..].Trim().Trim('\'');
            switch (key)
            {
                case "dbname":
                case "database":
                    builder.Database = value; break;
                case "user":
                case "username":
                case "userid":
                    builder.Username = value; break;
                case "password":
                    builder.Password = value; break;
                case "host":
                    builder.Host = value; break;
                case "port":
                    builder.Port = int.Parse(value); break;
                case "sslmode":
                    builder.SslMode = value == "require"
                        ? SslMode.Require : SslMode.Prefer; break;
            }
        }
        return builder.ConnectionString;
    }

    public static PgScope Connect(string schema, bool autocommit = true)
    {
        var conn = new NpgsqlConnection(NormalizeDsn(RuntimeDsn()));
        conn.Open();
        using (var cmd = conn.CreateCommand())
        {
            cmd.CommandText = "SET search_path TO \"" + schema.Replace("\"", "") + "\", public";
            cmd.ExecuteNonQuery();
        }
        var scope = new PgScope(conn, schema);
        if (!autocommit)
            scope.BeginTransaction();
        return scope;
    }

    /// <summary>Connection + optional transaction with Python-style
    /// commit-on-success / rollback-on-exception disposal.</summary>
    internal sealed class PgScope : IDisposable
    {
        private readonly NpgsqlConnection _conn;
        private NpgsqlTransaction? _tx;
        private bool _ended;

        public PgScope(NpgsqlConnection conn, string schema)
        {
            _conn = conn;
            Schema = schema;
        }

        public string Schema { get; }
        public NpgsqlConnection Connection => _conn;
        public NpgsqlTransaction? Transaction => _tx;

        public void BeginTransaction() => _tx = _conn.BeginTransaction();

        public void Commit()
        {
            _tx?.Commit();
            _tx = null;
        }

        public void Rollback()
        {
            try { _tx?.Rollback(); } catch { /* connection closing anyway */ }
            _tx = null;
        }

        public NpgsqlCommand Cmd(string sql)
        {
            var cmd = _conn.CreateCommand();
            cmd.Transaction = _tx;
            cmd.CommandText = sql;
            return cmd;
        }

        /// <summary>Execute and return all rows as dictionaries.</summary>
        public List<Dictionary<string, object?>> Query(string sql, params object?[] args)
        {
            using var cmd = Cmd(sql);
            for (int i = 0; i < args.Length; i++)
                cmd.Parameters.AddWithValue(args[i] ?? DBNull.Value);
            using var reader = cmd.ExecuteReader();
            var rows = new List<Dictionary<string, object?>>();
            while (reader.Read())
            {
                var row = new Dictionary<string, object?>(StringComparer.Ordinal);
                for (int c = 0; c < reader.FieldCount; c++)
                    row[reader.GetName(c)] = reader.IsDBNull(c) ? null : reader.GetValue(c);
                rows.Add(row);
            }
            return rows;
        }

        public Dictionary<string, object?>? QueryOne(string sql, params object?[] args)
            => Query(sql, args).FirstOrDefault();

        public int Execute(string sql, params object?[] args)
        {
            using var cmd = Cmd(sql);
            for (int i = 0; i < args.Length; i++)
                cmd.Parameters.AddWithValue(args[i] ?? DBNull.Value);
            return cmd.ExecuteNonQuery();
        }

        public void ExecuteScript(string sql)
        {
            using var cmd = Cmd(sql);
            cmd.ExecuteNonQuery();
        }

        public void Dispose()
        {
            if (_ended) return;
            _ended = true;
            try
            {
                if (_tx != null)
                {
                    _tx.Rollback();
                    _tx = null;
                }
            }
            catch { /* rollback failure is non-fatal on dispose */ }
            _conn.Dispose();
        }
    }
}
