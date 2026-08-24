"""视频探测、候选帧规划、评分和缓存测试。"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

from autoslice_cover.video import (
    CACHE_VERSION,
    FrameCandidate,
    FrameMetrics,
    VideoMetadata,
    _extract_frame,
    _improve_subtitle_candidates,
    _read_cached_candidates,
    _sort_candidates,
    extract_candidate_frames,
    extract_frame_at_timestamp,
    plan_candidate_timestamps,
    probe_video,
    score_frame,
)


class TimestampPlanningTests(unittest.TestCase):
    """验证候选时间点会避开视频首尾。"""

    def test_plans_evenly_spaced_timestamps_inside_video(self) -> None:
        timestamps = plan_candidate_timestamps(100.0, count=6)

        self.assertEqual(len(timestamps), 6)
        self.assertGreater(timestamps[0], 8.0)
        self.assertLess(timestamps[-1], 92.0)
        intervals = [right - left for left, right in zip(timestamps, timestamps[1:])]
        self.assertAlmostEqual(max(intervals), min(intervals), places=2)

    def test_uses_absolute_intro_and_outro_safety_for_finished_clips(self) -> None:
        timestamps = plan_candidate_timestamps(30.0, count=4)

        self.assertGreater(timestamps[0], 4.0)
        self.assertLess(timestamps[-1], 27.0)

    def test_anchor_planning_keeps_global_coverage_and_adds_local_density(self) -> None:
        timestamps = plan_candidate_timestamps(
            100.0,
            count=12,
            cover_anchor_seconds=50.0,
        )

        self.assertEqual(len(timestamps), 12)
        self.assertEqual(len(set(timestamps)), 12)
        self.assertIn(50.0, timestamps)
        self.assertTrue(any(timestamp < 25.0 for timestamp in timestamps))
        self.assertTrue(any(timestamp > 75.0 for timestamp in timestamps))
        self.assertGreaterEqual(
            sum(abs(timestamp - 50.0) <= 6.0 for timestamp in timestamps),
            4,
        )

    def test_invalid_anchor_falls_back_to_existing_uniform_plan(self) -> None:
        expected = plan_candidate_timestamps(30.0, count=6)

        for anchor in (None, float("nan"), float("inf"), -0.1, 30.1, "12", True):
            with self.subTest(anchor=anchor):
                self.assertEqual(
                    plan_candidate_timestamps(
                        30.0,
                        count=6,
                        cover_anchor_seconds=anchor,
                    ),
                    expected,
                )

    def test_anchor_planning_is_stable_at_both_ends_of_short_video(self) -> None:
        for anchor in (0.0, 0.1, 2.0):
            with self.subTest(anchor=anchor):
                timestamps = plan_candidate_timestamps(
                    2.0,
                    count=12,
                    cover_anchor_seconds=anchor,
                )

                self.assertEqual(len(timestamps), 12)
                self.assertEqual(len(set(timestamps)), 12)
                self.assertTrue(all(0.0 < timestamp < 2.0 for timestamp in timestamps))
                self.assertGreaterEqual(
                    sum(abs(timestamp - anchor) <= 0.75 for timestamp in timestamps),
                    4,
                )
        self.assertIn(0.04, plan_candidate_timestamps(2.0, count=12, cover_anchor_seconds=0.0))
        self.assertIn(1.96, plan_candidate_timestamps(2.0, count=12, cover_anchor_seconds=2.0))

    def test_single_candidate_uses_anchor_or_nearest_safe_boundary(self) -> None:
        self.assertEqual(
            plan_candidate_timestamps(100.0, count=1, cover_anchor_seconds=50.0),
            [50.0],
        )
        self.assertEqual(
            plan_candidate_timestamps(2.0, count=1, cover_anchor_seconds=0.0),
            [0.05],
        )
        self.assertEqual(
            plan_candidate_timestamps(2.0, count=1, cover_anchor_seconds=2.0),
            [1.95],
        )

    def test_rejects_invalid_duration_and_count(self) -> None:
        with self.assertRaisesRegex(ValueError, "时长必须为正数"):
            plan_candidate_timestamps(0)
        with self.assertRaisesRegex(ValueError, "数量必须为正数"):
            plan_candidate_timestamps(10, count=0)
        with self.assertRaisesRegex(ValueError, "不能为负数"):
            plan_candidate_timestamps(10, intro_seconds=-1)

    def test_extracts_exact_timeline_frame_and_reuses_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "clip.mp4"
            source.write_bytes(b"video")
            cache = root / "cache"
            metadata = VideoMetadata(str(source.resolve()), 45.0, 1920, 1080, 30.0)
            calls = []

            def fake_extract(_ffmpeg, _source, timestamp, output, _label):
                calls.append(timestamp)
                Image.new("RGB", (320, 180), "#4cb7ca").save(output)

            with (
                patch("autoslice_cover.video.probe_video", return_value=metadata),
                patch("autoslice_cover.video.find_media_binary", return_value="ffmpeg"),
                patch("autoslice_cover.video._extract_frame", side_effect=fake_extract),
            ):
                first, first_metadata = extract_frame_at_timestamp(
                    source,
                    12.3,
                    cache_dir=cache,
                )
                second, _ = extract_frame_at_timestamp(
                    source,
                    12.3,
                    cache_dir=cache,
                )

            self.assertEqual(calls, [12.3])
            self.assertEqual(first_metadata.duration, 45.0)
            self.assertEqual(first.timestamp, 12.3)
            self.assertFalse(first.cached)
            self.assertTrue(second.cached)
            self.assertEqual(first.path, second.path)


class FrameScoringTests(unittest.TestCase):
    """验证高对比、清晰画面会获得更高评分。"""

    def test_detailed_colorful_image_scores_above_flat_gray(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            flat_path = root / "flat.png"
            detailed_path = root / "detailed.png"
            Image.new("RGB", (320, 180), "#808080").save(flat_path)

            detailed = Image.new("RGB", (320, 180), "white")
            draw = ImageDraw.Draw(detailed)
            colors = ("#ff315d", "#ffe238", "#18dce8", "#202020")
            for y in range(0, 180, 15):
                for x in range(0, 320, 15):
                    draw.rectangle((x, y, x + 14, y + 14), fill=colors[(x // 15 + y // 15) % 4])
            detailed.save(detailed_path)

            flat_score, flat_metrics = score_frame(flat_path)
            detailed_score, detailed_metrics = score_frame(detailed_path)

        self.assertGreater(detailed_score, flat_score)
        self.assertGreater(detailed_metrics.sharpness, flat_metrics.sharpness)
        self.assertGreater(detailed_metrics.saturation, flat_metrics.saturation)

    def test_subtitle_band_is_detected_and_penalized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clean_path = root / "clean.png"
            subtitle_path = root / "subtitle.png"
            clean = Image.new("RGB", (640, 360), "#ba8fa9")
            clean.save(clean_path)

            subtitle = clean.copy()
            draw = ImageDraw.Draw(subtitle)
            for x in range(170, 470, 24):
                draw.rectangle((x, 196, x + 15, 220), fill="white", outline="#3c1d32", width=3)
            subtitle.save(subtitle_path)

            clean_score, clean_metrics = score_frame(clean_path)
            subtitle_score, subtitle_metrics = score_frame(subtitle_path)

        self.assertLess(clean_metrics.subtitle_risk, 0.1)
        self.assertGreater(subtitle_metrics.subtitle_risk, clean_metrics.subtitle_risk)
        self.assertLess(subtitle_score, clean_score)


class SubtitleNeighborhoodTests(unittest.TestCase):
    """验证字幕密集时只补足必要数量的邻域候选。"""

    def test_searches_neighbors_until_three_low_risk_frames_exist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "video.mp4"
            source.write_bytes(b"video")
            candidates = []
            for index in range(4):
                path = root / f"frame-{index + 1:02d}.jpg"
                Image.new("RGB", (320, 180), "#b98aa5").save(path)
                risk = 0.0 if index == 0 else 0.6
                candidates.append(
                    FrameCandidate(
                        path=str(path),
                        timestamp=10.0 + index * 5,
                        score=50.0,
                        metrics=FrameMetrics(0.5, 1.0, 0.5, 0.5, 0.4, risk),
                    )
                )

            def fake_extract(
                ffmpeg: str,
                video: Path,
                timestamp: float,
                output: Path,
                label: str,
            ) -> None:
                Image.new("RGB", (320, 180), "#d986ad").save(output)

            low_metrics = FrameMetrics(0.5, 1.0, 0.6, 0.7, 0.4, 0.0)
            with patch("autoslice_cover.video._extract_frame", side_effect=fake_extract) as extract:
                with patch("autoslice_cover.video.score_frame", return_value=(72.0, low_metrics)):
                    improved = _improve_subtitle_candidates(
                        candidates,
                        ffmpeg="ffmpeg",
                        source=source,
                        duration=40.0,
                    )

            self.assertEqual(sum(item.metrics.subtitle_risk <= 0.05 for item in improved), 3)
            self.assertEqual(extract.call_count, 2)
            self.assertTrue(all(Path(item.path).is_file() for item in improved))

    def test_anchor_sorting_keeps_local_moment_ahead_of_clean_remote_frame(self) -> None:
        local_subtitle = FrameCandidate(
            path="local-subtitle.jpg",
            timestamp=50.2,
            score=18.0,
            metrics=FrameMetrics(0.5, 0.7, 0.6, 0.6, 0.6, 0.9),
        )
        local_clean = FrameCandidate(
            path="local-clean.jpg",
            timestamp=50.4,
            score=55.0,
            metrics=FrameMetrics(0.5, 0.7, 0.6, 0.6, 0.6, 0.05),
        )
        remote_clean = FrameCandidate(
            path="remote-clean.jpg",
            timestamp=10.0,
            score=99.0,
            metrics=FrameMetrics(0.5, 1.0, 1.0, 1.0, 1.0, 0.0),
        )

        ranked = _sort_candidates(
            [remote_clean, local_subtitle, local_clean],
            cover_anchor_seconds=50.0,
            duration=100.0,
        )

        self.assertEqual(ranked[:2], [local_clean, local_subtitle])
        self.assertEqual(ranked[-1], remote_clean)

    def test_anchor_sorting_prefers_lower_subtitle_risk_when_quality_is_close(self) -> None:
        slightly_better_but_risky = FrameCandidate(
            path="risky.jpg",
            timestamp=50.2,
            score=22.7,
            metrics=FrameMetrics(0.5, 0.7, 0.6, 0.6, 0.6, 0.9),
        )
        nearly_equal_clean = FrameCandidate(
            path="clean.jpg",
            timestamp=50.4,
            score=57.9,
            metrics=FrameMetrics(0.5, 0.7, 0.6, 0.6, 0.6, 0.05),
        )

        ranked = _sort_candidates(
            [slightly_better_but_risky, nearly_equal_clean],
            cover_anchor_seconds=50.0,
            duration=100.0,
        )

        self.assertGreater(
            slightly_better_but_risky.score
            + slightly_better_but_risky.metrics.subtitle_risk * 42.0,
            nearly_equal_clean.score + nearly_equal_clean.metrics.subtitle_risk * 42.0,
        )
        self.assertEqual(ranked[0], nearly_equal_clean)

    def test_anchor_subtitle_search_does_not_rewrite_remote_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "video.mp4"
            source.write_bytes(b"video")
            local_path = root / "local.jpg"
            remote_path = root / "remote.jpg"
            Image.new("RGB", (320, 180), "#b98aa5").save(local_path)
            Image.new("RGB", (320, 180), "#b98aa5").save(remote_path)
            risky = FrameMetrics(0.5, 0.8, 0.6, 0.6, 0.4, 0.8)
            candidates = [
                FrameCandidate(str(local_path), 10.0, 40.0, risky),
                FrameCandidate(str(remote_path), 80.0, 40.0, risky),
            ]

            def fake_run(command, **_kwargs):
                Image.new("RGB", (320, 180), "#d986ad").save(Path(command[-1]))
                return subprocess.CompletedProcess(command, 0, "", "")

            clean = FrameMetrics(0.5, 0.8, 0.6, 0.6, 0.4, 0.0)
            with (
                patch("autoslice_cover.video.subprocess.run", side_effect=fake_run) as run,
                patch("autoslice_cover.video.score_frame", return_value=(70.0, clean)),
            ):
                improved = _improve_subtitle_candidates(
                    candidates,
                    ffmpeg="ffmpeg",
                    source=source,
                    duration=100.0,
                    cover_anchor_seconds=10.0,
                )

            self.assertEqual(run.call_count, 1)
            self.assertEqual(improved[0].metrics.subtitle_risk, 0.0)
            self.assertEqual(improved[1], candidates[1])


class MediaReliabilityTests(unittest.TestCase):
    """验证媒体进程超时和候选缓存的安全事务。"""

    def test_probe_and_frame_extraction_timeouts_are_bounded_and_clean_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "video.mp4"
            source.write_bytes(b"video")
            output = root / "frame.jpg"

            with (
                patch("autoslice_cover.video.find_media_binary", return_value="ffprobe"),
                patch(
                    "autoslice_cover.video.subprocess.run",
                    side_effect=subprocess.TimeoutExpired(["ffprobe"], 30),
                ) as run,
                self.assertRaisesRegex(RuntimeError, "ffprobe.*超时"),
            ):
                probe_video(source)
            self.assertIsNotNone(run.call_args.kwargs.get("timeout"))

            def timeout_with_partial(*_args, **_kwargs):
                output.write_bytes(b"partial")
                raise subprocess.TimeoutExpired(["ffmpeg"], 90)

            with (
                patch("autoslice_cover.video.subprocess.run", side_effect=timeout_with_partial),
                self.assertRaisesRegex(RuntimeError, "候选帧.*超时"),
            ):
                _extract_frame("ffmpeg", source, 1.0, output, "1")
            self.assertFalse(output.exists())

    def test_cache_rejects_wrong_version_path_escape_and_corrupt_image(self) -> None:
        metrics = FrameMetrics(0.5, 1.0, 0.5, 0.5, 0.4, 0.0).to_dict()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "cache"
            cache.mkdir()
            outside = root / "outside.jpg"
            Image.new("RGB", (32, 18), "red").save(outside)
            manifest = cache / "manifest.json"

            payload = {
                "version": CACHE_VERSION - 1,
                "candidates": [{
                    "filename": "frame.jpg",
                    "timestamp": 1.0,
                    "score": 50.0,
                    "metrics": metrics,
                }],
            }
            (cache / "frame.jpg").write_bytes(outside.read_bytes())
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            self.assertIsNone(_read_cached_candidates(manifest, expected_count=1))

            payload["version"] = CACHE_VERSION
            payload["candidates"][0]["filename"] = "../outside.jpg"
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            self.assertIsNone(_read_cached_candidates(manifest, expected_count=1))

            payload["candidates"][0]["filename"] = "frame.jpg"
            (cache / "frame.jpg").write_bytes(b"not-a-jpeg")
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            self.assertIsNone(_read_cached_candidates(manifest, expected_count=1))

    def test_cache_rejects_non_object_video_metadata(self) -> None:
        metrics = FrameMetrics(0.5, 1.0, 0.5, 0.5, 0.4, 0.0).to_dict()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manifest.json"
            Image.new("RGB", (32, 18), "red").save(root / "frame.jpg")
            payload = {
                "version": CACHE_VERSION,
                "cover_anchor_seconds": 1.0,
                "video": None,
                "candidates": [{
                    "filename": "frame.jpg",
                    "timestamp": 1.0,
                    "score": 50.0,
                    "metrics": metrics,
                }],
            }

            for invalid_video in (None, [], "invalid"):
                with self.subTest(video=invalid_video):
                    payload["video"] = invalid_video
                    manifest.write_text(json.dumps(payload), encoding="utf-8")
                    self.assertIsNone(
                        _read_cached_candidates(
                            manifest,
                            expected_count=1,
                            expected_cover_anchor_seconds=1.0,
                        )
                    )
                    payload["cover_anchor_seconds"] = None
                    manifest.write_text(json.dumps(payload), encoding="utf-8")
                    self.assertIsNone(
                        _read_cached_candidates(manifest, expected_count=1)
                    )
                    payload["cover_anchor_seconds"] = 1.0

    def test_concurrent_same_key_builds_once_and_waiter_reuses_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "video.mp4"
            source.write_bytes(b"video")
            cache = root / "cache"
            extraction_started = threading.Event()
            release_extraction = threading.Event()
            calls = []

            def fake_extract(_ffmpeg, _source, _timestamp, output, label):
                calls.append(label)
                if len(calls) == 1:
                    extraction_started.set()
                    release_extraction.wait(timeout=2)
                Image.new("RGB", (64, 36), "#d884ad").save(output)

            metadata = VideoMetadata(str(source.resolve()), 30.0, 640, 360, 30.0)
            metrics = FrameMetrics(0.5, 1.0, 0.5, 0.5, 0.4, 0.0)
            with (
                patch("autoslice_cover.video.probe_video", return_value=metadata),
                patch("autoslice_cover.video.find_media_binary", return_value="ffmpeg"),
                patch("autoslice_cover.video._extract_frame", side_effect=fake_extract),
                patch("autoslice_cover.video.score_frame", return_value=(70.0, metrics)),
                ThreadPoolExecutor(max_workers=2) as executor,
            ):
                first = executor.submit(
                    extract_candidate_frames,
                    source,
                    cache_dir=cache,
                    count=2,
                )
                self.assertTrue(extraction_started.wait(timeout=1))
                second = executor.submit(
                    extract_candidate_frames,
                    source,
                    cache_dir=cache,
                    count=2,
                )
                time.sleep(0.05)
                release_extraction.set()
                results = [first.result(timeout=3), second.result(timeout=3)]

            self.assertEqual(len(calls), 2)
            self.assertEqual(
                sorted(all(item.cached for item in result) for result in results),
                [False, True],
            )

    def test_failed_force_rebuild_preserves_previous_cache_and_cleans_staging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "video.mp4"
            source.write_bytes(b"video")
            cache = root / "cache"
            metadata = VideoMetadata(str(source.resolve()), 30.0, 640, 360, 30.0)
            metrics = FrameMetrics(0.5, 1.0, 0.5, 0.5, 0.4, 0.0)

            def successful_extract(_ffmpeg, _source, _timestamp, output, _label):
                Image.new("RGB", (64, 36), "#d884ad").save(output)

            with (
                patch("autoslice_cover.video.probe_video", return_value=metadata),
                patch("autoslice_cover.video.find_media_binary", return_value="ffmpeg"),
                patch("autoslice_cover.video._extract_frame", side_effect=successful_extract),
                patch("autoslice_cover.video.score_frame", return_value=(70.0, metrics)),
            ):
                first = extract_candidate_frames(source, cache_dir=cache, count=2)
            original_bytes = {
                Path(item.path).name: Path(item.path).read_bytes()
                for item in first
            }

            with (
                patch("autoslice_cover.video.probe_video", return_value=metadata),
                patch("autoslice_cover.video.find_media_binary", return_value="ffmpeg"),
                patch(
                    "autoslice_cover.video._extract_frame",
                    side_effect=RuntimeError("模拟提取失败"),
                ),
                self.assertRaisesRegex(RuntimeError, "模拟提取失败"),
            ):
                extract_candidate_frames(source, cache_dir=cache, count=2, force=True)

            reused = extract_candidate_frames(source, cache_dir=cache, count=2)
            leftovers = [
                path.name for path in cache.iterdir()
                if path.name.startswith(".")
            ]

            self.assertTrue(all(item.cached for item in reused))
            self.assertEqual(
                original_bytes,
                {Path(item.path).name: Path(item.path).read_bytes() for item in reused},
            )
            self.assertEqual(leftovers, [])

    def test_candidate_cache_is_isolated_by_cover_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "video.mp4"
            source.write_bytes(b"video")
            cache = root / "cache"
            metadata = VideoMetadata(str(source.resolve()), 30.0, 640, 360, 30.0)
            metrics = FrameMetrics(0.5, 1.0, 0.5, 0.5, 0.4, 0.0)
            calls = []

            def fake_run(command, **_kwargs):
                timestamp = float(command[command.index("-ss") + 1])
                calls.append(timestamp)
                Image.new("RGB", (64, 36), "#d884ad").save(Path(command[-1]))
                return subprocess.CompletedProcess(command, 0, "", "")

            with (
                patch("autoslice_cover.video.probe_video", return_value=metadata) as probe,
                patch("autoslice_cover.video.find_media_binary", return_value="ffmpeg"),
                patch("autoslice_cover.video.subprocess.run", side_effect=fake_run),
                patch("autoslice_cover.video.score_frame", return_value=(70.0, metrics)),
            ):
                first = extract_candidate_frames(
                    source,
                    cache_dir=cache,
                    count=2,
                    cover_anchor_seconds=8.0,
                )
                second = extract_candidate_frames(
                    source,
                    cache_dir=cache,
                    count=2,
                    cover_anchor_seconds=16.0,
                )
                reused = extract_candidate_frames(
                    source,
                    cache_dir=cache,
                    count=2,
                    cover_anchor_seconds=8.0,
                )

            self.assertEqual(len(calls), 4)
            self.assertEqual(probe.call_count, 2)
            self.assertNotEqual(
                {Path(candidate.path).parent for candidate in first},
                {Path(candidate.path).parent for candidate in second},
            )
            self.assertTrue(all(not candidate.cached for candidate in first + second))
            self.assertTrue(all(candidate.cached for candidate in reused))
            self.assertEqual(
                [candidate.timestamp for candidate in reused],
                [candidate.timestamp for candidate in first],
            )


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "需要 ffmpeg/ffprobe")
class VideoExtractionTests(unittest.TestCase):
    """使用真实合成视频验证探测、提取、排序和缓存。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.video_path = self.root / "synthetic.mp4"
        command = [
            shutil.which("ffmpeg") or "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=24",
            "-t",
            "4",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(self.video_path),
        ]
        subprocess.run(command, check=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_probe_reads_duration_dimensions_and_frame_rate(self) -> None:
        metadata = probe_video(self.video_path)

        self.assertAlmostEqual(metadata.duration, 4.0, places=1)
        self.assertEqual((metadata.width, metadata.height), (640, 360))
        self.assertAlmostEqual(metadata.fps, 24.0, places=1)

    def test_extracts_ranked_candidates_and_reuses_cache(self) -> None:
        cache_dir = self.root / "cache"
        first = extract_candidate_frames(self.video_path, cache_dir=cache_dir, count=4)
        mtimes = {candidate.path: Path(candidate.path).stat().st_mtime_ns for candidate in first}
        second = extract_candidate_frames(self.video_path, cache_dir=cache_dir, count=4)

        self.assertEqual(len(first), 4)
        self.assertEqual([item.score for item in first], sorted((item.score for item in first), reverse=True))
        self.assertTrue(all(Path(item.path).is_file() for item in first))
        self.assertTrue(all(not item.cached for item in first))
        self.assertTrue(all(item.cached for item in second))
        self.assertEqual(
            mtimes,
            {candidate.path: Path(candidate.path).stat().st_mtime_ns for candidate in second},
        )
        self.assertTrue(all(0.0 <= item.metrics.brightness <= 1.0 for item in first))


if __name__ == "__main__":
    unittest.main()
