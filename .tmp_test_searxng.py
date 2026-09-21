import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path("Standalone tools/local-model/src/backend/services").resolve()))

from xingcheng.infrastructure.xingcheng_tools.search.searxng import SearXNGProvider
from xingcheng.infrastructure.xingcheng_tools.search.types import SearchRequest


class Response:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self):
        return json.dumps(
            {
                "results": [
                    {
                        "url": "https://example.test/a",
                        "title": "ok",
                        "content": "snippet",
                    }
                ]
            }
        ).encode("utf-8")


urllib.request.urlopen = lambda request, timeout: Response()
provider = SearXNGProvider(timeout=2)
results = provider.search(
    SearchRequest(
        original_question="q",
        queries=["q"],
        need_full_text=False,
        need_reranker=False,
        need_citations=False,
    )
)
print(len(results), results[0].provider, results[0].url)
