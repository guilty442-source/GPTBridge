with open(r'E:\GPTBridge\.kilo\worktrees\rustic-calcium\shared-layer\src\shared_layer\database\__init__.py', 'r', encoding='utf-8') as f:
    content = f.read()

old = (
    '    "certify_rebuild": ("rebuild_certifier", "certify"),\n'
    '    "is_rebuild_certified": ("rebuild_certifier", "is_certified"),\n'
    '    "check_long_transactions": ("watchdog", "check_long_transactions"),\n'
    '    "collect_bloat_report": ("watchdog", "collect_bloat_report"),\n'
    '    "get_rpo_rto_classes": ("watchdog", "get_rpo_rto_classes"),\n'
    '    "get_capacity_thresholds": ("watchdog", "get_capacity_thresholds"),\n'
    '    "certify_startup": ("startup_certifier", "certify_startup"),\n'
    '    "is_database_ready": ("startup_certifier", "is_ready"),\n'
    '    "set_domain_readonly": ("readonly_domain", "set_readonly"),\n'
    '    "is_domain_readonly": ("readonly_domain", "is_readonly"),\n'
    '    "get_current_generation": ("generation_fence", "get_current_generation"),\n'
    '    "bump_generation": ("generation_fence", "bump_generation"),\n'
    '    "is_connection_stale": ("generation_fence", "is_connection_stale"),\n'
    '    "get_stale_sqlite_databases": ("generation_fence", "get_stale_sqlite_databases"),\n'
    '    "upsert_sqlite_generation": ("generation_fence", "upsert_sqlite_generation"),\n'
)

new = (
    '    "certify_rebuild": ("rebuild_certifier", "certify"),\n'
    '    "certify_rebuild_engine": ("rebuild_certifier", "certify_engine"),\n'
    '    "is_rebuild_certified": ("rebuild_certifier", "is_certified"),\n'
    '    "check_long_transactions": ("watchdog", "check_long_transactions"),\n'
    '    "terminate_long_transactions": ("watchdog", "terminate_long_transactions"),\n'
    '    "collect_bloat_report": ("watchdog", "collect_bloat_report"),\n'
    '    "collect_bloat_transport": ("watchdog", "collect_bloat_transport"),\n'
    '    "collect_bloat_audit": ("watchdog", "collect_bloat_audit"),\n'
    '    "get_rpo_rto_classes": ("watchdog", "get_rpo_rto_classes"),\n'
    '    "get_capacity_thresholds": ("watchdog", "get_capacity_thresholds"),\n'
    '    "certify_startup": ("startup_certifier", "certify_startup"),\n'
    '    "is_database_ready": ("startup_certifier", "is_ready"),\n'
    '    "set_domain_readonly": ("readonly_domain", "set_readonly"),\n'
    '    "is_domain_readonly": ("readonly_domain", "is_readonly"),\n'
    '    "get_current_generation": ("generation_fence", "get_current_generation"),\n'
    '    "bump_generation": ("generation_fence", "bump_generation"),\n'
    '    "is_connection_stale": ("generation_fence", "is_connection_stale"),\n'
    '    "get_stale_sqlite_databases": ("generation_fence", "get_stale_sqlite_databases"),\n'
    '    "get_stale_qdrant_collections": ("generation_fence", "get_stale_qdrant_collections"),\n'
    '    "upsert_sqlite_generation": ("generation_fence", "upsert_sqlite_generation"),\n'
    '    "upsert_qdrant_generation": ("generation_fence", "upsert_qdrant_generation"),\n'
    '    "sync_qdrant_generation": ("generation_fence", "sync_qdrant_generation"),\n'
    '    "BackupScheduler": ("backup_scheduler", "BackupScheduler"),\n'
    '    "get_backup_scheduler": ("backup_scheduler", "get_backup_scheduler"),\n'
    '    "SQLSLOMetrics": ("observability.slo", "SQLSLOMetrics"),\n'
    '    "SLOTarget": ("observability.slo", "SLOTarget"),\n'
    '    "get_sql_slo": ("observability.slo", "get_sql_slo"),\n'
)

if old in content:
    content = content.replace(old, new)
    with open(r'E:\GPTBridge\.kilo\worktrees\rustic-calcium\shared-layer\src\shared_layer\database\__init__.py', 'w', encoding='utf-8') as f:
        f.write(content)
    print('Replaced successfully')
else:
    print('Old not found')
    # Find the difference
    for i, (o, n) in enumerate(zip(old.split('\n'), new.split('\n'))):
        if o != n:
            print(f'Diff line {i}:')
            print(f'  Old: {repr(o)}')
            print(f'  New: {repr(n)}')