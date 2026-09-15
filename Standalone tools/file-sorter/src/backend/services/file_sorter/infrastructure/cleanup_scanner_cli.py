"""CLI entry points for the media cleanup scanner."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .cleanup_constants import (
    DEFAULT_ANALYSIS_SPEED,
    DEFAULT_SIMILAR_VIDEO_THRESHOLD,
    ProgressEventCallback,
)
from .cleanup_duplicates import print_progress_event
from .cleanup_utils import _configure_utf8_stdio


def run_cleanup_scan(
    target_dir: str | Path,
    *,
    image_cleanup: bool = False,
    similar_image_analysis: bool = False,
    video_cleanup: bool = True,
    similar_video_analysis: bool = False,
    similar_video_threshold: int | None = None,
    analysis_speed: int | None = None,
    parallel_analysis: bool = True,
    model_temperature: float = 0.0,
    model_top_p: float = 0.9,
    model_context_window: int = 8192,
    model_max_output_tokens: int = 512,
    progress_event_callback: ProgressEventCallback | None = None,
) -> dict[str, Any]:
    from .cleanup_scanner import CleanupScanner

    scanner = CleanupScanner(
        target_dir,
        image_cleanup=image_cleanup,
        similar_image_analysis=similar_image_analysis,
        video_cleanup=video_cleanup,
        similar_video_analysis=similar_video_analysis,
        similar_video_threshold=similar_video_threshold,
        analysis_speed=analysis_speed,
        parallel_analysis=parallel_analysis,
        model_temperature=model_temperature,
        model_top_p=model_top_p,
        model_context_window=model_context_window,
        model_max_output_tokens=model_max_output_tokens,
        progress_event_callback=progress_event_callback,
    )
    return scanner.run()


def main(argv: list[str] | None = None) -> int:
    _configure_utf8_stdio()
    parser = argparse.ArgumentParser(
        description="Scan media cleanup candidates without exact duplicate or similar image detection."
    )
    parser.add_argument("target_dir")
    parser.add_argument("--image-cleanup", action="store_true")
    parser.add_argument("--similar-image-analysis", action="store_true")
    parser.add_argument("--video-cleanup", action="store_true")
    parser.add_argument("--similar-video-analysis", action="store_true")
    parser.add_argument(
        "--similar-video-threshold",
        type=int,
        default=DEFAULT_SIMILAR_VIDEO_THRESHOLD,
    )
    parser.add_argument("--analysis-speed", type=int, default=DEFAULT_ANALYSIS_SPEED)
    parser.add_argument("--no-parallel-analysis", action="store_true")
    parser.add_argument("--model-temperature", type=float, default=0.0)
    parser.add_argument("--model-top-p", type=float, default=0.9)
    parser.add_argument("--model-context-window", type=int, default=8192)
    parser.add_argument("--model-max-output-tokens", type=int, default=512)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--progress-jsonl", action="store_true")
    args = parser.parse_args(argv)

    selected = bool(args.image_cleanup or args.similar_image_analysis or args.video_cleanup or args.similar_video_analysis)
    report = run_cleanup_scan(
        args.target_dir,
        image_cleanup=bool(args.image_cleanup),
        similar_image_analysis=bool(args.similar_image_analysis),
        video_cleanup=bool(args.video_cleanup or args.similar_video_analysis or not selected),
        similar_video_analysis=bool(args.similar_video_analysis),
        similar_video_threshold=args.similar_video_threshold,
        analysis_speed=args.analysis_speed,
        parallel_analysis=not bool(args.no_parallel_analysis),
        model_temperature=args.model_temperature,
        model_top_p=args.model_top_p,
        model_context_window=args.model_context_window,
        model_max_output_tokens=args.model_max_output_tokens,
        progress_event_callback=print_progress_event if args.progress_jsonl else None,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2 if args.json else None))
    return 0 if report.get("ok") is not False else 1


if __name__ == "__main__":
    raise SystemExit(main())
