import sys
sys.path.insert(0, 'E:/GPTBridge/.kilo/worktrees/rustic-calcium/Standalone tools/local-model/src/backend')
from services.xingcheng.infrastructure.generative_language_model import StarAutoregressiveLanguageModel, save_model, load_model

# Train a model
model = StarAutoregressiveLanguageModel()
print('Initial metrics:', model.metrics())

additional_data = [
    {'intent': 'conversation', 'target': '你好，我是星澄，有什麼可以幫助你的？', 'input_text': '你好', 'weight': 5},
    {'intent': 'conversation', 'target': '謝謝你的提問，很高興為你服務。', 'input_text': '謝謝', 'weight': 5},
    {'intent': 'analysis', 'target': '這段文字表達了正面的情感，主要關鍵字包括開心、滿意。', 'input_text': '情感分析', 'weight': 4},
    {'intent': 'coding', 'target': 'def hello():\n    print("Hello, World!")', 'input_text': 'Python hello world', 'weight': 4},
    {'intent': 'reasoning', 'target': '根據提供的前提，結論是合理的，因為前提一和前提二都支持該結論。', 'input_text': '邏輯推理', 'weight': 4},
]

for item in additional_data:
    model.learn(**item)

print('After training:', model.metrics())

# Test generation
result = model.generate(intent='conversation', prompt='你好', grounding='', max_tokens=30)
print('Generated:', repr(result['text']))

# Save model
save_model(model, 'E:/GPTBridge/.kilo/worktrees/rustic-calcium/trained_model.json')
print('Model saved!')

# Load model
loaded_model = load_model('E:/GPTBridge/.kilo/worktrees/rustic-calcium/trained_model.json')
print('Loaded metrics:', loaded_model.metrics())

# Test loaded model
result = loaded_model.generate(intent='conversation', prompt='你好', grounding='', max_tokens=30)
print('Loaded model generated:', repr(result['text']))