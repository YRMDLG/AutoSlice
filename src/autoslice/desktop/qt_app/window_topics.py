"""话题分析入口：字幕页“分析话题”，用已有字幕在后台跑旧流水线；进度和停止走任务中心，完成后可打开报告。

混入 DesktopWindow，排在 PreviewWindow 之前；同一时间只分析一场。
"""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QObject, QSize, Signal
from PySide6.QtWidgets import QMenu, QMessageBox, QPushButton

from autoslice.desktop.qt_preview import popups
from autoslice.desktop.qt_preview.icons import icon as desktop_icon
from autoslice.desktop.qt_preview.theme import COLORS, SIZES
from autoslice.desktop.topics import TopicAnalysisCancelled


class _TopicSignals(QObject):
    # (第几次分析, 提示, 百分比)；工作线程发出，回到界面线程处理
    progress = Signal(int, str, int)


class TopicAnalysisMixin:
    def _build_topic_button(self):
        self._topic_signals = _TopicSignals(self)
        self._topic_signals.progress.connect(self._topic_progress)
        self._topic_job = None
        self._topic_generation = 0
        button = QPushButton("分析中 100%")
        button.setObjectName("secondary")
        button.setIcon(desktop_icon("layers", COLORS.text))
        button.setIconSize(QSize(SIZES.icon_size, SIZES.icon_size))
        # 按最长文字定宽，进度变化时工具栏不跳
        button.setMinimumWidth(button.sizeHint().width())
        button.setText("分析话题")
        button.setEnabled(False)
        button.clicked.connect(self._topic_clicked)
        self.topic_button = button
        return button

    def _build_more_button(self):
        """顶栏“⋯ 更多”：识别字幕、分析话题 / 停止话题分析、识别设置。"""

        button = QPushButton()
        button.setObjectName("square")
        button.setFixedSize(34, 34)
        button.setToolTip("更多：识别字幕、分析话题")
        button.setAccessibleName("更多：识别字幕、分析话题")
        button.setIcon(desktop_icon("more", COLORS.text_secondary, SIZES.tool_icon_size))
        self.more_menu = QMenu(button)
        popups.MenuFx(self.more_menu, two_line=True)
        self.more_menu.aboutToShow.connect(self._fill_more_menu)
        button.clicked.connect(lambda: popups.MenuFx.popup_below(self.more_menu, button, align_right=True))
        self.more_button_header = button
        return button

    def _fill_more_menu(self):
        menu = self.more_menu
        menu.clear()
        video = self._topic_video()
        # 只给没有字幕的视频识别，不覆盖已有字幕（D-032）
        can_recognize = (video is not None and not getattr(video, "has_source_srt", True)
                         and not self._asr_running())
        recognize = menu.addAction("识别字幕", self._asr_start)
        recognize.setProperty("sub", "在本机识别，生成新的字幕草稿" if can_recognize else "已有字幕的视频不重新识别")
        recognize.setEnabled(can_recognize)
        recognize.setToolTip("在本机识别，生成字幕草稿" if can_recognize else
                             "已有字幕的视频不重新识别，免得覆盖校对结果；没有字幕的视频才可识别")
        job = self._topic_job
        here = job is not None and video is not None and video.path == job["video"]
        topic = menu.addAction("停止话题分析" if here else "分析话题", self.topic_button.click)
        topic.setProperty("sub", "已分析完的分块会保留" if here else "用现有字幕在后台分析，会调用 AI")
        topic.setEnabled(self.topic_button.isEnabled())
        topic.setToolTip(self.topic_button.toolTip())
        settings = menu.addAction("识别设置", self._open_asr_settings)
        settings.setProperty("sub", "识别模型、对齐模型、运行设备")
        menu.setToolTipsVisible(True)

    def _topic_video(self):
        return self.video_choice.currentData() if getattr(self, "project", None) is not None else None

    @staticmethod
    def _topic_subtitle(video) -> str:
        # 保存过的校对字幕优先：改过的字更准
        for path in (video.corrected_srt_path, video.srt_path):
            if path and Path(path).is_file():
                return path
        return ""

    def _topic_running(self) -> bool:
        return getattr(self, "_topic_job", None) is not None

    def _refresh_topic_button(self):
        button = getattr(self, "topic_button", None)
        if button is None:
            return
        video, job = self._topic_video(), self._topic_job
        if job is not None:
            here = video is not None and video.path == job["video"]
            button.setEnabled(True)
            # 分析进行中才在顶栏露出进度（点一下停止）；平时入口在“⋯ 更多”里
            button.setVisible(here)
            button.setText(f"分析中 {job['percent']}%" if here else "分析话题")
            button.setToolTip("正在后台分析话题，点一下可以停止" if here else
                              "正在分析另一个视频的话题，可在“任务”里查看或停止")
            return
        ready = video is not None and bool(self._topic_subtitle(video))
        button.setEnabled(ready)
        button.setVisible(False)
        button.setText("分析话题")
        button.setToolTip("用这个视频已有的字幕跑话题分析、生成报告（会调用 AI、消耗额度；不识别、不切片）"
                          if ready else "这个视频还没有字幕，先识别字幕再分析话题")

    def _topic_clicked(self):
        video, job = self._topic_video(), self._topic_job
        if job is not None:
            if video is not None and video.path == job["video"]:
                choice = QMessageBox.question(self, "停止话题分析",
                                              "已分析完的分块会留着，下次接着分析。现在停止吗？")
                if choice == QMessageBox.StandardButton.Yes:
                    self._topic_stop()
            else:
                self._show_transient_status("正在分析另一个视频的话题，可在“任务”里查看或停止。", 6000)
            return
        if video is None:
            return
        subtitle = self._topic_subtitle(video)
        request = self.topic_service.request_for(video.path, subtitle)
        problem = self.topic_service.problem(request)
        if problem:
            self._show_transient_status(problem, 6000)
            return
        existing = self.topic_service.existing_report(video.path)
        if existing:
            box = QMessageBox(self)
            box.setWindowTitle("分析话题")
            box.setText("这个视频已经有话题报告。重新分析会复用已分析完的分块，只补没分析的部分。")
            open_button = box.addButton("打开报告", QMessageBox.ButtonRole.AcceptRole)
            again = box.addButton("重新分析", QMessageBox.ButtonRole.ActionRole)
            box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is open_button:
                self._open_local_path(existing)
                return
            if box.clickedButton() is not again:
                return
        ai_problem = self.topic_service.ai_problem()
        if ai_problem:
            QMessageBox.information(self, "分析话题", ai_problem)
            return
        dirty = (self.document is not None and self.document.dirty
                 and self.document.video.path == video.path)
        lines = [
            f"将把字幕《{Path(subtitle).name}》分块发给 AI 做话题分析，会消耗额度；整场录播可能要十几分钟。",
            "用的是已保存的字幕" + ("（还没保存的改动不算）" if dirty else "") + "；不识别、不切片、不改字幕。",
            f"报告保存在：{self.topic_service.output_root}",
        ]
        notice = self.topic_service.model_notice()
        if notice:
            lines.append(notice)
        lines.append("开始吗？")
        if QMessageBox.question(self, "分析话题", "\n".join(lines)) != QMessageBox.StandardButton.Yes:
            return
        self._topic_start(video, request)

    def _topic_start(self, video, request):
        project = self.project
        cancel = threading.Event()
        self._topic_generation += 1
        generation = self._topic_generation
        self._topic_job = {"generation": generation, "video": video.path, "cancel": cancel, "percent": 0}
        self._task_update("topics", kind="话题分析", project=project, video_path=video.path,
                          message="准备分析…", cancel=cancel.set)
        self.app_status.setText(f"{project.title} · 话题分析中")
        self._refresh_topic_button()
        service, signals = self.topic_service, self._topic_signals

        def action():
            return service.run(request, cancel=cancel,
                               progress=lambda message, percent: signals.progress.emit(generation, message, percent))

        self._run(action, lambda result, error: self._topic_finished(generation, project, result, error))

    def _topic_stop(self):
        job = self._topic_job
        if job is not None:
            job["cancel"].set()
            self._task_update("topics", message="正在停止…（等手上这次 AI 请求回来）")
            self.app_status.setText("正在停止话题分析…")

    def _topic_progress(self, generation, message, percent):
        job = self._topic_job
        if job is None or job["generation"] != generation or job["cancel"].is_set():
            return
        job["percent"] = percent
        self._task_update("topics", message=f"{percent}% · {message}")
        self.app_status.setText(f"话题分析 {percent}% · {message}")
        self._refresh_topic_button()

    def _topic_finished(self, generation, project, result, error):
        job = self._topic_job
        if job is None or job["generation"] != generation:
            return
        self._topic_job = None
        self.app_status.setText("")
        if error is None:
            note = f"分析完成：{result.summary}" + (f"。{result.warning}" if result.warning else "")
            self._task_update("topics", state="done", message=note,
                              result=result.report_path or result.artifact_dir, result_label="打开报告")
            self._show_transient_status(f"✓ 话题分析完成：{result.summary}，可在“任务”里打开报告", 9000)
        elif isinstance(error, TopicAnalysisCancelled) or job["cancel"].is_set():
            self._task_update("topics", state="cancelled", message="已停止；分析完的分块留着，下次接着分析")
            self._show_transient_status("话题分析已停止", 6000)
        else:
            self._task_update("topics", state="error", message=f"分析失败：{error}")
            self._show_transient_status(f"{project.title} · 话题分析没成功：{error}", 9000)
        self._refresh_topic_button()

    def _topic_shutdown(self):
        # 关窗：只发停止信号；正在等的请求回来后线程自己退出，检查点不丢
        job = getattr(self, "_topic_job", None)
        if job is not None:
            job["cancel"].set()
