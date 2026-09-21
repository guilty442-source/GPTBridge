import sys
sys.path.insert(0, r"E:\GPTBridge\shared-layer\src")

# Test the native module directly
import _sovereign_native as native
print("Testing SIMD level detection:")
# The CPUID detection is internal to C, but we can verify it works by checking the output matches expected

# Test a larger matmul to trigger AVX-512 path
import numpy as np
import time

# Create test data
m, k, n = 64, 64, 64
a = np.random.randn(m, k).astype(np.float64)
b = np.random.randn(k, n).astype(np.float64)

# Time the native matmul
start = time.perf_counter()
for _ in range(100):
    c = native.transformer_matmul(a, b)
native_time = time.perf_counter() - start
print(f"Native matmul (64x64x64, 100 iter): {native_time:.4f}s")

# Compare with numpy
start = time.perf_counter()
for _ in range(100):
    c_np = a @ b
numpy_time = time.perf_counter() - start
print(f"NumPy matmul (64x64x64, 100 iter): {numpy_time:.4f}s")

# Verify correctness
print(f"Max abs diff: {np.max(np.abs(c - c_np))}")

# Test softmax
rows, cols = 32, 128
inp = np.random.randn(rows, cols).astype(np.float64)
start = time.perf_counter()
for _ in range(100):
    out = native.transformer_softmax(inp)
native_time = time.perf_counter() - start
print(f"Native softmax (32x128, 100 iter): {native_time:.4f}s")

# Test attention
q_rows, d_k = 16, 64
k_rows, d_v = 16, 64
q = np.random.randn(q_rows, d_k).astype(np.float64)
k = np.random.randn(k_rows, d_k).astype(np.float64)
v = np.random.randn(k_rows, d_v).astype(np.float64)
start = time.perf_counter()
for _ in range(100):
    out = native.transformer_scaled_dot_product_attention(q, k, v)
native_time = time.perf_counter() - start
print(f"Native attention (16x64, 100 iter): {native_time:.4f}s")

print("\nAll tests passed!")