import subprocess
RUNTIME_CHANNEL_DATABASES = {
    'shared-layer/data/ai-channel.sqlite3',
    'shared-layer/data/system-channel.sqlite3',
}
runtime_files = {
    f'{database_path}{suffix}'
    for database_path in RUNTIME_CHANNEL_DATABASES
    for suffix in ('', '-shm', '-wal')
}
input_data = '\n'.join(sorted(runtime_files)) + '\n'
result = subprocess.run(
    ['git', 'check-ignore', '--no-index', '--stdin'],
    cwd=r'E:\GPTBridge',
    check=True,
    capture_output=True,
    text=True,
    input=input_data,
)
print('stdout repr:', repr(result.stdout))
ignored_lines = set()
for line in result.stdout.splitlines():
    stripped = line.strip()
    if stripped.startswith('"') and stripped.endswith('"'):
        stripped = stripped[1:-1]
    stripped = stripped.rstrip('\r')
    ignored_lines.add(stripped)
print('ignored_lines:', ignored_lines)
print('runtime_files:', runtime_files)
not_ignored = sorted(runtime_files - ignored_lines)
print('not_ignored:', not_ignored)