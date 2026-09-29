"""星澄原生模型 (XingCheng Native Model) — 本地原生推論套件。

訓練與 Python 推論退役（B167/B38/E180）：JAX/XLA 與 PyTorch 已全數
退役——零 source、dependency、artifact、execution 與 fallback 角色，
無過渡期。``jax_backend``、``training/*.py``、``self_learning``、
``maturity`` 與 PyTorch 推論棧（``modules`` / ``kernels`` /
``inference`` / ``execution`` / ``quantization`` / ``checkpoint`` /
``cpp_export`` / ``benchmark`` / ``capability_eval``）皆已移除；
模型執行只由正式 C++ 推論引擎（``cpp_runtime`` →
``_xingcheng_inference``）服務，權重為已驗證
``star-native-inference-bundle/v1`` 匯出物，原生訓練管線為
``training/xingcheng_trainer.exe``（C++）＋ ``xct-executor``（.NET）。

本套件保留語言層推論周邊（chat_format / lifecycle / retention）與
C++ 引擎路由（cpp_runtime）。
``bpe.py``（NativeBPETokenizer / train_bpe，``tokenizers`` 套件
lineage）、``tokenizer.py`` 與 ``config.py``（XingChengTokenizer /
XingChengConfig，Python 分詞與模型組態）已隨 Python 訓練線退役——
訓練由 C++ ``xingcheng_trainer`` 承載，執行期分詞與組態由 C++ 引擎
讀取已驗證 bundle 的 ``tokenizer.json`` / weights 元資料。

載入行為：本套件不提供套件層級符號；子模組一律以明確 submodule
import 取用，import 本套件不載入任何外部數值框架，缺少相關 lineage
依賴時 fail-closed。
"""

__version__ = "1.00000"
