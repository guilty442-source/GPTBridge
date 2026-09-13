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
for i, line in enumerate(result.stdout.splitlines()):
    print(f'line {i}: repr={repr(line)}')
    stripped = line.strip()
    print(f'  after strip: repr={repr(stripped)}')
    if stripped.startswith('"') and stripped.endswith('"'):
        stripped = stripped[1:-1]
        print(f'  after quote strip: repr={repr(stripped)}')
        print(f'  endswith \\r: {stripped.endswith(chr(13))}')
        print(f'  last char code: {hex(ord(stripped[-1])) if stripped else "empty"}')
    result_rstrip = stripped.rstrip('\r')
    print(f'  rstrip result: repr={repr(result_rstrip)}')
    print(f'  same object: {result_rstrip is stripped}')
    stripped = result_rstrip
    print(f'  after rstrip assign: repr={repr(stripped)}')