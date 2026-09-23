"""Local cross-encoder reranker — governed, lazy, on-demand (G50).

Single reranker implementation for both canonical surfaces:

* ``RerankerFn`` (pipeline_retrieval): ``(query, candidates) -> (ranked,
  meta)`` over fused dict candidates;
* ``RerankFn`` (orchestrator): ``(query, list[RagEvidence]) ->
  list[RagEvidence]`` writing ``reranker_score`` per unit.

Load path mirrors ``LocalEmbeddingProvider``: the model is admitted through
``ModelResourceManager`` (``ModelRole.RERANKER``, on-demand) and loaded with
``local_files_only=True`` — a reranker must never trigger a network fetch on
the retrieval hot path.  Any denial/load/predict failure returns the input
order unchanged with ``applied=False`` so the caller falls back to RRF
ranking; a reranker is an accelerator, never a hard dependency.
"""
from __future__ import annotations

import logging
import os
from dataclasses import replace
from typing import Any

from .orchestration.evidence import RagEvidence
from .pipeline_retrieval import RerankerFn  # noqa: F401  (contract anchor)

_logger = logging.getLogger(__name__)

# §10.7: reranker 屬 on_demand 角色，載入前經受管資源閘門。
# Qwen3-Reranker-0.6B fp32 權重 ~2.4GB；2560MB 為含執行期 overhead 之保守估測。
_RERANKER_RAM_REQUIRED_MB = 2560
# CUDA 載入點（G50）：與 engine 同一 GpuCoordinator 預算；0.6B fp32
# 權重 ~2.4GB，估 1.5× 供 KV/activation 餘量。
_RERANKER_VRAM_REQUIRED_MB = 1536.0

_DEFAULT_MODEL = "Qwen/Qwen3-Reranker-0.6B"


class LocalCrossEncoderReranker:
    """Lazy local cross-encoder reranker (resource-gated, fail-open order).

    Construction is free: the model only loads on first ``predict`` call.
    """

    def __init__(self, model: str | None = None) -> None:
        self._model_name = model or os.environ.get(
            "GPTBRIDGE_RAG_RERANKER_MODEL", _DEFAULT_MODEL
        )
        self._model: Any = None
        self._model_id = f"sentence-transformers/{self._model_name}"
        self._error: str = ""

    @property
    def model_name(self) -> str:
        return self._model_name

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        if self._error:
            return None
        try:
            from core_system.model_resource_manager import (
                ModelRole,
                get_model_resource_manager,
            )

            mgr = get_model_resource_manager()
            decision = mgr.request_load(
                ModelRole.RERANKER,
                self._model_id,
                ram_mb=_RERANKER_RAM_REQUIRED_MB,
            )
            if not decision.admitted:
                self._error = f"reranker load denied by resource gate: {decision.reason}"
                _logger.warning("%s", self._error)
                return None
            try:
                from sentence_transformers import CrossEncoder

                self._model = CrossEncoder(
                    self._model_name,
                    device=self._cuda_device_or_cpu(),
                    local_files_only=True,
                    trust_remote_code=False,
                )
            except Exception:
                mgr.release(self._model_id)
                raise
        except Exception as exc:  # noqa: BLE001 — fail-open to RRF order
            self._error = str(exc)[:300]
            _logger.warning("reranker load failed: %s", self._error)
            return None
        return self._model

    def _cuda_device_or_cpu(self) -> str:
        """CUDA 載入點經 GpuCoordinator VRAM 預算；不足/無法判定時降級
        CPU（reranker 屬互動加速路徑，fail-soft 降級而非拒絕服務）。"""
        try:
            import torch

            if not torch.cuda.is_available():
                return "cpu"
        except Exception:
            return "cpu"
        timeout = float(
            os.environ.get("GPTBRIDGE_RAG_RERANKER_GPU_TIMEOUT_S", "15")
        )
        try:
            from shared_layer.adaptive.gpu_coordinator import (
                GpuCoordinator,
            )

            with GpuCoordinator().acquire(
                _RERANKER_VRAM_REQUIRED_MB,
                priority="inference",
                timeout=timeout,
            ):
                return "cuda"
        except Exception as exc:  # noqa: BLE001 — degrade, never deny
            _logger.info("reranker CUDA budget unavailable: %s", exc)
            return "cpu"

    def _score(self, query: str, passages: list[str]) -> list[float] | None:
        model = self._load()
        if model is None:
            return None
        try:
            scores = model.predict([(query, p) for p in passages])
        except Exception as exc:  # noqa: BLE001 — fail-open to RRF order
            self._error = str(exc)[:300]
            _logger.warning("reranker predict failed: %s", self._error)
            return None
        return [float(s) for s in scores]

    # -- dict-candidate surface (pipeline_retrieval RerankerFn) ----------

    def __call__(
        self, query: str, candidates: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        meta: dict[str, Any] = {
            "reranker_applied": False,
            "model": self._model_name,
            "fallback": "rrf-hybrid-ranking",
        }
        if not candidates:
            meta["reason"] = "no-candidates"
            return candidates, meta
        scores = self._score(
            query, [str(c.get("content") or "") for c in candidates]
        )
        if scores is None:
            meta["reason"] = self._error or "reranker-unavailable"
            return candidates, meta
        ranked = [
            {**c, "reranker_score": s} for c, s in zip(candidates, scores)
        ]
        ranked.sort(key=lambda c: -float(c["reranker_score"]))
        meta.update(
            {"reranker_applied": True, "candidate_count": len(ranked)}
        )
        meta.pop("reason", None)
        return ranked, meta

    # -- RagEvidence surface (orchestrator RerankFn) ---------------------

    def rerank_evidence(
        self, query: str, evidence: list[RagEvidence]
    ) -> list[RagEvidence]:
        if not evidence:
            return evidence
        scores = self._score(query, [e.content for e in evidence])
        if scores is None:
            return evidence
        ranked = [
            replace(e, reranker_score=s) for e, s in zip(evidence, scores)
        ]
        ranked.sort(key=lambda e: -e.reranker_score)
        return ranked


__all__ = ["LocalCrossEncoderReranker"]
