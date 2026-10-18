"""字幕识别：没有字幕的视频在字幕页顶部给“识别字幕”，识别在子进程里跑，成功后自动载入。

混入 DesktopWindow，排在 PreviewWindow 之前；状态都在窗口上，不单独实例化。
"""

from __future__ import annotations

import dataclasses

from autoslice.desktop.asr import load_settings
from autoslice.desktop.asr.engines import ENGINES
from autoslice.desktop.asr.hotwords import build_hotwords
from autoslice.desktop.asr.jobs import JobSpec

from .asr_panel import AsrBanner
from .asr_process import RecognizeJob


class RecognizeMixin:
    def _build_asr_banner(self):
        self.asr_banner = AsrBanner()
        self._asr_job = RecognizeJob(self)
        self._asr_video = None
        self.asr_banner.start_clicked.connect(self._asr_start)
        self.asr_banner.cancel_clicked.connect(self._asr_job.cancel)
        self.asr_banner.settings_clicked.connect(lambda: self._select_page(2))
        self._asr_job.progress.connect(self._asr_progress)
        self._asr_job.finished.connect(self._asr_finished)
        return self.asr_banner

    def _asr_running(self) -> bool:
        return getattr(self, "_asr_job", None) is not None and self._asr_job.running

    def _asr_hide(self, video=None) -> None:
        """换了视频：提示条只在正在识别的那个视频上继续显示。"""

        if not hasattr(self, "asr_banner"):
            return
        if self._asr_running() and video is not None and video.path == self._asr_video:
            return
        self.asr_banner.hide()

    def _asr_offer(self, video) -> None:
        """这个视频没有字幕：给出识别入口。"""

        if not hasattr(self, "asr_banner"):
            return
        if self._asr_running():
            if video.path == self._asr_video:
                return
            self.asr_banner.show_idle("正在识别另一个视频，等它完成后再识别这个。")
            self.asr_banner.start_button.setEnabled(False)
            return
        self.asr_banner.start_button.setEnabled(True)
        spec = ENGINES.get(load_settings(self.storage).engine)
        self.asr_banner.show_idle(f"用 {spec.label.split('（')[0]} 识别，识别完会自动载入，随后可以校对。" if spec else "")

    def _asr_start(self) -> None:
        video = self.video_choice.currentData() if self.project is not None else None
        if video is None or video.has_source_srt or self._asr_running():
            return
        settings = load_settings(self.storage)
        hotwords = build_hotwords(video.path, storage=self.storage, title=self.project.title) if settings.hotwords else []
        spec = JobSpec(video=video.path, output=video.srt_path, storage_root=str(self.storage.root),
                       settings=dataclasses.asdict(settings), hotwords=hotwords)
        self._asr_video = video.path
        self.asr_banner.start_button.setEnabled(True)
        self.asr_banner.show_running("准备中…", 0.0)
        self._asr_job.start(spec, self.storage.root / "cache" / "asr" / "job.json", settings.python)

    def _asr_progress(self, message: str, fraction: float) -> None:
        current = self.video_choice.currentData() if self.project is not None else None
        if current is not None and current.path == self._asr_video:
            self.asr_banner.show_running(message, fraction)
        self.app_status.setText(f"识别字幕 {fraction * 100:.0f}% · {message}")

    def _asr_finished(self, state: str, payload: dict) -> None:
        video_path, self._asr_video = self._asr_video, None
        current = self.video_choice.currentData() if self.project is not None else None
        here = current is not None and current.path == video_path
        self.app_status.setText("")
        if state == "error" and payload.get("code") == "exists":
            self.asr_banner.hide()
            self._show_transient_status(str(payload.get("message", "已有字幕，未覆盖")), 9000)
            if self.document is None or not self.document.dirty:
                self.refresh()
            return
        if state == "done":
            self.asr_banner.hide()
            note = f"字幕识别完成：{payload.get('cue_count', 0)} 条，用时 {payload.get('seconds', 0):.0f} 秒"
            warnings = payload.get("warnings") or []
            self._show_transient_status(note + (f"。{warnings[0]}" if warnings else ""), 9000)
            if self.document is None or not self.document.dirty:
                self.refresh()
            return
        if not here:
            if state == "error":
                self._show_transient_status(f"字幕识别没成功：{payload.get('message', '')}", 9000)
            return
        if state == "cancelled":
            self.asr_banner.show_cancelled()
        else:
            self.asr_banner.show_error(str(payload.get("message", "")))

    def _asr_shutdown(self) -> None:
        if self._asr_running():
            self._asr_job.cancel()
