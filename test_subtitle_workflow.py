import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from subtitle_workflow import (
    DEFAULT_SUBTITLE_STYLE,
    DEFAULT_VIDEO_EXPORT,
    EXACT_SUBTITLE_FONT,
    EXACT_SUBTITLE_FONT_RESOLVED,
    build_ass_document,
    burn_subtitles,
    high_confidence_corrections,
    normalise_subtitle_style,
    normalise_video_export,
    parse_srt_document,
    render_subtitle_preview,
    save_corrected_srt,
    scan_submission_pairs,
    serialise_srt,
    suggest_subtitle_corrections,
    verify_exact_subtitle_font,
    write_ass_from_srt,
)


SAMPLE_SRT = """1
00:00:01,000 --> 00:00:02,500 position:50%
音音晚上好

2
00:00:02,500 --> 00:00:05,000
我看到一个瓦衣
是兔女郎的瓦衣

3
00:00:05,000 --> 00:00:07,000
这个娃衣很特别
"""


class SubtitleParsingAndReviewTests(unittest.TestCase):
    def test_parse_and_serialise_preserve_multiline_timeline_and_settings(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "字幕.srt"
            path.write_text(SAMPLE_SRT, encoding="utf-8")
            cues = parse_srt_document(path)

        self.assertEqual(len(cues), 3)
        self.assertEqual(cues[1].text, "我看到一个瓦衣\n是兔女郎的瓦衣")
        self.assertEqual(cues[0].settings, " position:50%")
        rebuilt = serialise_srt(cues)
        self.assertIn("00:00:01,000 --> 00:00:02,500 position:50%", rebuilt)
        self.assertIn("我看到一个瓦衣\n是兔女郎的瓦衣", rebuilt)

    def test_gb18030_srt_is_supported(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "字幕.srt"
            path.write_bytes(SAMPLE_SRT.encode("gb18030"))
            cues = parse_srt_document(path)
        self.assertEqual(cues[0].text, "音音晚上好")

    def test_invalid_or_reverse_timeline_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "字幕.srt"
            path.write_text(
                "1\n00:00:03,000 --> 00:00:02,000\n测试\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "结束时间"):
                parse_srt_document(path)

    def test_save_corrected_srt_keeps_source_and_timeline(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "字幕.srt"
            source.write_text(SAMPLE_SRT, encoding="utf-8")
            source_before = source.read_bytes()
            output = save_corrected_srt(
                source,
                [{
                    "index": 2,
                    "original": "我看到一个瓦衣\n是兔女郎的瓦衣",
                    "corrected": "我看到一个娃衣\n是兔女郎的娃衣",
                }],
            )
            corrected = Path(output).read_text(encoding="utf-8")

            self.assertEqual(source.read_bytes(), source_before)
            self.assertIn("我看到一个娃衣\n是兔女郎的娃衣", corrected)
            self.assertIn("00:00:02,500 --> 00:00:05,000", corrected)
            self.assertTrue(output.endswith("_校对.srt"))

    def test_save_rejects_stale_original_and_unknown_index(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "字幕.srt"
            source.write_text(SAMPLE_SRT, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "原文已变化"):
                save_corrected_srt(
                    source,
                    [{"index": 1, "original": "旧原文", "corrected": "新文字"}],
                )
            with self.assertRaisesRegex(ValueError, "序号不存在"):
                save_corrected_srt(source, [{"index": 99, "corrected": "新文字"}])

    def test_scan_pairs_different_jianying_names_and_ignores_outputs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            clip = root / "投稿标题"
            clip.mkdir()
            (clip / "7月16日 (1).mp4").write_bytes(b"video")
            (clip / "7月16日 (2).srt").write_text(SAMPLE_SRT, encoding="utf-8")
            (clip / "7月16日 (1)_字幕版.mp4").write_bytes(b"output")
            (clip / "7月16日 (2)_校对.srt").write_text(SAMPLE_SRT, encoding="utf-8")

            pairs = scan_submission_pairs(root)

        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["title"], "投稿标题")
        self.assertEqual(pairs[0]["cue_count"], 3)
        self.assertTrue(pairs[0]["video_path"].endswith("7月16日 (1).mp4"))

    def test_review_retries_incomplete_batch_filters_rewrite_and_caches(self):
        calls = []

        def fake_runner(prompt, compact_prompt):
            calls.append(prompt)
            if len(calls) == 1:
                return {"reviewed_indices": [1, 2], "corrections": []}
            return {
                "reviewed_indices": [1, 2, 3],
                "corrections": [
                    {
                        "index": 1,
                        "original": "音音晚上好",
                        "corrected": "音音，晚上好！",
                        "reason": "只改标点",
                        "confidence": 0.99,
                    },
                    {
                        "index": 2,
                        "original": "我看到一个瓦衣\n是兔女郎的瓦衣",
                        "corrected": "我看到一个娃衣\n是兔女郎的娃衣",
                        "reason": "结合后一句‘娃衣’确认同音误识别",
                        "confidence": 0.97,
                    },
                    {
                        "index": 3,
                        "original": "这个娃衣很特别",
                        "corrected": "她觉得这一套兔女郎服装很独特",
                        "reason": "润色",
                        "confidence": 0.92,
                    },
                ],
            }

        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "字幕.srt"
            source.write_text(SAMPLE_SRT, encoding="utf-8")
            result = suggest_subtitle_corrections(
                source,
                context_title="兔女郎娃衣",
                llm_runner=fake_runner,
            )
            cached = suggest_subtitle_corrections(
                source,
                context_title="兔女郎娃衣",
                llm_runner=lambda *_: self.fail("命中缓存后不应调用 AI"),
            )

        self.assertEqual(len(calls), 2)
        self.assertEqual([item["index"] for item in result["suggestions"]], [2])
        self.assertEqual(result["suggestions"][0]["corrected"], "我看到一个娃衣\n是兔女郎的娃衣")
        self.assertFalse(result["cache_hit"])
        self.assertTrue(cached["cache_hit"])

    def test_review_cache_invalidates_when_source_changes(self):
        calls = []

        def runner(prompt, compact_prompt):
            calls.append(1)
            indices = json.loads(prompt.split("待检查序号：", 1)[1].split("\n", 1)[0])
            return {"reviewed_indices": indices, "corrections": []}

        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "字幕.srt"
            source.write_text(SAMPLE_SRT, encoding="utf-8")
            suggest_subtitle_corrections(source, llm_runner=runner)
            source.write_text(SAMPLE_SRT.replace("很特别", "非常特别"), encoding="utf-8")
            suggest_subtitle_corrections(source, llm_runner=runner)

        self.assertEqual(len(calls), 2)

    def test_high_confidence_only_selects_default_safe_items(self):
        selected = high_confidence_corrections({
            "suggestions": [
                {"index": 1, "confidence": 0.91},
                {"index": 2, "confidence": 0.72},
            ]
        })
        self.assertEqual([item["index"] for item in selected], [1])


class SubtitleRenderingTests(unittest.TestCase):
    @staticmethod
    def _make_video(path, duration=2):
        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", f"color=c=0x243044:s=640x360:d={duration}",
                "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                "-shortest", str(path),
            ],
            check=True,
        )

    def test_default_style_is_exact_user_requested_jianying_style(self):
        self.assertEqual(DEFAULT_SUBTITLE_STYLE, {
            "font_name": "Noto Sans S Chinese Black",
            "font_size": 8.0,
            "font_color": "ffffff",
            "outline_color": "d06e95",
            "outline_width": 60.0,
            "x": 0.0,
            "y": -788.0,
            "shadow": 0.0,
        })
        with self.assertRaisesRegex(ValueError, "字体必须"):
            normalise_subtitle_style({"font_name": "Noto Sans SC"})

    def test_default_video_export_matches_jianying_settings(self):
        self.assertEqual(normalise_video_export(), {
            "width": 1920,
            "height": 1080,
            "bitrate_kbps": 8000,
            "rate_control": "vbr",
            "codec": "h264",
            "container": "mp4",
            "fps": 60.0,
            "color_space": "bt709",
            "color_range": "tv",
            "audio": "copy",
        })
        self.assertEqual(DEFAULT_VIDEO_EXPORT["bitrate_kbps"], 8000)

    def test_ass_maps_style_color_position_and_escapes_text(self):
        cues = [
            parse_srt_document_from_text(
                "1\n00:00:00,000 --> 00:00:01,000\n测试{样式}\\路径\n第二行\n"
            )[0]
        ]
        document = build_ass_document(cues, 1920, 1080)
        self.assertIn(f"Style: Default,{EXACT_SUBTITLE_FONT},54.0", document)
        self.assertIn("&H00FFFFFF", document)
        self.assertIn("&H00956ED0", document)
        self.assertIn(",3.2,0.0,5,", document)
        self.assertIn(r"{\an5\pos(960,966)}", document)
        self.assertIn(r"测试\{样式\}\\路径\N第二行", document)

    def test_exact_font_resolves_to_noto_sans_hans_black(self):
        verify_exact_subtitle_font.cache_clear()
        result = verify_exact_subtitle_font()
        self.assertTrue(result["available"], result)
        self.assertEqual(result["requested"], EXACT_SUBTITLE_FONT)
        self.assertEqual(result["resolved"], EXACT_SUBTITLE_FONT_RESOLVED)

    def test_write_ass_saves_style_without_touching_srt(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            video = root / "clip.mp4"
            srt = root / "clip_校对.srt"
            self._make_video(video)
            srt.write_text(SAMPLE_SRT, encoding="utf-8")
            original = srt.read_bytes()

            result = write_ass_from_srt(srt, video)

            self.assertEqual(srt.read_bytes(), original)
            self.assertTrue(Path(result["ass_path"]).is_file())
            self.assertTrue(Path(result["style_path"]).is_file())
            self.assertIn(EXACT_SUBTITLE_FONT, Path(result["ass_path"]).read_text(encoding="utf-8"))
            style = json.loads(Path(result["style_path"]).read_text(encoding="utf-8"))
            self.assertEqual(style, DEFAULT_SUBTITLE_STYLE)

    def test_preview_and_software_burn_produce_valid_media(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            video = root / "clip.mp4"
            srt = root / "clip_校对.srt"
            output = root / "clip_字幕版.mp4"
            self._make_video(video)
            srt.write_text(
                "1\n00:00:00,000 --> 00:00:01,900\n音音字幕预览\n",
                encoding="utf-8",
            )

            fast_export = {
                "width": 640,
                "height": 360,
                "fps": 30,
                "bitrate_kbps": 1200,
            }
            jpeg, selected_time = render_subtitle_preview(
                video,
                srt,
                export_settings=fast_export,
            )
            result = burn_subtitles(
                video,
                srt,
                output_path=output,
                encoder="libx264",
                export_settings=fast_export,
            )
            decode = subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-xerror",
                    "-i", str(output), "-f", "null", os.devnull,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            self.assertTrue(jpeg.startswith(b"\xff\xd8"))
            self.assertGreater(len(jpeg), 1000)
            self.assertAlmostEqual(selected_time, 0.95, places=2)
            self.assertTrue(output.is_file())
            self.assertEqual(result["encoder"], "libx264")
            self.assertTrue(result["output_video_info"]["has_audio"])
            self.assertEqual(result["output_video_info"]["width"], 640)
            self.assertEqual(result["output_video_info"]["height"], 360)
            self.assertAlmostEqual(result["output_video_info"]["fps"], 30, places=2)
            self.assertEqual(result["output_video_info"]["color_space"], "bt709")
            self.assertEqual(result["output_video_info"]["color_transfer"], "bt709")
            self.assertEqual(result["output_video_info"]["color_primaries"], "bt709")
            self.assertLess(
                abs(result["output_video_info"]["duration"] - 2.0),
                0.2,
            )
            self.assertEqual(decode.returncode, 0, decode.stderr.decode("utf-8", errors="replace"))
            self.assertFalse((root / "clip_字幕版.part.mp4").exists())


def parse_srt_document_from_text(text):
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "inline.srt"
        path.write_text(text, encoding="utf-8")
        return parse_srt_document(path)


if __name__ == "__main__":
    unittest.main()
