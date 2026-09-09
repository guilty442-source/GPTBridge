from __future__ import annotations

import re
from typing import Any, Mapping


class ModelRegistryMixin:
    """Class-level constants for model registry, role assignments, and positioning."""

    MODEL = "gemma4:e2b-it-qat"
    MODEL_FAMILY = "Gemma 4"
    PARAMETER_CLASS = "E2B"
    PARAMETER_COUNT = "2.3B effective / 5.1B total"
    QUANTIZATION = "QAT-4bit"
    ARCHITECTURE = "dense-hybrid-attention-decoder-transformer"
    CONTEXT_WINDOW = 153_600
    RESIDENT_CONTEXT_WINDOW = 8_192
    NON_RESIDENT_CONTEXT_WINDOW = 153_600
    SAFE_CONTEXT_WINDOW = 8_192
    MIN_CONTEXT_WINDOW = 2_048
    CONTEXT_WINDOW_STEPS = (2_048, 4_096, 8_192, 16_384, 32_768, 65_536, 131_072, 153_600)
    CONTEXT_GROWTH_SUCCESS_THRESHOLD = 3
    MAX_PREDICT = 12_288
    RESIDENT_MODELS = frozenset({"qwen3.5:9b-q4_K_M"})
    RESIDENT_KEEP_ALIVE = -1
    NON_RESIDENT_KEEP_ALIVE = "5m"
    DEFAULT_GENERATION_TIMEOUT_SECONDS = 300.0
    SELF_UPGRADE_GENERATION_TIMEOUT_SECONDS = 90.0
    SELECTED_MODEL_GENERATION_TIMEOUT_SECONDS = 540.0
    MAX_CONCURRENT_TRANSFORMERS = 4
    COMMANDER_MAX_PARALLEL = 1
    VISUAL_FILE_MANAGEMENT_MODEL = "openbmb/minicpm-v4.6:q8_0"
    VISUAL_FILE_MANAGEMENT_INTENTS = frozenset({"visual"})
    REASONING_EFFORTS = frozenset({"none", "low", "medium", "high"})
    DOMAIN_REASONING_INTENTS = frozenset(
        {"reasoning", "calculation", "statistics", "analysis", "risk"}
    )
    DIVISION_OF_LABOR_INTENTS = frozenset(
        {
            "search",
            "data",
            "command_understanding",
            "coding",
            "command_execution",
            "autonomous_agent",
            "self_upgrade",
        }
    )
    LOW_EFFORT_MODEL_PREFERENCES: Mapping[str, tuple[str, ...]] = {
        "conversation": (MODEL, "glm4:9b"),
        "general": (MODEL, "glm4:9b"),
        "capabilities": ("gemma4:26b-a4b-it-qat",),
        "data_organization": ("ibm/granite4.2:30b-q4_K_M",),
        "command_understanding": ("qwen3.8:27b-q4_K_M",),
        "coding": ("granite-code:3b",),
        "command_execution": ("granite-code:3b",),
        "visual": (VISUAL_FILE_MANAGEMENT_MODEL,),
    }
    COMMAND_UNDERSTANDING_MODEL = "qwen3.8:27b-q4_K_M"
    FRONTEND_WORKER_MODEL = "rnj-1:8b-instruct-q4_K_M"
    TASK_ALLOCATION_MODEL = "qwen3.8:27b-q4_K_M"
    INTEGRATION_MODEL = "qwen3.8:27b-q4_K_M"
    EXECUTION_MODEL = "qwen3.6:35b-a3b-coding"
    INSPECTION_MODEL = "qwen3.8:27b-q4_K_M"
    RESULT_MODEL = "qwen3.8:27b-q4_K_M"
    FAILURE_ADJUDICATOR_MODEL = "qwen3.8:27b-q4_K_M"
    MODEL_ROLE_ASSIGNMENTS: Mapping[str, Mapping[str, Any]] = {
        "qwen3.8:27b-q4_K_M": {
            "primary_responsibility": "作業總指揮",
            "secondary_responsibilities": [
                "中文任務理解",
                "整合驗收",
                "模型失敗裁決",
                "動態改派",
            ],
            "task_levels": ["normal", "intermediate", "difficult"],
        },
        "qwen3.6:35b-a3b-coding": {
            "primary_responsibility": "主程式工程師",
            "secondary_responsibilities": ["Repository 級修改", "測試修復"],
            "task_levels": ["normal", "intermediate", "difficult"],
        },
        "qwen3-coder:30b-a3b-q4_K_M": {
            "primary_responsibility": "第二程式工程師",
            "secondary_responsibilities": ["替代實作", "平行開發"],
            "task_levels": ["normal", "intermediate", "difficult"],
        },
        "deepcoder:14b": {
            "primary_responsibility": "程式碼審查員",
            "secondary_responsibilities": ["Debug", "演算法驗證"],
            "task_levels": ["normal", "intermediate", "difficult"],
        },
        "granite-code:3b": {
            "primary_responsibility": "超輕量 Coding",
            "secondary_responsibilities": ["語法修正", "格式", "補全"],
            "task_levels": ["simple", "normal"],
        },
        "nemotron-3.5-lightning:30b-a3b-q4_K_M": {
            "primary_responsibility": "長流程自動化 Agent",
            "secondary_responsibilities": ["工具鏈", "持續任務"],
            "task_levels": ["normal", "intermediate", "difficult"],
        },
        "ornith-1.5:35b": {
            "primary_responsibility": "高難度 Coding／Agent 攻堅",
            "secondary_responsibilities": ["新解法探索", "長任務"],
            "task_levels": ["intermediate", "difficult"],
        },
        "gpt-oss:20b": {
            "primary_responsibility": "工具箱控制器",
            "secondary_responsibilities": ["Function Calling", "結構化任務"],
            "task_levels": ["normal", "intermediate", "difficult"],
        },
        "nemotron-3-nano:4b-q8_0": {
            "primary_responsibility": "高速 Agent",
            "secondary_responsibilities": ["短工具流程", "參數整理"],
            "task_levels": ["simple", "normal"],
        },
        "deepseek-r1:14b": {
            "primary_responsibility": "深度推理專家",
            "secondary_responsibilities": ["數學", "因果", "風險分析"],
            "task_levels": ["intermediate", "difficult"],
        },
        "deepseek-r1:8b-0528-qwen3-q4_K_M": {
            "primary_responsibility": "快速推理驗證",
            "secondary_responsibilities": ["中文第二意見"],
            "task_levels": ["normal", "intermediate"],
        },
        "ibm/granite4.2:30b-q4_K_M": {
            "primary_responsibility": "專業企業／投資資料處理",
            "secondary_responsibilities": ["RAG", "報表", "JSON", "合規"],
            "task_levels": ["normal", "intermediate", "difficult"],
        },
        "mistral-small:24b": {
            "primary_responsibility": "低延遲 Function Calling",
            "secondary_responsibilities": ["一般 Agent", "JSON"],
            "task_levels": ["normal", "intermediate", "difficult"],
        },
        "gemma4:26b-a4b-it-qat": {
            "primary_responsibility": "高階多模態大腦",
            "secondary_responsibilities": ["困難圖片／文件綜合推理"],
            "task_levels": ["intermediate", "difficult"],
        },
        "gemma4:12b-it-qat": {
            "primary_responsibility": "中階多模態",
            "secondary_responsibilities": ["文件／圖片綜合理解"],
            "task_levels": ["simple", "normal", "intermediate"],
        },
        "gemma4:e2b-it-qat": {
            "primary_responsibility": "高速看圖",
            "secondary_responsibilities": ["日常畫面問答"],
            "task_levels": ["simple", "normal"],
        },
        "glm4:9b": {
            "primary_responsibility": "中文快速分析 Worker",
            "secondary_responsibilities": ["摘要", "中文資料整理"],
            "task_levels": ["simple", "normal"],
        },
        "rnj-1:8b-instruct-q4_K_M": {
            "primary_responsibility": "高速 Code／STEM 工具工",
            "secondary_responsibilities": ["數學", "Tool Calling"],
            "task_levels": ["simple", "normal", "intermediate", "difficult"],
        },
        "openbmb/minicpm-v4.6:q8_0": {
            "primary_responsibility": "自動檔案視覺辨識",
            "secondary_responsibilities": ["圖片／影片分類", "標籤", "摘要"],
            "task_levels": ["simple", "normal", "intermediate", "difficult"],
        },
        "qwen3-vl:8b-thinking": {
            "primary_responsibility": "視覺推理專家",
            "secondary_responsibilities": ["OCR", "空間", "影片", "圖表推理"],
            "task_levels": ["normal", "intermediate", "difficult"],
        },
        "qwen3-embedding:4b": {
            "primary_responsibility": "高品質中文／Code Embedding",
            "secondary_responsibilities": ["高精度 RAG"],
            "task_levels": ["simple", "normal", "intermediate", "difficult"],
        },
        "nomic-embed-text-v2-moe:latest": {
            "primary_responsibility": "高速 Embedding",
            "secondary_responsibilities": ["一般記憶／RAG 索引"],
            "task_levels": ["simple", "normal", "intermediate", "difficult"],
        },
        "pdurugyan/qwen3-reranker-0.6b-q8_0:latest": {
            "primary_responsibility": "檢索結果重排序",
            "secondary_responsibilities": ["相關性評分", "候選內容篩選"],
            "task_levels": ["simple", "normal", "intermediate", "difficult"],
        },
    }
    MODEL_RUNTIME_POSITIONING: Mapping[str, Mapping[str, str]] = {
        "qwen3.8:27b-q4_K_M": {"execution_speed": "quality_first", "reasoning_intensity": "high"},
        "qwen3.6:35b-a3b-coding": {"execution_speed": "balanced", "reasoning_intensity": "high"},
        "qwen3-coder:30b-a3b-q4_K_M": {"execution_speed": "balanced", "reasoning_intensity": "high"},
        "deepcoder:14b": {"execution_speed": "balanced", "reasoning_intensity": "high"},
        "granite-code:3b": {"execution_speed": "ultra_fast", "reasoning_intensity": "low"},
        "nemotron-3.5-lightning:30b-a3b-q4_K_M": {"execution_speed": "fast", "reasoning_intensity": "medium"},
        "ornith-1.5:35b": {"execution_speed": "quality_first", "reasoning_intensity": "high"},
        "gpt-oss:20b": {"execution_speed": "balanced", "reasoning_intensity": "medium"},
        "nemotron-3-nano:4b-q8_0": {"execution_speed": "ultra_fast", "reasoning_intensity": "low"},
        "deepseek-r1:14b": {"execution_speed": "quality_first", "reasoning_intensity": "high"},
        "deepseek-r1:8b-0528-qwen3-q4_K_M": {"execution_speed": "balanced", "reasoning_intensity": "high"},
        "ibm/granite4.2:30b-q4_K_M": {"execution_speed": "balanced", "reasoning_intensity": "medium"},
        "mistral-small:24b": {"execution_speed": "fast", "reasoning_intensity": "low"},
        "gemma4:26b-a4b-it-qat": {"execution_speed": "quality_first", "reasoning_intensity": "high"},
        "gemma4:12b-it-qat": {"execution_speed": "balanced", "reasoning_intensity": "medium"},
        "gemma4:e2b-it-qat": {"execution_speed": "ultra_fast", "reasoning_intensity": "low"},
        "glm4:9b": {"execution_speed": "fast", "reasoning_intensity": "low"},
        "rnj-1:8b-instruct-q4_K_M": {"execution_speed": "fast", "reasoning_intensity": "medium"},
        "openbmb/minicpm-v4.6:q8_0": {"execution_speed": "fast", "reasoning_intensity": "medium"},
        "qwen3-vl:8b-thinking": {"execution_speed": "balanced", "reasoning_intensity": "high"},
        "qwen3-embedding:4b": {"execution_speed": "fast", "reasoning_intensity": "none"},
        "nomic-embed-text-v2-moe:latest": {"execution_speed": "ultra_fast", "reasoning_intensity": "none"},
        "pdurugyan/qwen3-reranker-0.6b-q8_0:latest": {"execution_speed": "ultra_fast", "reasoning_intensity": "none"},
    }
    EXECUTION_SPEED_LABELS: Mapping[str, str] = {
        "ultra_fast": "極速",
        "fast": "快速",
        "balanced": "均衡",
        "quality_first": "品質優先",
    }
    REASONING_INTENSITY_LABELS: Mapping[str, str] = {
        "none": "無",
        "low": "低",
        "medium": "中",
        "high": "高",
    }
    MODEL_SIZE_TIERS: Mapping[str, tuple[str, ...]] = {
        "small": (
            MODEL,
            "nemotron-3-nano:4b-q8_0",
            "granite-code:3b",
        ),
        "medium": (
            "rnj-1:8b-instruct-q4_K_M",
            "glm4:9b",
            "gemma4:12b-it-qat",
            "deepseek-r1:8b-0528-qwen3-q4_K_M",
            "deepcoder:14b",
            "deepseek-r1:14b",
        ),
        "large": (
            "qwen3.8:27b-q4_K_M",
            "qwen3.6:35b-a3b-coding",
            "qwen3-coder:30b-a3b-q4_K_M",
            "nemotron-3.5-lightning:30b-a3b-q4_K_M",
            "ornith-1.5:35b",
            "ibm/granite4.2:30b-q4_K_M",
            "gemma4:26b-a4b-it-qat",
            "mistral-small:24b",
            "gpt-oss:20b",
        ),
    }
    TASK_INTENSITY_SCHEDULING: Mapping[str, Mapping[str, Any]] = {
        "simple": {
            "primary_tier": "small",
            "collaboration": "single-small-model",
            "cross_validation": False,
        },
        "normal": {
            "primary_tier": "medium",
            "collaboration": "medium-model-with-small-assistance",
            "cross_validation": "risk-based",
        },
        "intermediate": {
            "primary_tier": "large",
            "collaboration": "one-large-with-one-or-two-specialists",
            "cross_validation": "risk-based",
        },
        "difficult": {
            "primary_tier": "large",
            "collaboration": "multi-model-collaboration",
            "cross_validation": True,
        },
    }
    TASK_LEVEL_LABELS: Mapping[str, str] = {
        "simple": "輕量",
        "normal": "普通",
        "intermediate": "中級",
        "difficult": "困難",
    }
    EMBEDDING_MODEL = "qwen3-embedding:4b"
    DOCUMENT_EMBEDDING_MODEL = "nomic-embed-text-v2-moe:latest"
    RERANKER_MODEL = "pdurugyan/qwen3-reranker-0.6b-q8_0:latest"
    NON_GENERATIVE_MODELS = frozenset(
        {EMBEDDING_MODEL, DOCUMENT_EMBEDDING_MODEL, RERANKER_MODEL}
    )
    DEFAULT_ENDPOINT = "http://127.0.0.1:11434"
    MODEL_NAME_PATTERN = re.compile(
        r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}(?::[A-Za-z0-9][A-Za-z0-9._-]{0,127})?$"
    )
