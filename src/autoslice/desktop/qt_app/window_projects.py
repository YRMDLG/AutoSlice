"""项目与视频：项目栏、设置页、扫描投稿目录、切换项目/视频、载入字幕与波形。

混入 DesktopWindow，排在 PreviewWindow 之前；状态都在窗口上，不单独实例化。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import (
    QPoint,
    Qt,
    QTimer,
)
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from autoslice.desktop.archive import ArchiveService
from autoslice.desktop.projects import PROJECT_SORT_OPTIONS, ProjectSnapshot, nested_root_candidate
from autoslice.desktop.qt_preview import motion, popups
from autoslice.desktop.qt_preview.icons import icon as desktop_icon
from autoslice.desktop.qt_preview.shell import Card
from autoslice.desktop.qt_preview.theme import COLORS, SIZES
from autoslice.desktop.qt_preview.window import ProjectOption, card, label, tool_icon
from autoslice.desktop.selection import CueSelection
from autoslice.desktop.subtitle_render import subtitle_font_problem
from autoslice.desktop.subtitles import SubtitleDocument

from . import settings_sections
from .ai_settings_panel import AISettingsPanel
from .appearance_panel import AppearancePanel
from .archive_panel import ArchivePanel
from .asr_panel import AsrSettingsPanel
from .learning_panel import LearningPanel
from .window_widgets import NoteLabel

# 草稿读出来不能直接用时给用户看的原因（不露内部状态码）
_DRAFT_SKIPPED = {
    "source_changed": "这个视频有一份没保存的字幕草稿，但字幕文件在那之后被改过（比如在别的软件里改了）。"
                      "为了不盖掉新的字幕，没有套用草稿。",
    "source_missing": "这个视频有一份没保存的字幕草稿，但读不到对应的字幕文件，没有套用草稿。",
    "incompatible": "这个视频有一份旧版本程序存的字幕草稿，格式对不上，没有套用。",
    "invalid": "这个视频的字幕草稿文件不完整或已损坏，没有套用。",
}


class ProjectsMixin:
    def _project_rail(self):
        """项目切换下拉的内容（设计稿）：项目逐行（序号、标题、状态，当前项打勾），
        分隔线下是“刷新项目列表”，右边是排序。空目录、选错层级时上面给出说明和入口。"""

        rail = QWidget()
        rail.setObjectName("projectRail")
        outer = QVBoxLayout(rail)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        column = QVBoxLayout()
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)
        outer.addLayout(column, 1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.project_scroll = scroll
        container = QWidget()
        self.projects_layout = QVBoxLayout(container)
        self.projects_layout.setContentsMargins(0, 0, 0, 0)
        self.projects_layout.setSpacing(1)
        self.projects_layout.addStretch()
        scroll.setWidget(container)
        column.addWidget(scroll, 1)
        # 第一次用或目录选错时列表是空的：直接给出选目录的入口，不用自己去设置页找
        self.empty_projects = QWidget()
        empty = QVBoxLayout(self.empty_projects)
        empty.setContentsMargins(12, 4, 12, 4)
        empty.setSpacing(8)
        self.empty_projects_hint = label("", "muted")
        self.empty_projects_hint.setWordWrap(True)
        empty.addWidget(self.empty_projects_hint)
        # 选高了一层时一键改用那个文件夹（M9）
        self.nested_root_button = QPushButton("")
        tool_icon(self.nested_root_button, "folder")
        self.nested_root_button.clicked.connect(self._use_nested_root)
        empty.addWidget(self.nested_root_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.nested_root_button.hide()
        choose = QPushButton("选择投稿目录")
        tool_icon(choose, "folder")
        choose.clicked.connect(self._browse_submission_root)
        empty.addWidget(choose, 0, Qt.AlignmentFlag.AlignLeft)
        self.empty_projects.hide()
        column.insertWidget(0, self.empty_projects)
        self.project_group = QButtonGroup(self)
        self.project_group.setExclusive(True)
        self.project_buttons = {}
        # 扫描中、扫描失败时才露出的一行状态；平时状态放在“刷新”的悬停说明里
        self.scan_status = label(str(self.service.root), "subtle")
        self.scan_status.setWordWrap(True)
        self.scan_status.setContentsMargins(10, 0, 10, 0)
        self.scan_status.hide()
        column.addWidget(self.scan_status)
        divider = QFrame()
        divider.setObjectName("hairline")
        divider.setFixedHeight(1)
        holder = QHBoxLayout()
        holder.setContentsMargins(10, 4, 10, 4)
        holder.addWidget(divider)
        outer.addLayout(holder)
        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.setSpacing(6)
        self.project_refresh = QPushButton("刷新项目列表")
        self.project_refresh.setObjectName("menuRow")
        self.project_refresh.setFixedHeight(32)
        tool_icon(self.project_refresh, "refresh")
        self.project_refresh.clicked.connect(self.refresh)
        footer.addWidget(self.project_refresh)
        footer.addStretch(1)
        self.project_sort = popups.ComboBox()
        self.project_sort.setObjectName("quietCombo")
        self.project_sort.setToolTip("项目排序")
        for key, title in PROJECT_SORT_OPTIONS:
            self.project_sort.addItem(title, key)
        self.project_sort.setCurrentIndex(self.project_sort.findData(self.service.sort_mode))
        self.project_sort.currentIndexChanged.connect(self._project_sort_changed)
        footer.addWidget(self.project_sort)
        outer.addLayout(footer)
        self.scan_status_changed()
        return rail

    def scan_status_changed(self) -> None:
        """扫描状态：正常时收进“刷新项目列表”的悬停说明，扫描中 / 失败时在列表下露出来。"""

        text = self.scan_status.text()
        if hasattr(self, "project_refresh"):
            self.project_refresh.setToolTip(text)
        if hasattr(self, "settings_scan_summary"):
            self.settings_scan_summary.setText(text.replace("已刷新 · ", "扫描到 "))
        self.scan_status.setVisible(text.startswith(("正在", "扫描失败")))

    def _settings_page(self):
        """设置页：一张大卡，左侧“本页目录”，右侧限宽的分区；点目录滚到对应分区，滚动时目录跟着亮。"""

        page = QWidget()
        page.setObjectName("settingsSurface")
        frame = Card(spotlight=False)
        frame_row = QHBoxLayout(frame)
        frame_row.setContentsMargins(0, 0, 0, 0)
        frame_row.setSpacing(0)
        toc = QWidget()
        toc.setFixedWidth(216)
        toc_column = QVBoxLayout(toc)
        toc_column.setContentsMargins(14, 30, 14, 20)
        toc_column.setSpacing(2)
        heading = label("设置", "settingsHeading")
        heading.setContentsMargins(12, 0, 0, 16)
        toc_column.addWidget(heading)
        frame_row.addWidget(toc)
        separator = QFrame()
        separator.setObjectName("vline")
        separator.setFixedWidth(1)
        separator_holder = QVBoxLayout()
        separator_holder.setContentsMargins(0, 24, 0, 24)
        separator_holder.addWidget(separator)
        frame_row.addLayout(separator_holder)
        scroll = QScrollArea()
        self.settings_scroll = scroll
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        outer = QVBoxLayout(body)
        outer.setContentsMargins(40, 28, 34, 40)
        # 限宽、靠左的内容列：大窗口里输入框也不会拉成一条线（靠 maximumWidth，不用对齐标志）
        content = QWidget()
        content.setMaximumWidth(700)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(32)
        # 放进横向一行再加进页面：滚动区按“宽度→高度”给内容定高，竖排里直接放限宽控件时，
        # Qt 按整页宽度（不是限宽）算折行，高度算少了，所有行被挤到最小高度以下（下拉框压扁、按钮被裁）。
        # 横排会先按限宽分好宽度再算高度。
        column = QHBoxLayout()
        column.setContentsMargins(0, 0, 0, 0)
        column.addWidget(content, 1)
        column.addStretch(0)
        outer.addLayout(column)
        outer.addStretch(1)

        # ── 投稿目录 ──
        directory_card, directory_column = card("投稿目录", "目录下每个子文件夹算一个项目，里面放视频（和字幕）。")
        dir_row = QHBoxLayout()
        dir_row.setSpacing(12)
        directory = NoteLabel(str(self.service.root))
        directory.setObjectName("monoPath")
        directory.setWordWrap(False)
        self.settings_directory = directory
        dir_row.addWidget(directory, 1)
        browse_dir = QPushButton("更改")
        browse_dir.setIcon(desktop_icon("folder", COLORS.muted, SIZES.tool_icon_size))
        browse_dir.setToolTip("选择投稿项目根目录")
        browse_dir.clicked.connect(self._browse_submission_root)
        dir_row.addWidget(browse_dir)
        directory_column.addLayout(dir_row)
        self.settings_scan_summary = label(self.scan_status.text(), "muted")
        directory_column.addWidget(self.settings_scan_summary)

        # ── 外观（主题、强调色；即时生效）──
        self.appearance_panel = AppearancePanel(self.storage)

        # ── AI 接口 ──
        self.ai_settings_panel = AISettingsPanel(self._run)

        # ── 学习旧切片（扫描本机旧投稿文件夹；学到的东西要确认才生效） ──
        self.archive_panel = ArchivePanel(ArchiveService(self.storage), self._run, self.forget_habit_cache)

        # ── 成长记录（字幕纠错记忆 + 封面作品库两块） ──
        self.learning_panel = LearningPanel(self.correction_memory, self.cover_editor.service.works)

        # ── 字幕识别 ──
        self.asr_settings_panel = AsrSettingsPanel(self.storage)

        # ── 字幕压制 ──
        render_card, render_column = card(
            "压制",
            "字幕样式可以存几套随时切换；“默认”就是原来的压制配置。在字幕页播放器下方点“字幕样式”边看预览边调。",
        )
        style_row = QHBoxLayout()
        # 文字由 _sync_style_labels 按当前样式填
        self.render_style_note = label("", "muted")
        self.render_style_note.setWordWrap(True)
        style_row.addWidget(self.render_style_note, 1)
        adjust_style = QPushButton("调整字幕样式")
        adjust_style.clicked.connect(self._open_style_from_settings)
        style_row.addWidget(adjust_style, 0, Qt.AlignmentFlag.AlignTop)
        render_column.addLayout(style_row)
        render_column.addWidget(label("压制使用既有 FFmpeg 工作流，NVENC 可用时自动优先。", "muted"))
        # 缺字体或 FFmpeg 时压制必失败：进设置页就检查并醒目提示
        self.render_font_note = label("", "danger")
        self.render_font_note.setWordWrap(True)
        self.render_font_note.hide()
        render_column.addWidget(self.render_font_note)

        # ── 封面素材 ──
        cover_card, cover_column = card("封面素材", "素材只扫描所选文件夹及其子文件夹，手动导入的素材也会保留。")
        cover_column.addWidget(label("素材文件夹", "muted"))
        material_row = QHBoxLayout()
        self.settings_material_directory = NoteLabel(str(self.cover_editor.service.asset_library.legacy_root))
        self.settings_material_directory.setObjectName("monoPath")
        self.settings_material_directory.setWordWrap(False)
        material_row.addWidget(self.settings_material_directory, 1)
        browse_materials = QPushButton("选择文件夹")
        browse_materials.clicked.connect(self._browse_material_directory)
        material_row.addWidget(browse_materials)
        cover_column.addLayout(material_row)
        cover_column.addWidget(label("桌面草稿保存在用户应用数据目录，重启后可恢复。", "muted"))

        # ── 播放器 ──
        player_card, player_column = card("播放器")
        player_status = "libmpv 已连接" if getattr(self, "player", None) is not None else (
            "没找到播放组件 libmpv-2.dll：视频放不了，字幕和封面照常能改。"
            "放进 %LOCALAPPDATA%\\AutoSlice\\bin 文件夹后重启即可。")
        player_note = label(player_status, "muted")
        player_note.setWordWrap(True)
        player_column.addWidget(player_note)
        hwdec = getattr(self, "_hwdec", None)
        if hwdec:
            player_column.addWidget(label(f"硬件解码：{hwdec}", "muted"))

        # ── 快捷键 ──
        shortcut_card, shortcut_column = card("快捷键", "字幕页：空格从选中句开头播放，Ctrl+S 保存，Ctrl+F 查找替换，F1~F4 试听。")
        shortcut_button = QPushButton("查看全部快捷键（Ctrl+/）")
        shortcut_button.clicked.connect(self._show_shortcuts)
        shortcut_column.addWidget(shortcut_button, 0, Qt.AlignmentFlag.AlignLeft)

        learning_cards = self.learning_panel.findChildren(QWidget, "settingsCard")
        sections = [
            ("投稿目录", directory_card, directory_card),
            ("外观", self.appearance_panel, self.appearance_panel),
            ("AI 接口", self.ai_settings_panel, self.ai_settings_panel),
            ("学习旧切片", self.archive_panel, self.archive_panel),
            ("字幕纠错记忆", self.learning_panel, learning_cards[0] if learning_cards else self.learning_panel),
        ]
        if len(learning_cards) > 1:
            sections.append(("封面作品库", None, learning_cards[1]))
        sections += [
            ("字幕识别", self.asr_settings_panel, self.asr_settings_panel),
            ("压制", render_card, render_card),
            ("封面素材", cover_card, cover_card),
            ("播放器", player_card, player_card),
            ("快捷键", shortcut_card, shortcut_card),
        ]
        # 各块统一改成设计稿分区：标题、说明在框外，框里一行一项、行间细线
        for _title, widget, _target in sections:
            if widget is None:
                continue
            cards = [widget] if widget.objectName() == "settingsCard" else []
            cards += widget.findChildren(QWidget, "settingsCard")
            for settings_card in cards:
                settings_sections.restyle(settings_card)
        group = QButtonGroup(self)
        group.setExclusive(True)
        self.settings_toc: list[tuple[QPushButton, QWidget]] = []
        for title, widget, target in sections:
            if widget is not None:
                layout.addWidget(widget)
            item = QPushButton(title)
            item.setObjectName("tocItem")
            item.setCheckable(True)
            item.clicked.connect(lambda _checked=False, section=target: self._scroll_settings_to(section))
            group.addButton(item)
            toc_column.addWidget(item)
            self.settings_toc.append((item, target))
        self.settings_toc[0][0].setChecked(True)
        toc_column.addStretch(1)
        scroll.setWidget(body)
        scroll.verticalScrollBar().valueChanged.connect(lambda _value: self._sync_settings_toc())
        # 滚动条收进卡片圆角以内，不贴着卡片右边
        scroll_holder = QVBoxLayout()
        scroll_holder.setContentsMargins(0, 8, 6, 8)
        scroll_holder.addWidget(scroll)
        frame_row.addLayout(scroll_holder, 1)
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.addWidget(frame)
        return page

    def _scroll_settings_to(self, section):
        """目录点一项：滚到那一节的标题处（平滑滚动），目录立即亮这一项。"""

        scroll = self.settings_scroll
        target = section.mapTo(scroll.widget(), QPoint(0, 0)).y() - 24
        motion.smooth_scroll(scroll, lambda: scroll.verticalScrollBar().setValue(max(0, target)))
        for item, widget in self.settings_toc:
            if widget is section:
                item.setChecked(True)

    def _sync_settings_toc(self):
        scroll = self.settings_scroll
        if scroll.widget() is None:
            return
        value = scroll.verticalScrollBar().value()
        current = None
        for item, widget in self.settings_toc:
            if widget.mapTo(scroll.widget(), QPoint(0, 0)).y() - 40 <= value:
                current = item
        bar = scroll.verticalScrollBar()
        if value >= bar.maximum() - 2 and self.settings_toc:
            current = self.settings_toc[-1][0] if value > 0 else current
        if current is not None and not current.isChecked():
            current.setChecked(True)

    def _project_sort_changed(self):
        snapshot = self.service.set_sort_mode(self.project_sort.currentData())
        for index, project in enumerate(snapshot.projects):
            button = self.project_buttons.get(project.id)
            if button is not None:
                self.projects_layout.removeWidget(button)
                self.projects_layout.insertWidget(index, button)
        self.cover_editor.set_project_list(snapshot.projects)
        self._save_session()

    def _browse_material_directory(self):
        from PySide6.QtWidgets import QFileDialog

        library = self.cover_editor.service.asset_library
        path = QFileDialog.getExistingDirectory(self, "选择封面素材文件夹", str(library.legacy_root))
        if not path:
            return
        try:
            directory = library.set_source_directory(path)
        except (OSError, ValueError) as exc:
            self._show_transient_status(f"素材文件夹设置失败：{exc}")
            return
        self.settings_material_directory.setText(str(directory))
        self._show_transient_status("素材文件夹已更新，下次打开素材库时生效")

    def _check_render_font(self):
        """每次进设置页都在后台查当前字幕样式的字体：没装新字体时直接用缓存，刚装好的字体回来就认。"""
        if not hasattr(self, "render_font_note"):
            return
        style = self._subtitle_style()
        font_name, bold = style.get("font_name"), style.get("bold", True)

        def done(problem, error):
            text = "" if error is not None else problem
            self.render_font_note.setText(f"⚠ {text}" if text else "")
            self.render_font_note.setVisible(bool(text))

        self._run(lambda: subtitle_font_problem(font_name, bold) if font_name else subtitle_font_problem(), done)

    def _browse_submission_root(self):
        """让用户选择投稿目录。"""
        from PySide6.QtWidgets import QFileDialog
        path = QFileDialog.getExistingDirectory(self, "选择投稿目录", str(self.service.root))
        if path:
            self._set_submission_root(path)

    def _set_submission_root(self, path):
        self.service.root = Path(path)
        self.settings_directory.setText(str(self.service.root))
        self._save_session()
        self._show_transient_status(f"投稿目录已更新：{path}")
        self.refresh()

    def _use_nested_root(self):
        if getattr(self, "_nested_root", ""):
            self._set_submission_root(self._nested_root)

    def refresh(self):
        if self._loading or self._saving or not self._resolve_unsaved():
            return
        self._scan_generation += 1
        generation = self._scan_generation
        self.scan_status.setText("正在扫描投稿目录…")
        self.scan_status_changed()
        self._run(self.service.refresh, lambda result, error: self._scanned(generation, result, error))

    def _scanned(self, generation, snapshot, error):
        if generation != self._scan_generation:
            return
        if error:
            self.scan_status.setText(f"扫描失败：{error}")
            self.scan_status_changed()
            return
        assert isinstance(snapshot, ProjectSnapshot)
        self.scan_status.setText(f"{snapshot.status} · {len(snapshot.projects)} 个项目")
        self.scan_status_changed()
        no_video = bool(snapshot.projects) and not any(project.videos for project in snapshot.projects)
        nested = None if no_video else nested_root_candidate(snapshot.projects)
        self._nested_root = nested.directory if nested else ""
        self.nested_root_button.setVisible(nested is not None)
        if nested is not None:
            name = Path(nested.directory).name
            self.empty_projects_hint.setText(
                f"“{name}”里的视频分在几个子文件夹里，被当成了一个项目——看起来投稿目录选高了一层。"
                f"每个装视频的文件夹应该各算一个项目。"
            )
            self.nested_root_button.setText(f"改用“{name}”作投稿目录")
        elif no_video:
            self.empty_projects_hint.setText(
                f"这个目录下的 {len(snapshot.projects)} 个文件夹里都没有视频，可能选到了上一级或别的目录。"
            )
        else:
            self.empty_projects_hint.setText(
                f"这个目录里还没有投稿项目：{self.service.root}。"
                "投稿目录下每个子文件夹算一个项目，里面放视频（和字幕）。"
            )
        self.empty_projects.setVisible(not snapshot.projects or no_video or nested is not None)
        self.cover_editor.set_project_list(snapshot.projects)
        while self.projects_layout.count() > 1:
            item = self.projects_layout.takeAt(0)
            item.widget().deleteLater()
        self.project_buttons.clear()
        for number, project in enumerate(snapshot.projects, 1):
            status = project.status
            if status == "素材已识别":
                status = "字幕已保存" if any(video.has_corrected_srt for video in project.videos) else ""
            if not status:
                status = f"{len(project.videos)} 个视频" if project.videos else "还没有视频"
            button = ProjectOption(number, project.title, status)
            self.project_group.addButton(button)
            button.clicked.connect(lambda _checked=False, p=project: self._select_real_project(p))
            self.projects_layout.insertWidget(self.projects_layout.count() - 1, button)
            self.project_buttons[project.id] = button
        rows = len(self.project_buttons)
        self.project_scroll.setVisible(rows > 0)
        self.project_scroll.setFixedHeight(min(rows, 8) * (ProjectOption.HEIGHT + 1) + (24 if rows > 8 else 0))
        motion.install_tree(self.project_rail)
        motion.stagger_in(list(self.project_buttons.values()))
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

    def _select_real_project(self, project, *, preferred_video=None):
        if hasattr(self, "project_popover"):
            self.project_popover.dismiss()
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
        self._ai_undo.clear()
        self._ai_redo.clear()
        self._ai_skip_undo.clear()
        self._ai_skip_redo.clear()
        self.selection = CueSelection()
        self._pending_filter = False
        self.filter_all.setChecked(True)
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
        preferred = preferred_video or self._session.get("video_path")
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
            self._asr_hide()
            self._media_ready = False
            self.play_button.setEnabled(False)
            if self.player:
                self.player.pause()
            self.video_placeholder.setText("该项目没有视频")
            self.video_stack.setCurrentIndex(0)
            self.subtitle_status.setText(project.error or "该项目没有视频")
        self._save_session()

    def _next_cover_video(self):
        """封面页“下一个”：按列表顺序找下一个还没导出过封面的视频，到末尾绕回开头；页面停在封面。

        都做过了就按顺序切到下一个（到末尾就说明一声）。
        """

        if self.project is None:
            return
        current = self.cover_editor.video
        order = [(project, video) for project in self.service.snapshot.projects for video in project.videos]
        here = next((i for i, (project, video) in enumerate(order)
                     if project.id == self.project.id and current is not None and video.path == current.path), -1)
        if here < 0 or len(order) < 2:
            self._show_transient_status("投稿列表里没有别的视频")
            return
        rest = order[here + 1:] + order[:here]
        exported = self.cover_editor.service.existing_exports
        target = next((item for item in rest if not exported(*item)), None)
        if target is None:
            if here + 1 >= len(order):
                self._show_transient_status("投稿列表里的视频都导出过封面了")
                return
            target = order[here + 1]
        project, video = target
        if project.id == self.project.id:
            self.video_choice.setCurrentIndex(list(project.videos).index(video))
            return
        self._select_real_project(project, preferred_video=video.path)
        if self.project is project:
            self._show_transient_status(f"已切到下一个：{project.title}")

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
        self._commit_editor()
        if self.project is not None:
            self.cover_editor.set_context(self.project, video)
        self._asr_hide(video)
        self.ai_session = None
        self._ai_selected = None
        self._ai_undo.clear()
        self._ai_redo.clear()
        self._ai_skip_undo.clear()
        self._ai_skip_redo.clear()
        self.selection = CueSelection()
        self.selected_index = None
        self._pending_filter = False
        self.filter_all.setChecked(True)
        self._render_ai()
        self._media_ready = False
        self._preview_attached = False
        self._pending_seek = None
        self._clear_playback_range()
        self._seek_guard_target = None
        self._seek_guard_until = 0.0
        self._player_duration = 0.0
        self._player_position = 0.0
        self.timeline.set_duration(0.0)
        self.timeline.set_playhead(0.0, playing=False)
        self.timeline.set_waveform(None)
        self.play_button.setEnabled(False)
        self._player_paused = True
        self._set_play_button(True)
        if self.player:
            self._player_load_token += 1
            token = self._player_load_token
            self.video_stack.setCurrentIndex(1)
            self._resume_pending = self._session.get("video_path") == video.path
            self.time_label.setText("正在加载视频…")
            self.time_total.setText("")
            self.video_info.setText("")
            self.player.load(video.path)
            QTimer.singleShot(20000, lambda: self._check_player_load(token))
        else:
            self.video_stack.setCurrentIndex(0)
        self._loading = True
        self.document = None
        self.model.set_pending_filter(None)
        self.model.set_document(None)
        self.timeline.set_document(None)
        # 先把上一个视频的“待检查 N”“存在重叠”、保存/压制按钮状态清掉，没字幕的视频不会再残留
        self._update_status()
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
            if not video.has_source_srt:
                self.subtitle_status.setText("这个视频还没有字幕")
                self._asr_offer(video)
            else:
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
            QMessageBox.warning(self, "没有套用字幕草稿", _DRAFT_SKIPPED.get(draft.status, _DRAFT_SKIPPED["invalid"]))
        self.document = document
        self.model.set_document(document)
        self.timeline.set_document(document)
        self.timeline.set_waveform(None, self._player_duration or None)
        motion.fade_in(self.table, motion.FADE + 60)
        motion.fade_in(self.timeline, motion.FADE + 60)
        self._start_waveform(video.path)
        self.ai_session, ai_status = self.ai_service.load(document, project.title)
        if ai_status == "missing":
            try:
                self.ai_service.config_loader()
            except (OSError, ValueError):
                ai_status = "unconfigured"
        if ai_status == "partial":
            # 字幕和检查时不同（如没恢复草稿）：对得上的建议留着，按当前字幕重新记一份
            self._save_ai_session()
        self.ai_note.setText("旧建议已过期，请主动重新检查。" if ai_status == "stale" else
                             "未配置 AI，请在设置页配置后主动检查。" if ai_status == "unconfigured" else
                             "已恢复待确认建议。" if ai_status == "ready" else
                             f"已恢复 {len(self.ai_session.pending)} 条待确认建议；"
                             f"另有 {self.ai_session.dropped} 条对应的字幕已经改过，已作废。"
                             if ai_status == "partial" and self.ai_session.dropped else
                             "已恢复待确认建议。" if ai_status == "partial" else
                             "AI 只生成待确认建议，不修改字幕。")
        self.ai_setup.setVisible(ai_status == "unconfigured")
        if self.ai_session is not None and not self.ai_session.local_only and self.ai_session.checked:
            left = len(self.ai_session.unchecked(document.entries))
            if left:
                # 上次取消了或者关窗了：说清楚还剩几条、怎么接着查
                self.ai_note.setText(f"{self.ai_note.text()}上次检查还有 {left} 条没查完（或之后改过），"
                                     "点“继续检查”只查这些。")
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
        if self._style_panel_open():
            # 样式面板开着时换了项目：“先选一个项目”这类提示要跟着变
            self._update_style_hint()
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
