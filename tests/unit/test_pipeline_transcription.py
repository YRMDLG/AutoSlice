import json
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from autoslice.pipeline_transcription import prepare_pipeline_subtitles


class PipelineTranscriptionTests(unittest.TestCase):
    def setUp(self):
        self.layout = {
            "asr_checkpoint_path": "artifact/asr.json",
            "corrected_srt_path": "artifact/corrected.srt",
        }

    def test_existing_srt_and_corrected_srt_are_returned(self):
        calls = []
        progress = []

        def seed(target, legacy):
            calls.append(("seed", target, legacy))

        def ensure(video_path, callback, checkpoint_path=None):
            calls.append(("ensure", video_path, callback, checkpoint_path))
            return "source.srt"

        def export(source_path, output_path=None):
            calls.append(("export", source_path, output_path))
            return "artifact/corrected.srt"

        result = prepare_pipeline_subtitles(
            "recording.flv",
            self.layout,
            "recording_asr_checkpoint.json",
            progress_callback=lambda *args: progress.append(args),
            ensure_progress_callback=lambda *args: progress.append(
                ("ensure", *args)
            ),
            seed_artifact_from_legacy=seed,
            ensure_srt=ensure,
            export_corrected_srt=export,
        )

        self.assertEqual(
            result,
            {
                "source_srt_path": "source.srt",
                "corrected_srt_path": "artifact/corrected.srt",
                "srt_path": "artifact/corrected.srt",
            },
        )
        self.assertEqual(calls[0], ("seed", "artifact/asr.json", "recording_asr_checkpoint.json"))
        self.assertEqual(calls[1][0], "ensure")
        self.assertEqual(calls[2], ("export", "source.srt", "artifact/corrected.srt"))
        self.assertEqual(
            progress,
            [
                ("Step 1/5: 检查/生成字幕...", 0, 100),
                ("已生成剪映校对字幕: corrected.srt", 14, 100),
            ],
        )

    def test_missing_corrected_srt_falls_back_to_source_srt(self):
        export_calls = []

        result = prepare_pipeline_subtitles(
            "recording.flv",
            self.layout,
            "legacy.json",
            progress_callback=None,
            ensure_progress_callback=None,
            seed_artifact_from_legacy=lambda *_args: None,
            ensure_srt=lambda *_args, **_kwargs: "source.srt",
            export_corrected_srt=lambda source, output_path=None: export_calls.append(
                (source, output_path)
            ) or None,
        )

        self.assertEqual(result["source_srt_path"], "source.srt")
        self.assertIsNone(result["corrected_srt_path"])
        self.assertEqual(result["srt_path"], "source.srt")
        self.assertEqual(export_calls, [("source.srt", "artifact/corrected.srt")])

    def test_missing_source_srt_keeps_failure_behavior_and_skips_export(self):
        export_calls = []
        progress = []

        with self.assertRaisesRegex(RuntimeError, "无法生成 SRT 字幕"):
            prepare_pipeline_subtitles(
                "recording.flv",
                self.layout,
                "legacy.json",
                progress_callback=lambda *args: progress.append(args),
                ensure_progress_callback=None,
                seed_artifact_from_legacy=lambda *_args: None,
                ensure_srt=lambda *_args, **_kwargs: None,
                export_corrected_srt=lambda *_args, **_kwargs: export_calls.append(True),
            )

        self.assertEqual(progress, [("Step 1/5: 检查/生成字幕...", 0, 100)])
        self.assertEqual(export_calls, [])

    def test_legacy_checkpoint_is_migrated_before_srt_generation(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            legacy = root / "legacy_asr.json"
            target = root / "artifact" / "asr.json"
            legacy.write_text('{"chunks": ["0"]}', encoding="utf-8")
            calls = []

            def seed(target_path, legacy_path):
                calls.append((target_path, legacy_path))
                target_path = Path(target_path)
                target_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(legacy_path, target_path)

            result = prepare_pipeline_subtitles(
                str(root / "recording.flv"),
                {
                    "asr_checkpoint_path": str(target),
                    "corrected_srt_path": str(root / "artifact" / "corrected.srt"),
                },
                str(legacy),
                progress_callback=lambda *_args: None,
                ensure_progress_callback=lambda *_args: None,
                seed_artifact_from_legacy=seed,
                ensure_srt=lambda _video, _progress, checkpoint_path=None: (
                    self.assertEqual(
                        json.loads(Path(checkpoint_path).read_text(encoding="utf-8")),
                        {"chunks": ["0"]},
                    )
                    or str(root / "recording.srt")
                ),
                export_corrected_srt=lambda *_args, **_kwargs: None,
            )

            self.assertEqual(calls, [(str(target), str(legacy))])
            self.assertEqual(result["srt_path"], str(root / "recording.srt"))


if __name__ == "__main__":
    unittest.main()
