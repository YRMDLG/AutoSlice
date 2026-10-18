"""桌面端字幕识别：文本处理、切块、流水线（续跑/取消/不覆盖）、引擎结果解析、任务协议、热词、自检、引擎对比。

全部用假引擎和 ffmpeg 合成的音频，不加载任何识别模型。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from autoslice.desktop.archive import Knowledge
from autoslice.desktop.asr import (
    AsrCancelled,
    AsrError,
    AsrSettings,
    Piece,
    TranscribeOptions,
    Word,
    audio,
    bench,
    doctor,
    jobs,
    text,
    transcribe,
)
from autoslice.desktop.asr.engines import ENGINES, create_engine
from autoslice.desktop.asr.engines.qwen3 import clean_text, parse_time_stamps
from autoslice.desktop.asr.engines.scripted import ScriptedEngine
from autoslice.desktop.asr.hotwords import build_hotwords
from autoslice.desktop.asr.pipeline import words_in_core
from autoslice.desktop.asr.settings import load_settings, save_settings
from autoslice.desktop.asr.types import AsrOutputExists, Chunk
from autoslice.desktop.asr.worker import run_spec
from autoslice.desktop.foundation import DesktopStorage
from autoslice.transcription.segments import split_subtitle_text_for_display
from tests.support.external_boundary_guard import EXTERNAL_BOUNDARY_GUARD


def ffmpeg_usable() -> bool:
    """Linux 纯逻辑测试里护栏会拦 ffmpeg；没装也跳过。"""

    return shutil.which("ffmpeg") is not None and not EXTERNAL_BOUNDARY_GUARD.linux_logic_only


def require_ffmpeg(test: unittest.TestCase) -> None:
    if not ffmpeg_usable():
        test.skipTest("需要 ffmpeg")


class NeedsFfmpeg(unittest.TestCase):
    def setUp(self):
        super().setUp()
        require_ffmpeg(self)


def make_audio(path: Path, *, speech=4.0, gap=1.5, tail=4.0) -> None:
    """speech 秒音 + gap 秒静音 + tail 秒音，16k 单声道。.wav/.mp4 都行。"""

    codec = ["-c:a", "aac"] if path.suffix == ".mp4" else []
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"sine=frequency=300:duration={speech}",
         "-f", "lavfi", "-i", f"anullsrc=r=16000:cl=mono:d={gap}",
         "-f", "lavfi", "-i", f"sine=frequency=500:duration={tail}",
         "-filter_complex", "[0:a][1:a][2:a]concat=n=3:v=0:a=1[out]", "-map", "[out]",
         "-ar", "16000", "-ac", "1", *codec, str(path)],
        check=True,
    )


def timed(text_value: str, start: float, end: float) -> Piece:
    """每个字均分时间的一句话（带字级时间）。"""

    words = text.spread_words(Piece(start, end, text_value))
    return Piece(words[0].start, words[-1].end, text_value, tuple(words))


class TextTests(unittest.TestCase):
    def test_punctuation_is_attached_to_the_previous_word(self):
        words = [Word(0, 1, "你"), Word(1, 2, "好"), Word(2, 3, "世"), Word(3, 4, "界")]
        attached = text.attach_punctuation("你好，世界！", words)
        self.assertEqual([word.text for word in attached], ["你", "好，", "世", "界！"])

    def test_attach_gives_up_when_the_engine_rewrote_the_content(self):
        self.assertIsNone(text.attach_punctuation("你好世界", [Word(0, 1, "你"), Word(1, 2, "号")]))

    def test_spread_keeps_english_words_whole_and_weights_by_length(self):
        words = text.spread_words(Piece(0, 8, "看ins上面的emoji"))
        self.assertEqual([word.text for word in words], ["看", "ins", "上", "面", "的", "emoji"])
        self.assertAlmostEqual(words[0].start, 0)
        self.assertAlmostEqual(words[-1].end, 8)
        self.assertGreater(words[-1].end - words[-1].start, words[0].end - words[0].start)

    def test_piece_words_prefers_engine_timing_and_falls_back_to_spreading(self):
        with_words = Piece(0, 2, "你好", (Word(0.2, 0.5, "你"), Word(0.9, 1.4, "好")))
        self.assertEqual(text.piece_words(with_words)[1].start, 0.9)
        self.assertEqual(len(text.piece_words(Piece(0, 2, "你好"))), 2)

    def test_cer_ignores_punctuation_case_and_width(self):
        errors, size = text.cer("在这个ins上面改。", "在这个 INS 上面，改")
        self.assertEqual((errors, size), (0, 9))
        errors, size = text.cer("教个朋友", "交个朋友")
        self.assertEqual((errors, size), (1, 4))

    def test_edit_distance_counts_insert_delete_replace(self):
        self.assertEqual(text.edit_distance("abc", "abd"), 1)
        self.assertEqual(text.edit_distance("abc", ""), 3)
        self.assertEqual(text.edit_distance("", "ab"), 2)
        self.assertEqual(text.edit_distance("kitten", "sitting"), 3)


class SegmentSplitTests(unittest.TestCase):
    def test_hard_cut_never_splits_an_english_word(self):
        parts = split_subtitle_text_for_display("今天我们聊聊ins上面的emoji", 16)
        self.assertEqual("".join(parts), "今天我们聊聊ins上面的emoji")
        self.assertIn("emoji", parts[-1])
        self.assertTrue(all("emoj" not in part or "emoji" in part for part in parts))

    def test_chinese_hard_cut_is_unchanged(self):
        self.assertEqual(split_subtitle_text_for_display("一二三四五六七八九十甲乙丙丁戊己庚辛", 16),
                         ["一二三四五六七八九十甲乙丙丁戊己", "庚辛"])


class ChunkPlanTests(unittest.TestCase):
    def test_short_audio_is_one_chunk(self):
        chunks = audio.plan_chunks(30.0, [], max_seconds=45)
        self.assertEqual(len(chunks), 1)
        self.assertEqual((chunks[0].core_start, chunks[0].core_end), (0.0, 30.0))

    def test_cuts_in_the_middle_of_the_longest_silence_in_range(self):
        silences = [(20.0, 20.4), (30.0, 32.0), (60.0, 61.0)]
        chunks = audio.plan_chunks(100.0, silences, max_seconds=45, min_seconds=12)
        self.assertEqual([round(chunk.core_end, 1) for chunk in chunks[:2]], [31.0, 60.5])
        self.assertEqual(chunks[0].core_end, chunks[1].core_start)
        self.assertLess(chunks[1].input_start, chunks[1].core_start)

    def test_hard_cuts_when_there_is_no_silence(self):
        chunks = audio.plan_chunks(100.0, [], max_seconds=40)
        self.assertEqual([chunk.core_end for chunk in chunks], [40.0, 80.0, 100.0])

    def test_chunks_cover_everything_without_overlap(self):
        chunks = audio.plan_chunks(333.3, [(i * 17.0, i * 17.0 + 0.5) for i in range(1, 19)], max_seconds=45)
        self.assertEqual(chunks[0].core_start, 0.0)
        self.assertEqual(chunks[-1].core_end, 333.3)
        for before, after in zip(chunks, chunks[1:]):
            self.assertEqual(before.core_end, after.core_start)
            self.assertLessEqual(before.core_end - before.core_start, 45)

    def test_parse_silences_closes_an_open_tail(self):
        stderr = "silence_start: 1.5\nsilence_end: 3.0 | silence_duration: 1.5\nsilence_start: 8.0\n"
        self.assertEqual(audio.parse_silences(stderr, 10.0), [(1.5, 3.0), (8.0, 10.0)])

    def test_words_in_core_assigns_each_word_to_exactly_one_chunk(self):
        first, second = Chunk(0, 0, 10, 0, 10.4), Chunk(1, 10, 20, 9.6, 20)
        words = [Word(9.7, 9.9, "a"), Word(9.9, 10.3, "b"), Word(10.3, 10.6, "c")]
        kept_first = [word.text for word in words_in_core(words, first, last=False)]
        kept_second = [word.text for word in words_in_core(words, second, last=True)]
        self.assertEqual(sorted(kept_first + kept_second), ["a", "b", "c"])


class AudioTests(NeedsFfmpeg):
    def test_extract_duration_and_silence_detection(self):
        with tempfile.TemporaryDirectory() as directory:
            source, wav = Path(directory, "in.mp4"), Path(directory, "out.wav")
            make_audio(source)
            audio.extract_wav(source, wav)
            duration = audio.wav_duration(wav)
            self.assertAlmostEqual(duration, 9.5, delta=0.3)
            silences = audio.detect_silences(wav, duration)
            self.assertEqual(len(silences), 1)
            self.assertAlmostEqual(silences[0][0], 4.0, delta=0.2)
            self.assertAlmostEqual(silences[0][1], 5.5, delta=0.2)

    def test_missing_ffmpeg_input_is_a_readable_error(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(AsrError):
                audio.extract_wav(Path(directory, "nope.mp4"), Path(directory, "x.wav"))


class PipelineTests(NeedsFfmpeg):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)
        self.video = self.root / "片.mp4"
        make_audio(self.video)
        self.out = self.root / "片.srt"
        self.work = self.root / "work"

    def script(self):
        return [[timed("大家好，欢迎来到直播间！", 0.3, 3.8)], [timed("今天我们聊聊ins上面的emoji。", 0.3, 3.8)]]

    def run_pipeline(self, engine, **options):
        return transcribe(self.video, self.out, engine, workdir=self.work, fingerprint="t",
                          options=TranscribeOptions(**options))

    def test_writes_srt_with_clean_text_and_real_timing(self):
        engine = ScriptedEngine(self.script(), max_chunk_seconds=6)
        result = self.run_pipeline(engine)
        body = self.out.read_text(encoding="utf-8")
        self.assertEqual(result.cue_count, body.count("-->"))
        flat = body.replace("\n", "")
        self.assertIn("大家好", flat)
        self.assertIn("emoji", flat)           # 英文词没被切开
        self.assertNotIn("！", body)             # 剪辑习惯：不留标点
        self.assertTrue(engine.closed)
        self.assertFalse(self.work.exists())     # 成功后清掉缓存
        starts = [line.split(" --> ")[0] for line in body.splitlines() if "-->" in line]
        self.assertEqual(starts, sorted(starts))

    def test_confirmed_fixed_corrections_apply_only_for_the_matching_streamer(self):
        from unittest.mock import patch

        from autoslice.streamer_profiles import add_streamer_profile_replacement

        wrong, right = "紫雨亭", "子语庭"
        script = [[timed(f"今天来聊聊{wrong}这个活动", 0.3, 3.8)], []]
        with patch.dict("os.environ", {"AUTOSLICE_STREAMER_PROFILE_OVERRIDES": str(self.root / "overrides.json")}):
            add_streamer_profile_replacement("zeyin", wrong, right)
            for folder, expected in (("【泽音】测试", right), ("某个没配置的", wrong)):
                target = self.root / folder
                target.mkdir()
                shutil.copy(self.video, target / "视频.mp4")
                out = target / "视频.srt"
                transcribe(target / "视频.mp4", out, ScriptedEngine(script, max_chunk_seconds=6),
                           workdir=self.root / f"w-{folder}", fingerprint="t")
                self.assertIn(expected, out.read_text(encoding="utf-8").replace("\n", "").replace(" ", ""), folder)

    def test_every_chunk_is_recognised_once_and_hotwords_reach_the_engine(self):
        engine = ScriptedEngine(self.script(), max_chunk_seconds=6)
        transcribe(self.video, self.out, engine, workdir=self.work, hotwords=["提督", "舰长"])
        self.assertEqual(len(engine.calls), 2)
        self.assertEqual(engine.hotwords, ("提督", "舰长"))

    def test_resumes_from_the_checkpoint_after_a_failure(self):
        failing = ScriptedEngine(self.script(), max_chunk_seconds=6, raise_on=1)
        with self.assertRaises(AsrError):
            self.run_pipeline(failing)
        self.assertFalse(self.out.exists())
        self.assertTrue((self.work / "checkpoint.json").is_file())
        resumed = ScriptedEngine([[], self.script()[1]], max_chunk_seconds=6)
        self.run_pipeline(resumed)
        self.assertEqual(len(resumed.calls), 1, "第一块在检查点里，不该再识别")
        self.assertIn("大家好", self.out.read_text(encoding="utf-8").replace("\n", ""))

    def test_a_changed_video_invalidates_the_checkpoint(self):
        with self.assertRaises(AsrError):
            self.run_pipeline(ScriptedEngine(self.script(), max_chunk_seconds=6, raise_on=1))
        make_audio(self.video, speech=4.2)
        fresh = ScriptedEngine(self.script(), max_chunk_seconds=6)
        self.run_pipeline(fresh)
        self.assertEqual(len(fresh.calls), 2)

    def test_cancel_keeps_the_checkpoint_and_writes_nothing(self):
        engine = ScriptedEngine(self.script(), max_chunk_seconds=6)
        with self.assertRaises(AsrCancelled):
            transcribe(self.video, self.out, engine, workdir=self.work, fingerprint="t",
                       cancelled=lambda: len(engine.calls) >= 1)
        self.assertFalse(self.out.exists())
        self.assertEqual(len(engine.calls), 1)
        self.assertTrue(engine.closed)

    def test_never_overwrites_an_existing_subtitle_by_default(self):
        self.out.write_text("1\n00:00:00,000 --> 00:00:01,000\n已有\n", encoding="utf-8")
        engine = ScriptedEngine(self.script(), max_chunk_seconds=6)
        with self.assertRaises(AsrError):
            self.run_pipeline(engine)
        self.assertEqual(engine.calls, [])
        self.assertIn("已有", self.out.read_text(encoding="utf-8"))
        self.run_pipeline(ScriptedEngine(self.script(), max_chunk_seconds=6), overwrite=True)
        self.assertNotIn("已有", self.out.read_text(encoding="utf-8"))

    def test_no_speech_is_an_error_and_leaves_no_file(self):
        with self.assertRaises(AsrError):
            self.run_pipeline(ScriptedEngine([[], []], max_chunk_seconds=6))
        self.assertFalse(self.out.exists())

    def test_subtitle_created_during_recognition_is_preserved_and_result_can_be_recovered(self):
        output = self.out

        class ConcurrentEngine(ScriptedEngine):
            def recognize(self, wav_path):
                output.write_text("用户保存的字幕", encoding="utf-8")
                return super().recognize(wav_path)

        with self.assertRaises(AsrOutputExists):
            self.run_pipeline(ConcurrentEngine(self.script(), max_chunk_seconds=6))
        self.assertEqual(self.out.read_text(encoding="utf-8"), "用户保存的字幕")
        self.assertTrue((self.work / "recognized.srt").is_file())
        self.assertEqual(list(self.root.glob(".*.asr-*.tmp")), [])
        recovered = self.root / "另存.srt"
        engine = ScriptedEngine([], max_chunk_seconds=6)
        transcribe(self.video, recovered, engine, workdir=self.work, fingerprint="t")
        self.assertEqual(engine.calls, [])
        self.assertIn("大家好", recovered.read_text(encoding="utf-8"))

    def test_engine_without_word_timing_is_flagged_as_estimated(self):
        engine = ScriptedEngine([[Piece(0.3, 3.8, "没有字级时间的一句话")], []], max_chunk_seconds=6)
        result = self.run_pipeline(engine)
        self.assertTrue(any("估算" in warning for warning in result.warnings))

    def test_progress_is_monotonic_and_ends_at_one(self):
        seen = []
        transcribe(self.video, self.out, ScriptedEngine(self.script(), max_chunk_seconds=6), workdir=self.work,
                   progress=lambda message, fraction: seen.append(fraction))
        self.assertEqual(seen, sorted(seen))
        self.assertEqual(seen[-1], 1.0)


class Qwen3ParseTests(unittest.TestCase):
    def test_objects_dicts_and_tuples(self):
        class Item:
            def __init__(self, text_value, start, end):
                self.text, self.start_time, self.end_time = text_value, start, end

        self.assertEqual(parse_time_stamps([Item("你", 0.1, 0.3), Item("好", 0.3, 0.5)], 2.0),
                         [Word(0.1, 0.3, "你"), Word(0.3, 0.5, "好")])
        self.assertEqual(parse_time_stamps([{"text": "你", "start_time": 0.1, "end_time": 0.3}], 2.0), [Word(0.1, 0.3, "你")])
        self.assertEqual(parse_time_stamps([("你", 0.1, 0.3)], 2.0), [Word(0.1, 0.3, "你")])
        self.assertEqual(parse_time_stamps([(0.1, 0.3, "你")], 2.0), [Word(0.1, 0.3, "你")])

    def test_batch_wrapper_and_milliseconds(self):
        nested = [[{"text": "你", "start": 100, "end": 300}, {"text": "好", "start": 300, "end": 9000}]]
        words = parse_time_stamps(nested, 5.0)
        self.assertEqual([word.text for word in words], ["你", "好"])
        self.assertAlmostEqual(words[-1].end, 9.0)

    def test_empty_and_malformed_items_are_skipped(self):
        self.assertEqual(parse_time_stamps(None, 5.0), [])
        self.assertEqual(parse_time_stamps([{"text": "", "start": 0, "end": 1}, {"text": "x"}], 5.0), [])

    def test_language_tag_is_stripped(self):
        self.assertEqual(clean_text("language Chinese<asr_text>你好"), "你好")
        self.assertEqual(clean_text("你好"), "你好")


class SettingsAndRegistryTests(unittest.TestCase):
    def test_settings_round_trip_and_ignore_garbage(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = DesktopStorage(directory)
            self.assertEqual(load_settings(storage), AsrSettings())
            save_settings(storage, AsrSettings(engine="funasr", device="cuda", hotwords=False))
            loaded = load_settings(storage)
            self.assertEqual((loaded.engine, loaded.device, loaded.hotwords), ("funasr", "cuda", False))
            (Path(directory) / "asr.json").write_text(json.dumps({"engine": 5, "device": ["x"], "unknown": 1}), encoding="utf-8")
            self.assertEqual(load_settings(storage), AsrSettings())

    def test_unknown_engine_is_a_readable_error(self):
        with self.assertRaises(AsrError):
            create_engine(AsrSettings(engine="nope"))

    def test_registry_is_consistent(self):
        for key, spec in ENGINES.items():
            self.assertEqual(key, spec.id)
            self.assertTrue(spec.label and spec.summary)

    def test_default_engine_is_registered(self):
        self.assertIn(AsrSettings().engine, ENGINES)


class JobProtocolTests(unittest.TestCase):
    def test_spec_round_trip_ignores_unknown_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "job.json")
            spec = jobs.JobSpec("a.mp4", "a.srt", directory, {"engine": "funasr", "device": "cpu", "zzz": 1}, ["提督"])
            spec.write(path)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["future_field"] = 1
            path.write_text(json.dumps(payload), encoding="utf-8")
            loaded = jobs.JobSpec.read(path)
            self.assertEqual(loaded.hotwords, ["提督"])
            self.assertEqual((loaded.asr_settings().engine, loaded.asr_settings().device), ("funasr", "cpu"))

    def test_events_survive_noise_lines(self):
        line = jobs.format_event("progress", message="识别中 1/3", fraction=0.4)
        self.assertEqual(jobs.parse_event(line), {"type": "progress", "message": "识别中 1/3", "fraction": 0.4})
        for noise in ("", "Loading model...", "@@asr {broken", '@@asr {"no_type": 1}', '@@asr [1]'):
            self.assertIsNone(jobs.parse_event(noise))

    def test_worker_command_points_the_child_at_this_source_tree(self):
        program, arguments, environment = jobs.worker_command(Path("job.json"), "")
        self.assertTrue(program)
        self.assertEqual(arguments[-2:], ["autoslice.desktop.asr.worker", "job.json"])
        self.assertTrue((Path(environment["PYTHONPATH"]) / "autoslice").is_dir())
        self.assertEqual(jobs.worker_command(Path("j"), "D:/py/python.exe")[0], "D:/py/python.exe")

    def test_frozen_build_has_no_python_of_its_own_and_points_the_child_at_worker_src(self):
        with self.assertRaises(ValueError) as caught:
            jobs.worker_command(Path("job.json"), "", frozen=True, bundle_dir="C:/app/_internal")
        self.assertIn("识别用的 Python", str(caught.exception))
        program, _arguments, environment = jobs.worker_command(
            Path("job.json"), "D:/py/python.exe", frozen=True, bundle_dir="C:/app/_internal")
        self.assertEqual(program, "D:/py/python.exe")
        self.assertEqual(Path(environment["PYTHONPATH"]), Path("C:/app/_internal") / "worker_src")


class WorkerTests(NeedsFfmpeg):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)
        self.video = self.root / "片.mp4"
        make_audio(self.video)

    def spec(self):
        return jobs.JobSpec(str(self.video), str(self.root / "片.srt"), str(self.root / "data"), {"engine": "funasr"})

    def test_success_emits_progress_then_done(self):
        events = []
        engine = ScriptedEngine([[timed("大家好欢迎来到直播间", 0.3, 3.8)], [timed("今天聊聊别的", 0.3, 3.8)]], max_chunk_seconds=6)
        code = run_spec(self.spec(), events.append, engine)
        parsed = [jobs.parse_event(line) for line in events]
        self.assertEqual(code, 0)
        self.assertEqual(parsed[-1]["type"], "done")
        self.assertGreaterEqual(parsed[-1]["cue_count"], 2)
        self.assertTrue(any(event["type"] == "progress" for event in parsed))

    def test_engine_failure_becomes_an_error_event_not_a_crash(self):
        events = []
        code = run_spec(self.spec(), events.append, ScriptedEngine([[]], max_chunk_seconds=6, raise_on=0))
        self.assertEqual(code, 2)
        self.assertEqual(jobs.parse_event(events[-1])["type"], "error")

    def test_unexpected_exception_is_reported_too(self):
        class Broken(ScriptedEngine):
            def recognize(self, wav_path):
                raise RuntimeError("CUDA out of memory")

        events = []
        code = run_spec(self.spec(), events.append, Broken([[]], max_chunk_seconds=6))
        self.assertEqual(code, 1)
        self.assertIn("CUDA out of memory", jobs.parse_event(events[-1])["message"])


class HotwordTests(unittest.TestCase):
    def test_includes_defaults_and_confirmed_learned_terms_without_duplicates_or_wrong_spellings(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = DesktopStorage(directory)
            Knowledge(storage).add_glossary("zeyin", ["子语庭", "提督", "一", "x" * 40])
            terms = build_hotwords("D:/x/【泽音】随便/视频.mp4", storage=storage, title="【泽音】随便")
            self.assertIn("子语庭", terms)
            self.assertEqual(terms.count("提督"), 1)
            self.assertNotIn("一", terms)
            self.assertFalse(any(len(term) > 20 for term in terms))
            self.assertLessEqual(len(terms), 60)

    def test_unknown_streamer_gets_only_the_generic_list(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = DesktopStorage(directory)
            Knowledge(storage).add_glossary("zeyin", ["子语庭"])
            terms = build_hotwords("D:/x/某个没配置的/视频.mp4", storage=storage, title="某个没配置的")
            self.assertNotIn("子语庭", terms)
            self.assertIn("提督", terms)

    def test_limit_is_respected(self):
        self.assertLessEqual(len(build_hotwords("D:/x/视频.mp4", limit=2)), 2)


class DoctorTests(unittest.TestCase):
    def test_unknown_engine_fails_fast(self):
        checks = doctor.run_checks(AsrSettings(engine="nope"))
        self.assertEqual(checks[-1].status, "fail")

    def test_transformers_checks_exact_declared_version_and_markers(self):
        declared = ['transformers==4.57.6', 'transformers>=99; extra == "vllm"']
        self.assertEqual(doctor.check_transformers("4.57.6", declared).status, "ok")
        for version in ("4.57.5", "5.13.0", "bad version"):
            self.assertEqual(doctor.check_transformers(version, declared).status, "warn")
        self.assertEqual(doctor.check_transformers("", declared).status, "fail")
        self.assertEqual(doctor.check_transformers("4.57.6", None).status, "warn")
        self.assertEqual(doctor.check_transformers("4.57.6", ["invalid ???"]).status, "warn")

    def test_qwen_dependency_metadata_is_read_without_importing_the_engine(self):
        from unittest.mock import patch

        with patch.object(doctor.importlib.metadata, "requires", return_value=["transformers==4.57.6"]):
            self.assertEqual(doctor.qwen_requirements(), ["transformers==4.57.6"])
        with patch.dict("sys.modules", {"packaging.requirements": None}):
            self.assertEqual(doctor.check_transformers("4.57.6", ["transformers==4.57.6"]).status, "warn")

    def test_local_model_folder_counts_as_present(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(doctor._model_cached(directory), directory)
        self.assertEqual(doctor._model_cached("不存在的/模型名"), "")
        self.assertEqual(doctor._model_cached("没有斜杠"), "")

    def test_report_marks_and_hints(self):
        text_value = doctor.format_checks([doctor.Check("a", "ok", "好"), doctor.Check("b", "fail", "坏", "这样修")])
        self.assertIn("✓ a：好", text_value)
        self.assertIn("✗ b：坏", text_value)
        self.assertIn("→ 这样修", text_value)

    def test_probe_reports_a_failing_engine_instead_of_raising(self):
        checks = doctor.probe(AsrSettings(engine="qwen3-asr", model="/definitely/not/a/model", aligner=""))
        self.assertTrue(any(check.status == "fail" for check in checks))


class FunAsrDeviceTests(unittest.TestCase):
    def test_explicit_cpu_and_cuda_and_auto_reach_the_shared_model_loader(self):
        import sys
        from types import ModuleType, SimpleNamespace
        from unittest.mock import patch

        from autoslice.desktop.asr.engines.funasr_legacy import FunAsrEngine
        from autoslice.transcription import model_runtime

        funasr = ModuleType("funasr")
        funasr.AutoModel = object
        torch = ModuleType("torch")
        torch.cuda = SimpleNamespace(is_available=lambda: True)
        with patch.dict(sys.modules, {"funasr": funasr, "torch": torch}), \
                patch.object(model_runtime, "load_funasr_model", return_value=SimpleNamespace()) as load, \
                patch.object(model_runtime, "funasr_generate_kwargs", return_value={}):
            for device, expected in (("cpu", "cpu"), ("cuda", "cuda:0"), ("auto", "cuda:0")):
                info = FunAsrEngine(AsrSettings(engine="funasr", device=device)).load()
                self.assertEqual(load.call_args.kwargs["device"], expected)
                self.assertEqual(info["设备"], expected)
            torch.cuda.is_available = lambda: False
            with self.assertRaisesRegex(AsrError, "CUDA"):
                FunAsrEngine(AsrSettings(engine="funasr", device="cuda")).load()


class BenchTests(unittest.TestCase):
    def test_pick_windows_spreads_without_overlap(self):
        from autoslice.desktop.archive.srt import Cue

        cues = [Cue(index, index * 5.0, index * 5.0 + 4.0, f"第{index}句") for index in range(1, 200)]
        windows = bench.pick_windows(cues, 120.0, 3)
        self.assertEqual(len(windows), 3)
        for before, after in zip(windows, windows[1:]):
            self.assertLessEqual(before[1], after[0])
        self.assertLess(windows[0][0], windows[-1][0])
        self.assertEqual(bench.pick_windows(cues[:5], 120.0, 3), [(cues[0].start, cues[4].end)])
        self.assertEqual(bench.pick_windows([], 120.0, 3), [])

    def test_reference_text_uses_cue_midpoints(self):
        from autoslice.desktop.archive.srt import Cue

        cues = [Cue(1, 0, 4, "甲"), Cue(2, 4, 8, "乙"), Cue(3, 8, 12, "丙")]
        self.assertEqual(bench.reference_text(cues, 3.0, 9.0), "乙")
        self.assertEqual(bench.reference_text(cues, 0, 12), "甲乙丙")

    def test_diff_examples_show_short_substitutions_only(self):
        examples = bench.diff_examples("今天我们教个朋友吧", "今天我们交个朋友吧")
        self.assertEqual(len(examples), 1)
        self.assertIn("教", examples[0][0])
        self.assertIn("交", examples[0][1])
        self.assertEqual(bench.diff_examples("一样", "一样"), [])

    def test_end_to_end_scoring_with_fake_engines_and_report(self):
        require_ffmpeg(self)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "【泽音】一场直播"
            folder.mkdir()
            make_audio(folder / "片.mp4", speech=6.0, gap=1.0, tail=6.0)
            (folder / "片.srt").write_text("1\n00:00:00,000 --> 00:00:03,000\n初稿\n", encoding="utf-8")
            reference = ("1\n00:00:00,500 --> 00:00:05,000\n大家好欢迎来到直播间\n\n"
                         "2\n00:00:07,500 --> 00:00:12,000\n今天我们教个朋友\n")
            (folder / "片_校对.srt").write_text(reference, encoding="utf-8")
            cases = bench.discover_cases([root], window_seconds=60, windows_per_video=1)
            self.assertEqual(len(cases), 1)
            self.assertTrue(cases[0].reference_srt.endswith("片_校对.srt"))

            scripts = {
                "完美": [[timed("大家好欢迎来到直播间", 0.5, 5.0), timed("今天我们教个朋友", 7.5, 12.0)]],
                "错一字": [[timed("大家好欢迎来到直播间", 0.5, 5.0), timed("今天我们交个朋友", 7.5, 12.0)]],
            }
            variants = [bench.Variant(label, AsrSettings(engine="funasr"), hot)
                        for label, hot in (("完美", False), ("错一字", True))]
            made = []

            def factory(settings):
                engine = ScriptedEngine(scripts[variants[len(made)].label], max_chunk_seconds=60)
                made.append(engine)
                return engine

            scores = bench.run_bench(cases, variants, storage=DesktopStorage(Path(directory) / "data"), engine_factory=factory)
            perfect, wrong = scores
            self.assertEqual(perfect.errors, 0)
            self.assertEqual(wrong.errors, 1)
            self.assertGreater(wrong.reference_chars, 10)
            self.assertLess(wrong.cer, 0.1)
            self.assertEqual(made[1].hotwords[0:1], ("提督",))
            self.assertEqual(made[0].hotwords, ())
            report = bench.render_report(scores, cases)
            self.assertLess(report.index("完美"), report.index("错一字"), "字错率低的排前面")
            self.assertIn("们教个→们交个", report)
            json.dumps(bench.to_json(scores), ensure_ascii=False)

    def test_a_failing_variant_is_reported_without_stopping_the_rest(self):
        require_ffmpeg(self)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "某场直播"
            folder.mkdir()
            make_audio(folder / "片.mp4", speech=4.0, gap=1.0, tail=4.0)
            (folder / "片.srt").write_text("1\n00:00:00,000 --> 00:00:03,000\n初稿\n", encoding="utf-8")
            (folder / "片_校对.srt").write_text("1\n00:00:00,500 --> 00:00:03,000\n大家好\n", encoding="utf-8")
            cases = bench.discover_cases([root], window_seconds=60, windows_per_video=1)
            variants = [bench.Variant("坏", AsrSettings(), False), bench.Variant("好", AsrSettings(), False)]
            engines = [ScriptedEngine([[]], max_chunk_seconds=60, raise_on=0), ScriptedEngine([[timed("大家好", 0.5, 3.0)]], max_chunk_seconds=60)]
            scores = bench.run_bench(cases, variants, storage=DesktopStorage(Path(directory) / "data"),
                                     engine_factory=lambda _settings: engines.pop(0))
            self.assertTrue(scores[0].error)
            self.assertEqual(scores[1].errors, 0)
            self.assertIn("失败", bench.render_report(scores, cases))

    def test_discover_ignores_folders_without_a_proofread_subtitle(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory, "只有初稿")
            folder.mkdir()
            (folder / "片.mp4").write_bytes(b"x")
            (folder / "片.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\n字\n", encoding="utf-8")
            self.assertEqual(bench.discover_cases([directory]), [])


if __name__ == "__main__":
    unittest.main()
