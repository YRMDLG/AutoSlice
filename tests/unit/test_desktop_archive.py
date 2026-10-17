"""旧切片档案学习：识别、增量扫描、只读保证、报告、建议与应用、运行时检索、命令行。

全部用临时目录里合成的“旧档案”，不碰用户真实的投稿目录和配置。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from autoslice.desktop.archive import ArchiveService, Knowledge, ScanOptions, classify_folder
from autoslice.desktop.archive.__main__ import main as archive_main
from autoslice.desktop.archive.extractors import DEFAULT_EXTRACTORS, Extractor
from autoslice.desktop.archive.extractors.corrections import align_cues, classify_edit
from autoslice.desktop.archive.extractors.terms import grams
from autoslice.desktop.archive.layout import infer_date
from autoslice.desktop.archive.report import build_digest, build_report
from autoslice.desktop.archive.scanner import open_store, scan_roots, walk_folders
from autoslice.desktop.archive.srt import Cue, parse_srt
from autoslice.desktop.foundation import DesktopStorage

try:
    from PIL import Image, ImageDraw
except ImportError:  # pragma: no cover
    Image = None

WRONG, RIGHT = "紫雨亭", "子语庭"


def srt_text(rows) -> str:
    blocks = []
    for index, (start, end, text) in enumerate(rows, 1):
        blocks.append(f"{index}\n00:00:{start:06.3f}".replace(".", ",") + f" --> 00:00:{end:06.3f}".replace(".", ",") + f"\n{text}")
    return "\n\n".join(blocks) + "\n"


RAW = [
    (0.0, 2.0, "大家好我是音姐"),
    (2.0, 4.5, f"今天来聊聊{WRONG}这个活动"),
    (4.5, 7.0, "这个选秀好离谱呃"),
    (7.0, 9.0, "我妈就看AI短剧"),
    (9.0, 11.0, "对对对就是这样，"),
]


def corrected_rows(fix_term: bool) -> list:
    rows = [list(item) for item in RAW]
    if fix_term:
        rows[1][2] = rows[1][2].replace(WRONG, RIGHT)
    rows[2][2] = "这个选秀好离谱"   # 删口癖
    rows[4][2] = "对对对就是这样"   # 只改标点
    return [tuple(item) for item in rows]


def make_archive(root: Path, count: int = 12) -> None:
    """count 个泽音的文件夹（前 8 个把“非月祭”改成了“子语庭”）、一个别的主播、一个只有图片的、一个回收站。"""

    for i in range(count):
        # 每隔三个是同一个系列（标题只差期数），其余标题里“子语庭”后面接的词各不相同
        words = ("直播", "翻车", "名场面", "惊喜", "整活", "唱歌", "聊天", "游戏")
        name = f"子语庭挑战第{i}期" if i % 3 == 0 else f"子语庭{words[i % len(words)]}{10 + i % 5}月{i + 1}日选秀带手机"
        folder = root / "2024-10" / f"【泽音】{name}"
        folder.mkdir(parents=True)
        (folder / "视频.mp4").write_bytes(b"v" * 100)
        (folder / "视频.srt").write_text(srt_text(RAW), encoding="utf-8")
        (folder / "视频_校对.srt").write_text(srt_text(corrected_rows(i < 8)), encoding="utf-8")
        (folder / "视频_字幕版.mp4").write_bytes(b"b" * 50)
        if Image is not None:
            image = Image.new("RGB", (320, 240), (40, 60, 160))
            ImageDraw.Draw(image).rectangle((0, 150, 320, 220), fill=(255, 216, 77))
            image.save(folder / "封面.jpg")
    other = root / "旧的" / "【别的主播】随便聊聊"
    other.mkdir(parents=True)
    (other / "a.mp4").write_bytes(b"v")
    (other / "a.srt").write_text(srt_text(RAW), encoding="utf-8")
    only_images = root / "只有图片"
    only_images.mkdir()
    (only_images / "x.png").write_bytes(b"png")
    trash = root / "$RECYCLE.BIN" / "垃圾"
    trash.mkdir(parents=True)
    (trash / "t.mp4").write_bytes(b"v")


def snapshot(root: Path) -> list:
    return sorted((str(path.relative_to(root)), path.stat().st_size, path.stat().st_mtime_ns) for path in root.rglob("*"))


class ArchiveTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.archive = base / "档案"
        self.archive.mkdir()
        self.storage = DesktopStorage(base / "appdata")
        self.service = ArchiveService(self.storage)
        # 固定纠错写进临时覆盖文件，不碰用户真实配置
        env = patch.dict(os.environ, {"AUTOSLICE_STREAMER_PROFILE_OVERRIDES": str(base / "overrides.json")})
        env.start()
        self.addCleanup(env.stop)
        self.overrides = base / "overrides.json"


class LayoutTests(unittest.TestCase):
    def test_pairs_source_srt_corrected_srt_and_burned_video_and_ignores_generated_names(self):
        files = [
            ("视频.mp4", 100, 5), ("视频.srt", 10, 5), ("视频_校对.srt", 10, 6), ("视频_字幕版.mp4", 50, 7),
            ("封面.jpg", 300, 1), ("素材.png", 900, 1), ("notes.txt", 2, 1),
        ]
        entry = classify_folder("D:/x/【泽音】10月3日测试", "【泽音】10月3日测试", files)
        self.assertEqual(len(entry.videos), 1)
        video = entry.videos[0]
        self.assertTrue(video.srt.endswith("视频.srt") and video.corrected.endswith("视频_校对.srt") and video.burned.endswith("视频_字幕版.mp4"))
        self.assertTrue(entry.has_pairs)
        # 名字带“封面”的图片优先于更大的图片
        self.assertTrue(entry.covers[0].endswith("封面.jpg"))
        self.assertEqual(len(entry.notes), 1)
        self.assertTrue(entry.date.endswith("-10-03"))

    def test_lone_video_and_lone_srt_pair_even_when_names_differ(self):
        entry = classify_folder("D:/x/t", "t", [("a.mp4", 1, 1), ("b.srt", 1, 1), ("b_校对.srt", 1, 1)])
        self.assertTrue(entry.videos[0].srt.endswith("b.srt") and entry.videos[0].corrected.endswith("b_校对.srt"))

    def test_srt_only_folder_and_folder_without_media_are_handled(self):
        only_srt = classify_folder("D:/x/t", "t", [("a.srt", 1, 1)])
        self.assertEqual((only_srt.videos[0].path, only_srt.videos[0].srt.endswith("a.srt")), ("", True))
        self.assertIsNone(classify_folder("D:/x/t", "t", [("a.png", 1, 1), ("b.txt", 1, 1)]))

    def test_dates_come_from_title_then_file_time(self):
        self.assertEqual(infer_date("【泽音】2024年10月3日"), "2024-10-03")
        self.assertEqual(infer_date("【泽音】20241003"), "2024-10-03")
        self.assertEqual(infer_date("没有日期"), "")
        self.assertRegex(infer_date("10月3日", 1_700_000_000_000_000_000), r"^2023-10-03$")
        self.assertRegex(infer_date("没有日期", 1_700_000_000_000_000_000), r"^2023-11-1[45]$")

    def test_signature_changes_when_a_file_changes(self):
        base = [("a.mp4", 1, 1), ("a.srt", 10, 5)]
        first = classify_folder("D:/x/t", "t", base).signature
        self.assertEqual(first, classify_folder("D:/x/t", "t", list(reversed(base))).signature)
        self.assertNotEqual(first, classify_folder("D:/x/t", "t", [("a.mp4", 1, 1), ("a.srt", 10, 6)]).signature)


class SrtTests(unittest.TestCase):
    def test_tolerates_bad_numbering_tags_and_other_encodings(self):
        text = "x\n00:00:01,000 --> 00:00:02,500\n<i>你好</i>\n\n7\n00:00:03.000 --> 00:00:04.000\n第二行\n第二行续\n\n坏块\n"
        cues = parse_srt(text)
        self.assertEqual([cue.text for cue in cues], ["你好", "第二行\n第二行续"])
        self.assertAlmostEqual(cues[1].start, 3.0)
        with tempfile.TemporaryDirectory() as folder:
            from autoslice.desktop.archive.srt import load_cues

            gbk = Path(folder) / "g.srt"
            gbk.write_bytes("1\n00:00:00,000 --> 00:00:01,000\n中文字幕\n".encode("gb18030"))
            self.assertEqual(load_cues(gbk)[0].text, "中文字幕")
            self.assertEqual(load_cues(Path(folder) / "不存在.srt"), [])


class ExtractorTests(unittest.TestCase):
    def test_edit_kinds(self):
        self.assertEqual(classify_edit("你好，世界", "你好，世界")[0], "same")
        self.assertEqual(classify_edit("你好 世界", "你好世界")[0], "linebreak")
        self.assertEqual(classify_edit("你好，世界", "你好世界")[0], "punctuation")
        self.assertEqual(classify_edit("这个选秀好离谱呃", "这个选秀好离谱")[0], "delete_filler")
        kind, pairs = classify_edit(f"聊聊{WRONG}活动", f"聊聊{RIGHT}活动")
        self.assertEqual((kind, pairs), ("replace", ((WRONG, RIGHT),)))
        self.assertEqual(classify_edit("今天来了", "今天来了很多人")[0], "insert_text")

    def test_alignment_counts_splits_and_merges(self):
        raw = [Cue(1, 0, 4, "甲乙丙丁"), Cue(2, 4, 6, "戊"), Cue(3, 6, 8, "己"), Cue(4, 8, 10, "庚")]
        corrected = [Cue(1, 0, 2, "甲乙"), Cue(2, 2, 4, "丙丁"), Cue(3, 4, 8, "戊己"), Cue(4, 8, 10, "庚")]
        pairs, splits, merges = align_cues(raw, corrected)
        self.assertEqual((splits, merges), (1, 1))
        self.assertEqual([(a.text, b.text) for a, b in pairs], [("庚", "庚")])
        positional, no_split, no_merge = align_cues(raw, [Cue(c.index, c.start, c.end, "x") for c in raw])
        self.assertEqual((len(positional), no_split, no_merge), (4, 0, 0))

    def test_term_candidates_skip_edge_stop_words(self):
        counts = grams("子语庭的活动，子语庭真好")
        self.assertIn("子语庭", counts)
        self.assertNotIn("的活", counts)
        self.assertEqual(counts["子语庭"], 2)


class ScanTests(ArchiveTestCase):
    def test_scan_extracts_facts_and_skips_unchanged_folders(self):
        make_archive(self.archive)
        first = self.service.scan([str(self.archive)])
        # 12 个泽音 + 1 个别的主播；只有图片的和回收站不算
        self.assertEqual((first.entries, first.changed, first.skipped), (13, 13, 0))
        with self.service._store() as store:
            self.assertEqual(store.scalar("SELECT COUNT(*) FROM entries"), 13)
            self.assertEqual(store.scalar("SELECT COUNT(DISTINCT entry_id) FROM corrections WHERE wrong = ?", (WRONG,)), 8)
            self.assertGreater(store.scalar("SELECT COUNT(*) FROM terms"), 50)
            self.assertEqual(store.scalar("SELECT COUNT(*) FROM subtitle_stats"), 13)
            self.assertEqual(store.scalar("SELECT COUNT(DISTINCT streamer) FROM entries WHERE streamer = 'zeyin'"), 1)
            self.assertEqual(store.scalar("SELECT COUNT(*) FROM entries WHERE streamer = 'ext:别的主播'"), 1)
        second = self.service.scan([str(self.archive)])
        self.assertEqual((second.changed, second.skipped), (0, 13))
        # 改一份校对稿：只重抽它
        target = next(self.archive.rglob("视频_校对.srt"))
        target.write_text(srt_text(corrected_rows(False)) + "\n", encoding="utf-8")
        os.utime(target, ns=(target.stat().st_atime_ns, target.stat().st_mtime_ns + 5_000_000_000))
        third = self.service.scan([str(self.archive)])
        self.assertEqual((third.changed, third.skipped), (1, 12))

    @unittest.skipIf(Image is None, "没有 Pillow")
    def test_cover_features(self):
        make_archive(self.archive, 3)
        self.service.scan([str(self.archive)])
        with self.service._store() as store:
            rows = store.query("SELECT * FROM covers")
        self.assertEqual(len(rows), 3)
        row = rows[0]
        self.assertEqual((row["width"], row["height"], row["named"]), (320, 240, 1))
        palette = json.loads(row["palette"])
        self.assertTrue(any(color[0].startswith("#ff") for color in palette))
        # 黄色条在下三分之一，文字边缘应该集中在下面
        self.assertGreater(row["edge_bottom"] + row["edge_mid"], row["edge_top"])

    def test_scan_never_modifies_or_writes_into_the_archive(self):
        make_archive(self.archive)
        before = snapshot(self.archive)
        self.service.scan([str(self.archive)])
        self.service.report()
        self.service.proposals()
        self.assertEqual(snapshot(self.archive), before)
        self.assertTrue(self.service.db_path.is_file())
        self.assertFalse(str(self.archive) in str(self.service.db_path))

    def test_cancel_keeps_progress_and_resumes_where_it_stopped(self):
        make_archive(self.archive)
        cancel = threading.Event()

        def stop_after_a_few(state):
            if state.changed >= 4:
                cancel.set()

        with patch("autoslice.desktop.archive.scanner._PROGRESS_INTERVAL", 0):
            cut = self.service.scan([str(self.archive)], progress=stop_after_a_few, cancel=cancel)
        self.assertTrue(cut.cancelled)
        self.assertTrue(0 < cut.changed < 13)
        # 被取消的那次不清理“已经不存在的文件夹”，已写入的保留
        with self.service._store() as store:
            self.assertEqual(store.scalar("SELECT COUNT(*) FROM entries"), cut.changed)
        rest = self.service.scan([str(self.archive)])
        self.assertFalse(rest.cancelled)
        self.assertEqual(rest.skipped, cut.changed)
        self.assertEqual(rest.entries, 13)

    def test_one_broken_folder_or_extractor_does_not_stop_the_scan(self):
        make_archive(self.archive, 4)
        (self.archive / "2024-10" / "坏字幕").mkdir()
        (self.archive / "2024-10" / "坏字幕" / "a.mp4").write_bytes(b"v")
        (self.archive / "2024-10" / "坏字幕" / "a.srt").write_bytes(b"\x00\xff\xfe garbage \x01")
        (self.archive / "2024-10" / "坏字幕" / "a_校对.srt").write_bytes(b"\xff" * 50)

        class Exploding(Extractor):
            name = "exploding"
            schema = "CREATE TABLE IF NOT EXISTS boom (entry_id INTEGER NOT NULL, x TEXT NOT NULL);"
            tables = {"boom": ("x",)}

            def extract(self, entry, context):
                if "第3期" in entry.title:
                    raise RuntimeError("故意的")
                return {"boom": [("ok",)]}

        extractors = (*DEFAULT_EXTRACTORS, Exploding())
        with open_store(self.service.db_path, extractors) as store:
            result = scan_roots(store, [str(self.archive)], ScanOptions(), extractors=extractors)
            self.assertEqual(result.entries, 6)  # 4 个泽音 + 坏字幕 + 别的主播
            self.assertEqual(len(result.errors), 1)
            self.assertIn("故意的", result.errors[0])
            # 出错的那个文件夹的其他事实照样写进去了
            self.assertEqual(store.scalar("SELECT COUNT(*) FROM subtitle_stats"), 5)

    def test_removed_folders_are_forgotten_after_a_complete_scan(self):
        make_archive(self.archive, 3)
        self.service.scan([str(self.archive)])
        victim = next(self.archive.rglob("视频_校对.srt")).parent
        for path in victim.iterdir():
            path.unlink()
        victim.rmdir()
        result = self.service.scan([str(self.archive)])
        self.assertEqual(result.removed, 1)
        with self.service._store() as store:
            self.assertEqual(store.scalar("SELECT COUNT(*) FROM entries"), 3 - 1 + 1)  # 另有 1 个别的主播
            self.assertEqual(store.scalar("SELECT COUNT(*) FROM corrections WHERE entry_id NOT IN (SELECT id FROM entries)"), 0)

    def test_walk_skips_system_and_hidden_directories(self):
        make_archive(self.archive, 1)
        (self.archive / ".git").mkdir()
        (self.archive / ".git" / "x.mp4").write_bytes(b"v")
        folders = [folder for folder, _name, _files in walk_folders(str(self.archive).replace("\\", "/"))]
        self.assertFalse(any("RECYCLE" in folder or ".git" in folder for folder in folders))


class ReportAndProposalTests(ArchiveTestCase):
    def setUp(self):
        super().setUp()
        make_archive(self.archive)
        self.service.scan([str(self.archive)])

    def test_report_summarizes_corrections_titles_habits_and_covers(self):
        with self.service._store() as store:
            report = build_report(store)
        self.assertEqual(report["overview"]["entries"], 13)
        self.assertEqual(report["overview"]["with_both"], 12)
        top = report["corrections"][0]
        self.assertEqual((top["wrong"], top["right"], top["videos"], top["dominance"]), (WRONG, RIGHT, 8, 1.0))
        self.assertEqual(report["edits"]["kinds"]["delete_filler"], 12)
        self.assertEqual(report["edits"]["kinds"]["punctuation"], 12)
        titles = report["titles"]
        self.assertEqual(titles["count"], 13)
        self.assertTrue(any("N" in item["pattern"] and item["count"] >= 4 for item in titles["series"]))
        self.assertGreater(titles["bracket_prefix_rate"], 0.9)
        habits = report["subtitle_habits"]
        self.assertEqual(habits["videos"], 13)
        self.assertGreater(habits["cps_p50"], 0)
        self.assertTrue(any(item["term"] == "子语庭" for items in report["terms"].values() for item in items))
        if Image is not None:
            self.assertEqual(report["covers"]["count"], 12)

    def test_digest_is_small_valid_and_has_no_paths(self):
        with self.service._store() as store:
            report = build_report(store)
        digest = build_digest(report, max_bytes=20_000)
        text = json.dumps(digest, ensure_ascii=False)
        self.assertLessEqual(len(text.encode("utf-8")), 20_000)
        self.assertNotIn(str(self.archive).replace("\\", "/"), text)
        self.assertNotIn("roots", digest["overview"])
        paths = self.service.report()
        self.assertTrue(all(path.is_file() for path in paths.values()))
        self.assertIn("旧切片学习报告", paths["markdown"].read_text(encoding="utf-8"))
        self.assertEqual(json.loads(paths["digest"].read_text(encoding="utf-8"))["kind"], "autoslice-archive-digest")

    def test_proposals_cover_replacements_terms_titles_and_limits(self):
        proposals = self.service.proposals()
        kinds = {item.kind for item in proposals}
        self.assertTrue({"replacement", "glossary", "titles"} <= kinds)
        replacement = next(item for item in proposals if item.kind == "replacement")
        self.assertEqual((replacement.streamer, replacement.payload), ("zeyin", {"wrong": WRONG, "right": RIGHT}))
        self.assertIn("8 个视频", replacement.evidence)
        # 别的主播（没有档案）的东西不会生成需要写进档案的建议
        self.assertFalse(any(item.streamer.startswith("ext:") for item in proposals))

    def test_applying_writes_overrides_and_knowledge_and_hides_done_proposals(self):
        proposals = self.service.proposals()
        chosen = [item for item in proposals if item.kind in ("replacement", "titles")] + [
            item for item in proposals if item.kind == "glossary" and item.payload["term"] == "子语庭"
        ]
        applied, failures = self.service.apply(chosen)
        self.assertEqual(failures, [])
        self.assertEqual(len(applied), len(chosen))
        from autoslice.streamer_profiles import resolve_streamer_profile

        self.assertIn((WRONG, RIGHT), resolve_streamer_profile("zeyin").asr_replacements)
        self.assertIn("子语庭", self.service.knowledge.glossary("zeyin"))
        remaining = {item.id for item in self.service.proposals()}
        self.assertFalse(remaining & {item.id for item in chosen})
        # 固定纠错写进词库后就成了“已有”，不会再被提；术语和标题范例保留，状态是已接受
        done = {item.id: item.status for item in self.service.proposals(include_done=True)}
        self.assertTrue(all(done[item.id] == "accepted" for item in chosen if item.kind != "replacement"))
        # 拒绝的也不再出现
        left = self.service.proposals()
        self.service.reject(left[:1])
        self.assertNotIn(left[0].id, {item.id for item in self.service.proposals()})

    def test_forget_everything_removes_learned_data_but_not_the_archive(self):
        before = snapshot(self.archive)
        self.service.apply([item for item in self.service.proposals() if item.kind == "titles"])
        self.assertTrue(self.service.db_path.is_file())
        self.service.forget_everything()
        self.assertFalse(self.service.dir.exists())
        self.assertEqual(snapshot(self.archive), before)
        self.assertEqual(self.service.roots(), [])


class KnowledgeTests(ArchiveTestCase):
    def test_relevant_glossary_prefers_terms_present_in_the_text_and_longer_ones(self):
        knowledge = Knowledge(self.storage)
        knowledge.add_glossary("zeyin", ["子语庭", "月祭", "四禧丸子", "无关词"])
        found = knowledge.relevant_glossary("zeyin", "今天聊子语庭和四禧丸子", limit=3)
        self.assertEqual(found[:2], ["四禧丸子", "子语庭"])
        self.assertEqual(knowledge.relevant_glossary("nobody", "x"), [])
        self.assertEqual(knowledge.add_glossary("zeyin", ["子语庭"]), 0)

    def test_title_examples_pick_the_most_similar_then_fill_with_recent(self):
        knowledge = Knowledge(self.storage)
        knowledge.set_titles("zeyin", ["【泽音】聊游戏", "【泽音】唱歌翻车", "【泽音】带手机选秀离谱", "【泽音】吃饭"])
        picked = knowledge.title_examples("zeyin", "选秀带手机", k=2)
        self.assertEqual(picked[0], "【泽音】带手机选秀离谱")
        self.assertEqual(len(picked), 2)
        self.assertEqual(knowledge.title_examples("zeyin", "", k=2), ["【泽音】带手机选秀离谱", "【泽音】吃饭"])

    def test_subtitle_limits_round_trip(self):
        knowledge = Knowledge(self.storage)
        knowledge.set_habits("zeyin", {"cps_limit": 9.5, "chars_limit": 20, "line_limit": 14})
        self.assertEqual(Knowledge(self.storage).subtitle_limits("zeyin")["cps_limit"], 9.5)
        self.assertEqual(knowledge.subtitle_limits("none"), {})


class RuntimeHookTests(ArchiveTestCase):
    def test_profile_id_for_uses_the_streamer_profiles_and_ignores_generic(self):
        from autoslice.desktop.archive.knowledge import profile_id_for

        self.assertEqual(profile_id_for("D:/x/【泽音】随便/视频.mp4", "【泽音】随便"), "zeyin")
        self.assertEqual(profile_id_for("D:/x/某个没配置的/视频.mp4", "某个没配置的"), "")

    def test_ai_review_gets_only_relevant_learned_terms_and_nothing_when_nothing_learned(self):
        from types import SimpleNamespace

        from autoslice.desktop.ai_review import AIReviewService
        from autoslice.desktop.projects import SubmissionProjectService
        from autoslice.desktop.subtitles import SubtitleDocument

        folder = self.archive / "【投稿】测试"
        folder.mkdir()
        (folder / "成片.mp4").touch()
        (folder / "成片.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\n今天聊子语庭\n", encoding="utf-8")
        project = SubmissionProjectService(self.archive).refresh().projects[0]
        document = SubtitleDocument.load(project.videos[0])
        calls = []

        def checker(path, **kwargs):
            calls.append(kwargs)
            return {"suggestions": []}

        config = SimpleNamespace(base_url="https://example.invalid", api_type="openai", model="m", review_reasoning_effort="high")
        profile = SimpleNamespace(id="zeyin", label="泽音", subtitle_review_fingerprint=lambda: "v1")
        service = AIReviewService(self.storage, checker=checker, config_loader=lambda: config)
        with patch("autoslice.desktop.ai_review.resolve_streamer_profile", return_value=profile):
            service.check(document, project.title)
            self.assertNotIn("glossary", calls[0])
            Knowledge(self.storage).add_glossary("zeyin", ["子语庭", "完全没提到的词"])
            service.check(document, project.title, force=True)
        self.assertEqual(calls[1]["glossary"][0], "子语庭")

    def test_quality_flags_use_personal_limits_only_when_given(self):
        from types import SimpleNamespace

        from autoslice.desktop.subtitle_preview import SubtitlePreviewService

        entry = SimpleNamespace(index=1, start="00:00:00,000", end="00:00:01,000", text="一二三四五六七八九十一二\n一二三四五六七八")
        self.assertEqual(SubtitlePreviewService.quality_flags([entry]), {})
        flags = SubtitlePreviewService.quality_flags([entry], {"cps_limit": 8.0, "chars_limit": 16, "line_limit": 10})
        text = "".join(flags[1])
        self.assertIn("读速偏快", text)
        self.assertIn("比你平时的字幕长", text)
        self.assertIn("单行比你平时的长", text)
        calm = SimpleNamespace(index=2, start="00:00:00,000", end="00:00:03,000", text="一二三四五")
        self.assertEqual(SubtitlePreviewService.quality_flags([calm], {"cps_limit": 8.0, "chars_limit": 16, "line_limit": 10}), {})


try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox
except ImportError:  # pragma: no cover
    QApplication = None


@unittest.skipIf(QApplication is None, "PySide6 不可用")
class ArchivePanelTests(ArchiveTestCase):
    def setUp(self):
        super().setUp()
        from autoslice.desktop.qt_app.archive_panel import ArchivePanel

        self.app = QApplication.instance() or QApplication([])
        self.applied = []

        def run(action, callback):  # 测试里同步执行，不涉及线程时序
            try:
                callback(action(), None)
            except Exception as exc:  # noqa: BLE001
                callback(None, exc)

        self.panel = ArchivePanel(self.service, run, lambda: self.applied.append(1))
        self.addCleanup(self.panel.deleteLater)
        make_archive(self.archive)

    def test_add_folder_scan_review_apply_and_forget(self):
        self.assertFalse(self.panel.start_button.isEnabled())
        self.assertIn("还没扫描过", self.panel.status.text())
        with patch.object(QFileDialog, "getExistingDirectory", return_value=str(self.archive)):
            self.panel._add()
        self.assertEqual(self.panel.folders.count(), 1)
        self.assertTrue(self.panel.start_button.isEnabled())
        self.assertFalse(self.panel.report_button.isEnabled())
        self.panel._start()
        self.assertFalse(self.panel.running)
        self.assertIn("已学习 13 个文件夹", self.panel.status.text())
        self.assertIn("建议等你确认", self.panel.status.text())
        self.assertTrue(all(button.isEnabled() for button in (
            self.panel.report_button, self.panel.proposals_button, self.panel.forget_button)))
        self.assertTrue((self.service.report_dir / "report.md").is_file())
        self.assertTrue(self.applied)
        # 建议对话框：固定纠错默认勾选、术语默认不勾；应用后行消失、固定纠错真的写进了词库
        from autoslice.desktop.qt_app.archive_panel import ProposalsDialog

        dialog = ProposalsDialog(self.service, self.service.proposals(), lambda: self.applied.append(2))
        self.addCleanup(dialog.deleteLater)
        checked_kinds = {item.kind for item in dialog.checked()}
        self.assertIn("replacement", checked_kinds)
        self.assertNotIn("glossary", checked_kinds)
        rows_before = dialog.list.count()
        dialog._apply()
        self.assertLess(dialog.list.count(), rows_before)
        self.assertIn("已应用", dialog.message.text())
        from autoslice.streamer_profiles import resolve_streamer_profile

        self.assertIn((WRONG, RIGHT), resolve_streamer_profile("zeyin").asr_replacements)
        # 忽略
        dialog._set_all(Qt.CheckState.Checked)
        count = dialog.list.count()
        dialog._reject()
        self.assertEqual(dialog.list.count(), 0)
        self.assertIn(f"已忽略 {count} 条", dialog.message.text())
        # 清除
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            self.panel._forget()
        self.assertFalse(self.service.dir.exists())
        self.assertIn("还没扫描过", self.panel.status.text())

    def test_progress_text_is_shown_while_running_and_stop_sets_the_flag(self):
        import threading

        from autoslice.desktop.archive import ScanProgress

        self.panel._cancel = threading.Event()
        self.panel._bridge.progress.emit(ScanProgress(folders=120, entries=80, changed=30, skipped=50))
        self.assertIn("已看 120 个文件夹 · 认出 80 个 · 新学 30 · 跳过 50", self.panel.status.text())
        self.panel._sync()
        self.assertTrue(self.panel.stop_button.isEnabled())
        self.assertFalse(self.panel.start_button.isEnabled())
        self.panel.stop()
        self.assertTrue(self.panel._cancel.is_set())

    def test_settings_page_has_the_panel_and_closing_the_window_stops_a_scan(self):
        from autoslice.desktop.projects import SubmissionProjectService
        from autoslice.desktop.qt_app.window import DesktopWindow

        window = DesktopWindow(SubmissionProjectService(self.archive), self.storage)
        self.addCleanup(window.close)
        self.assertTrue(hasattr(window, "archive_panel"))
        import threading

        window.archive_panel._cancel = threading.Event()
        window.close()
        self.assertTrue(window.archive_panel._cancel.is_set())


class CliTests(ArchiveTestCase):
    def run_cli(self, *args) -> tuple[int, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(io.StringIO()):
            code = archive_main(["--data-dir", str(self.storage.root), *args])
        return code, buffer.getvalue()

    def test_inspect_scan_proposals_apply_forget(self):
        make_archive(self.archive)
        before = snapshot(self.archive)
        code, out = self.run_cli("inspect", str(self.archive))
        self.assertEqual(code, 0)
        self.assertIn("认出 13 个投稿文件夹", out)
        self.assertIn("初稿+校对稿成对：12", out)
        self.assertFalse(self.service.db_path.exists())  # inspect 什么都不写
        code, out = self.run_cli("scan", str(self.archive))
        self.assertEqual(code, 0)
        self.assertIn("digest.json", out)
        code, out = self.run_cli("proposals")
        self.assertIn(f"「{WRONG}」→「{RIGHT}」", out)
        self.assertEqual(self.run_cli("apply")[0], 2)  # 不指定范围不会全应用
        code, out = self.run_cli("apply", "--kinds", "replacement")
        self.assertIn("已应用 1 条", out)
        self.assertEqual(self.run_cli("forget")[0], 2)
        self.assertEqual(self.run_cli("forget", "--yes")[0], 0)
        self.assertFalse(self.service.dir.exists())
        self.assertEqual(snapshot(self.archive), before)


if __name__ == "__main__":
    unittest.main()
