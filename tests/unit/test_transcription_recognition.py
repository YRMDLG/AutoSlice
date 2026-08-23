import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from autoslice.transcription import model_runtime, recognition


class TranscriptionRecognitionTests(unittest.TestCase):
    @staticmethod
    def _fake_ffmpeg(calls):
        def run(command, **_kwargs):
            calls.append(command)
            Path(command[-1]).write_bytes(b"audio")

        return run

    def test_source_audio_applies_foreground_filter_only_to_temporary_audio(self):
        calls = []
        with TemporaryDirectory() as directory:
            wav_path = Path(directory) / "source.wav"
            recognition.extract_source_audio(
                "recording.mp4",
                str(wav_path),
                foreground_only=True,
                run_command=self._fake_ffmpeg(calls),
            )

        command = calls[0]
        self.assertEqual(
            command[command.index("-af") + 1],
            model_runtime.FUNASR_FOREGROUND_AUDIO_FILTER,
        )

    def test_three_modes_control_temporary_audio_filter(self):
        observed = {}
        with TemporaryDirectory() as directory:
            for mode in ("off", "soft", "strict"):
                calls = []
                recognition.extract_source_audio(
                    "recording.mp4",
                    str(Path(directory) / f"{mode}.wav"),
                    background_filter_mode=mode,
                    run_command=self._fake_ffmpeg(calls),
                )
                observed[mode] = calls[0]

        self.assertNotIn("-af", observed["off"])
        self.assertIn("-af", observed["soft"])
        self.assertIn("-af", observed["strict"])

    def test_multichunk_run_uses_pre_context_and_persists_each_result(self):
        calls = []

        class Model:
            def __init__(self):
                self.calls = 0

            def generate(self, **_kwargs):
                self.calls += 1
                return [{"text": f"第{self.calls}块", "timestamp": [[0, 1000]]}]

        model = Model()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            video_path = root / "recording.flv"
            checkpoint_path = root / "checkpoint.json"
            video_path.write_bytes(b"source")
            checkpoint = {"source_fingerprint": "source", "chunks": {}}
            with (
                patch.object(model_runtime, "resolve_funasr_device", return_value="cpu"),
                patch.object(model_runtime, "load_funasr_model", return_value=model),
                patch.object(model_runtime, "funasr_generate_kwargs", return_value={}),
            ):
                result = recognition.recognize_missing_chunks(
                    str(video_path),
                    240.0,
                    2,
                    [0, 1],
                    str(checkpoint_path),
                    checkpoint,
                    auto_model_type=object,
                    run_command=self._fake_ffmpeg(calls),
                )
            persisted = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            leftovers = list(root.glob("*.wav"))

        self.assertEqual(result.completed_indices, (0, 1))
        self.assertEqual(model.calls, 2)
        self.assertEqual(len(calls), 3)
        second_chunk_command = calls[2]
        self.assertEqual(
            float(second_chunk_command[second_chunk_command.index("-ss") + 1]),
            100.0,
        )
        self.assertEqual(
            float(second_chunk_command[second_chunk_command.index("-t") + 1]),
            140.0,
        )
        self.assertEqual(list(persisted["chunks"]), ["0", "1"])
        self.assertEqual(leftovers, [])

    def test_session_falls_back_to_cpu_after_gpu_inference_failure(self):
        devices = []
        progress = []

        class GpuModel:
            def generate(self, **_kwargs):
                raise RuntimeError("CUDA out of memory")

        class CpuModel:
            def generate(self, **_kwargs):
                return [{"text": "成功", "timestamp": [[0, 1000]]}]

        def load_model(_auto_model, **kwargs):
            devices.append(kwargs["device"])
            return GpuModel() if kwargs["device"].startswith("cuda") else CpuModel()

        with (
            patch.object(model_runtime, "resolve_funasr_device", return_value="cuda:0"),
            patch.object(model_runtime, "load_funasr_model", side_effect=load_model),
            patch.object(model_runtime, "funasr_generate_kwargs", return_value={}),
            patch.object(model_runtime, "clear_funasr_cuda_cache") as clear_cache,
        ):
            session = recognition.FunASRRecognitionSession.load(
                object,
                hotwords="",
                foreground_only=False,
                progress_callback=lambda *args: progress.append(args),
            )
            result = session.generate("audio.wav", chunk_index=0, chunk_count=1)

        self.assertEqual(devices, ["cuda:0", "cpu"])
        self.assertEqual(result[0]["text"], "成功")
        self.assertEqual(session.device, "cpu")
        self.assertTrue(any("GPU 转录失败" in event[0] for event in progress))
        clear_cache.assert_called_once_with()

    def test_campp_load_fallback_is_persisted_without_speaker_usage(self):
        calls = []

        class BaseModel:
            _autoslice_device = "cpu"
            _autoslice_spk_source = None
            _autoslice_foreground_filter = "adaptive_gate"
            _autoslice_speaker_model_load_failed = True

            def generate(self, **_kwargs):
                return [{"text": "主播和背景都保留", "timestamp": [[0, 1000]]}]

        with TemporaryDirectory() as directory:
            root = Path(directory)
            video_path = root / "recording.flv"
            checkpoint_path = root / "checkpoint.json"
            video_path.write_bytes(b"source")
            checkpoint = {
                "source_fingerprint": "source",
                "speaker_model_ready": True,
                "chunks": {},
            }
            with (
                patch.object(
                    model_runtime,
                    "resolve_funasr_device",
                    return_value="cpu",
                ),
                patch.object(
                    model_runtime,
                    "load_funasr_model",
                    return_value=BaseModel(),
                ),
                patch.object(
                    model_runtime,
                    "funasr_generate_kwargs",
                    return_value={},
                ),
            ):
                result = recognition.recognize_missing_chunks(
                    str(video_path),
                    120.0,
                    1,
                    [0],
                    str(checkpoint_path),
                    checkpoint,
                    background_filter_mode="strict",
                    auto_model_type=object,
                    run_command=self._fake_ffmpeg(calls),
                )
            persisted = json.loads(
                checkpoint_path.read_text(encoding="utf-8")
            )

        self.assertFalse(result.speaker_model_used)
        self.assertTrue(result.speaker_model_load_failed)
        self.assertFalse(persisted["speaker_model_used"])
        self.assertTrue(persisted["speaker_model_load_failed"])
        self.assertEqual(persisted["foreground_filter_mode"], "adaptive_gate")
        self.assertEqual(
            persisted["chunks"]["0"]["result"][0]["text"],
            "主播和背景都保留",
        )

    def test_gpu_chunk_fallback_refreshes_final_foreground_filter_mode(self):
        calls = []
        loaded_devices = []

        class GpuSpeakerModel:
            _autoslice_device = "cuda:0"
            _autoslice_spk_source = "speaker-cache"
            _autoslice_foreground_filter = "speaker_diarization"
            _autoslice_speaker_model_load_failed = False

            def generate(self, **_kwargs):
                raise RuntimeError("GPU inference failed")

        class CpuBaseModel:
            _autoslice_device = "cpu"
            _autoslice_spk_source = None
            _autoslice_foreground_filter = "adaptive_gate"
            _autoslice_speaker_model_load_failed = True

            def generate(self, **_kwargs):
                return [{"text": "完整字幕", "timestamp": [[0, 1000]]}]

        def load_model(_auto_model, **kwargs):
            loaded_devices.append(kwargs["device"])
            if kwargs["device"].startswith("cuda"):
                return GpuSpeakerModel()
            return CpuBaseModel()

        with TemporaryDirectory() as directory:
            root = Path(directory)
            video_path = root / "recording.flv"
            checkpoint_path = root / "checkpoint.json"
            video_path.write_bytes(b"source")
            checkpoint = {
                "source_fingerprint": "source",
                "speaker_model_ready": True,
                "chunks": {},
            }
            with (
                patch.object(
                    model_runtime,
                    "resolve_funasr_device",
                    return_value="cuda:0",
                ),
                patch.object(
                    model_runtime,
                    "load_funasr_model",
                    side_effect=load_model,
                ),
                patch.object(
                    model_runtime,
                    "funasr_generate_kwargs",
                    return_value={},
                ),
                patch.object(model_runtime, "clear_funasr_cuda_cache"),
            ):
                result = recognition.recognize_missing_chunks(
                    str(video_path),
                    120.0,
                    1,
                    [0],
                    str(checkpoint_path),
                    checkpoint,
                    background_filter_mode="strict",
                    auto_model_type=object,
                    run_command=self._fake_ffmpeg(calls),
                )
            persisted = json.loads(
                checkpoint_path.read_text(encoding="utf-8")
            )

        self.assertEqual(loaded_devices, ["cuda:0", "cpu"])
        self.assertEqual(
            persisted["foreground_filter_mode"],
            "adaptive_gate",
        )
        self.assertFalse(persisted["speaker_model_used"])
        self.assertTrue(persisted["speaker_model_load_failed"])
        self.assertEqual(result.foreground_filter_mode, "adaptive_gate")
        self.assertFalse(result.speaker_model_used)
        self.assertTrue(result.speaker_model_load_failed)

    def test_repeated_failure_reports_chunk_index_and_cleans_audio(self):
        calls = []

        class Model:
            def __init__(self):
                self.calls = 0

            def generate(self, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    return [{"text": "完成", "timestamp": [[0, 1000]]}]
                raise RuntimeError("decode failed")

        model = Model()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            video_path = root / "recording.flv"
            checkpoint_path = root / "checkpoint.json"
            video_path.write_bytes(b"source")
            checkpoint = {"source_fingerprint": "source", "chunks": {}}
            with (
                patch.object(model_runtime, "resolve_funasr_device", return_value="cpu"),
                patch.object(model_runtime, "load_funasr_model", return_value=model),
                patch.object(model_runtime, "funasr_generate_kwargs", return_value={}),
                patch.object(model_runtime, "FUNASR_CPU_RETRY_DELAY_SEC", 0),
                self.assertRaises(recognition.FunASRChunkRecognitionError) as raised,
            ):
                recognition.recognize_missing_chunks(
                    str(video_path),
                    240.0,
                    2,
                    [0, 1],
                    str(checkpoint_path),
                    checkpoint,
                    auto_model_type=object,
                    run_command=self._fake_ffmpeg(calls),
                )
            persisted = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            leftovers = list(root.glob("*.wav"))

        self.assertEqual(raised.exception.chunk_index, 1)
        self.assertIn("连续失败", str(raised.exception))
        self.assertEqual(list(persisted["chunks"]), ["0"])
        self.assertEqual(model.calls, 3)
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
