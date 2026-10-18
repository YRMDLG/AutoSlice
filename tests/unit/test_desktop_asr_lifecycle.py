"""用无害 Python 子进程验证识别进程树与缓存互斥，不加载模型、不处理真实媒体。"""

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from autoslice.desktop.asr import audio
from autoslice.desktop.asr.process_tree import ProcessTree
from autoslice.desktop.asr.types import AsrError
from autoslice.desktop.asr.work_lock import WorkLock


class LifecycleTests(unittest.TestCase):
    def launch(self, code, *args):
        return ProcessTree(sys.executable, ["-u", "-c", code, *args], {"PYTHONIOENCODING": "utf-8"})

    def test_cancel_stops_descendants_before_the_task_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "orphan.txt"
            child = f"import time; from pathlib import Path; time.sleep(0.6); Path({str(marker)!r}).touch()"
            code = ("import subprocess,sys,time; subprocess.Popen([sys.executable,'-c'," + repr(child)
                    + "],creationflags=" + str(getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    + "); print('ready',flush=True); time.sleep(30)")
            tree = self.launch(code)
            try:
                self.assertEqual(tree.process.stdout.readline().strip(), b"ready")
                tree.cancel()
                tree.wait()
                time.sleep(0.8)
                self.assertFalse(marker.exists())
            finally:
                tree.cancel()
                tree.wait()
                tree.process.stdout.close()

    def test_normal_exit_also_reaps_leftover_descendants(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "orphan.txt"
            child = f"import time; from pathlib import Path; time.sleep(0.6); Path({str(marker)!r}).touch()"
            code = ("import subprocess,sys; subprocess.Popen([sys.executable,'-c'," + repr(child)
                    + "],creationflags=" + str(getattr(subprocess, "CREATE_NO_WINDOW", 0)) + ")")
            tree = self.launch(code)
            self.assertEqual(tree.wait(), 0)
            tree.process.stdout.close()
            time.sleep(0.8)
            self.assertFalse(marker.exists())

    def test_job_assignment_failure_does_not_execute_the_worker(self):
        if os.name != "nt":
            self.skipTest("Windows Job 专用")
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "started.txt"
            code = f"from pathlib import Path; Path({str(marker)!r}).touch()"
            with patch("autoslice.desktop.asr.process_tree.WindowsJob.assign", side_effect=OSError("挂入失败")):
                with self.assertRaises(OSError):
                    self.launch(code)
            self.assertFalse(marker.exists())

    def test_killing_the_ui_owner_closes_its_job_and_stops_the_worker(self):
        if os.name != "nt":
            self.skipTest("Windows Job 句柄生命周期专用")
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "orphan.txt"
            child = ("import time; from pathlib import Path; print('ready',flush=True); time.sleep(0.7); "
                     f"Path({str(marker)!r}).touch()")
            owner = ("import sys,time; from autoslice.desktop.asr.process_tree import ProcessTree; "
                     f"tree=ProcessTree(sys.executable,['-u','-c',{child!r}],{{}}); "
                     "print(tree.process.stdout.readline().decode().strip(),flush=True); time.sleep(30)")
            process = subprocess.Popen([sys.executable, "-u", "-c", owner], stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                self.assertEqual(process.stdout.readline().strip(), b"ready")
                process.kill()
                process.wait(timeout=5)
                time.sleep(1)
                self.assertFalse(marker.exists())
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                process.stdout.close()
                process.stderr.close()

    def test_lock_is_exclusive_and_is_released_when_owner_is_killed(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory) / "cache"
            code = ("import sys,time; from pathlib import Path; from autoslice.desktop.asr.work_lock import WorkLock; "
                    "lock=WorkLock(Path(sys.argv[1])); lock.__enter__(); print('locked',flush=True); time.sleep(30)")
            tree = self.launch(code, str(work))
            try:
                self.assertEqual(tree.process.stdout.readline().strip(), b"locked")
                with self.assertRaisesRegex(AsrError, "正在识别"):
                    with WorkLock(work):
                        self.fail("另一个进程持锁时不应进入")
            finally:
                tree.cancel()
                tree.wait()
                tree.process.stdout.close()
            with WorkLock(work):
                self.assertTrue(work.parent.joinpath("locks", "cache.lock").is_file())

    def test_audio_publishes_atomically_and_preserves_old_wav_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "audio.wav"
            target.write_bytes(b"previous")

            def fail(command):
                self.assertEqual(command[command.index("-f") + 1], "wav")
                Path(command[-1]).write_bytes(b"incomplete")
                raise AsrError("模拟中断")

            # 截获公开 subprocess 边界，避免调用真实 FFmpeg。
            def runner(command, **kwargs):
                fail(command)

            with patch.object(audio.subprocess, "run", side_effect=runner):
                with self.assertRaises(AsrError):
                    audio.extract_wav("input.mp4", target)
            self.assertEqual(target.read_bytes(), b"previous")
            self.assertEqual(list(target.parent.glob("*.part.wav")), [])
