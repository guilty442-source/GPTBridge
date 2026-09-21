import sys
sys.path.insert(0, 'E:/GPTBridge/.kilo/worktrees/rustic-calcium/Standalone tools/local-model/src/backend')
from services.xingcheng.infrastructure.native_model import StarNativeLanguageModel
import time

model = StarNativeLanguageModel(model_role='main')

# Train some data
additional_data = [
    {'intent': 'conversation', 'target': '你好，我是星澄，有什麼可以幫助你的？', 'input_text': '你好', 'weight': 5},
    {'intent': 'conversation', 'target': '謝謝你的提問，很高興為你服務。', 'input_text': '謝謝', 'weight': 5},
    {'intent': 'analysis', 'target': '這段文字表達了正面的情感，主要關鍵字包括開心、滿意。', 'input_text': '情感分析', 'weight': 4},
]

for item in additional_data:
    model.language_model.learn(**item)

# Test fast engine
print('Testing fast_engine property...')
start = time.perf_counter()
result = model.fast_engine.generate_fast(intent='conversation', prompt='你好', grounding='', max_tokens=30)
first = time.perf_counter() - start
print('First run: {:.2f}ms'.format(first*1000))
print('Result: {}'.format(result['text']))

# Test cached
start = time.perf_counter()
result = model.fast_engine.generate_fast(intent='conversation', prompt='你好', grounding='', max_tokens=30)
cached = time.perf_counter() - start
print('Cached run: {:.2f}ms'.format(cached*1000))
print('Result: {}'.format(result['text']))

print('Speedup: {:.1f}x'.format(first/cached))