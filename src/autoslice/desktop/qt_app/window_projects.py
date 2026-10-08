"""项目与视频：项目栏、设置页、扫描投稿目录、切换项目/视频、载入字幕与波形。

混入 DesktopWindow，排在 PreviewWindow 之前；状态都在窗口上，不单独实例化。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import (
    Qt,
    QTimer,
)
from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from autoslice.desktop.projects import ProjectSnapshot
from autoslice.desktop.qt_preview.window import ProjectItem, label, line
from autoslice.desktop.subtitles import SubtitleDocument
from autoslice.subtitle_workflow import DEFAULT_SUBTITLE_STYLE

from .learning_panel import LearningPanel


class ProjectsMixin:
    def _project_rail(self):
        rail = QWidget()
        rail.setObjectName("projectRail")
        column = QVBoxLayout(rail)
        column.setContentsMargins(12, 20, 12, 12)
        column.setSpacing(8)
        heading = QHBoxLayout()
        heading.addWidget(label("投稿项目", "sectionTitle"))
        heading.addStretch()
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self.refresh)
        heading.addWidget(refresh)
        column.addLayout(heading)
        column.addWidget(line())
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        container = QWidget()
        self.projects_layout = QVBoxLayout(container)
        self.projects_layout.setContentsMargins(0, 0, 0, 0)
        self.projects_layout.setSpacing(4)
        self.projects_layout.addStretch()
        scroll.setWidget(container)
        column.addWidget(scroll, 1)
        self.project_group = QButtonGroup(self)
        self.project_group.setExclusive(True)
        self.project_buttons = {}
        self.scan_status = label(str(self.service.root), "subtle")
        self.scan_status.setWordWrap(True)
        column.addWidget(self.scan_status)
        return rail

    def _settings_page(self):
        page = QWidget()
        page.setObjectName("settingsSurface")
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(32, 26, 32, 26)
        layout.setSpacing(16)
        layout.addWidget(label("设置", "pageTitle"))
        layout.addWidget(line())

        # ── 投稿目录 ──
        layout.addWidget(label("投稿目录", "sectionTitle"))
        dir_row = QHBoxLayout()
        dir_row.setSpacing(8)
        directory = label(str(self.service.root), "muted")
        directory.setWordWrap(True)
        dir_row.addWidget(directory, 1)
        browse_dir = QPushButton("更改")
        browse_dir.setObjectName("quiet")
        browse_dir.setFixedHeight(28)
        browse_dir.setToolTip("选择投稿项目根目录")
        browse_dir.clicked.connect(self._browse_submission_root)
        dir_row.addWidget(browse_dir)
        layout.addLayout(dir_row)

        layout.addSpacing(8)
        layout.addWidget(line())
        layout.addSpacing(8)

        # ── 字幕压制 ──
        layout.addWidget(label("字幕压制", "sectionTitle"))
        layout.addWidget(label(
            f"默认样式：{DEFAULT_SUBTITLE_STYLE['font_name']} · "
            f"#{DEFAULT_SUBTITLE_STYLE['outline_color']} 描边", "muted"
        ))
        layout.addWidget(label("压制使用既有 FFmpeg 工作流，NVENC 可用时自动优先。", "muted"))

        layout.addSpacing(8)
        layout.addWidget(line())
        layout.addSpacing(8)

        # ── 封面 ──
        layout.addWidget(label("封面", "sectionTitle"))
        layout.addWidget(label("封面字体、默认样式和素材库在封面编辑器中直接管理。", "muted"))
        layout.addWidget(label("桌面草稿保存在用户应用数据目录，重启后可恢复。", "muted"))

        layout.addSpacing(8)
        layout.addWidget(line())
        layout.addSpacing(8)

        # ── 成长记录 ──
        self.learning_panel = LearningPanel(self.correction_memory)
        layout.addWidget(self.learning_panel)

        layout.addSpacing(8)
        layout.addWidget(line())
        layout.addSpacing(8)

        # ── AI ──
        layout.addWidget(label("AI 检查", "sectionTitle"))
        ai_help = label("沿用 AutoSlice 的 api_config.json 或环境变量配置。"
                        "AI 只在手动点击时运行，不会在页面加载或切换时自动调用。", "muted")
        ai_help.setWordWrap(True)
        layout.addWidget(ai_help)

        layout.addSpacing(8)
        layout.addWidget(line())
        layout.addSpacing(8)

        # ── 播放器 ──
        layout.addWidget(label("播放器", "sectionTitle"))
        player_status = "libmpv 已连接" if getattr(self, "player", None) is not None else "libmpv 未连接（仅预览模式）"
        layout.addWidget(label(player_status, "muted"))
        hwdec = getattr(self, "_hwdec", None)
        if hwdec:
            layout.addWidget(label(f"硬件解码：{hwdec}", "muted"))

        layout.addStretch()
        scroll.setWidget(body)
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.addWidget(scroll)
        return page

    def _browse_submission_root(self):
        """让用户选择投稿目录。"""
        from PySide6.QtWidgets import QFileDialog
        path = QFileDialog.getExistingDirectory(self, "选择投稿目录", str(self.service.root))
        if path:
            self.service.root = Path(path)
            self._show_transient_status(f"投稿目录已更新：{path}")
            self.refresh()

    def refresh(self):
        if self._loading or self._saving or not self._resolve_unsaved():
            return
        self._scan_generation += 1
        generation = self._scan_generation
        self.scan_status.setText("正在扫描投稿目录…")
        self._run(self.service.refresh, lambda result, error: self._scanned(generation, result, error))

    def _scanned(self, generation, snapshot, error):
        if generation != self._scan_generation:
            return
        if error:
            self.scan_status.setText(f"扫描失败：{error}")
            return
        assert isinstance(snapshot, ProjectSnapshot)
        self.scan_status.setText(f"{snapshot.status} · {len(snapshot.projects)} 个项目")
        self.cover_editor.set_project_list(snapshot.projects)
        while self.projects_layout.count() > 1:
            item = self.projects_layout.takeAt(0)
            item.widget().deleteLater()
        self.project_buttons.clear()
        for project in snapshot.projects:
            status = project.status
            if status == "素材已识别":
                status = "字幕已保存" if any(video.has_corrected_srt for video in project.videos) else ""
            button = ProjectItem(project.title, status, False)
            self.project_group.addButton(button)
            button.clicked.connect(lambda _checked=False, p=project: self._select_real_project(p))
            self.projects_layout.insertWidget(self.projects_layout.count() - 1, button)
            self.project_buttons[project.id] = button
        project_id = self.project.id if self.project else self._session.get("project_id")
        selected = next((p for p in snapshot.projects if p.id == project_id), None)
        if selected:
            self.project = None
            self.document = None
            self.model.set_document(None)
            self.timeline.set_document(None)
            self._select_real_project(selected)
        elif self.project:
            self.project = None
            self.document = None
            self.model.set_document(None)
            self.timeline.set_document(None)
            self.top_project.setText("请选择项目")
            self.video_name.setText("选择视频")
            self.cover_editor.set_context(None, None)

    def _select_real_project(self, project):
        if self._loading or self._saving:
            self._restore_project_button()
            return
        if self.project and project.id == self.project.id and self.document is not None:
            self.project_buttons[project.id].setChecked(True)
            return
        if not self._resolve_unsaved():
            self._restore_project_button()
            return
        self.project = project
        self.ai_session = None
        self._ai_selected = None
        self._pending_filter = False
        self._render_ai()
        self.selected_index = None
        self.document = None
        self.model.set_document(None)
        self.timeline.set_document(None)
        self.top_project.setText(project.title if len(project.title) <= 35 else project.title[:35] + "…")
        self.top_project.setToolTip(project.title)
        self.project_buttons[project.id].setChecked(True)
        self.cover_editor.set_context(project, None)
        self.video_choice.blockSignals(True)
        self.video_choice.clear()
        for video in project.videos:
            self.video_choice.addItem(video.name, video)
        preferred = self._session.get("video_path")
        index = next((i for i, video in enumerate(project.videos) if video.path == preferred), 0)
        self.video_choice.setCurrentIndex(index if project.videos else -1)
        self.video_choice.blockSignals(False)
        single_video = len(project.videos) == 1
        self.video_choice.setVisible(len(project.videos) > 1)
        self.video_name.setVisible(single_video or not project.videos)
        self.video_name.setText(project.videos[index].name if project.videos else "该项目没有视频")
        self.video_name.setToolTip(project.videos[index].name if project.videos else "")
        if project.videos:
            self._load_video(project.videos[index])
        else:
            self._media_ready = False
            self.play_button.setEnabled(False)
            if self.player:
                self.player.pause()
            self.video_placeholder.setText("该项目没有视频")
            self.video_stack.setCurrentIndex(0)
            self.subtitle_status.setText(project.error or "该项目没有视频")
        self._save_session()

    def _next_cover_video(self):
        """封面页“下一个”：同项目的下一个视频，否则列表里的下一个项目；页面停在封面。"""

        if self.project is None:
            return
        current = self.cover_editor.video
        videos = list(self.project.videos)
        index = next((i for i, video in enumerate(videos) if current is not None and video.path == current.path), -1)
        if 0 <= index < len(videos) - 1:
            self.video_choice.setCurrentIndex(index + 1)
            return
        projects = [project for project in self.service.snapshot.projects if project.videos]
        position = next((i for i, project in enumerate(projects) if project.id == self.project.id), -1)
        if position + 1 >= len(projects):
            self._show_transient_status("已经是投稿列表里的最后一个视频")
            return
        self._select_real_project(projects[position + 1])
        if self.project is projects[position + 1]:
            self._show_transient_status(f"已切到下一个：{self.project.title}")

    def _restore_project_button(self):
        if self.project and self.project.id in self.project_buttons:
            self.project_buttons[self.project.id].setChecked(True)

    def _video_changed(self, index):
        if index < 0 or self.project is None:
            return
        if self._loading or self._saving:
            self.video_choice.blockSignals(True)
            old_path = self.document.video.path if self.document else self._session.get("video_path")
            old = next((i for i, v in enumerate(self.project.videos) if v.path == old_path), 0)
            self.video_choice.setCurrentIndex(old)
            self.video_choice.blockSignals(False)
            return
        video = self.video_choice.itemData(index)
        if self.document and self.document.video.path == video.path:
            return
        if not self._resolve_unsaved():
            if self.document:
                old = next((i for i, v in enumerate(self.project.videos)
                            if v.path == self.document.video.path), 0)
                self.video_choice.blockSignals(True)
                self.video_choice.setCurrentIndex(old)
                self.video_choice.blockSignals(False)
            return
        self._load_video(video)

    def _load_video(self, video):
        if self.project is not None:
            self.cover_editor.set_context(self.project, video)
        self.ai_session = None
        self._ai_selected = None
        self._pending_filter = False
        self._render_ai()
        self._media_ready = False
        self._preview_attached = False
        self._pending_seek = None
        self.play_button.setEnabled(False)
        self._player_paused = True
        self._set_play_button(True)
        if self.player:
            self._player_load_token += 1
            token = self._player_load_token
            self.video_stack.setCurrentIndex(1)
            self._player_duration = 0.0
            self._player_position = 0.0
            self._resume_pending = self._session.get("video_path") == video.path
            self.time_label.setText("正在加载视频…")
            self.player.load(video.path)
            QTimer.singleShot(20000, lambda: self._check_player_load(token))
        else:
            self.video_stack.setCurrentIndex(0)
        self._loading = True
        self.document = None
        self.model.set_document(None)
        self.timeline.set_document(None)
        self.subtitle_status.setText("正在加载字幕…")
        project = self.project
        def action():
            document = SubtitleDocument.load(video)
            baseline = self.storage.capture_baseline(
                video.srt_path, dependencies=[video.corrected_srt_path]
            )
            draft = self.storage.read_draft(
                "subtitle", project.directory, video.srt_path,
                dependencies=[video.corrected_srt_path],
            )
            return document, baseline, draft
        self._run(action, lambda result, error: self._loaded(project, video, result, error))

    def _loaded(self, project, video, result, error):
        self._loading = False
        if self.project is not project or self.video_choice.currentData() != video:
            return
        if error:
            self.subtitle_status.setText(f"字幕无法加载：{error}")
            return
        document, self._baseline, draft = result
        if draft.status == "ready":
            choice = QMessageBox.question(
                self, "恢复字幕草稿", "发现未正式保存的字幕草稿。是否恢复？\n正式 SRT 不会因此写入。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if choice == QMessageBox.StandardButton.Yes:
                try:
                    self.selected_index = document.restore_draft(draft.payload)
                except ValueError as exc:
                    QMessageBox.warning(self, "草稿无法恢复", str(exc))
            else:
                self._remove_draft(document)
        elif draft.status not in ("missing",):
            QMessageBox.warning(self, "草稿需要检查",
                                f"字幕草稿状态：{draft.status}。已保留草稿，没有自动应用。")
        self.document = document
        self.model.set_document(document)
        self.timeline.set_document(document)
        self.timeline.set_waveform(None, self._player_duration or None)
        self._start_waveform(video.path)
        self.ai_session, ai_status = self.ai_service.load(document, project.title)
        if ai_status == "missing":
            try:
                self.ai_service.config_loader()
            except (OSError, ValueError):
                ai_status = "unconfigured"
        self.ai_note.setText("旧建议已过期，请主动重新检查。" if ai_status == "stale" else
                             "未配置 AI，请在设置页配置后主动检查。" if ai_status == "unconfigured" else
                             "已恢复待确认建议。" if ai_status == "ready" else
                             "AI 只生成待确认建议，不修改字幕。")
        self._render_ai()
        if getattr(self, "_ai_return_project", None) == project.id and self.ai_session and self.ai_session.pending:
            self._set_ai_open(True)
            self._select_ai(self.ai_session.pending[0].suggestion_id)
        if self.selected_index is None and self._session.get("video_path") == video.path:
            self.selected_index = self._session.get("cue_index")
        self._select_cue(self.selected_index)
        self._update_status()
        if self._media_ready and self.preview_toggle.isChecked():
            self._refresh_subtitle_preview()
        self._save_session()

    def _start_waveform(self, video_path):
        self._waveform_generation += 1
        generation = self._waveform_generation
        cached = self.waveform_cache.load(video_path)
        if cached is not None:
            self.timeline.set_waveform(cached.samples, cached.duration)
            return
        self.timeline.set_waveform(None, self._player_duration or None)
        self._show_transient_status("正在生成波形…")
        self._run(
            lambda: self.waveform_cache.load_or_generate(video_path),
            lambda result, error: self._waveform_ready(generation, result, error),
        )

    def _waveform_ready(self, generation, result, error):
        if generation != self._waveform_generation or self.document is None:
            return
        if error:
            self._show_transient_status(f"波形生成失败：{error}")
            return
        self.timeline.set_waveform(result.samples, result.duration)
        self._show_transient_status("波形已就绪")
