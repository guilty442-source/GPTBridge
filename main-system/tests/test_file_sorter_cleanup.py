from __future__ import annotations

import os
import json
from pathlib import Path
from unittest.mock import patch

from file_sorter.infrastructure.cleanup import (
    find_exact_duplicate_candidates,
    recycle_exact_duplicate_candidates,
    run_cleanup_scan,
)
from file_sorter.infrastructure.minicpm_visual_service import (
    MODEL_NAME,
    MiniCPMVisualRecognitionService,
)


def test_exact_duplicates_keep_oldest_and_recycle_only_extra(tmp_path: Path) -> None:
    target = tmp_path / "library"
    trash = tmp_path / "trash"
    target.mkdir()
    trash.mkdir()
    keep = target / "original.bin"
    duplicate = target / "copy.bin"
    keep.write_bytes(b"same-content")
    duplicate.write_bytes(b"same-content")
    os.utime(keep, ns=(1_000_000_000, 1_000_000_000))
    os.utime(duplicate, ns=(2_000_000_000, 2_000_000_000))

    candidates = find_exact_duplicate_candidates(target, quiet_seconds=0)
    assert len(candidates) == 1
    assert Path(candidates[0]["keep_path"]) == keep.resolve()
    assert Path(candidates[0]["path"]) == duplicate.resolve()

    def fake_recycler(path: Path) -> None:
        path.replace(trash / path.name)

    result = recycle_exact_duplicate_candidates(
        target,
        candidates,
        recycler=fake_recycler,
    )
    assert result["ok"] is True
    assert result["recycled_count"] == 1
    assert keep.exists()
    assert not duplicate.exists()


def test_minicpm_visual_service_recognizes_decodable_image(tmp_path: Path) -> None:
    from PIL import Image

    image_path = tmp_path / "empty-scene.png"
    Image.new("RGB", (640, 480), color=(240, 240, 240)).save(image_path)
    response_body = json.dumps({
        "message": {"content": json.dumps({
            "classification": "non_person_candidate",
            "confidence": 0.91,
            "reason": "畫面中沒有真人",
            "tags": ["空景"],
            "summary": "淺色空白畫面",
        }, ensure_ascii=False)}
    }, ensure_ascii=False).encode("utf-8")

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self) -> bytes:
            return response_body

    with patch(
        "file_sorter.infrastructure.minicpm_visual_service.request.urlopen",
        return_value=FakeResponse(),
    ) as mocked:
        result = MiniCPMVisualRecognitionService().predict(image_path)

    assert result.classification == "non_person_candidate"
    assert result.model == MODEL_NAME
    assert result.tags == ("空景",)
    sent = json.loads(mocked.call_args.args[0].data.decode("utf-8"))
    assert sent["model"] == MODEL_NAME
    assert sent["messages"][0]["images"]


def test_minicpm_visual_service_compares_images_with_model(tmp_path: Path) -> None:
    from PIL import Image

    left = tmp_path / "left.png"
    right = tmp_path / "right.png"
    Image.new("RGB", (320, 240), color="navy").save(left)
    Image.new("RGB", (320, 240), color="navy").save(right)
    response_body = json.dumps({
        "message": {"content": json.dumps({
            "similarity": 99,
            "same_content": True,
            "reason": "主體與構圖相同",
        }, ensure_ascii=False)}
    }, ensure_ascii=False).encode("utf-8")

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self) -> bytes:
            return response_body

    with patch(
        "file_sorter.infrastructure.minicpm_visual_service.request.urlopen",
        return_value=FakeResponse(),
    ) as mocked:
        result = MiniCPMVisualRecognitionService().compare(left, right, media_type="image")

    assert result.similarity == 99
    assert result.same_content is True
    sent = json.loads(mocked.call_args.args[0].data.decode("utf-8"))
    assert sent["model"] == MODEL_NAME
    assert len(sent["messages"][0]["images"]) == 2


def test_plain_file_scan_does_not_start_minicpm(tmp_path: Path) -> None:
    target = tmp_path / "library"
    target.mkdir()
    (target / "notes.txt").write_text("plain classification", encoding="utf-8")
    with patch(
        "file_sorter.infrastructure.minicpm_visual_service.MiniCPMVisualRecognitionService",
        side_effect=AssertionError("MiniCPM-V must stay off"),
    ):
        report = run_cleanup_scan(
            target,
            image_cleanup=False,
            similar_image_analysis=False,
            video_cleanup=True,
            similar_video_analysis=False,
        )
    assert report["ok"] is True
    assert report["visual_recognition_service"] == "openbmb/minicpm-v4.6:q8_0"
    assert report["visual_recognition_service_enabled"] is False
