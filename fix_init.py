with open(r'E:\GPTBridge\.kilo\worktrees\rustic-calcium\shared-layer\src\shared_layer\database\__init__.py', 'rb') as f:
    content = f.read()

old = (
    b'    "certify_rebuild": ("rebuild_certifier", "certify"),\r\n'
    b'    "is_rebuild_certified": ("rebuild_certifier", "is_certified"),\r\n'
    b'    "check_long_transactions": ("watchdog", "check_long_transactions"),\r\n'
    b'    "collect_bloat_report": ("watchdog", "collect_bloat_report"),\r\n'
    b'    "get_rpo_rto_classes": ("watchdog", "get_rpo_rto_classes"),\r\n'
    b'    "get_capacity_thresholds": ("watchdog", "get_capacity_thresholds"),\r\n'
    b'    "certify_startup": ("startup_certifier", "certify_startup"),\r\n'
    b'    "is_database_ready": ("startup_certifier", "is_ready"),\r\n'
    b'    "set_domain_readonly": ("readonly_domain", "set_readonly"),\r\n'
    b'    "is_domain_readonly": ("readonly_domain", "is_readonly"),\r\n'
    b'    "get_current_generation": ("generation_fence", "get_current_generation"),\r\n'
    b'    "bump_generation": ("generation_fence", "bump_generation"),\r\n'
    b'    "is_connection_stale": ("generation_fence", "is_connection_stale"),\r\n'
    b'    "get_stale_sqlite_databases": ("generation_fence", "get_stale_sqlite_databases"),\r\n'
    b'    "upsert_sqlite_generation": ("generation_fence", "upsert_sqlite_generation"),\r\n'
)

new = (
    b'    "certify_rebuild": ("rebuild_certifier", "certify"),\r\n'
    b'    "certify_rebuild_engine": ("rebuild_certifier", "certify_engine"),\r\n'
    b'    "is_rebuild_certified": ("rebuild_certifier", "is_certified"),\r\n'
    b'    "check_long_transactions": ("watchdog", "check_long_transactions"),\r\n'
    b'    "terminate_long_transactions": ("watchdog", "terminate_long_transactions"),\r\n'
    b'    "collect_bloat_report": ("watchdog", "collect_bloat_report"),\r\n'
    b'    "collect_bloat_transport": ("watchdog", "collect_bloat_transport"),\r\n'
    b'    "collect_bloat_audit": ("watchdog", "collect_bloat_audit"),\r\n'
    b'    "get_rpo_rto_classes": ("watchdog", "get_rpo_rto_classes"),\r\n'
    b'    "get_capacity_thresholds": ("watchdog", "get_capacity_thresholds"),\r\n'
    b'    "certify_startup": ("startup_certifier", "certify_startup"),\r\n'
    b'    "is_database_ready": ("startup_certifier", "is_ready"),\r\n'
    b'    "set_domain_readonly": ("readonly_domain", "set_readonly"),\r\n'
    b'    "is_domain_readonly": ("readonly_domain", "is_readonly"),\r\n'
    b'    "get_current_generation": ("generation_fence", "get_current_generation"),\r\n'
    b'    "bump_generation": ("generation_fence", "bump_generation"),\r\n'
    b'    "is_connection_stale": ("generation_fence", "is_connection_stale"),\r\n'
    b'    "get_stale_sqlite_databases": ("generation_fence", "get_stale_sqlite_databases"),\r\n'
    b'    "get_stale_qdrant_collections": ("generation_fence", "get_stale_qdrant_collections"),\r\n'
    b'    "upsert_sqlite_generation": ("generation_fence", "upsert_sqlite_generation"),\r\n'
    b'    "upsert_qdrant_generation": ("generation_fence", "upsert_qdrant_generation"),\r\n'
    b'    "sync_qdrant_generation": ("generation_fence", "sync_qdrant_generation"),\r\n'
    b'    "BackupScheduler": ("backup_scheduler", "BackupScheduler"),\r\n'
    b'    "get_backup_scheduler": ("backup_scheduler", "get_backup_scheduler"),\r\n'
    b'    "SQLSLOMetrics": ("observability.slo", "SQLSLOMetrics"),\r\n'
    b'    "SLOTarget": ("observability.slo", "SLOTarget"),\r\n'
    b'    "get_sql_slo": ("observability.slo", "get_sql_slo"),\r\n'
)

if old in content:
    content = content.replace(old, new)
    with open(r'E:\GPTBridge\.kilo\worktrees\rustic-calcium\shared-layer\src\shared_layer\database\__init__.py', 'wb') as f:
        f.write(content)
    print('Replaced successfully')
else:
    print('Old not found')
    # Find where it differs
    old_lines = old.split(b'\r\n')
    for i, line in enumerate(old_lines):
        if line and line not in content:
            print(f'Missing at line {i}: {line}')