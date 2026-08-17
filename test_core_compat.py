"""退役 ``core`` 期间必须保持的兼容契约。"""

import json
import unittest
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import call, patch

import app as app_module
import core


class CoreCompatibilityTests(unittest.TestCase):
    def test_module_exposes_only_documented_compatibility_surface(self):
        self.assertEqual(
            core.__all__,
            [
                "LegacyCoreDeprecatedError",
                "generate_srt",
                "parse_timeline_json",
                "process_video",
            ],
        )
        retired_helpers = {
            "seconds_to_srt_time",
            "parse_srt",
            "extract_danmaku_timestamps",
            "find_dense_periods",
            "find_context_boundaries",
            "merge_overlapping_periods",
            "detect_video_watching_segments",
            "parse_timeline_docx",
            "slice_video",
            "get_video_duration",
            "is_file_locked",
        }
        for name in retired_helpers:
            with self.subTest(name=name):
                self.assertFalse(hasattr(core, name))

    def test_parse_timeline_json_preserves_complete_mark_contract(self):
        with TemporaryDirectory() as td:
            json_path = Path(td) / "clip_marks.json"
            json_path.write_text(
                json.dumps(
                    {
                        "time_basis": "video_elapsed_seconds",
                        "clip_marks": [
                            {
                                "start": 100,
                                "end": 120,
                                "topic_start": 103,
                                "topic_end": 118,
                                "title": "测试片段",
                            },
                            {
                                "start": 200,
                                "end": 230,
                                "title": "绝对时钟片段",
                                "time_basis": "stream_clock_seconds",
                            },
                            {"start": 300, "end": 300, "title": "无效片段"},
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            marks = core.parse_timeline_json(str(json_path))

        self.assertEqual(
            marks,
            [
                {
                    "start": 100.0,
                    "end": 120.0,
                    "topic_start": 103.0,
                    "topic_end": 118.0,
                    "title": "测试片段",
                    "time_basis": "video_elapsed_seconds",
                },
                {
                    "start": 200.0,
                    "end": 230.0,
                    "topic_start": 200.0,
                    "topic_end": 230.0,
                    "title": "绝对时钟片段",
                    "time_basis": "stream_clock_seconds",
                },
            ],
        )

    def test_generate_srt_forwards_result_and_progress_callback(self):
        progress_callback = unittest.mock.Mock()
        with patch(
            "topic_engine.ensure_srt",
            return_value=r"X:\fixtures\录播\测试.srt",
        ) as ensure_srt:
            result = core.generate_srt(
                r"X:\fixtures\录播\测试.flv",
                progress_callback=progress_callback,
            )

        self.assertEqual(result, r"X:\fixtures\录播\测试.srt")
        ensure_srt.assert_called_once_with(
            r"X:\fixtures\录播\测试.flv",
            progress_callback=progress_callback,
        )

    def test_generate_srt_reports_engine_failure_without_running_fallback(self):
        progress_callback = unittest.mock.Mock()
        with patch(
            "topic_engine.ensure_srt",
            side_effect=RuntimeError("ASR unavailable"),
        ):
            result = core.generate_srt(
                r"X:\fixtures\录播\测试.flv",
                progress_callback=progress_callback,
            )

        self.assertIsNone(result)
        progress_callback.assert_called_once_with(
            "识别失败: ASR unavailable",
            0,
            1,
        )

    def test_generate_srt_reenters_the_atomic_engine_on_each_call(self):
        with patch(
            "topic_engine.ensure_srt",
            side_effect=[r"X:\first.srt", r"X:\second.srt"],
        ) as ensure_srt:
            first = core.generate_srt(r"X:\first.flv")
            second = core.generate_srt(r"X:\second.flv")

        self.assertEqual((first, second), (r"X:\first.srt", r"X:\second.srt"))
        self.assertEqual(
            ensure_srt.call_args_list,
            [
                call(r"X:\first.flv", progress_callback=None),
                call(r"X:\second.flv", progress_callback=None),
            ],
        )

    def test_product_json_reslice_delegates_to_slice_from_marks(self):
        updates = []

        def record_update(task_id, **changes):
            updates.append((task_id, changes))

        with (
            patch.object(app_module, "update_task", side_effect=record_update),
            patch.object(
                app_module,
                "streamer_profile_context",
                return_value=nullcontext(),
            ),
            patch(
                "topic_engine.slice_from_marks",
                return_value=(1, r"X:\output\录播_话题切片"),
            ) as slice_from_marks,
            patch.object(
                app_module,
                "process_video",
                side_effect=AssertionError("JSON 重切不得进入旧 process_video"),
            ),
        ):
            app_module.run_slice_task(
                "direct_slice:test",
                r"X:\input\录播.flv",
                r"X:\input\录播.ass",
                r"X:\output",
                "timeline",
                "",
                timeline_json=r"X:\marks\clip_marks.json",
                streamer_profile="generic",
            )

        slice_from_marks.assert_called_once()
        self.assertEqual(
            slice_from_marks.call_args.args,
            (
                r"X:\input\录播.flv",
                r"X:\marks\clip_marks.json",
                r"X:\output",
            ),
        )
        self.assertEqual(
            slice_from_marks.call_args.kwargs["streamer_profile_id"],
            "generic",
        )
        self.assertTrue(
            callable(slice_from_marks.call_args.kwargs["progress_callback"])
        )
        self.assertEqual(updates[-1][1]["status"], "done")
        self.assertEqual(updates[-1][1]["step"], 100)
        self.assertIn(r"X:\output\录播_话题切片", updates[-1][1]["result"])

    def test_process_video_is_a_deprecation_tombstone(self):
        with self.assertRaisesRegex(
            core.LegacyCoreDeprecatedError,
            r"core\.process_video\(\) 已退役.*slice_from_marks",
        ):
            core.process_video(
                r"X:\input\录播.flv",
                r"X:\input\录播.ass",
                r"X:\output",
                mode="danmaku",
            )


if __name__ == "__main__":
    unittest.main()
