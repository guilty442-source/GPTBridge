using System.Text.Json.Nodes;
using GPTBridge.CodexPipeline;

internal static partial class Program
{
    public static async Task<int> Main(string[] args)
    {
        var scan = false;
        var all = false;
        var execute = false;
        var apply = false;
        var autoExecute = false;
        string? request = null;
        string? source = null;
        string? prepared = null;
        string? staging = null;
        string? auditResultPath = null;
        string? root = null;
        string? canonicalPath = null;
        string? hashContent = null;
        string? hashRevision = null;
        string? hashDoc = null;
        string? sealPreview = null;
        string? artifactTables = null;
        string? artifactVersion = null;
        string? roundtrip = null;
        string? roundtripOut = null;
        string? exportTarget = null;
        string? verifyParity = null;
        string? buildRequest = null;
        string? buildSource = null;
        string? buildOutput = null;
        string? ledgerRoot = null;
        string? successorVersion = null;
        var authorityState = false;
        var repairProjections = false;
        var archProjections = false;
        var mirrorZh = false;
        var mirrorCheck = false;
        var archDocs = false;
        var watch = false;
        var maintain = false;
        var pinSync = false;
        var migrationStatus = false;
        var migrationApply = false;
        var schemaParity = false;
        string? parityDescriptor = null;
        string? migrationSequence = null;
        string? migrationDb = null;
        string? watchInterval = null;
        var codexRead = false;
        var mintDualKey = false;
        var revokeReads = false;
        string? readActor = null;
        string? readPurpose = null;
        string? readScope = null;
        string? readClass = null;
        string? readTtl = null;
        string? readGrant = null;
        string? readOp = null;
        string? readArg = null;
        string? dkOperation = null;
        string? dkPrimary = null;
        string? dkSecondary = null;
        string? officialSovereign = null;
        for (var i = 0; i < args.Length; i++)
        {
            string Value()
            {
                if (i + 1 >= args.Length)
                    throw new ArgumentException(
                        $"missing value for {args[i]}");
                return args[++i];
            }
            switch (args[i])
            {
                case "--scan": scan = true; break;
                case "--all": all = true; break;
                case "--execute": execute = true; break;
                case "--apply": apply = true; break;
                case "--auto-execute": autoExecute = true; break;
                case "--authority-state": authorityState = true; break;
                case "--repair-projections": repairProjections = true;
                    break;
                case "--arch-projections": archProjections = true;
                    break;
                case "--mirror-zh": mirrorZh = true; break;
                case "--mirror-check": mirrorCheck = true; break;
                case "--arch-docs": archDocs = true; break;
                case "--watch": watch = true; break;
                case "--maintain": maintain = true; break;
                case "--pin-sync": pinSync = true; break;
                case "--migration-status": migrationStatus = true;
                    break;
                case "--schema-parity": schemaParity = true; break;
                case "--parity-descriptor": parityDescriptor = Value();
                    break;
                case "--migration-apply": migrationApply = true; break;
                case "--sequence": migrationSequence = Value(); break;
                case "--migration-db": migrationDb = Value(); break;
                case "--interval": watchInterval = Value(); break;
                case "--codex-read": codexRead = true; break;
                case "--mint-dual-key": mintDualKey = true; break;
                case "--revoke-codex-reads": revokeReads = true; break;
                case "--actor": readActor = Value(); break;
                case "--purpose": readPurpose = Value(); break;
                case "--scope": readScope = Value(); break;
                case "--class": readClass = Value(); break;
                case "--ttl": readTtl = Value(); break;
                case "--grant": readGrant = Value(); break;
                case "--op": readOp = Value(); break;
                case "--arg": readArg = Value(); break;
                case "--operation": dkOperation = Value(); break;
                case "--primary": dkPrimary = Value(); break;
                case "--secondary": dkSecondary = Value(); break;
                case "--official-sovereign":
                    officialSovereign = Value(); break;
                case "--request": request = Value(); break;
                case "--source": source = Value(); break;
                case "--prepared": prepared = Value(); break;
                case "--staging": staging = Value(); break;
                case "--audit-result": auditResultPath = Value(); break;
                case "--root": root = Value(); break;
                case "--canonical": canonicalPath = Value(); break;
                case "--hash-content": hashContent = Value(); break;
                case "--hash-revision": hashRevision = Value(); break;
                case "--hash-doc": hashDoc = Value(); break;
                case "--seal-preview": sealPreview = Value(); break;
                case "--artifact-tables": artifactTables = Value(); break;
                case "--artifact-version": artifactVersion = Value(); break;
                case "--roundtrip":
                    roundtrip = Value();
                    roundtripOut = Value();
                    break;
                case "--export": exportTarget = Value(); break;
                case "--verify-parity": verifyParity = Value(); break;
                case "--build-successor": buildRequest = Value();
                    buildSource = Value(); buildOutput = Value(); break;
                case "--ledger-root": ledgerRoot = Value(); break;
                case "--successor-version": successorVersion = Value(); break;
                default:
                    Console.Error.WriteLine(
                        $"unrecognized argument: {args[i]}");
                    Usage();
                    return 2;
            }
        }
        if (root is not null)
            Repo.SetRoot(root);
        try
        {
            if (canonicalPath is not null)
            {
                var payload = Repo.ToPlain(
                    JsonNode.Parse(File.ReadAllText(canonicalPath)));
                Console.WriteLine(CanonJson.Serialize(payload));
                return 0;
            }
            if (hashContent is not null)
            {
                var payload = Repo.ToPlain(
                    JsonNode.Parse(File.ReadAllText(hashContent)));
                return Emit(new Dictionary<string, object?>
                {
                    ["content_hash"] =
                        AmendmentContract.ContentHash(payload),
                });
            }
            if (hashRevision is not null)
            {
                var payload = (Dictionary<string, object?>)Repo.ToPlain(
                    JsonNode.Parse(File.ReadAllText(hashRevision)))!;
                return Emit(new Dictionary<string, object?>
                {
                    ["revision_entry_hash"] =
                        AmendmentContract.RevisionEntryHash(payload),
                });
            }
            if (hashDoc is not null)
                // Python open().read() normalizes CRLF→LF; hash the
                // logical text, not the raw file bytes.
                return Emit(new Dictionary<string, object?>
                {
                    ["search_document_hash"] =
                        AmendmentContract.SearchDocumentHash(
                            File.ReadAllText(hashDoc)
                                .Replace("\r\n", "\n")),
                });
            if (sealPreview is not null)
                return Emit(
                    AmendmentContract.ComputeSealPreview(sealPreview));
            if (artifactTables is not null)
                return Emit(StageCodec.ArtifactTableNames(artifactTables)
                    ?.OrderBy(t => t, StringComparer.Ordinal).ToList()
                    ?? (object)new Dictionary<string, object?>
                    { ["error"] = "ARTIFACT_UNREADABLE" });
            if (artifactVersion is not null)
                return Emit(new Dictionary<string, object?>
                {
                    ["codex_version"] =
                        StageCodec.ArtifactVersion(artifactVersion),
                });
            if (roundtrip is not null)
            {
                var target = roundtripOut
                    ?? throw new ArgumentException(
                        "--roundtrip requires <sql> <out>");
                var schema = StageCodec.StageName("parity"
                    + Guid.NewGuid().ToString("N")[..12]);
                try
                {
                    StageCodec.MaterializeArtifact(roundtrip, schema);
                    StageCodec.DumpSchema(schema, target);
                }
                finally
                {
                    StageCodec.DropSchema(schema);
                }
                return Emit(new Dictionary<string, object?>
                {
                    ["roundtrip"] = target,
                    ["source_sha256"] = FileHash(roundtrip),
                    ["target_sha256"] = FileHash(target),
                });
            }
            if (exportTarget is not null)
                return Emit(new Dictionary<string, object?>
                {
                    ["exported"] =
                        PgExport.ExportPostgresqlCodex(exportTarget),
                });
            if (migrationStatus)
                return Emit(MigrationExecutor.Status(Repo.Root()));
            if (schemaParity)
                return Emit(MachineSchemaParity.Probe());
            if (parityDescriptor is not null)
                return Emit(MachineSchemaParity.Describe(
                    parityDescriptor));
            if (migrationApply)
            {
                if (!long.TryParse(migrationSequence, out var seq))
                    throw new ArgumentException(
                        "--migration-apply requires --sequence <n>");
                var outcome = MigrationExecutor.Apply(
                    Repo.Root(), seq, migrationDb);
                Emit(outcome);
                return outcome.TryGetValue("applied", out var ap)
                    && ap is true ? 0 : 1;
            }
            if (pinSync)
                return Emit(CodexAutomation.PinSync());
            if (maintain)
                return Emit(CodexAutomation.Maintain());
            if (watch)
                return await CodexAutomation.RunWatch(
                    double.TryParse(watchInterval, out var seconds)
                        && seconds > 0 ? seconds : null);
            if (authorityState)
                return Emit(PgExport.AuthorityState());
            if (archDocs)
                // Read-only architecture registry × docs completeness
                // report (xingcheng_codex_alignment deep surface).
                return Emit(ArchitectureDocs.Report(Repo.Root()));
            if (mirrorCheck)
            {
                // Read-only live mirror validation — loads the five
                // zh-TW parts from the canonical codex root and checks
                // chain/hash/parity/replacement damage against the live
                // authority. Never re-renders (xingcheng_codex_
                // mirror_check deep surface).
                var checkRoot = UpdatePipeline.CanonicalCodexRoot();
                var errors = MirrorWriter.MirrorErrors(
                    PgDsn.CodexSchema, checkRoot, "live");
                return Emit(new Dictionary<string, object?>(
                    StringComparer.Ordinal)
                {
                    ["ok"] = errors.Length == 0,
                    ["errors"] = errors,
                    ["root"] = checkRoot,
                });
            }
            if (verifyParity is not null)
                return Emit(PgExport.VerifySqlParity(verifyParity));
            if (repairProjections)
                return Emit(
                    GenerationProjections.RepairLiveProjections());
            if (archProjections)
                return Emit(ArchitectureProjection.Refresh(
                    Repo.Root()));
            if (mirrorZh)
            {
                var codexRoot = UpdatePipeline.CanonicalCodexRoot();
                var tempRoot = Path.Combine(Path.GetTempPath(),
                    $"codex-mirror-{Guid.NewGuid():N}");
                try
                {
                    MirrorWriter.RenderMirrorParts(PgDsn.CodexSchema,
                        tempRoot, codexRoot);
                    foreach (var name in ChineseMirror.PartNames)
                        UpdatePipeline.AtomicReplace(
                            Path.Combine(tempRoot, name),
                            Path.Combine(codexRoot, name));
                    var mirrorErrors = MirrorWriter.MirrorErrors(
                        PgDsn.CodexSchema, codexRoot, "live");
                    return Emit(new Dictionary<string, object?>(
                        StringComparer.Ordinal)
                    {
                        ["ok"] = mirrorErrors.Length == 0,
                        ["errors"] = mirrorErrors,
                        ["root"] = codexRoot,
                    });
                }
                finally
                {
                    if (Directory.Exists(tempRoot))
                        Directory.Delete(tempRoot, true);
                }
            }
            if (revokeReads)
            {
                CodexSessions.RevokeCodexReadContexts();
                return Emit(new Dictionary<string, object?>
                {
                    ["ok"] = true,
                    ["revocation_generation"] =
                        CodexEntryState.CurrentRevocation(),
                });
            }
            if (mintDualKey)
            {
                if (dkOperation is null || dkPrimary is null
                    || dkSecondary is null || readPurpose is null
                    || readScope is null)
                    throw new ArgumentException("--mint-dual-key requires"
                        + " --operation --primary --secondary --purpose"
                        + " --scope");
                var nonce = CodexDualKey.MintDualKeyGrant(dkOperation,
                    dkPrimary, dkSecondary, readPurpose,
                    SplitScope(readScope),
                    readClass ?? CodexSessions.AccessReview,
                    double.TryParse(readTtl, out var grantTtl)
                        ? grantTtl : CodexDualKey.DefaultGrantTtl);
                return Emit(new Dictionary<string, object?>
                {
                    ["ok"] = true,
                    ["grant_nonce"] = nonce,
                });
            }
            if (officialSovereign is not null)
            {
                var sovereign = CodexOfficial.OfficialSelfDeclaration(
                    officialSovereign);
                return Emit(new Dictionary<string, object?>
                {
                    ["ok"] = sovereign is not null,
                    ["sovereign"] = SovereignJson(sovereign),
                });
            }
            if (codexRead)
            {
                if (readActor is null || readPurpose is null
                    || readScope is null || readOp is null)
                    throw new ArgumentException("--codex-read requires"
                        + " --actor --purpose --scope --op");
                using var session = CodexSessions.OpenCodexSession(
                    readActor, readPurpose, SplitScope(readScope),
                    readClass ?? CodexSessions.AccessReview,
                    double.TryParse(readTtl, out var sessionTtl)
                        ? sessionTtl
                        : CodexEntryState.DefaultSessionTtl,
                    readGrant);
                return Emit(new Dictionary<string, object?>(
                    StringComparer.Ordinal)
                {
                    ["ok"] = true,
                    ["session_nonce"] = session.Nonce,
                    ["codex_version"] = session.VersionText,
                    ["result"] = SessionOp(session, readOp, readArg),
                });
            }
            if (buildRequest is not null)
            {
                if (buildSource is null || buildOutput is null)
                    throw new ArgumentException(
                        "--build-successor requires"
                        + " <request> <source> <output>");
                var ledger = new CodexAmendmentRequestLedger(ledgerRoot);
                var built = SuccessorBuilder.BuildSuccessor(
                    buildRequest, buildSource, buildOutput, ledger,
                    successorVersion: successorVersion);
                return Emit(built.AsDict());
            }
            if (execute)
            {
                if (request is null)
                {
                    Console.Error.WriteLine(
                        "--execute requires --request");
                    Usage();
                    return 2;
                }
                Dictionary<string, object?>? auditResult = null;
                if (!string.IsNullOrEmpty(auditResultPath))
                    auditResult = (Dictionary<string, object?>)
                        Repo.ToPlain(
                            JsonNode.Parse(File.ReadAllText(
                                auditResultPath)))!;
                var result = Executor.ExecuteAmendment(
                    requestPath: request,
                    preparedDatabase:
                        string.IsNullOrEmpty(prepared) ? null : prepared,
                    auditResult: auditResult,
                    apply: apply,
                    stagingRoot:
                        string.IsNullOrEmpty(staging) ? null : staging);
                Emit(result.ToDict());
                return result.Ok ? 0 : 1;
            }
            if (scan)
                return Emit(Driver.ScanRequests());
            if (request is not null)
            {
                var outcome = await Driver.AdvanceRequest(request,
                    sourceDatabase:
                        string.IsNullOrEmpty(source) ? null : source,
                    autoExecute: autoExecute);
                Emit(outcome);
                return outcome.TryGetValue("ok", out var ok)
                    && ok is true ? 0 : 1;
            }
            if (all)
            {
                var outcomes = await Driver.AdvanceAll(
                    sourceDatabase:
                        string.IsNullOrEmpty(source) ? null : source,
                    autoExecute: autoExecute);
                Emit(outcomes);
                return outcomes.All(item =>
                    item.TryGetValue("ok", out var ok)
                    && ok is true) ? 0 : 1;
            }
            Usage();
            return 2;
        }
        catch (CodexReadDenied error)
        {
            Emit(new Dictionary<string, object?>(
                StringComparer.Ordinal)
            {
                ["ok"] = false,
                ["denied"] = error.Code,
                ["inner"] = error.Source,
            });
            return 1;
        }
        catch (ExecutorDenied error)
        {
            Emit(new Dictionary<string, object?>(
                StringComparer.Ordinal)
            {
                ["ok"] = false,
                ["error_code"] = ExecutorDenied.FailureCode,
                ["reason"] = error.Message,
            });
            return 1;
        }
        catch (ArgumentException error)
        {
            Console.Error.WriteLine(error.Message);
            Usage();
            return 2;
        }
        catch (Exception error)
        {
            Emit(new Dictionary<string, object?>(
                StringComparer.Ordinal)
            {
                ["ok"] = false,
                ["error"] = $"{error.GetType().Name}:{error.Message}",
                ["trace"] = error.StackTrace,
            });
            return 1;
        }
    }
}
