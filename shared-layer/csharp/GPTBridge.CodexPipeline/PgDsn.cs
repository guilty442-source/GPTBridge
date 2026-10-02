using Npgsql;

namespace GPTBridge.CodexPipeline;

/// <summary>
/// DSN resolution and shared constants for the PostgreSQL Codex authority.
///
/// Fail-closed boundary (parity with codex_postgresql_dsn.py): both entry
/// points throw unless the corresponding environment variable is set —
/// no silent fallback to another engine or to superuser credentials.
/// </summary>
internal static class PgDsn
{
    public const string CodexSchema = "gptbridge_codex";
    public const string AuthorityUri = "postgresql://local/gptbridge_codex";

    public static readonly System.Text.RegularExpressions.Regex Identifier =
        new("^[A-Za-z_][A-Za-z0-9_]*$",
            System.Text.RegularExpressions.RegexOptions.Compiled);

    public static string RuntimeDsn()
    {
        var value = Environment.GetEnvironmentVariable(
            "GPTBRIDGE_POSTGRES_DSN")?.Trim() ?? "";
        if (value.Length == 0)
            throw new InvalidOperationException("GPTBRIDGE_POSTGRES_DSN_REQUIRED");
        return value;
    }

    public static string AdminDsn()
    {
        var value = Environment.GetEnvironmentVariable(
            "GPTBRIDGE_POSTGRES_ADMIN_DSN")?.Trim() ?? "";
        if (value.Length == 0)
            throw new InvalidOperationException(
                "GPTBRIDGE_POSTGRES_ADMIN_DSN_REQUIRED");
        return value;
    }

    /// <summary>Normalize the governed DSN into Npgsql keyword form.
    /// The Python oracle uses psycopg (libpq), which accepts both
    /// ``postgresql://`` URIs and ``key=value`` pairs with libpq names
    /// (``dbname``/``user``) that Npgsql rejects — translate both into
    /// canonical Npgsql keywords.</summary>
    internal static string Normalize(string dsn)
    {
        var value = dsn.Trim();
        if (value.StartsWith("postgresql://", StringComparison.Ordinal)
            || value.StartsWith("postgres://", StringComparison.Ordinal))
        {
            var uri = new Uri(value);
            var builder = new NpgsqlConnectionStringBuilder
            {
                Host = uri.Host,
                Database = uri.AbsolutePath.TrimStart('/'),
            };
            if (uri.Port > 0)
                builder.Port = uri.Port;
            var userInfo = uri.UserInfo;
            if (userInfo.Length > 0)
            {
                var split = userInfo.Split(':', 2);
                builder.Username = Uri.UnescapeDataString(split[0]);
                if (split.Length > 1)
                    builder.Password =
                        Uri.UnescapeDataString(split[1]);
            }
            return builder.ConnectionString;
        }
        // Npgsql emits semicolon-delimited strings when an executor
        // selects a scratch database. Accept that canonical form as well
        // as libpq inputs; never reinterpret its quoted password fields.
        if (value.Contains(';'))
        {
            try { return new NpgsqlConnectionStringBuilder(value).ConnectionString; }
            catch (ArgumentException) { /* may be a libpq quoted value */ }
        }
        // libpq keyword form: "dbname=x user=y password=z host=h port=p"
        var mapped = new NpgsqlConnectionStringBuilder();
        foreach (var pair in SplitPairs(value))
        {
            var eq = pair.IndexOf('=');
            if (eq <= 0)
                throw new InvalidOperationException(
                    "POSTGRES_DSN_MALFORMED");
            var key = pair[..eq].Trim().ToLowerInvariant();
            var val = pair[(eq + 1)..].Trim()
                .Trim('\'').Replace("\\'", "'").Replace("\\\\", "\\");
            switch (key)
            {
                case "dbname": mapped.Database = val; break;
                case "database": mapped.Database = val; break;
                case "user": case "username": case "uid":
                    mapped.Username = val; break;
                case "password": case "pwd":
                    mapped.Password = val; break;
                case "host": case "server":
                    mapped.Host = val; break;
                case "port":
                    mapped.Port = int.Parse(val,
                        System.Globalization.CultureInfo
                            .InvariantCulture);
                    break;
                case "sslmode":
                    mapped.SslMode = (Npgsql.SslMode)Enum.Parse(
                        typeof(Npgsql.SslMode), val, true); break;
                default:
                    mapped[key] = val; break;
            }
        }
        return mapped.ConnectionString;
    }

    private static IEnumerable<string> SplitPairs(string dsn)
    {
        var current = new System.Text.StringBuilder();
        var inQuote = false;
        for (var i = 0; i < dsn.Length; i++)
        {
            var c = dsn[i];
            if (c == '\\' && i + 1 < dsn.Length)
            {
                current.Append(c).Append(dsn[++i]);
                continue;
            }
            if (c == '\'')
            {
                inQuote = !inQuote;
                current.Append(c);
                continue;
            }
            if (char.IsWhiteSpace(c) && !inQuote)
            {
                if (current.Length > 0)
                {
                    yield return current.ToString();
                    current.Clear();
                }
                continue;
            }
            current.Append(c);
        }
        if (current.Length > 0)
            yield return current.ToString();
    }

    /// <summary>Admin connection — the only write path (autocommit).</summary>
    public static NpgsqlConnection Admin()
    {
        var connection = new NpgsqlConnection(Normalize(AdminDsn()));
        connection.Open();
        return connection;
    }

    /// <summary>Runtime read-only connection to the live authority.</summary>
    public static NpgsqlConnection Readonly()
    {
        var connection = new NpgsqlConnection(Normalize(RuntimeDsn()));
        connection.Open();
        return connection;
    }
}
