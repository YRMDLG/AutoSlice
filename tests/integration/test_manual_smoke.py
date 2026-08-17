"""安全真实流水线冒烟脚本的隔离测试。"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from scripts.manual import run_real_pipeline_smoke as smoke


class ManualSmokeTests(unittest.TestCase):
    """所有输入均为临时空占位文件，runner 始终使用 mock。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="autoslice-manual-smoke-")
        self.root = Path(self.temporary.name)
        self.video = self.root / "recording.mp4"
        self.srt = self.root / "recording.srt"
        self.danmaku = self.root / "danmaku.xml"
        self.timeline = self.root / "timeline.docx"
        for path in (self.video, self.srt, self.danmaku, self.timeline):
            path.touch()
        self.srt.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\n占位字幕\n",
            encoding="utf-8",
        )
        self.output_dir = self.root / "smoke-output"

    def tearDown(self):
        self.temporary.cleanup()

    def base_argv(self) -> list[str]:
        return [
            "--video",
            os.fspath(self.video),
            "--srt",
            os.fspath(self.srt),
            "--danmaku",
            os.fspath(self.danmaku),
            "--timeline",
            os.fspath(self.timeline),
            "--output-dir",
            os.fspath(self.output_dir),
        ]

    def invoke(self, argv, *, analyze_runner=None, slice_runner=None):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                code = smoke.main(
                    argv,
                    analyze_runner=analyze_runner,
                    slice_runner=slice_runner,
                )
            except SystemExit as exc:
                code = int(exc.code)
        return code, stdout.getvalue(), stderr.getvalue()

    def analysis_result(self, *, secret=""):
        artifact_dir = self.output_dir / "recording_分析整理包"
        return {
            "artifact_dir": os.fspath(artifact_dir),
            "overview_path": os.fspath(artifact_dir / "00_概览.md"),
            "json_path": os.fspath(artifact_dir / "data" / "clip_marks.json"),
            "topic_count": 3,
            "clip_marks": [{"start": 1, "end": 2, "title": "占位"}],
            "streamer_profile_id": "generic",
            "report": f"不应输出的字幕正文 {secret}",
            "token": secret,
            "proxy_auth": secret,
        }

    def test_no_arguments_and_missing_arguments_have_zero_side_effects(self):
        analyzer = mock.Mock()
        slicer = mock.Mock()
        before = set(self.root.iterdir())
        no_args = self.invoke(
            [],
            analyze_runner=analyzer,
            slice_runner=slicer,
        )
        missing_args = self.invoke(
            ["--video", os.fspath(self.video)],
            analyze_runner=analyzer,
            slice_runner=slicer,
        )

        self.assertEqual(no_args[0], 2)
        self.assertIn("usage:", no_args[2])
        self.assertIn("缺少参数", no_args[2])
        self.assertEqual(missing_args[0], 2)
        analyzer.assert_not_called()
        slicer.assert_not_called()
        self.assertEqual(set(self.root.iterdir()), before)

    def test_complete_dry_run_never_calls_runners_or_creates_output(self):
        analyzer = mock.Mock()
        slicer = mock.Mock()
        code, stdout, stderr = self.invoke(
            self.base_argv(),
            analyze_runner=analyzer,
            slice_runner=slicer,
        )

        self.assertEqual(code, 0, stderr)
        self.assertIn('"mode": "dry-run"', stdout)
        self.assertIn("未导入真实 owner", stdout)
        analyzer.assert_not_called()
        slicer.assert_not_called()
        self.assertFalse(self.output_dir.exists())

    def test_configured_profile_is_not_limited_to_hardcoded_choices(self):
        analyzer = mock.Mock()
        slicer = mock.Mock()
        resolved = mock.Mock(id="other-streamer")
        with mock.patch(
            "streamer_profiles.resolve_streamer_profile",
            return_value=resolved,
        ) as resolve_profile:
            code, stdout, stderr = self.invoke(
                [*self.base_argv(), "--streamer-profile", "other-streamer"],
                analyze_runner=analyzer,
                slice_runner=slicer,
            )

        self.assertEqual(code, 0, stderr)
        resolve_profile.assert_called_once_with(
            "other-streamer",
            self.video.resolve(),
        )
        self.assertIn('"streamer_profile": "other-streamer"', stdout)
        analyzer.assert_not_called()
        slicer.assert_not_called()

    def test_unknown_profile_is_rejected_before_any_runner(self):
        analyzer = mock.Mock()
        slicer = mock.Mock()
        with mock.patch(
            "streamer_profiles.resolve_streamer_profile",
            side_effect=ValueError("private detail"),
        ):
            code, stdout, stderr = self.invoke(
                [*self.base_argv(), "--streamer-profile", "unknown"],
                analyze_runner=analyzer,
                slice_runner=slicer,
            )

        self.assertEqual(code, 2)
        self.assertIn("未在当前主播配置中定义", stderr)
        self.assertNotIn("private detail", stdout + stderr)
        analyzer.assert_not_called()
        slicer.assert_not_called()
        self.assertFalse(self.output_dir.exists())

    def test_allow_ffmpeg_without_paid_llm_is_rejected(self):
        analyzer = mock.Mock()
        slicer = mock.Mock()
        code, _stdout, stderr = self.invoke(
            [*self.base_argv(), "--allow-ffmpeg"],
            analyze_runner=analyzer,
            slice_runner=slicer,
        )

        self.assertEqual(code, 2)
        self.assertIn("不能单独使用", stderr)
        analyzer.assert_not_called()
        slicer.assert_not_called()
        self.assertFalse(self.output_dir.exists())

    def test_missing_srt_requires_explicit_asr_authorization(self):
        self.srt.unlink()
        argv = [
            "--video",
            os.fspath(self.video),
            "--danmaku",
            os.fspath(self.danmaku),
            "--output-dir",
            os.fspath(self.output_dir),
        ]
        analyzer = mock.Mock()
        code, stdout, stderr = self.invoke(
            argv,
            analyze_runner=analyzer,
            slice_runner=mock.Mock(),
        )

        self.assertEqual(code, 2)
        self.assertIn("--allow-asr", stderr)
        self.assertNotIn(os.fspath(self.root), stdout + stderr)
        analyzer.assert_not_called()
        self.assertFalse(self.output_dir.exists())

    def test_optional_timeline_and_authorized_asr_match_real_pipeline_contract(self):
        self.srt.unlink()
        argv = [
            "--video",
            os.fspath(self.video),
            "--danmaku",
            os.fspath(self.danmaku),
            "--output-dir",
            os.fspath(self.output_dir),
            "--allow-asr",
        ]
        dry_analyzer = mock.Mock()
        dry = self.invoke(
            argv,
            analyze_runner=dry_analyzer,
            slice_runner=mock.Mock(),
        )

        self.assertEqual(dry[0], 0, dry[2])
        self.assertIn('"srt_status": "missing-or-empty-asr-authorized"', dry[1])
        self.assertIn('"timeline": null', dry[1])
        dry_analyzer.assert_not_called()
        self.assertFalse(self.output_dir.exists())

        analyzer = mock.Mock(return_value=self.analysis_result())
        paid = self.invoke(
            [*argv, "--allow-paid-llm"],
            analyze_runner=analyzer,
            slice_runner=mock.Mock(),
        )
        self.assertEqual(paid[0], 0, paid[2])
        analyzer.assert_called_once_with(
            os.fspath(self.video.resolve()),
            ass_path=os.fspath(self.danmaku.resolve()),
            manual_timeline_path=None,
            output_dir=os.fspath(self.output_dir.resolve()),
            streamer_profile_id="generic",
        )

    def test_paid_only_calls_analysis_owner_without_slicing(self):
        analyzer = mock.Mock(return_value=self.analysis_result())
        slicer = mock.Mock()
        code, stdout, stderr = self.invoke(
            [*self.base_argv(), "--allow-paid-llm"],
            analyze_runner=analyzer,
            slice_runner=slicer,
        )

        self.assertEqual(code, 0, stderr)
        analyzer.assert_called_once_with(
            os.fspath(self.video.resolve()),
            ass_path=os.fspath(self.danmaku.resolve()),
            manual_timeline_path=os.fspath(self.timeline.resolve()),
            output_dir=os.fspath(self.output_dir.resolve()),
            streamer_profile_id="generic",
        )
        self.assertNotIn("srt_path", analyzer.call_args.kwargs)
        self.assertNotIn("auto_slice", analyzer.call_args.kwargs)
        slicer.assert_not_called()
        self.assertTrue(self.output_dir.is_dir())
        self.assertIn('"slice_count": 0', stdout)
        self.assertIn("不会生成成片", stdout)

    def test_dual_authorization_analyzes_then_calls_unique_slice_owner(self):
        calls = []

        def analyze(*args, **kwargs):
            calls.append("analyze")
            return self.analysis_result()

        def slice_marks(*args, **kwargs):
            calls.append("slice")
            return 2, os.fspath(self.output_dir / "recording_话题切片")

        analyzer = mock.Mock(side_effect=analyze)
        slicer = mock.Mock(side_effect=slice_marks)
        code, stdout, stderr = self.invoke(
            [
                *self.base_argv(),
                "--allow-paid-llm",
                "--allow-ffmpeg",
            ],
            analyze_runner=analyzer,
            slice_runner=slicer,
        )

        self.assertEqual(code, 0, stderr)
        self.assertEqual(calls, ["analyze", "slice"])
        json_path = self.analysis_result()["json_path"]
        slicer.assert_called_once_with(
            os.fspath(self.video.resolve()),
            json_path,
            os.fspath(self.output_dir.resolve()),
            streamer_profile_id="generic",
        )
        self.assertIn('"slice_count": 2', stdout)
        self.assertIn('"artifact_dir":', stdout)
        self.assertIn('"overview_path":', stdout)
        self.assertIn('"json_path":', stdout)
        self.assertIn('"topic_count": 3', stdout)

    def test_relative_missing_sibling_and_output_conflicts_are_rejected(self):
        cases = []
        relative = self.base_argv()
        relative[1] = "recording.mp4"
        cases.append(("relative", relative))

        missing = self.base_argv()
        missing[7] = os.fspath(self.root / "missing.docx")
        cases.append(("missing", missing))

        wrong_srt = self.root / "other.srt"
        wrong_srt.touch()
        sibling = self.base_argv()
        sibling[3] = os.fspath(wrong_srt)
        cases.append(("sibling", sibling))

        conflict = self.base_argv()
        conflict[9] = os.fspath(self.root)
        cases.append(("conflict", conflict))

        for name, argv in cases:
            with self.subTest(name=name):
                analyzer = mock.Mock()
                slicer = mock.Mock()
                code, _stdout, _stderr = self.invoke(
                    argv,
                    analyze_runner=analyzer,
                    slice_runner=slicer,
                )
                self.assertEqual(code, 2)
                analyzer.assert_not_called()
                slicer.assert_not_called()
        self.assertFalse(self.output_dir.exists())

    def test_summaries_redact_secrets_and_input_bodies(self):
        secret = "SUPER_SECRET_TOKEN_AND_SUBTITLE_BODY"
        self.srt.write_text(secret, encoding="utf-8")
        self.danmaku.write_text(secret, encoding="utf-8")
        analyzer = mock.Mock(return_value=self.analysis_result(secret=secret))
        code, stdout, stderr = self.invoke(
            [*self.base_argv(), "--allow-paid-llm"],
            analyze_runner=analyzer,
            slice_runner=mock.Mock(),
        )

        self.assertEqual(code, 0, stderr)
        self.assertNotIn(secret, stdout)
        self.assertNotIn(secret, stderr)
        self.assertNotIn(os.fspath(self.video.parent), stdout.split("可复核执行结果：")[0])
        self.assertNotIn('"report"', stdout)
        self.assertNotIn('"token"', stdout)
        self.assertNotIn('"proxy_auth"', stdout)

    def test_runner_exceptions_exit_safely_without_echoing_details(self):
        secret = "PRIVATE_TOKEN_OR_SUBTITLE_TEXT"
        analyzer = mock.Mock(side_effect=RuntimeError(secret))
        slicer = mock.Mock()
        code, stdout, stderr = self.invoke(
            [
                *self.base_argv(),
                "--allow-paid-llm",
                "--allow-ffmpeg",
            ],
            analyze_runner=analyzer,
            slice_runner=slicer,
        )

        self.assertEqual(code, 1)
        self.assertIn("详细信息已隐藏", stderr)
        self.assertNotIn(secret, stdout)
        self.assertNotIn(secret, stderr)
        slicer.assert_not_called()

    def test_failed_first_run_can_resume_only_with_matching_marker(self):
        secret = "PRIVATE_FAILURE_DETAIL"
        failed_analyzer = mock.Mock(side_effect=RuntimeError(secret))
        first_code, first_stdout, first_stderr = self.invoke(
            [*self.base_argv(), "--allow-paid-llm"],
            analyze_runner=failed_analyzer,
            slice_runner=mock.Mock(),
        )

        self.assertEqual(first_code, 1)
        self.assertTrue(self.output_dir.is_dir())
        marker = self.output_dir / smoke.OUTPUT_MARKER_NAME
        self.assertTrue(marker.is_file())
        self.assertNotIn(os.fspath(self.root), marker.read_text(encoding="utf-8"))
        self.assertNotIn(secret, first_stdout + first_stderr)

        no_resume_analyzer = mock.Mock()
        no_resume = self.invoke(
            [*self.base_argv(), "--allow-paid-llm"],
            analyze_runner=no_resume_analyzer,
            slice_runner=mock.Mock(),
        )
        self.assertEqual(no_resume[0], 2)
        self.assertIn("--resume-existing-output", no_resume[2])
        no_resume_analyzer.assert_not_called()

        dry_run_analyzer = mock.Mock()
        dry_run = self.invoke(
            [*self.base_argv(), "--resume-existing-output"],
            analyze_runner=dry_run_analyzer,
            slice_runner=mock.Mock(),
        )
        self.assertEqual(dry_run[0], 0, dry_run[2])
        self.assertIn("<validated-existing-smoke-directory>", dry_run[1])
        dry_run_analyzer.assert_not_called()

        successful_analyzer = mock.Mock(return_value=self.analysis_result())
        resumed = self.invoke(
            [
                *self.base_argv(),
                "--allow-paid-llm",
                "--resume-existing-output",
            ],
            analyze_runner=successful_analyzer,
            slice_runner=mock.Mock(),
        )
        self.assertEqual(resumed[0], 0, resumed[2])
        successful_analyzer.assert_called_once()

    def test_resume_rejects_arbitrary_or_mismatched_existing_directory(self):
        self.output_dir.mkdir()
        arbitrary_analyzer = mock.Mock()
        arbitrary = self.invoke(
            [*self.base_argv(), "--resume-existing-output"],
            analyze_runner=arbitrary_analyzer,
            slice_runner=mock.Mock(),
        )
        self.assertEqual(arbitrary[0], 2)
        self.assertIn("本脚本创建", arbitrary[2])
        arbitrary_analyzer.assert_not_called()

        self.output_dir.rmdir()
        failed_analyzer = mock.Mock(side_effect=RuntimeError("hidden"))
        first = self.invoke(
            [*self.base_argv(), "--allow-paid-llm"],
            analyze_runner=failed_analyzer,
            slice_runner=mock.Mock(),
        )
        self.assertEqual(first[0], 1)

        other_timeline = self.root / "other.docx"
        other_timeline.touch()
        mismatched_args = self.base_argv()
        mismatched_args[7] = os.fspath(other_timeline)
        mismatched_analyzer = mock.Mock()
        mismatched = self.invoke(
            [*mismatched_args, "--resume-existing-output"],
            analyze_runner=mismatched_analyzer,
            slice_runner=mock.Mock(),
        )
        self.assertEqual(mismatched[0], 2)
        self.assertIn("输入或主播配置不一致", mismatched[2])
        mismatched_analyzer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
