import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from autoslice.transcription import model_runtime


class TranscriptionModelRuntimeTests(unittest.TestCase):
    def test_speaker_model_accepts_downloaded_campplus_bin(self):
        with TemporaryDirectory() as directory:
            model_dir = Path(directory)
            (model_dir / "campplus_cn_common.bin").write_bytes(b"weights")
            with (
                patch.object(
                    model_runtime,
                    "FUNASR_SPK_CACHE_MODEL_DIR",
                    str(model_dir),
                ),
                patch.dict(
                    os.environ,
                    {"AUTOSLICE_FUNASR_SPK_DIR": ""},
                ),
            ):
                resolved = model_runtime.resolve_funasr_speaker_model_source()

        self.assertEqual(resolved, str(model_dir))

    def test_nano_generate_kwargs_use_native_hotwords_and_speaker_results(self):
        class FunASRNano:
            pass

        model = SimpleNamespace(
            model=FunASRNano(),
            _autoslice_spk_source="speaker-cache",
        )
        kwargs = model_runtime.funasr_generate_kwargs(
            model,
            hotwords="泽音 melody",
        )

        self.assertEqual(kwargs["hotwords"], ["泽音", "melody"])
        self.assertEqual(kwargs["language"], "中文")
        self.assertTrue(kwargs["return_spk_res"])
        self.assertNotIn("return_raw_text", kwargs)

    def test_model_load_falls_back_to_cpu_and_preserves_runtime_metadata(self):
        calls = []
        progress = []

        def fake_auto_model(**kwargs):
            calls.append(kwargs)
            if kwargs["device"].startswith("cuda"):
                raise RuntimeError("cuda unavailable")
            return SimpleNamespace()

        with (
            patch.object(
                model_runtime,
                "resolve_funasr_model_source",
                return_value="asr-cache",
            ),
            patch.object(
                model_runtime,
                "resolve_funasr_aux_model_source",
                side_effect=("vad-cache", "punc-cache"),
            ),
            patch.object(
                model_runtime,
                "resolve_funasr_speaker_model_source",
                return_value="speaker-cache",
            ),
        ):
            model = model_runtime.load_funasr_model(
                fake_auto_model,
                progress_callback=lambda *args: progress.append(args),
                device="cuda:0",
                foreground_only=True,
            )

        self.assertEqual([call["device"] for call in calls], ["cuda:0", "cpu"])
        self.assertEqual(model._autoslice_device, "cpu")
        self.assertEqual(model._autoslice_spk_source, "speaker-cache")
        self.assertEqual(model._autoslice_foreground_filter, "speaker_diarization")
        self.assertTrue(any("自动改用 CPU" in event[0] for event in progress))

    def test_nano_with_speaker_filter_also_loads_punctuation_model(self):
        calls = []

        class LoadedModel:
            pass

        def fake_auto_model(**kwargs):
            calls.append(kwargs)
            return LoadedModel()

        with (
            patch.object(
                model_runtime,
                "resolve_funasr_model_source",
                return_value="C:/models/Fun-ASR-Nano-2512",
            ),
            patch.object(
                model_runtime,
                "resolve_funasr_aux_model_source",
                side_effect=("vad-cache", "punc-cache"),
            ),
            patch.object(
                model_runtime,
                "resolve_funasr_speaker_model_source",
                return_value="speaker-cache",
            ),
        ):
            model_runtime.load_funasr_model(
                fake_auto_model,
                device="cpu",
                foreground_only=True,
            )

        self.assertEqual(calls[0]["punc_model"], "punc-cache")
        self.assertEqual(calls[0]["spk_model"], "speaker-cache")

    def test_off_skips_speaker_model_but_soft_and_strict_request_it(self):
        calls = []

        def fake_auto_model(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace()

        def auxiliary_source(model_id, _cache_dir):
            if model_id == model_runtime.FUNASR_VAD_MODEL:
                return "vad-cache"
            return "punc-cache"

        with (
            patch.object(
                model_runtime,
                "resolve_funasr_model_source",
                return_value="asr-cache",
            ),
            patch.object(
                model_runtime,
                "resolve_funasr_aux_model_source",
                side_effect=auxiliary_source,
            ),
            patch.object(
                model_runtime,
                "resolve_funasr_speaker_model_source",
                return_value="speaker-cache",
            ) as resolve_speaker,
        ):
            for mode in ("off", "soft", "strict"):
                model_runtime.load_funasr_model(
                    fake_auto_model,
                    device="cpu",
                    background_filter_mode=mode,
                )

        self.assertNotIn("spk_model", calls[0])
        self.assertEqual(calls[1]["spk_model"], "speaker-cache")
        self.assertEqual(calls[2]["spk_model"], "speaker-cache")
        self.assertEqual(resolve_speaker.call_count, 2)

    def test_legacy_soft_falls_back_to_cpu_base_asr_when_campp_load_fails(self):
        calls = []
        progress = []

        def fake_auto_model(**kwargs):
            calls.append(dict(kwargs))
            if "spk_model" in kwargs:
                raise RuntimeError("private CAM++ path failed")
            return SimpleNamespace()

        with (
            patch.object(
                model_runtime,
                "resolve_funasr_model_source",
                return_value="asr-cache",
            ),
            patch.object(
                model_runtime,
                "resolve_funasr_aux_model_source",
                side_effect=lambda model_id, _cache: (
                    "vad-cache"
                    if model_id == model_runtime.FUNASR_VAD_MODEL
                    else "punc-cache"
                ),
            ),
            patch.object(
                model_runtime,
                "resolve_funasr_speaker_model_source",
                return_value="speaker-cache",
            ),
        ):
            model = model_runtime.load_funasr_model(
                fake_auto_model,
                device="cpu",
                foreground_only=True,
                progress_callback=lambda *args: progress.append(args),
            )

        self.assertEqual(
            [(call["device"], "spk_model" in call) for call in calls],
            [("cpu", True), ("cpu", False)],
        )
        self.assertIsNone(model._autoslice_spk_source)
        self.assertTrue(model._autoslice_speaker_model_load_failed)
        self.assertEqual(model._autoslice_foreground_filter, "adaptive_gate")
        self.assertNotIn(
            "private CAM++ path failed",
            " ".join(event[0] for event in progress),
        )

    def test_cuda_campp_failures_fall_back_to_cpu_base_asr(self):
        calls = []

        def fake_auto_model(**kwargs):
            calls.append(dict(kwargs))
            if "spk_model" in kwargs:
                raise RuntimeError("CAM++ incompatible")
            return SimpleNamespace()

        with (
            patch.object(
                model_runtime,
                "resolve_funasr_model_source",
                return_value="asr-cache",
            ),
            patch.object(
                model_runtime,
                "resolve_funasr_aux_model_source",
                side_effect=lambda model_id, _cache: (
                    "vad-cache"
                    if model_id == model_runtime.FUNASR_VAD_MODEL
                    else "punc-cache"
                ),
            ),
            patch.object(
                model_runtime,
                "resolve_funasr_speaker_model_source",
                return_value="speaker-cache",
            ),
        ):
            model = model_runtime.load_funasr_model(
                fake_auto_model,
                device="cuda:0",
                background_filter_mode="soft",
            )

        self.assertEqual(
            [(call["device"], "spk_model" in call) for call in calls],
            [("cuda:0", True), ("cpu", True), ("cpu", False)],
        )
        self.assertEqual(model._autoslice_device, "cpu")
        self.assertIsNone(model._autoslice_spk_source)
        self.assertTrue(model._autoslice_speaker_model_load_failed)
        self.assertEqual(model._autoslice_foreground_filter, "adaptive_gate")

    def test_campp_and_cpu_base_asr_failures_still_raise(self):
        calls = []

        def fail_all_models(**kwargs):
            calls.append(dict(kwargs))
            raise RuntimeError("model initialization failed")

        with (
            patch.object(
                model_runtime,
                "resolve_funasr_model_source",
                return_value="asr-cache",
            ),
            patch.object(
                model_runtime,
                "resolve_funasr_aux_model_source",
                side_effect=lambda model_id, _cache: (
                    "vad-cache"
                    if model_id == model_runtime.FUNASR_VAD_MODEL
                    else "punc-cache"
                ),
            ),
            patch.object(
                model_runtime,
                "resolve_funasr_speaker_model_source",
                return_value="speaker-cache",
            ),
            self.assertRaisesRegex(RuntimeError, "FunASR 模型加载失败"),
        ):
            model_runtime.load_funasr_model(
                fail_all_models,
                device="cpu",
                background_filter_mode="strict",
            )

        self.assertEqual(
            [(call["device"], "spk_model" in call) for call in calls],
            [("cpu", True), ("cpu", False)],
        )

    def test_model_load_reports_actionable_cpu_torch_install_hint(self):
        def missing_torch(**_kwargs):
            raise ModuleNotFoundError("No module named 'torch'")

        with patch.object(
            model_runtime,
            "resolve_funasr_model_source",
            return_value="asr-cache",
        ):
            with self.assertRaisesRegex(RuntimeError, r'\.\[asr-cpu\]'):
                model_runtime.load_funasr_model(
                    missing_torch,
                    device="cpu",
                )

    def test_public_status_never_returns_local_model_path(self):
        with TemporaryDirectory(prefix="autoslice-private-model-") as directory:
            model_dir = Path(directory) / "Fun-ASR-Nano-2512"
            model_dir.mkdir()
            (model_dir / "model.pt").write_bytes(b"weights")
            with (
                patch.object(
                    model_runtime,
                    "resolve_funasr_model_source",
                    return_value=str(model_dir),
                ),
                patch.object(
                    model_runtime,
                    "resolve_funasr_device",
                    return_value="cpu",
                ),
                patch.object(
                    model_runtime,
                    "resolve_funasr_speaker_model_source",
                    return_value=None,
                ),
            ):
                status = model_runtime.funasr_public_status()

        self.assertEqual(status["model_key"], "nano")
        self.assertTrue(status["model_ready"])
        self.assertEqual(status["subtitle_background_filter_default"], "soft")
        self.assertEqual(status["analysis_background_filter_default"], "off")
        self.assertEqual(
            [item["mode"] for item in status["background_filter_modes"]],
            ["off", "soft", "strict"],
        )
        self.assertIn("无法保证 100%", status["background_filter_limit"])
        self.assertNotIn(directory, json.dumps(status, ensure_ascii=False))

    def test_public_status_reuses_speaker_model_ready_owner(self):
        with (
            patch.object(
                model_runtime,
                "resolve_funasr_model_source",
                return_value="asr-cache",
            ),
            patch.object(
                model_runtime,
                "resolve_funasr_device",
                return_value="cpu",
            ),
            patch.object(
                model_runtime,
                "funasr_speaker_model_ready",
                return_value=True,
            ) as speaker_ready,
        ):
            status = model_runtime.funasr_public_status()

        speaker_ready.assert_called_once_with()
        self.assertTrue(status["speaker_filter_ready"])


if __name__ == "__main__":
    unittest.main()
