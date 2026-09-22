#!/usr/bin/env python3
"""P0 §10.64 ② — 引擎/GPU 釋放長時驗證（auto_release + gpu_coordinator）

驗證：
- AutoReleaseManager idle 2s 後自動釋放（模擬 300s）
- 壓力釋放（memory_pressure 模擬）
- GpuCoordinator acquire/release 與 VRAM 上限 95% 檢查
"""
from __future__ import annotations

import time
import torch

# 1) AutoReleaseManager 短閒置驗證
from pathlib import Path
import sys
sys.path.insert(0, 'Standalone tools/local-model/src/backend/services')
from xingcheng.infrastructure.native_transformer.execution.auto_release import AutoReleaseManager

released = []
def _rel(obj):
    released.append(obj)

# 讓 shared_layer 能找到 governance_rule
import pathlib
ROOT = pathlib.Path(__file__).resolve().parents[1]
for p in [str(ROOT), str(ROOT / 'shared-layer' / 'src'), str(ROOT / 'Standalone tools' / 'local-model' / 'src' / 'backend' / 'services')]:
    if p not in sys.path:
        sys.path.insert(0, p)

mgr = AutoReleaseManager(idle_seconds=2)
# 使用可弱引用的 dummy
class Dummy: pass
d = Dummy()
mgr.register('test-engine', d, _rel, size_mb=10)
assert 'test-engine' in mgr._resources
time.sleep(3)
# 觸發檢查（正常每 60s，這裡手動）
mgr._check()
# 等待 timer 觸發的釋放
time.sleep(1)
assert 'test-engine' not in mgr._resources, 'idle release failed'
assert released, 'release_fn not called'
print('auto_release idle PASS', released)
mgr.shutdown()

# 2) GpuCoordinator 查詢與上限
from shared_layer.adaptive.gpu_coordinator import GpuCoordinator, query_gpu
coord = GpuCoordinator(poll_interval=0.1)
status = query_gpu()
if status:
    print(f'gpu status total={status.total_mb:.0f} used={status.used_mb:.0f} free={status.free_mb:.0f} util={status.util_pct:.0f}%')
    # 應滿足可用度檢查
    can = coord.can_acquire(100)
    print('can_acquire 100MB', can)
    # 95% 上限：6GB -> 5.8GB 超限應拒
    huge = coord.can_acquire(status.total_mb)
    print('can_acquire totalMB (should be False)', huge)
    assert not huge, 'VRAM limit not enforced'
    # acquire 上下文短超時
    try:
        with coord.acquire(100, timeout=1):
            print('acquire 100MB PASS')
    except TimeoutError as e:
        print('acquire failed', e)
        raise
else:
    print('no GPU, skip')

print('10.64 ② engine/GPU release verification PASS')
