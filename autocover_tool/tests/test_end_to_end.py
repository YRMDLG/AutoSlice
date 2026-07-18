"""从真实视频扫描到双比例导出的端到端测试。"""

from __future__ import annotations

import io
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from app import create_app


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "需要 ffmpeg/ffprobe")
class EndToEndTests(unittest.TestCase):
    """使用临时合成视频覆盖完整 Web 工作流。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.clips = self.root / "切片"
        self.clips.mkdir()
        self.video = self.clips / "01_真实链路测试.mp4"
        command = [
            shutil.which("ffmpeg") or "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=960x540:rate=24",
            "-t",
            "6",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(self.video),
        ]
        subprocess.run(command, check=True)
        self.title_file = self.root / "投稿标题.md"
        self.title_file.write_text(
            "原文件：`01_真实链路测试.mp4`\n\n**【泽音】音音发现离谱现场，当场笑出声**\n",
            encoding="utf-8",
        )
        self.output = self.root / "输出"
        self.cache = self.root / "缓存"
        self.client = create_app({"TESTING": True}).test_client()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_scan_extract_preview_and_export_real_video(self) -> None:
        scan = self.client.post(
            "/api/workspace/scan",
            json={
                "root": str(self.clips),
                "title_file": str(self.title_file),
                "output_dir": str(self.output),
                "cache_dir": str(self.cache),
            },
        )
        self.assertEqual(scan.status_code, 200)
        task = scan.get_json()["tasks"][0]
        self.assertEqual(task["title"], "【泽音】音音发现离谱现场，当场笑出声")

        candidates = self.client.post(
            f"/api/tasks/{task['id']}/candidates",
            json={"count": 3},
        )
        self.assertEqual(candidates.status_code, 200)
        ready_task = candidates.get_json()["task"]
        self.assertEqual(ready_task["status"], "ready")
        self.assertEqual(len(ready_task["candidates"]), 3)
        self.assertNotIn(str(self.root), str(ready_task))

        cached = self.client.post(
            f"/api/tasks/{task['id']}/candidates",
            json={"count": 3},
        ).get_json()["task"]
        self.assertTrue(all(item["cached"] for item in cached["candidates"]))

        preview = self.client.post(
            f"/api/tasks/{task['id']}/preview",
            json={"canvas_key": "4x3"},
        ).get_json()["preview"]
        with self.client.get(f"/api/media/{preview['media_token']}") as response:
            self.assertEqual(response.status_code, 200)
            with Image.open(io.BytesIO(response.data)) as image:
                self.assertEqual(image.size, (1440, 1080))

        saved = self.client.post(f"/api/tasks/{task['id']}/save", json={})
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(len(saved.get_json()["outputs"]), 2)
        with Image.open(self.output / "01_真实链路测试-4x3.jpg") as home:
            self.assertEqual(home.size, (1440, 1080))
        with Image.open(self.output / "01_真实链路测试-16x9.jpg") as wide:
            self.assertEqual(wide.size, (1920, 1080))


if __name__ == "__main__":
    unittest.main()
