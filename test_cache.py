import sys
sys.path.insert(0, 'E:/GPTBridge/.kilo/worktrees/rustic-calcium/Standalone tools/local-model/src/backend')
from services.xingcheng.infrastructure.generative_language_model import StarAutoregressiveLanguageModel
from services.xingcheng.infrastructure.fast_inference import create_fast_engine
import time

model = StarAutoregressiveLanguageModel()

additional_data = [
    {'intent': 'conversation', 'target': '你好，我是星澄，有什麼可以幫助你的？', 'input_text': '你好', 'weight': 5},
    {'intent': 'conversation', 'target': '謝謝你的提問，很高興為你服務。', 'input_text': '謝謝', 'weight': 5},
]

for item in additional_data:
    model.learn(**item)

fast_engine = create_fast_engine(model, enable_result_cache=True, result_cache_size=128)

# Test cache behavior with different parameters
print("=== Result Cache Test ===")

# Same prompt, same params - should cache
print("\nSame prompt + params (should cache):")
for i in range(3):
    start = time.perf_counter()
    result = fast_engine.generate_fast(intent='conversation', prompt='你好', grounding='', max_tokens=30, temperature=0.55, top_k=4)
    elapsed = time.perf_counter() - start
    print(f"  Run {i+1}: {elapsed*1000:.2f}ms")

# Same prompt, different temperature - should NOT cache
print("\nSame prompt, different temperature (should NOT cache):")
for temp in [0.55, 0.7, 0.3]:
    start = time.perf_counter()
    result = fast_engine.generate_fast(intent='conversation', prompt='你好', grounding='', max_tokens=30, temperature=temp, top_k=4)
    elapsed = time.perf_counter() - start
    print(f"  temp={temp}: {elapsed*1000:.2f}ms")

# Same prompt, different max_tokens - should NOT cache
print("\nSame prompt, different max_tokens (should NOT cache):")
for mt in [30, 50, 100]:
    start = time.perf_counter()
    result = fast_engine.generate_fast(intent='conversation', prompt='你好', grounding='', max_tokens=mt, temperature=0.55, top_k=4)
    elapsed = time.perf_counter() - start
    print(f"  max_tokens={mt}: {elapsed*1000:.2f}ms")

# Same prompt, different top_k - should NOT cache
print("\nSame prompt, different top_k (should NOT cache):")
for tk in [4, 8, 1]:
    start = time.perf_counter()
    result = fast_engine.generate_fast(intent='conversation', prompt='你好', grounding='', max_tokens=30, temperature=0.55, top_k=tk)
    elapsed = time.perf_counter() - start
    print(f"  top_k={tk}: {elapsed*1000:.2f}ms")

# Test cache eviction (LRU)
print("\n=== Cache Eviction Test (size=3) ===")
fast_engine2 = create_fast_engine(model, enable_result_cache=True, result_cache_size=3)

prompts = ['你好', '謝謝', '早安', '再見', '你好']
for p in prompts:
    start = time.perf_counter()
    result = fast_engine2.generate_fast(intent='conversation', prompt=p, grounding='', max_tokens=30)
    elapsed = time.perf_counter() - start
    print(f'  "{p}": {elapsed*1000:.2f}ms')

# Test disabling cache
print("\n=== Cache Disabled ===")
fast_engine3 = create_fast_engine(model, enable_result_cache=False)
for i in range(3):
    start = time.perf_counter()
    result = fast_engine3.generate_fast(intent='conversation', prompt='你好', grounding='', max_tokens=30)
    elapsed = time.perf_counter() - start
    print(f"  Run {i+1}: {elapsed*1000:.2f}ms")

print("\nAll tests completed!")