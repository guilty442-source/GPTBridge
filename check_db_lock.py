import psutil
for proc in psutil.process_iter(['pid', 'name', 'open_files']):
    try:
        if proc.info['open_files']:
            for f in proc.info['open_files']:
                if 'governance_codex.sqlite3' in f.path:
                    print(f'PID: {proc.info["pid"]}, Name: {proc.info["name"]}, File: {f.path}')
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass