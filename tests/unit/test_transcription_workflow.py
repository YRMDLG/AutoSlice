import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from autoslice.transcription import workflow


class TranscriptionWorkflowTests(unittest.TestCase):
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
