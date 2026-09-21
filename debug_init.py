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

print('Old in content:', old in content)
print('Old length:', len(old))

# Find the actual content
idx = content.find(b'certify_rebuild')
print('Found at:', idx)
print('Context:', content[idx-4:idx+200])