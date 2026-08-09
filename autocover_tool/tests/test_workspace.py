"""切片工作区扫描、状态和安全媒体访问测试。"""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from autocover.video import FrameCandidate, FrameMetrics
from autocover.workspace import (
    DEFAULT_INPUT_DIR,
    DEFAULT_OUTPUT_DIR,
    MEDIA_TOKEN_TTL_SEC,
    CoverWorkspace,
)


def _candidate(path: Path, timestamp: float, score: float) -> FrameCandidate:
    Image.new("RGB", (320, 180), "#d884ad").save(path)
    return FrameCandidate(
        path=str(path.resolve()),
        timestamp=timestamp,
        score=score,
        metrics=FrameMetrics(0.5, 1.0, 0.6, 0.7, 0.4, 0.0),
    )


class WorkspaceTests(unittest.TestCase):
    """验证工作区面向 API 的稳定行为。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.clips = self.root / "切片"
        self.clips.mkdir()
        (self.clips / "01_12.5s_司机回头.mp4").write_bytes(b"video-a")
        nested = self.clips / "第二组"
        nested.mkdir()
        (nested / "02_30.0s_线下秘密.FLV").write_bytes(b"video-b")
        (nested / "忽略.txt").write_text("不是视频", encoding="utf-8")
        self.title_file = self.root / "投稿标题.md"
        self.title_file.write_text(
            """原文件：`01_12.5s_司机回头.mp4`\n\n**【泽音】打车聊3D被司机回头盯上**\n""",
            encoding="utf-8",
        )
        self.workspace = CoverWorkspace(
            self.clips,
            title_file=self.title_file,
            cache_dir=self.root / "缓存",
            output_dir=self.root / "输出",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_scans_recursively_and_matches_titles(self) -> None:
        tasks = self.workspace.scan()

        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0].title, "【泽音】打车聊3D被司机回头盯上")
        self.assertEqual(tasks[0].template_key, "headline")
        self.assertEqual(tasks[1].title, "线下秘密")
        self.assertEqual(tasks[1].relative_path, "第二组/02_30.0s_线下秘密.FLV")
        self.assertNotEqual(tasks[0].id, tasks[1].id)
        for task in tasks:
            for field in (
                "folder_created_at", "folder_modified_at",
                "source_created_at", "source_modified_at",
            ):
                self.assertIsInstance(getattr(task, field), float)
                self.assertGreater(getattr(task, field), 0)

    def test_output_paths_preserve_relative_directory(self) -> None:
        nested_task = self.workspace.scan()[1]

        self.assertEqual(Path(nested_task.output_paths["4x3"]).parent.name, "第二组")
        self.assertEqual(Path(nested_task.output_paths["4x3"]).name, "02_30.0s_线下秘密-4x3.jpg")
        self.assertEqual(Path(nested_task.output_paths["16x9"]).name, "02_30.0s_线下秘密-16x9.jpg")

    def test_same_stem_different_extensions_get_distinct_output_names(self) -> None:
        collision_dir = self.clips / "同名"
        collision_dir.mkdir()
        (collision_dir / "same.mp4").write_bytes(b"mp4")
        (collision_dir / "same.mkv").write_bytes(b"mkv")

        tasks = [
            task
            for task in self.workspace.scan()
            if task.relative_path.startswith("同名/")
        ]

        self.assertEqual(len(tasks), 2)
        self.assertEqual(
            {Path(task.output_paths["4x3"]).name for task in tasks},
            {"same-mp4-4x3.jpg", "same-mkv-4x3.jpg"},
        )

    def test_default_output_directory_and_explicit_override(self) -> None:
        default_workspace = CoverWorkspace(self.clips, cache_dir=self.root / "默认缓存")

        self.assertEqual(DEFAULT_INPUT_DIR, (Path.cwd() / "input").resolve())
        self.assertEqual(DEFAULT_OUTPUT_DIR, (Path.cwd() / "covers").resolve())
        self.assertEqual(default_workspace.output_dir, DEFAULT_OUTPUT_DIR.resolve())
        self.assertEqual(self.workspace.output_dir, (self.root / "输出").resolve())

    def test_scan_ignores_material_and_cover_directories(self) -> None:
        for directory_name in ("视频素材", "封面", "封面输出"):
            ignored = self.clips / directory_name / "子目录"
            ignored.mkdir(parents=True)
            (ignored / f"不应扫描-{directory_name}.mp4").write_bytes(b"ignored")

        tasks = self.workspace.scan()

        self.assertEqual(len(tasks), 2)
        self.assertFalse(
            any(
                directory_name in task.relative_path
                for task in tasks
                for directory_name in ("视频素材", "封面", "封面输出")
            )
        )

    def test_default_output_keeps_source_folder_to_avoid_same_name_collisions(self) -> None:
        videos_root = self.root / "Videos"
        title_folder = videos_root / "【泽音】下飞机遇到狂风"
        title_folder.mkdir(parents=True)
        (title_folder / "7月13日.mp4").write_bytes(b"video")
        default_output = videos_root / "封面"

        with patch("autocover.workspace.DEFAULT_OUTPUT_DIR", default_output):
            task = CoverWorkspace(title_folder).scan()[0]

        expected = default_output / title_folder.name / "7月13日-4x3.jpg"
        self.assertEqual(Path(task.output_paths["4x3"]), expected.resolve())

    def test_generates_candidates_and_exposes_only_media_tokens(self) -> None:
        task = self.workspace.scan()[0]
        frames = self.root / "帧"
        frames.mkdir()
        candidates = [
            _candidate(frames / "best.jpg", 8.0, 88.0),
            _candidate(frames / "other.jpg", 15.0, 72.0),
        ]

        with patch("autocover.workspace.extract_candidate_frames", return_value=candidates):
            updated = self.workspace.generate_candidates(task.id)

        payload = self.workspace.task_payload(task.id)
        self.assertEqual(updated.status, "ready")
        self.assertEqual(len(payload["candidates"]), 2)
        self.assertTrue(payload["candidates"][0]["selected"])
        self.assertNotIn(str(self.root), str(payload))
        token = payload["candidates"][1]["token"]
        self.workspace.select_candidate(task.id, token)
        self.assertEqual(self.workspace.selected_candidate(task.id).timestamp, 15.0)

    def test_rejects_unknown_or_cross_task_media_tokens(self) -> None:
        first, second = self.workspace.scan()
        frame = _candidate(self.root / "frame.jpg", 5.0, 80.0)
        with patch("autocover.workspace.extract_candidate_frames", return_value=[frame]):
            self.workspace.generate_candidates(first.id)
        token = self.workspace.task_payload(first.id)["candidates"][0]["token"]

        with self.assertRaisesRegex(KeyError, "令牌无效"):
            self.workspace.resolve_media("../../Windows/system.ini")
        with self.assertRaisesRegex(ValueError, "不是该任务"):
            self.workspace.select_candidate(second.id, token)

    def test_media_tokens_expire_and_use_lru_limit(self) -> None:
        files = []
        for index in range(3):
            path = self.root / f"media-{index}.jpg"
            Image.new("RGB", (32, 32), "#d884ad").save(path)
            files.append(path)

        with patch("autocover.workspace.time.time", return_value=100.0):
            expiring_token = self.workspace.media_token(files[0])
        with (
            patch(
                "autocover.workspace.time.time",
                return_value=100.0 + MEDIA_TOKEN_TTL_SEC + 1,
            ),
            self.assertRaisesRegex(KeyError, "已过期"),
        ):
            self.workspace.resolve_media(expiring_token)

        with (
            patch("autocover.workspace.MEDIA_TOKEN_LIMIT", 2),
            patch(
                "autocover.workspace.time.time",
                side_effect=(200.0, 201.0, 202.0),
            ),
        ):
            oldest = self.workspace.media_token(files[0])
            self.workspace.media_token(files[1])
            self.workspace.media_token(files[2])
        with (
            patch("autocover.workspace.time.time", return_value=203.0),
            self.assertRaisesRegex(KeyError, "已过期"),
        ):
            self.workspace.resolve_media(oldest)

    def test_preview_cache_keeps_only_recent_versions(self) -> None:
        task = self.workspace.scan()[0]
        preview_dir = self.workspace.cache_dir / "previews" / task.id
        preview_dir.mkdir(parents=True)
        for index in range(5):
            cover = preview_dir / f"4x3-{index}.jpg"
            background = preview_dir / f"4x3-{index}-background.jpg"
            cover.write_bytes(b"cover")
            background.write_bytes(b"background")
            os.utime(cover, (100 + index, 100 + index))
            os.utime(background, (100 + index, 100 + index))

        removed = self.workspace.cleanup_preview_cache(task.id, keep=2)

        self.assertEqual(
            {path.name for path in preview_dir.glob("*.jpg")},
            {
                "4x3-3.jpg",
                "4x3-3-background.jpg",
                "4x3-4.jpg",
                "4x3-4-background.jpg",
            },
        )
        self.assertEqual(len(removed), 6)

    def test_candidate_failure_is_recorded_on_task(self) -> None:
        task = self.workspace.scan()[0]
        with patch(
            "autocover.workspace.extract_candidate_frames",
            side_effect=RuntimeError("ffmpeg 提取失败"),
        ):
            with self.assertRaisesRegex(RuntimeError, "ffmpeg"):
                self.workspace.generate_candidates(task.id)

        self.assertEqual(task.status, "error")
        self.assertEqual(task.error, "ffmpeg 提取失败")

    def test_rescan_keeps_existing_candidate_selection(self) -> None:
        task = self.workspace.scan()[0]
        frames = self.root / "候选"
        frames.mkdir()
        candidates = [
            _candidate(frames / "one.jpg", 5.0, 80.0),
            _candidate(frames / "two.jpg", 10.0, 70.0),
        ]
        with patch("autocover.workspace.extract_candidate_frames", return_value=candidates):
            self.workspace.generate_candidates(task.id)
        second_token = self.workspace.task_payload(task.id)["candidates"][1]["token"]
        self.workspace.select_candidate(task.id, second_token)

        rescanned = self.workspace.scan()[0]
        self.assertEqual(rescanned.status, "ready")
        self.assertEqual(rescanned.selected_index, 1)

    def test_duplicate_candidate_generation_is_rejected_while_extracting(self) -> None:
        task = self.workspace.scan()[0]
        started = threading.Event()
        release = threading.Event()
        frame = _candidate(self.root / "并发候选.jpg", 5.0, 80.0)
        call_count = 0
        call_lock = threading.Lock()

        def blocking_extract(*_args, **_kwargs):
            nonlocal call_count
            with call_lock:
                call_count += 1
            started.set()
            release.wait(timeout=2)
            return [frame]

        with (
            patch(
                "autocover.workspace.extract_candidate_frames",
                side_effect=blocking_extract,
            ),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            first = executor.submit(self.workspace.generate_candidates, task.id)
            self.assertTrue(started.wait(timeout=1))
            with self.assertRaisesRegex(RuntimeError, "正在提取"):
                self.workspace.generate_candidates(task.id)
            release.set()
            completed = first.result(timeout=3)

        self.assertEqual(call_count, 1)
        self.assertEqual(completed.status, "ready")

    def test_generation_completion_updates_task_rebuilt_by_rescan(self) -> None:
        task = self.workspace.scan()[0]
        started = threading.Event()
        release = threading.Event()
        frame = _candidate(self.root / "重扫候选.jpg", 5.0, 80.0)

        def blocking_extract(*_args, **_kwargs):
            started.set()
            release.wait(timeout=2)
            return [frame]

        with (
            patch(
                "autocover.workspace.extract_candidate_frames",
                side_effect=blocking_extract,
            ),
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            future = executor.submit(self.workspace.generate_candidates, task.id)
            self.assertTrue(started.wait(timeout=1))
            rescanned = self.workspace.scan()[0]
            self.assertEqual(rescanned.status, "extracting")
            release.set()
            completed = future.result(timeout=3)

        current = self.workspace.get_task(task.id)
        self.assertIs(completed, current)
        self.assertEqual(current.status, "ready")
        self.assertEqual(current.candidates, (frame,))

    def test_generation_does_not_resurrect_task_removed_while_extracting(self) -> None:
        task = self.workspace.scan()[0]
        started = threading.Event()
        release = threading.Event()
        frame = _candidate(self.root / "移除候选.jpg", 5.0, 80.0)

        def blocking_extract(*_args, **_kwargs):
            started.set()
            release.wait(timeout=2)
            return [frame]

        with (
            patch(
                "autocover.workspace.extract_candidate_frames",
                side_effect=blocking_extract,
            ),
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            future = executor.submit(self.workspace.generate_candidates, task.id)
            self.assertTrue(started.wait(timeout=1))
            self.workspace.remove_task(task.id)
            release.set()
            with self.assertRaisesRegex(KeyError, "提取期间被移除"):
                future.result(timeout=3)

        with self.assertRaisesRegex(KeyError, "封面任务不存在"):
            self.workspace.get_task(task.id)

    def test_remove_task_only_updates_the_current_queue(self) -> None:
        first, second = self.workspace.scan()
        source = Path(first.video_path)

        removed = self.workspace.remove_task(first.id)

        self.assertEqual(removed.id, first.id)
        self.assertTrue(source.is_file())
        self.assertEqual([task.id for task in self.workspace.list_tasks()], [second.id])
        with self.assertRaisesRegex(KeyError, "封面任务不存在"):
            self.workspace.get_task(first.id)
        with self.assertRaisesRegex(KeyError, "封面任务不存在"):
            self.workspace.remove_task("not-found")

    def test_validates_root_and_editable_fields(self) -> None:
        with self.assertRaisesRegex(NotADirectoryError, "目录不存在"):
            CoverWorkspace(self.root / "没有这个目录")

        task = self.workspace.scan()[0]
        with self.assertRaisesRegex(ValueError, "标题不能为空"):
            self.workspace.update_task(task.id, title="  ")
        with self.assertRaisesRegex(ValueError, "不支持的封面模板"):
            self.workspace.update_task(task.id, template_key="unknown")

    def test_update_task_validates_every_field_before_committing(self) -> None:
        task = self.workspace.scan()[0]
        original = (task.title, task.template_key, task.palette_key)

        with self.assertRaisesRegex(ValueError, "不支持的调色板"):
            self.workspace.update_task(
                task.id,
                title="不应留下的新标题",
                template_key="headline",
                palette_key="unknown",
            )

        current = self.workspace.get_task(task.id)
        self.assertEqual(
            (current.title, current.template_key, current.palette_key),
            original,
        )


if __name__ == "__main__":
    unittest.main()
