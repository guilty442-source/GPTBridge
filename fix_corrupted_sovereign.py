import sqlite3
from pathlib import Path

db_path = Path('E:/GPTBridge/governance_rule/codex/data/governance_codex.sqlite3')

# Make writable
import ctypes
FILE_ATTRIBUTE_READONLY = 0x1
p = str(db_path)
kernel32 = ctypes.windll.kernel32
GetFileAttributesW = kernel32.GetFileAttributesW
GetFileAttributesW.argtypes = [ctypes.c_wchar_p]
GetFileAttributesW.restype = ctypes.c_uint
SetFileAttributesW = kernel32.SetFileAttributesW
SetFileAttributesW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint]
SetFileAttributesW.restype = ctypes.c_bool
attrs = kernel32.GetFileAttributesW(p)
kernel32.SetFileAttributesW(p, attrs & ~FILE_ATTRIBUTE_READONLY)

conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
conn.isolation_level = None  # Autocommit mode

# Find the corrupted entry
target_rowid = None
for row in conn.execute('SELECT rowid, provision_id FROM provision_law_classification WHERE provision_type = "sovereign"'):
    pid = row['provision_id']
    if '\ufffd' in pid or any(ord(c) > 127 for c in pid):
        print(f'rowid: {row["rowid"]}, provision_id: {pid!r}')
        target_rowid = row['rowid']

if target_rowid:
    # Try different Chinese characters
    test_values = ["星澄", "xingcheng", "\u661f\u6fa4", "xing-cheng"]

    for test_val in test_values:
        cursor = conn.execute('UPDATE provision_law_classification SET provision_id = ? WHERE rowid = ?', (test_val, target_rowid))
        print(f'Test value: {test_val!r}, Rows affected: {cursor.rowcount}')

        # Verify
        for row in conn.execute('SELECT provision_id FROM provision_law_classification WHERE rowid = ?', (target_rowid,)):
            print(f'  After UPDATE: {row["provision_id"]!r}')

        # If it worked, break
        if test_val != '�P��':
            break
else:
    print('No corrupted entry found')

# Verify
for row in conn.execute('SELECT * FROM provision_law_classification WHERE provision_id = "星澄"'):
    print(dict(row))

# Also check for any remaining corrupted entries
for row in conn.execute('SELECT rowid, provision_id FROM provision_law_classification WHERE provision_type = "sovereign"'):
    pid = row['provision_id']
    if '\ufffd' in pid or any(ord(c) > 127 for c in pid):
        print(f'STILL CORRUPTED: rowid: {row["rowid"]}, provision_id: {pid!r}')

conn.close()

# Make read-only again
import ctypes
FILE_ATTRIBUTE_READONLY = 0x1
p = 'E:/GPTBridge/governance_rule/codex/data/governance_codex.sqlite3'
kernel32 = ctypes.windll.kernel32
GetFileAttributesW = kernel32.GetFileAttributesW
GetFileAttributesW.argtypes = [ctypes.c_wchar_p]
GetFileAttributesW.restype = ctypes.c_uint
SetFileAttributesW = kernel32.SetFileAttributesW
SetFileAttributesW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint]
SetFileAttributesW.restype = ctypes.c_bool
attrs = kernel32.GetFileAttributesW(p)
kernel32.SetFileAttributesW(p, attrs | 0x1)
print('Set read-only:', kernel32.SetFileAttributesW(p, attrs | 0x1))