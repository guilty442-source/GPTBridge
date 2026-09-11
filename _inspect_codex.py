import json, sqlite3, time
from pathlib import Path

for i in range(3):
    conn = sqlite3.connect('governance_rule/codex/data/governance_codex.sqlite3')
    md = dict(conn.execute('SELECT key, value FROM metadata'))
    arts = conn.execute('SELECT COUNT(*) FROM articles').fetchone()[0]
    manuals = conn.execute('SELECT COUNT(*) FROM maintenance_manual_directory').fetchone()[0]
    conn.close()
    m = json.loads(Path('governance_rule/codex/governance_codex.zh-TW.txt').read_text(encoding='utf-8'))
    mv = m.get('codex_version')
    marts = len(m['tables']['articles'])
    mman = len(m['tables'].get('maintenance_manual_directory', []))
    print(f'[{i}] db v={md.get("codex_version")} arts={arts} manuals={manuals} | mirror v={mv} arts={marts} manuals={mman}')
    time.sleep(2)
