import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from autoslice.transcription import checkpoints


class TranscriptionCheckpointTests(unittest.TestCase):
    def test_three_background_modes_have_distinct_source_fingerprints(self):
        with TemporaryDirectory() as directory:
            video_path = Path(directory) / "recording.flv"
            video_path.write_bytes(b"source")
            with (
                patch.object(
                    checkpoints.model_runtime,
                    "resolve_funasr_model_source",
                    return_value="asr-cache",
                ),
                patch.object(
                    checkpoints.model_runtime,
                    "resolve_funasr_aux_model_source",
                    return_value=None,
                ),
                patch.object(
                    checkpoints.model_runtime,
                    "resolve_funasr_speaker_model_source",
                    return_value=None,
                ),
            ):
                fingerprints = {
                    mode: checkpoints.funasr_source_fingerprint(
                        video_path,
                        120.0,
                        background_filter_mode=mode,
                    )
                    for mode in ("off", "soft", "strict")
                }

        self.assertEqual(len(set(fingerprints.values())), 3)
        self.assertEqual(checkpoints.FUNASR_CHECKPOINT_VERSION, 3)

    def test_old_checkpoint_mismatch_safely_starts_without_old_chunks(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            video_path = root / "recording.flv"
            checkpoint_path = root / "checkpoint.json"
            video_path.write_bytes(b"source")
            checkpoint_path.write_text(
                json.dumps({
                    "version": checkpoints.FUNASR_CHECKPOINT_VERSION,
                    "source_fingerprint": "legacy-fingerprint",
                    "chunk_count": 1,
                    "foreground_only": True,
                    "chunks": {"0": {"result": "legacy-shape"}},
                }),
                encoding="utf-8",
            )

            _, refreshed = checkpoints.prepare_funasr_checkpoint(
                video_path,
                120.0,
                1,
                checkpoint_path=checkpoint_path,
                background_filter_mode="strict",
            )

        self.assertEqual(refreshed["background_filter_mode"], "strict")
        self.assertEqual(refreshed["chunks"], {})

    def test_streamer_mapping_change_invalidates_funasr_checkpoint(self):
        profile_before = SimpleNamespace(
            subtitle_glossary=("音音",),
            asr_replacements=(("错词", "正词"),),
            canonical_name="泽音",
            report_name="音音",
            aliases=(),
        )
        profile_after = SimpleNamespace(
            subtitle_glossary=("音音",),
            asr_replacements=(("错词", "新正词"),),
            canonical_name="泽音",
            report_name="音音",
            aliases=(),
        )
        with patch.object(
            checkpoints.model_runtime,
            "current_streamer_profile",
            side_effect=(profile_before, profile_after),
        ):
            hotwords_before = checkpoints.model_runtime.funasr_hotwords()
            hotwords_after = checkpoints.model_runtime.funasr_hotwords()

        self.assertNotEqual(hotwords_before, hotwords_after)

        valid_result = [{"text": "测试", "timestamp": [[0, 1000]]}]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            video_path = root / "recording.flv"
            video_path.write_bytes(b"source")
            checkpoint_path = root / "checkpoint.json"
            with patch.object(
                checkpoints,
                "funasr_model_runtime_signature",
                return_value={"runtime": "stable"},
            ):
                _, original = checkpoints.prepare_funasr_checkpoint(
                    video_path,
                    120.0,
                    1,
                    checkpoint_path=checkpoint_path,
                    hotwords=hotwords_before,
                )
                original["chunks"] = {
                    "0": {
                        "fingerprint": checkpoints.funasr_chunk_fingerprint(
                            original["source_fingerprint"],
                            0,
                            0.0,
                            120.0,
                        ),
                        "input_start": 0.0,
                        "input_duration": 120.0,
                        "result": valid_result,
                    }
                }
                checkpoints.write_funasr_checkpoint(checkpoint_path, original)
                _, refreshed = checkpoints.prepare_funasr_checkpoint(
                    video_path,
                    120.0,
                    1,
                    checkpoint_path=checkpoint_path,
                    hotwords=hotwords_after,
                )

        self.assertNotEqual(
            original["source_fingerprint"], refreshed["source_fingerprint"]
        )
        self.assertEqual(refreshed["chunks"], {})

    def test_chunk_window_keeps_context_outside_core_ownership(self):
        self.assertEqual(
            checkpoints.funasr_chunk_input_window(0, 250.0),
            (0.0, 120.0, 0.0, 120.0),
        )
        self.assertEqual(
            checkpoints.funasr_chunk_input_window(1, 250.0),
            (120.0, 120.0, 100.0, 140.0),
        )
        self.assertEqual(
            checkpoints.funasr_chunk_input_window(2, 250.0),
            (240.0, 10.0, 220.0, 30.0),
        )

    def test_prepare_only_restores_structurally_valid_chunks(self):
        valid_result = [{"text": "测试", "timestamp": [[0, 1000]]}]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            video_path = root / "recording.flv"
            video_path.write_bytes(b"source")
            checkpoint_path = root / "checkpoint.json"
            with patch.object(
                checkpoints,
                "funasr_model_runtime_signature",
                return_value={"runtime": "stable"},
            ):
                _, payload = checkpoints.prepare_funasr_checkpoint(
                    video_path,
                    240.0,
                    2,
                    checkpoint_path=checkpoint_path,
                )
                source_fingerprint = payload["source_fingerprint"]
                payload["chunks"] = {
                    "0": {
                        "fingerprint": checkpoints.funasr_chunk_fingerprint(
                            source_fingerprint,
                            0,
                            0.0,
                            120.0,
                        ),
                        "input_start": 0.0,
                        "input_duration": 120.0,
                        "result": valid_result,
                    },
                    "1": {
                        "fingerprint": checkpoints.funasr_chunk_fingerprint(
                            source_fingerprint,
                            1,
                            120.0,
                            120.0,
                        ),
                        "input_start": 101.0,
                        "input_duration": 140.0,
                        "result": valid_result,
                    },
                }
                checkpoints.write_funasr_checkpoint(checkpoint_path, payload)
                _, recovered = checkpoints.prepare_funasr_checkpoint(
                    video_path,
                    240.0,
                    2,
                    checkpoint_path=checkpoint_path,
                )

        self.assertEqual(list(recovered["chunks"]), ["0"])

    def test_completed_checkpoint_reuses_only_matching_srt_entries(self):
        result = [{"text": "测试", "timestamp": [[0, 1000]]}]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            srt_path = root / "recording.srt"
            checkpoint_path = root / "checkpoint.json"
            srt_path.write_text("subtitle", encoding="utf-8")
            checkpoint_path.write_text(
                json.dumps(
                    {
                        "status": "completed",
                        "duration": 120.0,
                        "chunk_count": 1,
                        "chunks": {"0": {"result": result}},
                        "coverage": {"start": 0.0, "end": 120.0},
                        "segment_count": 1,
                    }
                ),
                encoding="utf-8",
            )

            reusable = checkpoints.existing_srt_is_reusable(
                srt_path,
                checkpoint_path,
                read_srt_entries=lambda _path: [(0.0, 1.0, "测试")],
            )
            mismatched = checkpoints.existing_srt_is_reusable(
                srt_path,
                checkpoint_path,
                read_srt_entries=lambda _path: [
                    (0.0, 1.0, "测试"),
                    (1.0, 2.0, "多余"),
                ],
            )
            wrong_mode_fingerprint = checkpoints.existing_srt_is_reusable(
                srt_path,
                checkpoint_path,
                read_srt_entries=lambda _path: [(0.0, 1.0, "测试")],
                expected_source_fingerprint="different-mode",
            )

        self.assertTrue(reusable)
        self.assertFalse(mismatched)
        self.assertFalse(wrong_mode_fingerprint)

    def test_new_user_srt_can_supersede_failed_checkpoint(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            srt_path = root / "recording.srt"
            checkpoint_path = root / "checkpoint.json"
            srt_path.write_text("subtitle", encoding="utf-8")
            checkpoint_path.write_text('{"status":"failed"}', encoding="utf-8")
            os.utime(checkpoint_path, ns=(1_000_000_000, 1_000_000_000))
            os.utime(srt_path, ns=(2_000_000_000, 2_000_000_000))

            reusable = checkpoints.existing_srt_is_reusable(
                srt_path,
                checkpoint_path,
                read_srt_entries=lambda _path: [(0.0, 1.0, "人工字幕")],
            )

        self.assertTrue(reusable)

    def test_atomic_write_failure_preserves_previous_checkpoint_and_cleans_temp(self):
        with TemporaryDirectory() as directory:
            checkpoint_path = Path(directory) / "checkpoint.json"
            checkpoint_path.write_text('{"old":true}', encoding="utf-8")
            with (
                patch.object(
                    checkpoints,
                    "replace_file_atomically",
                    side_effect=OSError("disk busy"),
                ),
                self.assertRaises(OSError),
            ):
                checkpoints.write_funasr_checkpoint(
                    checkpoint_path,
                    {"new": True},
                )

            previous = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            temp_exists = Path(str(checkpoint_path) + ".tmp").exists()

        self.assertEqual(previous, {"old": True})
        self.assertFalse(temp_exists)

    def test_quarantine_uses_owner_atomic_commit(self):
        with TemporaryDirectory() as directory:
            srt_path = Path(directory) / "recording.srt"
            srt_path.write_text("incomplete", encoding="utf-8")

            quarantined = checkpoints.quarantine_incomplete_srt(srt_path)

            self.assertFalse(srt_path.exists())
            self.assertEqual(Path(quarantined).read_text(encoding="utf-8"), "incomplete")


if __name__ == "__main__":
    unittest.main()
