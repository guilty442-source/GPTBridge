from __future__ import annotations

import hashlib
from typing import Any

from .analytics_common import (
    _json,
    _mean,
    normalized_probability,
    rounded,
)


class AnalyticsStoreCalibrationMixin:
    """Decision calibration scoring and reporting methods."""

    def calibration(self) -> dict[str, Any]:
        all_decisions = self.decisions(2000)
        evaluated = [
            item
            for item in all_decisions
            if item.get("outcome", {}).get("evaluable") is True
            and bool(item.get("eligible_for_calibration"))
        ]
        scored, correct, bucket_values = self._calibration_scores(evaluated)
        average_confidence = _mean(
            [normalized_probability(item.get("confidence"), 0.5) for item in evaluated]
        ) if evaluated else 0.0
        accuracy = correct / len(evaluated) if evaluated else 0.0
        reliability_gap = average_confidence - accuracy
        confidence_multiplier = max(0.65, min(1.05, 1.0 - max(0.0, reliability_gap)))
        return {
            "status": "ready" if scored else "collecting",
            "evaluated_count": len(scored),
            "pending_count": sum(
                1
                for item in all_decisions
                if bool(item.get("eligible_for_calibration")) and not item.get("outcome")
            ),
            "not_calibrated_count": sum(
                1
                for item in all_decisions
                if not bool(item.get("eligible_for_calibration"))
            ),
            "brier_score": rounded(_mean(scored), 4) if scored else None,
            "accuracy_percent": rounded(accuracy * 100, 2) if scored else None,
            "average_confidence_percent": rounded(average_confidence * 100, 2) if scored else None,
            "reliability_gap_percent": rounded(reliability_gap * 100, 2) if scored else None,
            "confidence_multiplier": rounded(confidence_multiplier, 4),
            "confidence_buckets": self._confidence_buckets(bucket_values),
            "sample_version": self._calibration_sample_version(evaluated),
            "slices": self._calibration_slices(evaluated),
            "calibration_label": "穩定" if scored and _mean(scored) <= 0.2 else "需校準" if scored else "累積結果中",
        }

    def _calibration_scores(
        self,
        evaluated: list[dict[str, Any]],
    ) -> tuple[list[float], int, dict[str, list[tuple[float, float]]]]:
        scored: list[float] = []
        correct = 0
        bucket_values: dict[str, list[tuple[float, float]]] = {
            "low": [],
            "medium": [],
            "high": [],
        }
        for item in evaluated:
            outcome = (
                1.0 if item.get("outcome", {}).get("success") is True else 0.0
            )
            probability = normalized_probability(item.get("confidence"), 0.5)
            correct += int(outcome == 1.0)
            scored.append((probability - outcome) ** 2)
            bucket = "high" if probability >= 0.75 else "medium" if probability >= 0.55 else "low"
            bucket_values[bucket].append((probability, outcome))
        return scored, correct, bucket_values

    def _confidence_buckets(
        self,
        bucket_values: dict[str, list[tuple[float, float]]],
    ) -> list[dict[str, Any]]:
        return [
            {
                "key": key,
                "label": {"low": "低信心", "medium": "中信心", "high": "高信心"}[key],
                "count": len(values),
                "average_confidence": rounded(_mean([value[0] for value in values]) * 100, 2) if values else None,
                "accuracy_percent": rounded(_mean([value[1] for value in values]) * 100, 2) if values else None,
            }
            for key, values in bucket_values.items()
        ]

    def _calibration_slices(
        self,
        evaluated: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        grouped: dict[tuple[str, str], list[tuple[float, float]]] = {}
        for item in evaluated:
            probability = normalized_probability(item.get("confidence"), 0.5)
            outcome = 1.0 if item.get("outcome", {}).get("success") is True else 0.0
            evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
            dimensions = {
                "direction": str(item.get("prediction_direction") or "unknown"),
                "market": str(evidence.get("market") or "UNKNOWN"),
                "asset_type": str(evidence.get("asset_type") or "UNKNOWN"),
            }
            for dimension, key in dimensions.items():
                grouped.setdefault((dimension, key), []).append((probability, outcome))
        return [
            {
                "dimension": dimension,
                "key": key,
                "evaluated_count": len(values),
                "brier_score": rounded(
                    _mean([(probability - outcome) ** 2 for probability, outcome in values]),
                    4,
                ),
                "accuracy_percent": rounded(
                    _mean([outcome for _probability, outcome in values]) * 100,
                    2,
                ),
                "average_confidence_percent": rounded(
                    _mean([probability for probability, _outcome in values]) * 100,
                    2,
                ),
            }
            for (dimension, key), values in sorted(grouped.items())
        ]

    def _calibration_sample_version(
        self,
        evaluated: list[dict[str, Any]],
    ) -> str:
        if not evaluated:
            return ""
        return hashlib.sha256(
            _json(
                [
                    {
                        "decision_id": item.get("decision_id"),
                        "evaluated_at": item.get("outcome", {}).get("evaluated_at"),
                        "direction": item.get("prediction_direction"),
                        "success": item.get("outcome", {}).get("success"),
                    }
                    for item in evaluated
                ]
            ).encode("utf-8")
        ).hexdigest()[:24]


__all__ = ['AnalyticsStoreCalibrationMixin']
