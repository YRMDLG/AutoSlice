import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from autoslice.transcription import workflow


class TranscriptionWorkflowTests(unittest.TestCase):
    @staticmethod
    def _speaker_checkpoint():
        return {
            "speaker_model_used": True,
            "chunks": {
                "0": {
                    "result": [{
                        "text": "主播背景继续",
                        "timestamp": [
                            [0, 1000],
                            [1000, 2000],
                            [2000, 3000],
                            [3000, 4000],
                            [4000, 5000],
                            [5000, 6000],
                        ],
                        "speaker_segments": [
                            {
                                "speaker": "0",
                                "start": 0,
                                "end": 2000,
                                "text": "主播",
                                "timestamp": [[0, 1000], [1000, 2000]],
                            },
                            {
                                "speaker": "1",
                                "start": 2000,
                                "end": 4000,
                                "text": "背景",
                                "timestamp": [[2000, 3000], [3000, 4000]],
                            },
                            {
                                "speaker": "0",
                                "start": 4000,
                                "end": 6000,
                                "text": "继续",
                                "timestamp": [[4000, 5000], [5000, 6000]],
                            },
                        ],
                    }],
                },
            },
        }

    def test_soft_keeps_candidates_and_strict_only_removes_them(self):
        strict_checkpoint = self._speaker_checkpoint()
        soft_segments, soft_stats = workflow._collect_checkpoint_segments(
            self._speaker_checkpoint(),
            chunk_count=1,
            duration=120.0,
            streamer_name="",
            background_filter_mode="soft",
        )
        strict_segments, strict_stats = workflow._collect_checkpoint_segments(
            strict_checkpoint,
            chunk_count=1,
            duration=120.0,
            streamer_name="",
            background_filter_mode="strict",
        )

        self.assertIn("背景", "".join(segment[2] for segment in soft_segments))
        self.assertNotIn("背景", "".join(segment[2] for segment in strict_segments))
        self.assertEqual(soft_stats["detected_speaker_count"], 2)
        self.assertEqual(soft_stats["candidate_segment_count"], 1)
        self.assertEqual(soft_stats["candidate_seconds"], 2.0)
        self.assertEqual(soft_stats["removed_segment_count"], 0)
        self.assertEqual(strict_stats["candidate_segment_count"], 0)
        self.assertEqual(strict_stats["removed_segment_count"], 1)
        self.assertEqual(strict_stats["removed_seconds"], 2.0)
        self.assertEqual(
            len(strict_checkpoint["chunks"]["0"]["result"][0]["speaker_segments"]),
            3,
        )

    def test_default_ensure_srt_mode_is_off_for_full_analysis(self):
        observed = {}
        with TemporaryDirectory() as directory:
            root = Path(directory)
            video_path = root / "recording.flv"
            video_path.write_bytes(b"video")
            checkpoint = {
                "speaker_model_ready": False,
                "speaker_model_used": False,
                "device": "cpu",
                "chunks": {
                    "0": {
                        "result": [{
                            "text": "完整人声",
                            "timestamp": [
                                [0, 500],
                                [500, 1000],
                                [1000, 1500],
                                [1500, 2000],
                            ],
                        }],
                    },
                },
            }

            def prepare(*_args, **kwargs):
                observed.update(kwargs)
                return str(root / "checkpoint.json"), checkpoint

            with (
                patch.object(workflow, "probe_video_duration", return_value=120.0),
                patch.object(workflow.model_runtime, "funasr_hotwords", return_value=[]),
                patch.object(
                    workflow.checkpoint_store,
                    "prepare_funasr_checkpoint",
                    side_effect=prepare,
                ),
                patch.object(
                    workflow.checkpoint_store,
                    "write_funasr_checkpoint",
                ),
            ):
                workflow.ensure_srt(str(video_path))

        self.assertEqual(observed["background_filter_mode"], "off")
        self.assertEqual(checkpoint["background_filter"]["actual_mode"], "off")
        self.assertEqual(
            checkpoint["background_filter"]["removed_segment_count"],
            0,
        )

    def test_strict_campp_load_failure_generates_complete_soft_fallback_srt(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            video_path = root / "recording.flv"
            video_path.write_bytes(b"video")
            checkpoint = {
                "speaker_model_ready": True,
                "speaker_model_used": False,
                "speaker_model_load_failed": True,
                "device": "cpu",
                "chunks": {
                    "0": {
                        "result": [{
                            "text": "主播和背景都保留",
                            "timestamp": [
                                [0, 500],
                                [500, 1000],
                                [1000, 1500],
                                [1500, 2000],
                                [2000, 2500],
                                [2500, 3000],
                                [3000, 3500],
                                [3500, 4000],
                            ],
                        }],
                    },
                },
            }
            with (
                patch.object(workflow, "probe_video_duration", return_value=120.0),
                patch.object(
                    workflow.model_runtime,
                    "funasr_hotwords",
                    return_value=[],
                ),
                patch.object(
                    workflow.checkpoint_store,
                    "prepare_funasr_checkpoint",
                    return_value=(str(root / "checkpoint.json"), checkpoint),
                ),
                patch.object(
                    workflow.checkpoint_store,
                    "write_funasr_checkpoint",
                ),
                patch.object(
                    workflow.recognition,
                    "recognize_missing_chunks",
                ) as recognize,
            ):
                srt_path = workflow.ensure_srt(
                    str(video_path),
                    background_filter_mode="strict",
                )
            content = Path(srt_path).read_text(encoding="utf-8")

        recognize.assert_not_called()
        self.assertIn("主播和背景都保留", content)
        result = checkpoint["background_filter"]
        self.assertEqual(result["requested_mode"], "strict")
        self.assertEqual(result["actual_mode"], "soft")
        self.assertTrue(result["speaker_model_ready"])
        self.assertFalse(result["speaker_model_used"])
        self.assertTrue(result["speaker_model_load_failed"])
        self.assertEqual(result["mode"], "adaptive_gate")
        self.assertEqual(result["removed_segment_count"], 0)
        self.assertEqual(result["removed_seconds"], 0.0)
        self.assertIn("加载失败", result["fallback_reason"])
        self.assertIn("未区分或删除", result["fallback_reason"])

    def test_reuses_complete_srt_without_probing_or_recognizing(self):
        events = []
        with TemporaryDirectory() as directory:
            video_path = Path(directory) / "recording.flv"
            srt_path = video_path.with_suffix(".srt")
            srt_path.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\n测试字幕\n\n",
                encoding="utf-8",
            )
            with (
                patch.object(
                    workflow.checkpoint_store,
                    "existing_srt_is_reusable",
                    return_value=True,
                ),
                patch.object(workflow, "probe_video_duration") as probe,
                patch.object(
                    workflow.recognition,
                    "recognize_missing_chunks",
                ) as recognize,
            ):
                result = workflow.ensure_srt(
                    str(video_path),
                    progress_callback=lambda *args: events.append(args),
                )

        self.assertEqual(result, str(srt_path))
        probe.assert_not_called()
        recognize.assert_not_called()
        self.assertEqual(events[-1][0], "SRT 已存在，跳过转录")

    def test_complete_checkpoint_is_written_to_formal_srt(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            video_path = root / "recording.flv"
            video_path.write_bytes(b"video")
            checkpoint_path = root / "checkpoint.json"
            checkpoint = {
                "chunks": {
                    "0": {
                        "result": [{
                            "text": "测试",
                            "timestamp": [[0, 500], [500, 1000]],
                        }],
                    },
                },
            }
            with (
                patch.object(workflow, "probe_video_duration", return_value=120.0),
                patch.object(workflow.model_runtime, "funasr_hotwords", return_value=[]),
                patch.object(
                    workflow.checkpoint_store,
                    "prepare_funasr_checkpoint",
                    return_value=(str(checkpoint_path), checkpoint),
                ),
                patch.object(
                    workflow.checkpoint_store,
                    "write_funasr_checkpoint",
                ),
                patch.object(
                    workflow.recognition,
                    "recognize_missing_chunks",
                ) as recognize,
            ):
                result = workflow.ensure_srt(str(video_path))
            content = Path(result).read_text(encoding="utf-8")

        recognize.assert_not_called()
        self.assertIn("测试", content)
        self.assertEqual(checkpoint["status"], "completed")
        self.assertEqual(checkpoint["segment_count"], 1)

    def test_empty_checkpoint_does_not_create_empty_srt(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            video_path = root / "recording.flv"
            video_path.write_bytes(b"video")
            checkpoint = {"chunks": {"0": {"result": []}}}
            with (
                patch.object(workflow, "probe_video_duration", return_value=120.0),
                patch.object(workflow.model_runtime, "funasr_hotwords", return_value=[]),
                patch.object(
                    workflow.checkpoint_store,
                    "prepare_funasr_checkpoint",
                    return_value=(str(root / "checkpoint.json"), checkpoint),
                ),
                patch.object(
                    workflow.checkpoint_store,
                    "write_funasr_checkpoint",
                ),
            ):
                result = workflow.ensure_srt(str(video_path))

            srt_exists = video_path.with_suffix(".srt").exists()

        self.assertIsNone(result)
        self.assertFalse(srt_exists)
        self.assertEqual(checkpoint["status"], "completed_empty")

    def test_recognition_failure_records_chunk_and_cleans_temporary_srt(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            video_path = root / "recording.flv"
            video_path.write_bytes(b"video")
            checkpoint = {"chunks": {}}
            failure = RuntimeError("decode failed")
            failure.chunk_index = 0
            with (
                patch.object(workflow, "probe_video_duration", return_value=120.0),
                patch.object(workflow.model_runtime, "funasr_hotwords", return_value=[]),
                patch.object(
                    workflow.checkpoint_store,
                    "prepare_funasr_checkpoint",
                    return_value=(str(root / "checkpoint.json"), checkpoint),
                ),
                patch.object(
                    workflow.checkpoint_store,
                    "write_funasr_checkpoint",
                ),
                patch.object(
                    workflow.recognition,
                    "recognize_missing_chunks",
                    side_effect=failure,
                ),
                self.assertRaisesRegex(RuntimeError, "decode failed"),
            ):
                workflow.ensure_srt(str(video_path))

            temp_exists = Path(str(video_path.with_suffix(".srt")) + ".tmp").exists()

        self.assertEqual(checkpoint["status"], "failed")
        self.assertEqual(checkpoint["last_failure"]["chunk_index"], 0)
        self.assertFalse(temp_exists)


if __name__ == "__main__":
    unittest.main()
