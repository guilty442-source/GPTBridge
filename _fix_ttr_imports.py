from pathlib import Path
p = Path(r"Standalone tools/local-model/tests/test_transformer_training_repository.py")
d = p.read_text(encoding="utf-8")
old = "import asyncio\nimport hashlib\nimport json\nimport sys\nfrom pathlib import Path\nimport pytest\nfrom xingcheng.application.service import LocalAiService"
new = "import asyncio\nimport hashlib\nimport json\nimport sys\nimport tempfile\nimport time\nfrom pathlib import Path\n\nimport psycopg\nimport pytest\nfrom xingcheng.application.service import LocalAiService"
assert old in d
p.write_text(d.replace(old, new, 1), encoding="utf-8")
print("ok")
