import sys
sys.path.insert(0, 'E:/GPTBridge/.kilo/worktrees/rustic-calcium/Standalone tools/local-model/src/backend')
from services.xingcheng.infrastructure.generative_language_model import StarAutoregressiveLanguageModel
from services.xingcheng.infrastructure.fast_inference import create_fast_engine
import time

model = StarAutoregressiveLanguageModel()

# Train some data
additional_data = [
    {'intent': 'conversation', 'target': '你好，我是星澄，有什麼可以幫助你的？', 'input_text': '你好', 'weight': 5},
    {'intent': 'conversation', 'target': '謝謝你的提問，很高興為你服務。', 'input_text': '謝謝', 'weight': 5},
    {'intent': 'analysis', 'target': '這段文字表達了正面的情感，主要關鍵字包括開心、滿意。', 'input_text': '情感分析', 'weight': 4},
    {'intent': 'coding', 'target': 'def hello():\n    print("Hello, World!")', 'input_text': 'Python hello world', 'weight': 4},
    {'intent': 'reasoning', 'target': '根據提供的前提，結論是合理的，因為前提一和前提二都支持該結論。', 'input_text': '邏輯推理', 'weight': 4},
]

for item in additional_data:
    model.learn(**item)

# Create fast engine
fast_engine = create_fast_engine(model)

# Test first run (cache miss)
start = time.perf_counter()
result = fast_engine.generate_fast(intent='conversation', prompt='你好', grounding='', max_tokens=30)
first_run = time.perf_counter() - start
print(f'First run (cache miss): {first_run*1000:.2f}ms')

# Test subsequent runs (cache hit)
for i in range(5):
    start = time.perf_counter()
    result = fast_engine.generate_fast(intent='conversation', prompt='你好', grounding='', max_tokens=30)
    elapsed = time.perf_counter() - start
    print(f'Run {i+2} (cache hit): {elapsed*1000:.2f}ms')

# Test different prompts (new cache entries)
print('\nDifferent prompts:')
prompts = ['你好', '謝謝', '早安', '再見', '你好']
for p in prompts:
    start = time.perf_counter()
    result = fast_engine.generate_fast(intent='conversation', prompt=p, grounding='', max_tokens=30)
    elapsed = time.perf_counter() - start
    print(f'  "{p}": {elapsed*1000:.2f}ms')

# Benchmark 100 runs
print('\n100 runs benchmark:')
start = time.perf_counter()
for _ in range(100):
    result = fast_engine.generate_fast(intent='conversation', prompt='你好', grounding='', max_tokens=30)
total = time.perf_counter() - start
print(f'100 runs: {total:.3f}s ({100/total:.1f} tok/s)')

# Test with grounding
print('\nWith grounding:')
start = time.perf_counter()
result = fast_engine.generate_fast(
    intent='conversation',
    prompt='請根據以下內容回答',
    grounding='用戶詢問產品價格，產品A價格為100元，產品B價格為200元。',
    max_tokens=30
)
elapsed = time.perf_counter() - start
print(f'  With grounding: {elapsed*1000:.2f}ms')
print(f'  Result: {result["text"]}')