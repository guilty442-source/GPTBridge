import sys
sys.path.insert(0, r"E:\GPTBridge\shared-layer\src")

from shared_layer.performance.native_dispatcher import native_available, matmul, softmax, scaled_dot_product_attention
print('native_available:', native_available())

# Test matmul
a = [[1.0, 2.0], [3.0, 4.0]]
b = [[5.0, 6.0], [7.0, 8.0]]
result = matmul(a, b)
print('matmul result:', result)

# Test softmax
input_2d = [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]
result = softmax(input_2d)
print('softmax result:', result)

# Test attention
q = [[1.0, 0.0], [0.0, 1.0]]
k = [[1.0, 0.0], [0.0, 1.0]]
v = [[1.0, 2.0], [3.0, 4.0]]
result = scaled_dot_product_attention(q, k, v)
print('attention result:', result)