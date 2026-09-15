from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Sequence

from ..infrastructure.analytics_repository import InvestmentAnalyticsStore, utc_text
from ..infrastructure.privacy import protect_text, unprotect_text
from .automation_common import _hash, _json


class ModelGovernance:
    MINIMUM_CALIBRATION_SAMPLES = 30
    MINIMUM_SLICE_SAMPLES = 20
    BRIER_READ_ONLY_THRESHOLD = 0.35
    SLICE_BRIER_READ_ONLY_THRESHOLD = 0.40

    def __init__(self, store: InvestmentAnalyticsStore) -> None:
        self.store = store

    def register(self, model_name: str, version: str, *, prompt: str = "", config: dict[str, Any] | None = None) -> str:
        prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT model_version_id FROM model_versions WHERE model_name=? AND version=? AND prompt_hash=?",
                (model_name, version, prompt_hash),
            ).fetchone()
            if row:
                return str(row["model_version_id"])
            model_version_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO model_versions VALUES(?, ?, ?, ?, ?, 'active', ?)",
                (model_version_id, model_name, version, prompt_hash, utc_text(), protect_text(_json(config or {}))),
            )
        return model_version_id

    def is_read_only(self, model_name: str) -> bool:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM model_versions WHERE model_name=? AND status='read_only' LIMIT 1",
                (model_name,),
            ).fetchone()
        return bool(row)

    def record_run(
        self,
        *,
        model_name: str,
        version: str,
        inputs: Any,
        outputs: Any,
        data_sources: list[str] | None = None,
        metrics: dict[str, Any] | None = None,
        prompt: str = "",
        analysis_run_id: str = "",
        decision_count: int = 0,
    ) -> str:
        model_id = self.register(model_name, version, prompt=prompt)
        run_id = uuid.uuid4().hex
        normalized_metrics = self._normalized_metrics(metrics)
        calibration = normalized_metrics.get("calibration")
        if isinstance(calibration, dict) and calibration.get("sample_version"):
            sample_version = str(calibration["sample_version"])
            if self._calibration_sample_recorded(model_id, sample_version):
                normalized_metrics.pop("calibration", None)
                normalized_metrics["calibration_reference"] = sample_version
                normalized_metrics["calibration_duplicate"] = True
        with self.store.connect() as connection:
            connection.execute(
                "INSERT INTO model_governance_runs VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id, model_id, analysis_run_id, utc_text(), _hash(inputs), _hash(outputs),
                    protect_text(_json(data_sources or [])), protect_text(_json(normalized_metrics)), int(decision_count),
                ),
            )
        return run_id

    def _normalized_metrics(self, metrics: dict[str, Any] | None) -> dict[str, Any]:
        output = dict(metrics or {})
        if isinstance(output.get("calibration"), dict):
            source = dict(output["calibration"])
        elif output.get("brier_score") is not None:
            source = {
                key: output.pop(key)
                for key in (
                    "status",
                    "sample_version",
                    "evaluated_count",
                    "brier_score",
                    "accuracy_percent",
                    "reliability_gap_percent",
                    "slices",
                )
                if key in output
            }
        else:
            source = self.store.calibration()
        evaluated_count = max(0, int(source.get("evaluated_count") or 0))
        brier = source.get("brier_score")
        sample_version = str(
            source.get("sample_version")
            or source.get("evaluation_sample_id")
            or ""
        )
        if not sample_version and evaluated_count and brier is not None:
            sample_version = _hash(
                {
                    "evaluated_count": evaluated_count,
                    "brier_score": brier,
                    "accuracy_percent": source.get("accuracy_percent"),
                    "slices": source.get("slices") or [],
                }
            )[:24]
        output["calibration"] = {
            "status": str(source.get("status") or ("ready" if brier is not None else "collecting")),
            "sample_version": sample_version,
            "evaluated_count": evaluated_count,
            "brier_score": float(brier) if brier is not None else None,
            "accuracy_percent": source.get("accuracy_percent"),
            "reliability_gap_percent": source.get("reliability_gap_percent"),
            "slices": [
                dict(item)
                for item in source.get("slices", [])
                if isinstance(item, dict)
            ],
        }
        return output

    def _calibration_sample_recorded(
        self,
        model_version_id: str,
        sample_version: str,
    ) -> bool:
        with self.store.connect() as connection:
            rows = connection.execute(
                """
                SELECT metrics_encrypted
                FROM model_governance_runs
                WHERE model_version_id=?
                ORDER BY created_at DESC LIMIT 500
                """,
                (model_version_id,),
            ).fetchall()
        for row in rows:
            try:
                metrics = json.loads(
                    unprotect_text(str(row["metrics_encrypted"] or "")) or "{}"
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            calibration = metrics.get("calibration")
            if (
                isinstance(calibration, dict)
                and str(calibration.get("sample_version") or "") == sample_version
            ):
                return True
        return False

    def dashboard(self) -> dict[str, Any]:
        with self.store.connect() as connection:
            versions = connection.execute("SELECT model_version_id, model_name, version, prompt_hash, registered_at, status, config_encrypted FROM model_versions ORDER BY registered_at DESC LIMIT 500").fetchall()
            runs = connection.execute(
                """
                SELECT r.*, v.model_name, v.version, v.status AS model_status
                FROM model_governance_runs r JOIN model_versions v ON v.model_version_id=r.model_version_id
                ORDER BY r.created_at DESC LIMIT 200
                """
            ).fetchall()
        run_items, calibration_samples, brier_scores = self._dashboard_samples(runs)
        affected = self._degraded_model_versions(calibration_samples)
        if affected:
            with self.store.connect() as connection:
                connection.executemany("UPDATE model_versions SET status='read_only' WHERE model_version_id=?", [(value,) for value in affected])
            version_items = [
                dict(row) | {"config_encrypted": "", "status": "read_only" if row["model_version_id"] in affected else row["status"]}
                for row in versions
            ]
        else:
            version_items = [dict(row) | {"config_encrypted": ""} for row in versions]
        return self._dashboard_result(
            versions,
            runs,
            run_items,
            calibration_samples,
            brier_scores,
            affected,
            version_items,
        )

    def _dashboard_samples(
        self,
        runs: Sequence[Any],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[float]]:
        run_items = []
        brier_scores: list[float] = []
        calibration_samples: list[dict[str, Any]] = []
        seen_samples: set[tuple[str, str]] = set()
        for source in runs:
            item = dict(source)
            item["data_sources"] = json.loads(unprotect_text(item.pop("data_sources_encrypted")) or "[]")
            item["metrics"] = json.loads(unprotect_text(item.pop("metrics_encrypted")) or "{}")
            calibration = item["metrics"].get("calibration")
            if not isinstance(calibration, dict) and item["metrics"].get("brier_score") is not None:
                calibration = item["metrics"]
            if isinstance(calibration, dict) and calibration.get("brier_score") is not None:
                sample = self._calibration_sample(item, calibration, seen_samples)
                if sample is not None:
                    calibration_samples.append(sample)
                    if sample["evaluated_count"] >= self.MINIMUM_CALIBRATION_SAMPLES:
                        brier_scores.append(sample["brier_score"])
            run_items.append(item)
        return run_items, calibration_samples, brier_scores

    @staticmethod
    def _calibration_sample(
        item: dict[str, Any],
        calibration: dict[str, Any],
        seen_samples: set[tuple[str, str]],
    ) -> dict[str, Any] | None:
        sample_version = str(
            calibration.get("sample_version")
            or item["governance_run_id"]
        )
        key = (str(item["model_version_id"]), sample_version)
        if key in seen_samples:
            return None
        seen_samples.add(key)
        return {
            "model_version_id": str(item["model_version_id"]),
            "sample_version": sample_version,
            "evaluated_count": max(
                0, int(calibration.get("evaluated_count") or 0)
            ),
            "brier_score": float(calibration["brier_score"]),
            "slices": [
                dict(value)
                for value in calibration.get("slices", [])
                if isinstance(value, dict)
            ],
        }

    def _degraded_model_versions(
        self,
        calibration_samples: Sequence[dict[str, Any]],
    ) -> set[str]:
        affected: set[str] = set()
        for sample in calibration_samples:
            overall_bad = (
                sample["evaluated_count"] >= self.MINIMUM_CALIBRATION_SAMPLES
                and sample["brier_score"] > self.BRIER_READ_ONLY_THRESHOLD
            )
            slice_bad = any(
                str(item.get("dimension") or "") in {"market", "asset_type"}
                and int(item.get("evaluated_count") or 0) >= self.MINIMUM_SLICE_SAMPLES
                and float(item.get("brier_score") or 0)
                > self.SLICE_BRIER_READ_ONLY_THRESHOLD
                for item in sample["slices"]
            )
            if overall_bad or slice_bad:
                affected.add(sample["model_version_id"])
        return affected

    def _dashboard_result(
        self,
        versions: Sequence[Any],
        runs: Sequence[Any],
        run_items: list[dict[str, Any]],
        calibration_samples: list[dict[str, Any]],
        brier_scores: list[float],
        affected: set[str],
        version_items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        average_brier = sum(brier_scores) / len(brier_scores) if brier_scores else None
        degraded = bool(affected)
        any_read_only = any(item.get("status") == "read_only" for item in version_items)
        return {
            "status": "read_only" if degraded or any_read_only else "ready",
            "version_count": len(versions),
            "run_count": len(runs),
            "evaluated_run_count": len(brier_scores),
            "calibration_sample_count": len(calibration_samples),
            "average_brier_score": average_brier,
            "guardrail": (
                "read-only when a distinct calibration sample has at least "
                f"{self.MINIMUM_CALIBRATION_SAMPLES} outcomes and Brier score exceeds "
                f"{self.BRIER_READ_ONLY_THRESHOLD}, or a market/asset slice has at least "
                f"{self.MINIMUM_SLICE_SAMPLES} outcomes and exceeds "
                f"{self.SLICE_BRIER_READ_ONLY_THRESHOLD}"
            ),
            "metrics_schema": {
                "calibration": {
                    "sample_version": "stable hash/version of the evaluated decision set",
                    "evaluation_sample_id": "accepted alias for sample_version",
                    "evaluated_count": "integer",
                    "brier_score": "0..1",
                    "accuracy_percent": "0..100 or null",
                    "reliability_gap_percent": "percentage points or null",
                    "slices": [
                        {
                            "dimension": "market|asset_type|direction",
                            "key": "slice identifier",
                            "evaluated_count": "integer",
                            "brier_score": "0..1",
                        }
                    ],
                }
            },
            "versions": version_items,
            "runs": run_items[:50],
        }
