"""AutoCover 桌面编辑器。

桌面层只维护交互状态和后台任务；媒体取帧、草稿持久化以及 Pillow 渲染
继续由 ``cover_service`` 负责。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QObject, QRectF, QRunnable, QSize, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QIcon,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QColorDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from autoslice.desktop.foundation import DesktopStorage
from autoslice.desktop.projects import ProjectVideo, SubmissionProject
from autoslice_cover.document_layout import BACKGROUND_SCALE_MAX, BACKGROUND_SCALE_MIN
from autoslice_cover.document_render import rgba
from autoslice_cover.fonts import resolve_font_selection

from .cover_asset_dialog import CoverAssetDialog
from .cover_batch_dialog import BatchTarget, CoverBatchDialog
from .cover_canvas import CoverCanvas
from .cover_history import CoverHistory
from .cover_model import (
    AssetRef,
    BackgroundObject,
    CoverDocument,
    ImageObject,
    Rect,
    ShapeObject,
    StickerObject,
    TextObject,
    TextWrap,
    Transform,
    insert_overlay,
    object_for_profile,
    resize_text_style,
    restack_object,
    set_object_visible,
    text_override_payload,
    update_text_object,
)
from .cover_service import (
    CoverDraft,
    CoverFrame,
    CoverScheme,
    CoverService,
    best_overview_frame,
    primary_copy_ids,
    recommended_frame,
    wrap_cover_title,
)
from .cover_style import STYLE_PRESETS, StylePreset
from .qt_preview.icons import icon


class _JobSignals(QObject):
    finished = Signal(object, object)


class _Job(QRunnable):
    def __init__(self, action):
        super().__init__()
        self.action = action
        self.signals = _JobSignals()

    def run(self):
        try:
            self.signals.finished.emit(self.action(), None)
        except Exception as exc:  # noqa: BLE001 - 回到界面显示可执行错误
            self.signals.finished.emit(None, exc)


class _TitleEdit(QPlainTextEdit):
    """允许手动换行，并保留旧测试/调用方使用的 ``text()``。"""

    def text(self) -> str:
        return self.toPlainText()


class _ColorButton(QPushButton):
    """带色块的颜色按钮；可选“无”与透明度，替代手填十六进制。"""

    color_changed = Signal()

    def __init__(self, *, allow_none: bool = False, allow_alpha: bool = False, parent=None):
        super().__init__(parent)
        self._color = ""
        self._allow_none = allow_none
        self._allow_alpha = allow_alpha
        self.setFixedHeight(26)
        self.setIconSize(QSize(14, 14))
        if allow_none:
            menu = QMenu(self)
            menu.addAction("选择颜色…", self._pick)
            menu.addAction("无", lambda: self._choose(""))
            self.setMenu(menu)
        else:
            self.clicked.connect(self._pick)
        self._refresh()

    def color(self) -> str:
        return self._color

    def set_color(self, value: str | None) -> None:
        self._color = (value or "").upper()
        self._refresh()

    def _choose(self, value: str) -> None:
        if value != self._color:
            self.set_color(value)
            self.color_changed.emit()

    def _pick(self) -> None:
        options = QColorDialog.ColorDialogOption.ShowAlphaChannel if self._allow_alpha else QColorDialog.ColorDialogOption(0)
        picked = QColorDialog.getColor(QColor(*rgba(self._color or "#FFFFFF")), self, "选择颜色", options)
        if not picked.isValid():
            return
        value = f"#{picked.red():02X}{picked.green():02X}{picked.blue():02X}"
        if self._allow_alpha and picked.alpha() < 255:
            value += f"{picked.alpha():02X}"
        self._choose(value)

    def _refresh(self) -> None:
        self.setText(self._color or "无")
        pixmap = QPixmap(14, 14)
        pixmap.fill(QColor(*rgba(self._color)) if self._color else QColor(0, 0, 0, 0))
        if not self._color:
            painter = QPainter(pixmap)
            painter.setPen(QPen(QColor("#A1AFBC"), 1.5))
            painter.drawLine(2, 12, 12, 2)
            painter.end()
        self.setIcon(QIcon(pixmap))


def _preset_icon(preset: StylePreset) -> QIcon:
    """把预设画成“字”的小样，按钮上直接看到效果。"""

    pixmap = QPixmap(30, 22)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    if preset.backdrop:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(*rgba(preset.backdrop)))
        painter.drawRoundedRect(1, 1, 28, 20, 4, 4)
    font = QFont()
    font.setPixelSize(16)
    font.setBold(True)
    # 双色预设画“黄青”这类两个字，A 色在前；描边按字号比例画。
    pairs = (
        ((2, preset.label[0], preset.context_fill, preset.context_stroke or preset.stroke), (16, preset.label[1], preset.fill, preset.stroke))
        if preset.context_fill and len(preset.label) >= 2
        else ((7, "字", preset.fill, preset.stroke),)
    )
    stroke = preset.stroke_ratio * 16
    outer = preset.outer_stroke_ratio * 16
    for x, glyph, fill, stroke_color in pairs:
        glyphs = QPainterPath()
        glyphs.addText(x, 17, font, glyph)
        if preset.outer_stroke and outer:
            painter.strokePath(glyphs, QPen(QColor(*rgba(preset.outer_stroke)), (stroke + outer) * 2))
        if stroke:
            painter.strokePath(glyphs, QPen(QColor(*rgba(stroke_color)), stroke * 2))
        painter.fillPath(glyphs, QColor(*rgba(fill)))
    painter.end()
    return QIcon(pixmap)


class CoverEditorWidget(QWidget):
    """真正可鼠标操作的 AutoCover 编辑器。"""

    status_changed = Signal(str)

    def __init__(self, storage: DesktopStorage, parent=None):
        super().__init__(parent)
        self.service = CoverService(storage)
        self.project: SubmissionProject | None = None
        self.video: ProjectVideo | None = None
        self.draft = CoverDraft("")
        self.document: CoverDocument | None = None
        self._preview_path: Path | None = None
        self._background_source: str | None = None
        self._context_generation = 0
        self._busy = False
        self._preview_dirty = False
        self._pending_export = False
        self._current_playhead = 0.0
        self._frame_extract_pending = False
        self._frame_request_generation = 0
        self._nearby_request_generation = 0
        self._copy_variants = ()
        self._copy_variant_index = -1
        self._selected_text_id: str | None = None
        self._canvas_key = "4x3"
        self._preview_request_generation = 0
        self._jobs: set[_Job] = set()
        self.history = CoverHistory()
        self._frame_locked = False
        self._selected_frame_timestamp: float | None = None
        self._wider_frames: tuple[tuple[Path, float], ...] = ()
        self._nearby_pending: set[float] = set()
        self._nearby_results: list[CoverFrame] = []
        self._nearby_error = None
        self._schemes: tuple[CoverScheme, ...] = ()
        self._batch_projects: tuple[SubmissionProject, ...] = ()
        self._scheme_batch = 0
        self._scheme_generation = 0
        self._draft_timer = QTimer(self)
        self._draft_timer.setSingleShot(True)
        self._draft_timer.setInterval(500)
        self._draft_timer.timeout.connect(self._save_draft)
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(120)
        self._preview_timer.timeout.connect(self._render_preview)
        self._build()
        # 封面页自己的撤销/重做；文案框获得焦点时由输入框处理文字撤销。
        for sequence, action in (
            (QKeySequence.StandardKey.Undo, self._undo),
            (QKeySequence.StandardKey.Redo, self._redo),
            (QKeySequence("Ctrl+Y"), self._redo),
        ):
            shortcut = QShortcut(sequence, self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(action)

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── 单行工具栏：比例 + 撤销/重做 + 导出 ──
        toolbar = QWidget()
        toolbar.setObjectName("coverToolbar")
        toolbar.setFixedHeight(44)
        toolbar_row = QHBoxLayout(toolbar)
        toolbar_row.setContentsMargins(12, 0, 12, 0)
        toolbar_row.setSpacing(6)

        self.canvas_ratio_group = QButtonGroup(self)
        self.canvas_ratio_group.setExclusive(True)
        self.canvas_ratio_buttons: dict[str, QPushButton] = {}
        for key, text in (("4x3", "4:3"), ("16x9", "16:9")):
            btn = QPushButton(text)
            btn.setCheckable(True)
            btn.setMinimumWidth(48)
            btn.setFixedHeight(28)
            btn.setToolTip(f"切换 {text} 主画布")
            self.canvas_ratio_group.addButton(btn)
            self.canvas_ratio_buttons[key] = btn
            toolbar_row.addWidget(btn)
            btn.clicked.connect(lambda _c=False, k=key: self._set_canvas_key(k))
        self.canvas_ratio_buttons["4x3"].setChecked(True)

        toolbar_row.addSpacing(8)

        self.undo_button = QPushButton("")
        self.undo_button.setIcon(icon("undo"))
        self.undo_button.setIconSize(QSize(16, 16))
        self.undo_button.setFixedSize(28, 28)
        self.undo_button.setObjectName("quiet")
        self.undo_button.setToolTip("撤销")
        self.undo_button.setEnabled(False)
        self.undo_button.clicked.connect(self._undo)
        self.redo_button = QPushButton("")
        self.redo_button.setIcon(icon("redo"))
        self.redo_button.setIconSize(QSize(16, 16))
        self.redo_button.setFixedSize(28, 28)
        self.redo_button.setObjectName("quiet")
        self.redo_button.setToolTip("重做")
        self.redo_button.setEnabled(False)
        self.redo_button.clicked.connect(self._redo)
        toolbar_row.addWidget(self.undo_button)
        toolbar_row.addWidget(self.redo_button)
        toolbar_row.addSpacing(8)
        # 插入：文字 / 素材 / 形状常驻工具栏。
        self.add_text_button = QPushButton("文字")
        self.add_text_button.setIcon(icon("text"))
        self.add_text_button.setIconSize(QSize(16, 16))
        self.add_text_button.setObjectName("quiet")
        self.add_text_button.setFixedHeight(28)
        self.add_text_button.setToolTip("新建文本框；双击画布上的文字可直接改字")
        self.add_text_button.clicked.connect(self._add_text)
        self.add_text_button.setEnabled(False)
        toolbar_row.addWidget(self.add_text_button)
        self.asset_menu_button = QPushButton("素材")
        self.asset_menu_button.setIcon(icon("image"))
        self.asset_menu_button.setIconSize(QSize(16, 16))
        self.asset_menu_button.setObjectName("quiet")
        self.asset_menu_button.setFixedHeight(28)
        self.asset_menu_button.setToolTip("从素材库选择或导入图片，作为可编辑对象加入画布")
        asset_menu = QMenu(self.asset_menu_button)
        asset_menu.addAction("从素材库选择…", self._browse_assets)
        asset_menu.addAction("导入图片…", self._import_asset)
        self.asset_menu_button.setMenu(asset_menu)
        self.asset_menu_button.setEnabled(False)
        toolbar_row.addWidget(self.asset_menu_button)
        self.shape_menu_button = QPushButton("形状")
        self.shape_menu_button.setIcon(icon("shapes"))
        self.shape_menu_button.setIconSize(QSize(16, 16))
        self.shape_menu_button.setObjectName("quiet")
        self.shape_menu_button.setFixedHeight(28)
        self.shape_menu_button.setToolTip("添加圆圈、箭头或矩形强调框")
        self.shape_menu_button.setMenu(self._shape_menu(self.shape_menu_button))
        self.shape_menu_button.setEnabled(False)
        toolbar_row.addWidget(self.shape_menu_button)

        toolbar_row.addStretch(1)

        self.draft_status = QLabel("")
        self.draft_status.setObjectName("subtle")
        toolbar_row.addWidget(self.draft_status)

        self.batch_button = QPushButton("批量出图")
        self.batch_button.setIcon(icon("layers"))
        self.batch_button.setIconSize(QSize(16, 16))
        self.batch_button.setObjectName("quiet")
        self.batch_button.setFixedHeight(28)
        self.batch_button.setToolTip("给投稿目录里的多个视频一次出封面（4:3 + 16:9）")
        self.batch_button.clicked.connect(self._open_batch)
        self.batch_button.setEnabled(False)
        toolbar_row.addWidget(self.batch_button)
        self.export_button = QPushButton("导出")
        self.export_button.setFixedHeight(28)
        self.export_button.setToolTip("导出当前画布比例")
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self._export)
        toolbar_row.addWidget(self.export_button)
        self.export_both_button = QPushButton("导出 4:3 + 16:9")
        # 投稿要两个比例，双比例导出是主动作；旧网页端同样以“保存双比例”为主按钮。
        self.export_both_button.setObjectName("primary")
        self.export_both_button.setIcon(icon("download"))
        self.export_both_button.setIconSize(QSize(16, 16))
        self.export_both_button.setFixedHeight(28)
        self.export_both_button.setToolTip("分别导出 4:3（1440×1080）与 16:9（1920×1080），不覆盖已有文件")
        self.export_both_button.setEnabled(False)
        self.export_both_button.clicked.connect(self._export_both)
        toolbar_row.addWidget(self.export_both_button)

        # 面板折叠按钮
        self.panel_toggle = QPushButton("")
        self.panel_toggle.setIcon(icon("chevron_right"))
        self.panel_toggle.setIconSize(QSize(16, 16))
        self.panel_toggle.setFixedSize(28, 28)
        self.panel_toggle.setObjectName("quiet")
        self.panel_toggle.setToolTip("收起/展开属性面板")
        self.panel_toggle.setCheckable(True)
        toolbar_row.addWidget(self.panel_toggle)
        root.addWidget(toolbar)

        # ── 隐藏控件：被代码引用但不直接放在布局里 ──
        self.project_label = QLabel("")
        self.video_label = QLabel("")
        self.export_summary = QLabel("")
        self.export_summary.setObjectName("subtle")
        self.export_summary.setWordWrap(False)
        self.export_summary.setMaximumWidth(420)
        toolbar_row.insertWidget(toolbar_row.indexOf(self.export_button), self.export_summary)
        self.notice_label = QLabel("")
        self.notice_label.setObjectName("coverNoticeInfo")
        self.notice_label.setWordWrap(True)
        self.notice_label.setVisible(False)
        root.insertWidget(1, self.notice_label)
        self.canvas_hint = QLabel("")
        self.canvas_hint.setObjectName("subtle")
        self.canvas_ratio_status = QLabel("")
        self.canvas_ratio_status.setObjectName("subtle")
        self.copy_role_hint = QLabel("")
        self.copy_role_hint.setObjectName("subtle")
        self.copy_role_hint.setWordWrap(True)
        self.copy_policy_hint = QLabel("")
        self.copy_policy_hint.setObjectName("subtle")
        self.copy_policy_hint.setWordWrap(True)
        self.more_button = QPushButton("")
        self.advanced_widget = QWidget()
        self.advanced_widget.setVisible(False)
        self.media_controls = QWidget()
        self.media_controls.setVisible(False)
        self.import_button = QPushButton("导入底图")
        self.import_button.clicked.connect(self._import_image)
        self.import_button.setVisible(False)

        # ── 主内容区：画布 + 右侧上下文面板 ──
        content = QHBoxLayout()
        content.setSpacing(0)
        content.setContentsMargins(0, 0, 0, 0)

        # 中央画布
        center = QWidget()
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(12, 8, 12, 8)
        center_layout.setSpacing(6)
        self.canvas = CoverCanvas()
        self.canvas.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.canvas.title_position_changed.connect(self._title_position_changed)
        self.canvas.title_position_finished.connect(self._gesture_finished)
        self.canvas.background_position_changed.connect(self._background_position_changed)
        self.canvas.background_position_finished.connect(self._gesture_finished)
        self.canvas.zoom_changed.connect(self._zoom_changed)
        self.canvas.selected_changed.connect(self._canvas_selection_changed)
        self.canvas.selected_object_changed.connect(self._canvas_object_selected)
        self.canvas.safe_area_warning_changed.connect(self._canvas_safety_changed)
        self.canvas.object_changed.connect(self._canvas_object_changed)
        self.canvas.object_preview_changed.connect(self._canvas_object_preview_changed)
        self.canvas.delete_requested.connect(self._delete_object)
        self.canvas.duplicate_requested.connect(self._duplicate_object)
        self.canvas.edit_requested.connect(self._edit_text)
        center_layout.addWidget(self.canvas, 1)

        hint_row = QHBoxLayout()
        hint_row.setContentsMargins(0, 0, 0, 0)
        hint_row.setSpacing(8)
        self.canvas_hint.setWordWrap(False)
        self.canvas_hint.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        hint_row.addWidget(self.canvas_hint, 1)
        center_layout.addLayout(hint_row)
        content.addWidget(center, 1)

        # ── 右侧上下文面板（QStackedWidget 按选中对象切换）──
        self.right_panel = QWidget()
        self.right_panel.setObjectName("coverPanel")
        self.right_panel.setMinimumWidth(0)
        self.right_panel.setMaximumWidth(280)
        right_layout = QVBoxLayout(self.right_panel)
        right_layout.setContentsMargins(10, 10, 10, 10)
        right_layout.setSpacing(6)

        right_layout.addLayout(self._build_scheme_section())
        right_layout.addSpacing(6)

        # 面板标题（随上下文变化）
        self.panel_title = QLabel("封面文案")
        self.panel_title.setObjectName("sectionTitle")
        right_layout.addWidget(self.panel_title)

        # 上下文面板堆栈
        self.panel_stack = QStackedWidget()
        right_layout.addWidget(self.panel_stack, 1)

        # ── 文字面板（默认）──
        self.copy_controls = self._build_text_panel()
        self.panel_stack.addWidget(self.copy_controls)

        # ── 素材对象面板 ──
        self.asset_controls = self._build_asset_panel()
        self.panel_stack.addWidget(self.asset_controls)

        # ── 背景面板 ──
        self.bg_controls = self._build_bg_panel()
        self.panel_stack.addWidget(self.bg_controls)

        # ── 空状态面板（无选中/无项目）──
        self.empty_controls = self._build_empty_panel()
        self.panel_stack.addWidget(self.empty_controls)

        content.addWidget(self.right_panel)

        # 面板折叠逻辑
        self._panel_visible = True
        self._last_panel_width = 280
        self.panel_toggle.toggled.connect(self._toggle_panel)
        root.addLayout(content, 1)

        # ── 底部附近帧条（精简为一行）──
        strip = QWidget()
        strip.setObjectName("frameStrip")
        strip_layout = QHBoxLayout(strip)
        strip_layout.setContentsMargins(12, 4, 12, 6)
        strip_layout.setSpacing(4)
        self.frame_center_label = QLabel("0.00s")
        self.frame_center_label.setObjectName("subtle")
        self.frame_center_label.setMinimumWidth(62)
        strip_layout.addWidget(self.frame_center_label)
        self.current_frame_button = QPushButton("当前帧")
        self.current_frame_button.setFixedHeight(24)
        self.current_frame_button.setObjectName("quiet")
        self.current_frame_button.setToolTip("读取字幕页播放位置")
        self.current_frame_button.clicked.connect(self._use_current_frame)
        strip_layout.addWidget(self.current_frame_button)
        self.frame_lock_button = QPushButton("锁帧")
        self.frame_lock_button.setFixedHeight(24)
        self.frame_lock_button.setObjectName("quiet")
        self.frame_lock_button.setCheckable(True)
        self.frame_lock_button.setToolTip("锁定后候选和AI不换帧")
        self.frame_lock_button.clicked.connect(self._toggle_frame_lock)
        strip_layout.addWidget(self.frame_lock_button)
        self.more_frames_button = QPushButton("更多")
        self.more_frames_button.setFixedHeight(24)
        self.more_frames_button.setObjectName("quiet")
        self.more_frames_button.setToolTip("扩大取帧范围")
        self.more_frames_button.clicked.connect(self._find_more_frames)
        strip_layout.addWidget(self.more_frames_button)
        strip_layout.addSpacing(8)
        self.nearby_frame_buttons = []
        for _ in range(7):
            button = QPushButton("--")
            button.setObjectName("frameThumb")
            button.setCheckable(True)
            button.setMinimumWidth(86)
            button.setMaximumHeight(58)
            button.setIconSize(QSize(80, 45))
            button.setProperty("timestamp", 0.0)
            button.clicked.connect(
                lambda _checked=False, item=button: self._choose_nearby_frame(
                    float(item.property("timestamp") or 0.0)
                )
            )
            self.nearby_frame_buttons.append(button)
            strip_layout.addWidget(button, 1)
        root.addWidget(strip)

        # 初始状态：无项目时隐藏右侧面板，显示空状态
        self._hide_panel()
        self.panel_toggle.setEnabled(False)
        self._show_empty_panel()
        self._update_canvas_hint()

    def _build_scheme_section(self) -> QVBoxLayout:
        """快速方案：参考旧网页端“推荐排版”，三张缩略图三选一后再微调。"""

        section = QVBoxLayout()
        section.setSpacing(4)
        scheme_head = QHBoxLayout()
        scheme_head.setSpacing(4)
        scheme_title = QLabel("快速方案")
        scheme_title.setObjectName("sectionTitle")
        scheme_head.addWidget(scheme_title)
        scheme_head.addStretch(1)
        self.scheme_refresh_button = QPushButton("")
        self.scheme_refresh_button.setIcon(icon("refresh"))
        self.scheme_refresh_button.setIconSize(QSize(15, 15))
        self.scheme_refresh_button.setFixedSize(26, 26)
        self.scheme_refresh_button.setObjectName("quiet")
        self.scheme_refresh_button.setToolTip("换一批：轮换文案和配色（本地生成，不调用 AI）")
        self.scheme_refresh_button.clicked.connect(self._next_scheme_batch)
        self.scheme_refresh_button.setEnabled(False)
        scheme_head.addWidget(self.scheme_refresh_button)
        section.addLayout(scheme_head)
        scheme_row = QHBoxLayout()
        scheme_row.setSpacing(4)
        self.scheme_buttons: list[QPushButton] = []
        self.scheme_labels: list[QLabel] = []
        for index in range(3):
            column = QVBoxLayout()
            column.setSpacing(2)
            button = QPushButton("")
            button.setObjectName("schemeThumb")
            button.setCheckable(True)
            button.setFixedSize(84, 66)
            button.setIconSize(QSize(78, 58))
            button.setEnabled(False)
            button.clicked.connect(lambda _checked=False, value=index: self._apply_scheme(value))
            caption = QLabel("—")
            caption.setObjectName("subtle")
            caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
            column.addWidget(button, 0, Qt.AlignmentFlag.AlignHCenter)
            column.addWidget(caption)
            scheme_row.addLayout(column)
            self.scheme_buttons.append(button)
            self.scheme_labels.append(caption)
        section.addLayout(scheme_row)
        return section

    def _toggle_panel(self, checked):
        """手动切换面板折叠/展开。"""
        if checked:
            self._hide_panel()
        else:
            self._show_panel()

    def _hide_panel(self):
        """收起右侧面板。"""
        if self.right_panel.width() > 0:
            self._last_panel_width = self.right_panel.width()
        self.right_panel.setMaximumWidth(0)
        self.right_panel.setMinimumWidth(0)
        self.panel_toggle.setIcon(icon("chevron_left"))
        self._panel_visible = False

    def _show_panel(self):
        """展开右侧面板。"""
        self.right_panel.setMinimumWidth(0)
        self.right_panel.setMaximumWidth(self._last_panel_width)
        self.panel_toggle.setIcon(icon("chevron_right"))
        self._panel_visible = True

    def _show_empty_panel(self):
        """显示空状态面板（无项目或无选中对象）。"""
        self.panel_title.setText("封面工具")
        self.panel_stack.setCurrentWidget(self.empty_controls)

    def _set_notice(self, text: str = "", level: str = "info"):
        """警告和错误用画布上方的提示条；例行进度只进状态栏，不让画布上下跳。"""
        text = str(text or "").strip()
        if level == "info":
            if text:
                self.status_changed.emit(text)
            text = ""
        if not text:
            self.notice_label.clear()
            self.notice_label.setVisible(False)
            return
        object_name = {
            "error": "coverNoticeError",
            "warning": "coverNoticeWarning",
        }.get(level, "coverNoticeInfo")
        self.notice_label.setObjectName(object_name)
        self.notice_label.setText(text)
        self.notice_label.setVisible(True)
        self.notice_label.style().unpolish(self.notice_label)
        self.notice_label.style().polish(self.notice_label)

    def _update_nearby_frame_selection(self, timestamp: float | None = None):
        if timestamp is not None:
            self._selected_frame_timestamp = max(0.0, float(timestamp))
        selected = self._selected_frame_timestamp
        distances = [
            abs(float(button.property("timestamp") or 0.0) - selected)
            if selected is not None else float("inf")
            for button in self.nearby_frame_buttons
        ]
        nearest = min(distances, default=float("inf"))
        nearest_index = distances.index(nearest) if nearest <= 0.06 else -1
        for index, button in enumerate(self.nearby_frame_buttons):
            button.setChecked(index == nearest_index)

    def _build_text_panel(self) -> QWidget:
        """文字对象属性面板：文案、字号、对齐、样式。"""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.title_edit = _TitleEdit()
        self.title_edit.setPlaceholderText("封面文案…")
        self.title_edit.setMaximumHeight(72)
        self.title_edit.textChanged.connect(self._draft_changed)
        layout.addWidget(self.title_edit)

        # 当前文本框身份 + 换文案；选择文本框直接在画布上点。
        role_row = QHBoxLayout()
        role_row.setSpacing(4)
        self.copy_role_label = QLabel("")
        self.copy_role_label.setObjectName("subtle")
        role_row.addWidget(self.copy_role_label, 1)
        self.copy_button = QPushButton("换一版文案")
        self.copy_button.setFixedHeight(26)
        self.copy_button.setObjectName("quiet")
        self.copy_button.setToolTip("只换 A/B 主文案的文字，位置和字号不变")
        self.copy_button.clicked.connect(self._cycle_copy)
        role_row.addWidget(self.copy_button)
        layout.addLayout(role_row)

        # 字号 + 对齐
        size_align = QHBoxLayout()
        size_align.setSpacing(6)
        size_align.addWidget(QLabel("字号"))
        self.font_spin = QSpinBox()
        self.font_spin.setRange(24, 320)
        self.font_spin.setSuffix(" px")
        self.font_spin.setFixedHeight(26)
        self.font_spin.valueChanged.connect(self._draft_changed)
        size_align.addWidget(self.font_spin, 1)
        self.align_buttons: dict[str, QPushButton] = {}
        self.align_group = QButtonGroup(self)
        self.align_group.setExclusive(True)
        for key, text in (("left", "左"), ("center", "中"), ("right", "右")):
            btn = QPushButton(text)
            btn.setCheckable(True)
            btn.setFixedHeight(26)
            btn.setMinimumWidth(28)
            self.align_group.addButton(btn)
            self.align_buttons[key] = btn
            size_align.addWidget(btn)
            btn.clicked.connect(lambda _c=False, k=key: self._set_text_align(k))
        self.align_buttons["left"].setChecked(True)
        layout.addLayout(size_align)

        # 配色预设常驻：最常用的一步，不藏在折叠里。
        preset_row = QHBoxLayout()
        preset_row.setSpacing(3)
        self.style_preset_buttons: dict[str, QPushButton] = {}
        for preset in STYLE_PRESETS:
            button = QPushButton("")
            button.setIcon(_preset_icon(preset))
            button.setIconSize(QSize(26, 20))
            button.setFixedSize(32, 26)
            button.setObjectName("quiet")
            button.setToolTip(f"{preset.label}（同时应用到所有文本框）")
            button.clicked.connect(lambda _checked=False, item=preset: self._apply_style_preset(item))
            self.style_preset_buttons[preset.key] = button
            preset_row.addWidget(button)
        preset_row.addStretch(1)
        layout.addLayout(preset_row)

        # 样式细节折叠区
        self.style_toggle = QPushButton("字体与效果")
        self.style_toggle.setIcon(icon("chevron_right"))
        self.style_toggle.setIconSize(QSize(14, 14))
        self.style_toggle.setFixedHeight(26)
        self.style_toggle.setObjectName("quiet")
        self.style_toggle.setCheckable(True)
        layout.addWidget(self.style_toggle)
        self.style_widget = QWidget()
        style_outer = QVBoxLayout(self.style_widget)
        style_outer.setContentsMargins(8, 0, 0, 0)
        style_outer.setSpacing(4)
        style_form = QFormLayout()
        style_form.setContentsMargins(0, 0, 0, 0)
        style_form.setSpacing(4)
        style_outer.addLayout(style_form)
        font_row = QHBoxLayout()
        self.font_path_edit = QLineEdit()
        self.font_path_edit.setReadOnly(True)
        self.font_path_edit.setPlaceholderText("默认字体")
        font_row.addWidget(self.font_path_edit, 1)
        self.font_button = QPushButton("…")
        self.font_button.setFixedWidth(24)
        self.font_button.clicked.connect(self._pick_font)
        font_row.addWidget(self.font_button)
        style_form.addRow("字体", font_row)
        self.fill_color_button = _ColorButton()
        self.fill_color_button.color_changed.connect(self._draft_changed)
        style_form.addRow("填充", self.fill_color_button)
        stroke_row = QHBoxLayout()
        stroke_row.setSpacing(4)
        self.stroke_button = _ColorButton()
        self.stroke_button.color_changed.connect(self._draft_changed)
        self.stroke_spin = QSpinBox()
        self.stroke_spin.setRange(0, 64)
        self.stroke_spin.setToolTip("描边宽度")
        self.stroke_spin.valueChanged.connect(self._draft_changed)
        stroke_row.addWidget(self.stroke_button, 1)
        stroke_row.addWidget(self.stroke_spin)
        style_form.addRow("描边", stroke_row)
        outer_row = QHBoxLayout()
        outer_row.setSpacing(4)
        self.outer_stroke_button = _ColorButton(allow_none=True)
        self.outer_stroke_button.setToolTip("在描边外再加一层，常用白色外边")
        self.outer_stroke_button.color_changed.connect(self._draft_changed)
        self.outer_stroke_spin = QSpinBox()
        self.outer_stroke_spin.setRange(0, 48)
        self.outer_stroke_spin.setToolTip("外描边宽度")
        self.outer_stroke_spin.valueChanged.connect(self._draft_changed)
        outer_row.addWidget(self.outer_stroke_button, 1)
        outer_row.addWidget(self.outer_stroke_spin)
        style_form.addRow("外描边", outer_row)
        self.backdrop_button = _ColorButton(allow_none=True, allow_alpha=True)
        self.backdrop_button.setToolTip("文字底条，画面杂乱时提高可读性；可设透明度")
        self.backdrop_button.color_changed.connect(self._draft_changed)
        style_form.addRow("底条", self.backdrop_button)
        self.line_spacing_spin = QDoubleSpinBox()
        self.line_spacing_spin.setRange(0.5, 3.0)
        self.line_spacing_spin.setSingleStep(0.05)
        self.line_spacing_spin.setDecimals(2)
        self.line_spacing_spin.valueChanged.connect(self._draft_changed)
        style_form.addRow("行距", self.line_spacing_spin)
        self.shadow_check = QCheckBox("阴影")
        self.shadow_check.stateChanged.connect(self._draft_changed)
        style_form.addRow("", self.shadow_check)
        self.style_widget.setVisible(False)
        self.style_toggle.toggled.connect(self.style_widget.setVisible)
        self.style_toggle.toggled.connect(
            lambda checked: self.style_toggle.setIcon(icon("chevron_down" if checked else "chevron_right"))
        )
        layout.addWidget(self.style_widget)

        layout.addStretch(1)
        return panel

    def _build_asset_panel(self) -> QWidget:
        """素材/形状对象属性面板。"""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # 对象操作
        ops_row = QHBoxLayout()
        ops_row.setSpacing(4)
        self.duplicate_asset_button = QPushButton("复制")
        self.duplicate_asset_button.setFixedHeight(26)
        self.duplicate_asset_button.clicked.connect(self._duplicate_selected_object)
        ops_row.addWidget(self.duplicate_asset_button)
        self.delete_asset_button = QPushButton("删除")
        self.delete_asset_button.setFixedHeight(26)
        self.delete_asset_button.clicked.connect(self._delete_selected_object)
        ops_row.addWidget(self.delete_asset_button)
        self.raise_asset_button = QPushButton("前移")
        self.raise_asset_button.setFixedHeight(26)
        self.raise_asset_button.clicked.connect(lambda: self._move_selected_layer(1))
        ops_row.addWidget(self.raise_asset_button)
        self.lower_asset_button = QPushButton("后移")
        self.lower_asset_button.setFixedHeight(26)
        self.lower_asset_button.clicked.connect(lambda: self._move_selected_layer(-1))
        ops_row.addWidget(self.lower_asset_button)
        layout.addLayout(ops_row)

        # 变换
        transform_form = QFormLayout()
        transform_form.setSpacing(4)
        self.overlay_scale_spin = QDoubleSpinBox()
        self.overlay_scale_spin.setRange(0.05, 4.0)
        self.overlay_scale_spin.setSingleStep(0.05)
        self.overlay_scale_spin.setDecimals(2)
        self.overlay_scale_spin.setSuffix("×")
        self.overlay_scale_spin.valueChanged.connect(self._store_overlay_controls)
        transform_form.addRow("缩放", self.overlay_scale_spin)
        self.overlay_rotation_spin = QDoubleSpinBox()
        self.overlay_rotation_spin.setRange(-180.0, 180.0)
        self.overlay_rotation_spin.setSingleStep(1.0)
        self.overlay_rotation_spin.setDecimals(0)
        self.overlay_rotation_spin.setSuffix("°")
        self.overlay_rotation_spin.valueChanged.connect(self._store_overlay_controls)
        transform_form.addRow("旋转", self.overlay_rotation_spin)
        layout.addLayout(transform_form)

        # 添加新素材入口
        layout.addSpacing(4)
        add_row = QHBoxLayout()
        add_row.setSpacing(4)
        self.import_asset_button2 = QPushButton("导入")
        self.import_asset_button2.setFixedHeight(26)
        self.import_asset_button2.setObjectName("quiet")
        self.import_asset_button2.setToolTip("导入图片素材")
        self.import_asset_button2.clicked.connect(self._import_asset)
        add_row.addWidget(self.import_asset_button2)
        self.add_shape_button2 = QPushButton("形状")
        self.add_shape_button2.setFixedHeight(26)
        self.add_shape_button2.setObjectName("quiet")
        self.add_shape_button2.setToolTip("添加圆圈/箭头/矩形")
        self.add_shape_button2.setMenu(self._shape_menu(self.add_shape_button2))
        add_row.addWidget(self.add_shape_button2)
        layout.addLayout(add_row)

        layout.addStretch(1)
        return panel

    def _build_bg_panel(self) -> QWidget:
        """背景取景面板。"""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        zoom_row = QHBoxLayout()
        zoom_row.addWidget(QLabel("缩放"))
        self.zoom_spin = QDoubleSpinBox()
        self.zoom_spin.setRange(BACKGROUND_SCALE_MIN, BACKGROUND_SCALE_MAX)
        self.zoom_spin.setSingleStep(0.1)
        self.zoom_spin.setDecimals(1)
        self.zoom_spin.setSuffix(" ×")
        self.zoom_spin.setFixedHeight(26)
        self.zoom_spin.valueChanged.connect(self._zoom_changed)
        zoom_row.addWidget(self.zoom_spin, 1)
        layout.addLayout(zoom_row)

        fit_row = QHBoxLayout()
        fit_row.setSpacing(4)
        self.fill_button = QPushButton("充满")
        self.fill_button.setFixedHeight(26)
        self.fill_button.clicked.connect(self._fill_canvas)
        fit_row.addWidget(self.fill_button)
        self.fit_button = QPushButton("适应")
        self.fit_button.setFixedHeight(26)
        self.fit_button.clicked.connect(self._fit_canvas)
        fit_row.addWidget(self.fit_button)
        layout.addLayout(fit_row)

        # 取帧操作
        layout.addSpacing(4)
        extract_row = QHBoxLayout()
        extract_row.setSpacing(4)
        self.extract_button = QPushButton("取帧")
        self.extract_button.setFixedHeight(26)
        self.extract_button.setObjectName("quiet")
        self.extract_button.setToolTip("从当前视频指定时间取帧")
        self.extract_button.clicked.connect(self._extract_frame)
        extract_row.addWidget(self.extract_button)
        self.import_bg_button = QPushButton("导入底图")
        self.import_bg_button.setFixedHeight(26)
        self.import_bg_button.setObjectName("quiet")
        self.import_bg_button.clicked.connect(self._import_image)
        extract_row.addWidget(self.import_bg_button)
        layout.addLayout(extract_row)

        # 精确时间
        time_row = QHBoxLayout()
        time_row.addWidget(QLabel("时间"))
        self.timestamp_edit = QDoubleSpinBox()
        self.timestamp_edit.setRange(0.0, 24 * 60 * 60)
        self.timestamp_edit.setDecimals(2)
        self.timestamp_edit.setSuffix(" 秒")
        self.timestamp_edit.setFixedHeight(26)
        time_row.addWidget(self.timestamp_edit, 1)
        layout.addLayout(time_row)

        layout.addStretch(1)
        return panel

    def _build_empty_panel(self) -> QWidget:
        """无项目/无选中时的默认面板：素材入口 + AI。"""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        hint = QLabel("选择项目后，点击画布中的文字或素材进行编辑")
        hint.setObjectName("subtle")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        layout.addSpacing(8)

        # 素材入口
        assets_label = QLabel("素材")
        assets_label.setObjectName("subtle")
        layout.addWidget(assets_label)
        asset_row = QHBoxLayout()
        asset_row.setSpacing(4)
        self.import_asset_button = QPushButton("导入")
        self.import_asset_button.setFixedHeight(26)
        self.import_asset_button.setToolTip("导入图片素材")
        self.import_asset_button.clicked.connect(self._import_asset)
        asset_row.addWidget(self.import_asset_button)
        self.browse_asset_button = QPushButton("素材库")
        self.browse_asset_button.setFixedHeight(26)
        self.browse_asset_button.setToolTip("浏览本地素材")
        self.browse_asset_button.clicked.connect(self._browse_assets)
        asset_row.addWidget(self.browse_asset_button)
        self.add_shape_button = QPushButton("形状")
        self.add_shape_button.setFixedHeight(26)
        self.add_shape_button.setToolTip("添加圆圈/箭头/矩形")
        self.add_shape_button.setMenu(self._shape_menu(self.add_shape_button))
        asset_row.addWidget(self.add_shape_button)
        layout.addLayout(asset_row)

        layout.addStretch(1)
        return panel

    def _canvas_selection_changed(self, title_selected: bool):
        """根据画布选择切换右侧上下文面板。"""

        title_selected = bool(title_selected)
        if not title_selected:
            self._update_canvas_hint()
        if self.document is not None:
            if title_selected:
                text_ids = [item.id for item in self.document.objects if isinstance(item, TextObject)]
                selected = self._selected_text_id if self._selected_text_id in text_ids else next(
                    (item.id for item in self.document.objects if isinstance(item, TextObject) and item.copy_role == "B"),
                    text_ids[0] if text_ids else None,
                )
                self._selected_text_id = selected
            else:
                selected = next((item.id for item in self.document.objects if isinstance(item, BackgroundObject)), None)
            self.document = replace(self.document, selected_object_id=selected)
        if hasattr(self, "canvas"):
            self.canvas.set_selected_object(
                self._selected_text_id if title_selected and self._selected_text_id else "background"
            )
        # 切换右侧面板
        if title_selected:
            self.panel_title.setText("封面文案")
            self.panel_stack.setCurrentWidget(self.copy_controls)
            self._sync_selected_text_controls()
        else:
            self.panel_title.setText("底图取景")
            self.panel_stack.setCurrentWidget(self.bg_controls)
        self._sync_overlay_controls()

    def _selected_text(self) -> TextObject | None:
        if self.document is None:
            return None
        texts = [item for item in self.document.objects if isinstance(item, TextObject)]
        selected_id = self._selected_text_id
        candidate = next((item for item in texts if item.id == selected_id), None)
        if candidate is None:
            candidate = next((item for item in texts if item.copy_role == "B"), None) or (texts[0] if texts else None)
            self._selected_text_id = candidate.id if candidate else None
        if candidate is None:
            return None
        effective = object_for_profile(self.document, candidate.id, self._canvas_key)
        return effective if isinstance(effective, TextObject) else candidate

    def _sync_selected_text_controls(self):
        text = self._selected_text()
        if text is None:
            return
        self.title_edit.blockSignals(True)
        self.font_spin.blockSignals(True)
        for widget in (self.font_path_edit, self.stroke_spin, self.outer_stroke_spin, self.line_spacing_spin, self.shadow_check):
            widget.blockSignals(True)
        try:
            self.title_edit.setPlainText(text.text)
            font_resolution = resolve_font_selection(text.style.font_family)
            selected_font_path = Path(text.style.font_family) if text.style.font_family else None
            self.font_path_edit.setText(
                str(selected_font_path.resolve())
                if selected_font_path is not None and selected_font_path.is_file()
                else ""
            )
            self.font_path_edit.setToolTip(
                f"实际字体：{font_resolution.family}"
                + (f" · {font_resolution.path}" if font_resolution.path else "")
                + (f" · {font_resolution.warning}" if font_resolution.warning else "")
            )
            self.font_spin.setValue(int(text.style.font_size))
            self.fill_color_button.set_color(text.style.fill_color)
            self.stroke_button.set_color(text.style.stroke_color)
            self.stroke_spin.setValue(int(text.style.stroke_width))
            self.outer_stroke_button.set_color(text.style.outer_stroke)
            self.outer_stroke_spin.setValue(int(text.style.outer_stroke_width))
            self.backdrop_button.set_color(text.style.backdrop)
            self.line_spacing_spin.setValue(float(text.style.line_spacing))
            self.shadow_check.setChecked(bool(text.style.shadow))
            self._update_role_label(text)
            for key, button in self.align_buttons.items():
                button.blockSignals(True)
                button.setChecked(key == text.align)
                button.blockSignals(False)
        finally:
            self.title_edit.blockSignals(False)
            self.font_spin.blockSignals(False)
            for widget in (self.font_path_edit, self.stroke_spin, self.outer_stroke_spin, self.line_spacing_spin, self.shadow_check):
                widget.blockSignals(False)

    def _canvas_object_selected(self, object_id: str):
        if self.document is None:
            return
        selected = next((item for item in self.document.objects if item.id == object_id), None)
        if isinstance(selected, TextObject):
            self._selected_text_id = selected.id
            self.document = replace(self.document, selected_object_id=selected.id)
            self._canvas_selection_changed(True)
        elif isinstance(selected, BackgroundObject):
            self.document = replace(self.document, selected_object_id=selected.id)
            self._canvas_selection_changed(False)
        elif isinstance(selected, (ImageObject, StickerObject, ShapeObject)):
            self.document = replace(self.document, selected_object_id=selected.id)
            self.panel_title.setText("素材")
            self.panel_stack.setCurrentWidget(self.asset_controls)
            self.canvas.set_selected_object(selected.id)
            self._sync_overlay_controls()

    def _select_copy_role(self, role: str):
        if self.document is None or role not in {"A", "B"}:
            return
        selected = next((item for item in self.document.objects if isinstance(item, TextObject) and item.copy_role == role), None)
        if selected is None:
            return
        self._selected_text_id = selected.id
        self.document = replace(self.document, selected_object_id=selected.id)
        self.canvas.set_selected_object(selected.id)
        self._sync_selected_text_controls()

    def _commit_document_change(self, before: CoverDocument | None = None) -> None:
        """原子提交文档，并让所有界面和异步任务转向同一份状态。"""

        if self.document is None:
            return
        self.draft = CoverDraft.from_document(self.document)
        # CoverDocument 是唯一主状态。候选、撤销和手势提交后先把控件重读
        # 为新文档，再启动任何延迟任务，避免旧控件反向覆盖 profile override。
        self._apply_draft()
        self._sync_overlay_controls()
        self.history.commit(self.document)
        self.undo_button.setEnabled(self.history.can_undo)
        self.redo_button.setEnabled(self.history.can_redo)
        self._invalidate_render_requests()
        self._draft_timer.start()
        self._preview_timer.start()

    def _invalidate_render_requests(self) -> None:
        """使正在运行的预览回调失效，并允许新状态立即排队。"""

        self._preview_request_generation += 1
        self._busy = False
        self._preview_dirty = False

    def _undo(self):
        document = self.history.undo()
        if document is None:
            return
        self.document = document
        self.draft = CoverDraft.from_document(self.document)
        self._apply_draft()
        self._sync_overlay_controls()
        self._invalidate_render_requests()
        self.undo_button.setEnabled(self.history.can_undo)
        self.redo_button.setEnabled(self.history.can_redo)
        self._draft_timer.start()
        self._preview_timer.start()

    def _redo(self):
        document = self.history.redo()
        if document is None:
            return
        self.document = document
        self.draft = CoverDraft.from_document(self.document)
        self._apply_draft()
        self._sync_overlay_controls()
        self._invalidate_render_requests()
        self.undo_button.setEnabled(self.history.can_undo)
        self.redo_button.setEnabled(self.history.can_redo)
        self._draft_timer.start()
        self._preview_timer.start()

    def _import_asset(self):
        source, _ = QFileDialog.getOpenFileName(self, "选择封面素材", "", "图片 (*.png *.jpg *.jpeg *.webp *.bmp)")
        if not source or self.document is None:
            return
        try:
            asset = self.service.asset_library.import_file(source)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "无法导入素材", str(exc))
            return
        self._insert_overlay(ImageObject(
            id=self._new_object_id("image"),
            asset=AssetRef(path=asset.path, asset_id=asset.asset_id),
            transform=Transform(x=0.62, y=0.54, scale=0.85, rotation=0.0),
        ))
        self.service.asset_library.mark_used(asset.asset_id)

    def _browse_assets(self):
        if self.document is None:
            return
        try:
            assets = self.service.asset_library.list_assets(preferred_group=self.project.title if self.project else None)
        except (OSError, ValueError) as exc:
            message = f"素材库暂不可用：{exc}"
            self._set_notice(message, "error")
            self.status_changed.emit(message)
            return
        if not assets:
            message = "素材库为空；可以先导入一张自定义图片"
            self._set_notice(message, "warning")
            self.status_changed.emit(message)
            return
        dialog = CoverAssetDialog(assets, self)
        if dialog.exec() and (asset := dialog.selected_asset()) is not None:
            self._insert_asset_object(asset)

    def _new_object_id(self, prefix: str) -> str:
        existing = {item.id for item in self.document.objects} if self.document else set()
        index = len(existing) + 1
        while f"{prefix}-{index}" in existing:
            index += 1
        return f"{prefix}-{index}"

    def _insert_overlay(self, overlay) -> None:
        """新对象插在其他素材之上、文字之下，并立即选中。"""

        before = self.document
        self.document = insert_overlay(self.document, overlay)
        self._selected_text_id = None
        self.canvas.set_selected_object(overlay.id)
        self._commit_document_change(before)
        self._canvas_object_selected(overlay.id)

    def _insert_asset_object(self, asset):
        if self.document is None:
            return
        overlay_type = StickerObject if asset.group != "我的导入" else ImageObject
        overlay_kwargs = {
            "id": self._new_object_id("image"),
            "asset": AssetRef(path=asset.path, asset_id=asset.asset_id),
            "transform": Transform(x=0.62, y=0.54, scale=0.85),
        }
        if overlay_type is StickerObject:
            overlay_kwargs["category"] = asset.group
        self._insert_overlay(overlay_type(**overlay_kwargs))
        self.service.asset_library.mark_used(asset.asset_id)

    def _shape_menu(self, parent) -> QMenu:
        menu = QMenu(parent)
        for shape_type, label in (("circle", "圆圈"), ("arrow", "箭头"), ("rect", "矩形框")):
            menu.addAction(label, lambda value=shape_type: self._add_shape(value))
        return menu

    def _add_shape(self, shape_type: str = "circle"):
        if self.document is None or shape_type not in {"circle", "arrow", "rect"}:
            return
        self._insert_overlay(ShapeObject(
            id=self._new_object_id("shape"), shape_type=shape_type,
            transform=Transform(x=0.58, y=0.44, scale=1.0),
        ))
        label = {"circle": "圆圈", "arrow": "箭头", "rect": "矩形框"}[shape_type]
        self.status_changed.emit(f"已添加{label}，可直接在画布上拖动")

    def _primary_copy_ids(self) -> dict[str, str]:
        """每个角色的第一个文本框是 A/B 主文案；复制或新建的不参与换一版。"""

        return primary_copy_ids(self.document) if self.document else {}

    def _update_role_label(self, text: TextObject | None) -> None:
        if text is None:
            self.copy_role_label.setText("")
        elif self._primary_copy_ids().get(text.copy_role) != text.id:
            self.copy_role_label.setText("自建文本框")
        else:
            self.copy_role_label.setText("主文案 B · 大字" if text.copy_role == "B" else "上下文 A · 小字")

    def _add_text(self):
        """新建文本框：沿用主文案样式放在画面中部，选中后直接输入文字。"""

        if self.document is None:
            return
        before = self.document
        source_id = self._primary_copy_ids().get("B")
        source = object_for_profile(self.document, source_id, self._canvas_key) if source_id else None
        style = source.style if isinstance(source, TextObject) else None
        new_id = self._new_object_id("text")
        width = 0.6
        text = TextObject(
            id=new_id,
            copy_role="B",
            text="双击修改文字",
            z_index=max((item.z_index for item in self.document.objects), default=0) + 1,
            transform=Transform(x=0.2, y=0.42),
            rect=Rect(width=width, height=0.16),
            wrap=TextWrap(max_width=width, max_lines=8),
            align="center",
        )
        if style is not None:
            text = replace(text, style=resize_text_style(style, max(48, min(120, round(style.font_size * 0.8)))))
        self.document = replace(self.document, objects=(*self.document.objects, text), selected_object_id=new_id)
        self._selected_text_id = new_id
        self.canvas.set_selected_object(new_id)
        self._commit_document_change(before)
        self._edit_text(new_id)
        self.status_changed.emit("已新建文本框：直接输入文字，拖四角缩放")

    def _edit_text(self, object_id: str):
        """双击文字：右侧文案框获得焦点并全选，直接输入即可替换。"""

        if self.document is None:
            return
        self._canvas_object_selected(object_id)
        if not self._panel_visible:
            self.panel_toggle.setChecked(False)
            self._show_panel()
        self.title_edit.setFocus(Qt.FocusReason.OtherFocusReason)
        self.title_edit.selectAll()

    def _clear_schemes(self, caption: str = "—"):
        self._scheme_generation += 1
        self._schemes = ()
        for button, label in zip(self.scheme_buttons, self.scheme_labels):
            button.setIcon(QIcon())
            button.setChecked(False)
            button.setEnabled(False)
            label.setText(caption)
        self.scheme_refresh_button.setEnabled(False)

    def _refresh_schemes(self, *, batch: int | None = None):
        """后台生成三套方案和缩略图；只出缩略图，不改当前封面。"""

        if self.document is None or not self.draft.image_path:
            self._clear_schemes()
            return
        if batch is not None:
            self._scheme_batch = max(0, int(batch))
        self._clear_schemes("生成中…")
        generation = self._scheme_generation
        document, image_path = self.document, self.draft.image_path
        variants, scheme_batch, service = self._copy_variants, self._scheme_batch, self.service

        def build():
            schemes = service.layout_schemes(document, image_path, variants, batch=scheme_batch)
            return schemes, tuple(service.scheme_thumbnail(item.document, width=156) for item in schemes)

        self._run(build, lambda result, error: self._schemes_ready(generation, result, error))

    def _schemes_ready(self, generation: int, result, error):
        if generation != self._scheme_generation:
            return
        self.scheme_refresh_button.setEnabled(self.document is not None)
        if error or not result:
            for label in self.scheme_labels:
                label.setText("—")
            if error:
                self.status_changed.emit(f"方案生成失败：{error}")
            return
        schemes, thumbnails = result
        self._schemes = tuple(schemes)
        for index, (button, label) in enumerate(zip(self.scheme_buttons, self.scheme_labels)):
            if index >= len(self._schemes):
                continue
            scheme = self._schemes[index]
            pixmap = QPixmap()
            pixmap.loadFromData(thumbnails[index])
            button.setIcon(QIcon(pixmap))
            button.setEnabled(True)
            button.setToolTip(f"{scheme.label}：{scheme.reason}\n点击套用，Ctrl+Z 可撤销")
            label.setText(scheme.label)

    def _apply_scheme(self, index: int):
        if self.document is None or not 0 <= index < len(self._schemes):
            return
        scheme = self._schemes[index]
        before = self.document
        self.document = self.service.apply_scheme(self.document, scheme)
        for position, button in enumerate(self.scheme_buttons):
            button.setChecked(position == index)
        if self.document != before:
            self._commit_document_change(before)
            self.canvas.set_document(self.document, self._canvas_key)
            self._sync_selected_text_controls()
        self.status_changed.emit(f"已套用方案：{scheme.label}（Ctrl+Z 可撤销）")

    def _next_scheme_batch(self):
        self._refresh_schemes(batch=self._scheme_batch + 1)

    def _selected_render_object(self):
        if self.document is None:
            return None
        object_id = self.document.selected_object_id
        return next((item for item in self.document.objects if item.id == object_id), None)

    def _sync_overlay_controls(self):
        """同步当前图片、贴图或强调形状的缩放和旋转控件。"""

        selected = self._selected_render_object()
        if self.document is not None and selected is not None:
            effective = object_for_profile(self.document, selected.id, self._canvas_key)
            if isinstance(effective, (ImageObject, StickerObject, ShapeObject)):
                selected = effective
        enabled = isinstance(selected, (ImageObject, StickerObject, ShapeObject))
        for widget in (self.overlay_scale_spin, self.overlay_rotation_spin):
            widget.blockSignals(True)
            widget.setEnabled(enabled)
        try:
            if enabled:
                self.overlay_scale_spin.setValue(float(selected.transform.scale))
                self.overlay_rotation_spin.setValue(float(selected.transform.rotation))
            else:
                self.overlay_scale_spin.setValue(1.0)
                self.overlay_rotation_spin.setValue(0.0)
        finally:
            for widget in (self.overlay_scale_spin, self.overlay_rotation_spin):
                widget.blockSignals(False)

    def _store_overlay_controls(self):
        """把对象缩放/旋转写入当前比例的 profile override。"""

        if self.document is None:
            return
        selected = self._selected_render_object()
        if not isinstance(selected, (ImageObject, StickerObject, ShapeObject)):
            self._sync_overlay_controls()
            return
        current = object_for_profile(self.document, selected.id, self._canvas_key)
        if not isinstance(current, (ImageObject, StickerObject, ShapeObject)):
            current = selected
        updated = replace(
            current,
            transform=replace(
                current.transform,
                scale=self.overlay_scale_spin.value(),
                rotation=self.overlay_rotation_spin.value(),
            ),
        )
        if updated == current:
            return
        profile = self.document.profiles[self._canvas_key]
        payload = {"transform": updated.transform.to_payload(), "visible": bool(updated.visible)}
        if isinstance(updated, (ImageObject, StickerObject)):
            payload["opacity"] = updated.opacity
        else:
            payload.update({
                "shape_type": updated.shape_type,
                "fill": updated.fill,
                "stroke": updated.stroke,
                "stroke_width": updated.stroke_width,
                "width": updated.width,
                "height": updated.height,
            })
        self.document = replace(
            self.document,
            active_profile=self._canvas_key,
            profiles={
                **self.document.profiles,
                self._canvas_key: replace(profile, overrides={**profile.overrides, updated.id: payload}),
            },
        )
        self.history.commit(self.document)
        self.undo_button.setEnabled(self.history.can_undo)
        self.redo_button.setEnabled(self.history.can_redo)
        self.canvas.set_document(self.document, self._canvas_key)
        self.draft = CoverDraft.from_document(self.document)
        self._invalidate_render_requests()
        self._draft_timer.start()
        self._preview_timer.start()

    def _delete_object(self, object_id: str):
        """选中框左上角的删除按钮。"""

        if self.document is None:
            return
        self.document = replace(self.document, selected_object_id=object_id)
        self._delete_selected_object()
        background = next((item for item in self.document.objects if isinstance(item, BackgroundObject)), None)
        if background is not None:
            self._canvas_object_selected(background.id)

    def _duplicate_object(self, object_id: str):
        """选中框左下角的复制按钮：文字复制成新的独立文本框。"""

        if self.document is None:
            return
        source = next((item for item in self.document.objects if item.id == object_id), None)
        if isinstance(source, TextObject):
            self._duplicate_text(source)
            return
        self.document = replace(self.document, selected_object_id=object_id)
        self._duplicate_selected_object()

    def _duplicate_text(self, source: TextObject):
        before = self.document
        new_id = self._new_object_id("text")
        z_index = max((item.z_index for item in self.document.objects), default=0) + 1
        profiles = {}
        for key, profile in self.document.profiles.items():
            effective = object_for_profile(self.document, source.id, key)
            effective = effective if isinstance(effective, TextObject) else source
            shifted = replace(
                effective, id=new_id, z_index=z_index,
                transform=replace(effective.transform, x=min(0.92, effective.transform.x + 0.03), y=min(0.92, effective.transform.y + 0.04)),
            )
            profiles[key] = replace(profile, overrides={**profile.overrides, new_id: text_override_payload(shifted)})
        base = object_for_profile(self.document, source.id, self._canvas_key)
        base = base if isinstance(base, TextObject) else source
        duplicate = replace(
            base, id=new_id, z_index=z_index, visible=True,
            transform=replace(base.transform, x=min(0.92, base.transform.x + 0.03), y=min(0.92, base.transform.y + 0.04)),
        )
        self.document = replace(
            self.document,
            objects=(*self.document.objects, duplicate),
            profiles=profiles,
            selected_object_id=new_id,
        )
        self._selected_text_id = new_id
        self.canvas.set_selected_object(new_id)
        self._commit_document_change(before)
        self._canvas_object_selected(new_id)
        self.status_changed.emit("已复制文本框，可直接拖动到新位置")

    def _duplicate_selected_object(self):
        from dataclasses import replace as dc_replace
        item = self._selected_render_object()
        if self.document is None or not isinstance(item, (ImageObject, StickerObject, ShapeObject)):
            self.status_changed.emit("请先在画布上选中图片或强调对象")
            return
        duplicate = dc_replace(
            item, id=self._new_object_id(item.kind),
            transform=dc_replace(item.transform, x=min(0.92, item.transform.x + 0.04), y=min(0.92, item.transform.y + 0.04)),
        )
        self._insert_overlay(duplicate)

    def _delete_selected_object(self):
        item = self._selected_render_object()
        if self.document is None or item is None or item.kind == "background":
            return
        before = self.document
        if isinstance(item, TextObject) and item.id in self._primary_copy_ids().values():
            # A/B 主文案两个比例一起隐藏，换一版或重新填字后还能回来。
            self.document = replace(set_object_visible(self.document, item.id, False), selected_object_id=None)
        elif isinstance(item, TextObject):
            # 复制出来的文本框直接移除，连同比例覆盖。
            profiles = {
                key: replace(profile, overrides={k: v for k, v in profile.overrides.items() if k != item.id})
                for key, profile in self.document.profiles.items()
            }
            self.document = replace(
                self.document,
                objects=tuple(obj for obj in self.document.objects if obj.id != item.id),
                profiles=profiles,
                selected_object_id=None,
            )
        else:
            self.document = replace(self.document, objects=tuple(obj for obj in self.document.objects if obj.id != item.id), selected_object_id=None)
        self._commit_document_change(before)

    def _move_selected_layer(self, delta: int):
        item = self._selected_render_object()
        if self.document is None or item is None:
            return
        before = self.document
        # 与相邻对象交换次序，一次点击就能越过文字等相邻层。
        self.document = restack_object(self.document, item.id, delta)
        if self.document != before:
            self._commit_document_change(before)

    def _canvas_object_changed(self, item, profile_key: str):
        """接收手势提交后的对象变换，保持 CoverDocument 为唯一主状态。"""

        if self.document is None or not hasattr(item, "id"):
            return
        profile = self.document.profiles.get(profile_key)
        if profile is None:
            return
        payload = {"transform": item.transform.to_payload(), "visible": bool(getattr(item, "visible", True))}
        if isinstance(item, BackgroundObject):
            payload.update({"scale": item.scale, "pan_x": item.pan_x, "pan_y": item.pan_y, "fit_mode": item.fit_mode})
        elif isinstance(item, TextObject):
            payload.update({"rect": item.rect.to_payload(), "wrap": item.wrap.to_payload(), "align": item.align, "style": item.style.to_payload()})
        elif isinstance(item, (ImageObject, StickerObject)):
            payload.update({"opacity": item.opacity})
        elif isinstance(item, ShapeObject):
            payload.update({"shape_type": item.shape_type, "fill": item.fill, "stroke": item.stroke, "stroke_width": item.stroke_width, "width": item.width, "height": item.height})
        self.document = replace(
            self.document,
            active_profile=profile_key,
            selected_object_id=item.id,
            profiles={
                **self.document.profiles,
                profile_key: replace(profile, overrides={**profile.overrides, item.id: payload}),
            },
        )
        if not getattr(item, "visible", True):
            if isinstance(item, TextObject):
                self.document = set_object_visible(self.document, item.id, False)
            elif isinstance(item, (ImageObject, StickerObject, ShapeObject)):
                self.document = replace(
                    self.document,
                    objects=tuple(obj for obj in self.document.objects if obj.id != item.id),
                    selected_object_id=None,
                )
        self.draft = CoverDraft.from_document(self.document)
        self.history.commit(self.document)
        self.undo_button.setEnabled(self.history.can_undo)
        self.redo_button.setEnabled(self.history.can_redo)
        self.canvas.set_document(self.document, self._canvas_key)
        if isinstance(item, TextObject):
            self._selected_text_id = item.id
            # Canvas 手势先发 object_changed、再发位置兼容信号；先同步字号，
            # 避免后续旧兼容入口用右侧面板的旧值覆盖画布刚调整的字号。
            self.font_spin.blockSignals(True)
            try:
                self.font_spin.setValue(int(item.style.font_size))
            finally:
                self.font_spin.blockSignals(False)
            for key, button in self.align_buttons.items():
                button.blockSignals(True)
                button.setChecked(key == item.align)
                button.blockSignals(False)
            self._update_title_rect()
        elif isinstance(item, (ImageObject, StickerObject, ShapeObject)):
            self._sync_overlay_controls()
        self._draft_timer.start()
        self._invalidate_render_requests()
        self._preview_timer.start()

    def _canvas_object_preview_changed(self, item, _profile_key: str):
        """只更新轻量控件显示；拖动帧不得写文档、保存或渲染。"""

        if isinstance(item, TextObject):
            self._selected_text_id = item.id
            self.font_spin.blockSignals(True)
            try:
                self.font_spin.setValue(int(item.style.font_size))
            finally:
                self.font_spin.blockSignals(False)
            for key, button in self.align_buttons.items():
                button.blockSignals(True)
                button.setChecked(key == item.align)
                button.blockSignals(False)
        elif isinstance(item, BackgroundObject):
            self.zoom_spin.blockSignals(True)
            try:
                self.zoom_spin.setValue(float(item.scale))
            finally:
                self.zoom_spin.blockSignals(False)

    def _canvas_safety_changed(self, warning: bool):
        if warning:
            self.canvas_hint.setText(
                f"{self._canvas_label()} · 标题靠近画布边缘，仍可继续拖动；滚轮缩放"
            )
        else:
            self._update_canvas_hint()

    def _canvas_label(self) -> str:
        return "4:3" if self._canvas_key == "4x3" else "16:9"

    def _update_canvas_hint(self):
        if self.project is None or self.video is None:
            self.canvas_hint.setText("先到“字幕”页选择投稿项目和视频")
            return
        self.canvas_hint.setText(
            f"{self._canvas_label()} 主画布 · 拖动文字或底图；四角缩放/旋转，两侧改行宽；双击改字；滚轮缩放底图"
        )

    def _set_canvas_key(self, canvas_key: str):
        if canvas_key not in {"4x3", "16x9"} or canvas_key == self._canvas_key:
            return
        if self.document is not None:
            self.document = replace(self.document, active_profile=canvas_key)
            self.draft = CoverDraft.from_document(self.document)
            self._draft_timer.start()
        self._canvas_key = canvas_key
        # 旧比例的后台渲染结果只丢弃，不阻塞新比例的主画布。
        self._invalidate_render_requests()
        for key, button in self.canvas_ratio_buttons.items():
            button.setChecked(key == canvas_key)
        self._update_canvas_hint()
        self._update_export_summary()
        if self.document is not None:
            self._apply_draft()
            self._sync_overlay_controls()
        if self.draft.image_path and self.video is not None:
            self.canvas.setText(f"正在准备 {self._canvas_label()} 预览…")
            self._render_preview()
            return
        self.canvas.setText(f"加载底图后在这里预览 {self._canvas_label()} 画布")

    def _update_export_summary(self):
        """把当前输出契约放在导出动作旁，tooltip 只补充完整路径。"""

        if self.video is None:
            self.export_summary.setText("未选择视频 · 输出到项目目录")
            self.export_summary.setToolTip("选择投稿项目和视频后显示封面文件名与完整输出路径")
            if hasattr(self, "export_button"):
                self.export_button.setText("导出 4:3")
                self.export_button.setToolTip("导出 4:3（1440×1080 JPG）")
            return
        stem = Path(self.video.name).stem or "封面"
        if self._canvas_key == "4x3":
            filename = f"AutoCover-{stem}.jpg"
            size = "1440×1080"
        else:
            filename = f"AutoCover-{stem}-16x9.jpg"
            size = "1920×1080"
        output_dir = Path(self.project.directory) if self.project is not None else Path(self.video.path).parent
        self.export_summary.setText(
            f"{self._canvas_label()} · {size} · 项目目录 · {filename}"
        )
        self.export_summary.setToolTip(str(output_dir / filename))
        if hasattr(self, "export_button"):
            self.export_button.setText(f"导出 {self._canvas_label()}")
            self.export_button.setToolTip(
                f"导出 {self._canvas_label()}（{size} JPG）到项目目录，不覆盖已有文件"
            )

    @staticmethod
    def _nearby_offsets(center: float) -> tuple[float, ...]:
        """附近 7 帧的偏移；片头不足 1.2 秒时整体后移，避免重复的 0 秒帧。"""

        before = min(3, int(max(0.0, float(center or 0.0)) / 0.4 + 1e-6))
        return tuple(round((index - before) * 0.4, 2) for index in range(7))

    def _refresh_nearby_frame_strip(self, center: float):
        center = max(0.0, float(center or 0.0))
        self.frame_center_label.setText(f"当前 {center:.2f} 秒")
        offsets = self._nearby_offsets(center)
        available = self.video is not None and Path(self.video.path).is_file()
        for button, offset in zip(self.nearby_frame_buttons, offsets):
            timestamp = max(0.0, center + offset)
            button.setProperty("timestamp", timestamp)
            button.setText(f"{timestamp:.2f}s")
            button.setToolTip(f"{timestamp:.2f} 秒")
            button.setProperty("recommended", False)
            button.setIcon(QIcon())
            button.setEnabled(available)
        self._update_nearby_frame_selection(
            self._selected_frame_timestamp if self._selected_frame_timestamp is not None else center
        )

    def _queue_nearby_thumbnails(self, center: float):
        # 页面未显示时不启动 FFmpeg；进入封面页后由 showEvent 补排队。
        if not self.isVisible() or self.video is None or not Path(self.video.path).is_file():
            return
        center = max(0.0, float(center or 0.0))
        offsets = self._nearby_offsets(center)
        self._nearby_request_generation += 1
        request_generation = self._nearby_request_generation
        video = self.video
        timestamps = sorted({round(max(0.0, center + offset), 3) for offset in offsets})
        self._nearby_pending = set(timestamps)
        self._nearby_results: list[CoverFrame] = []
        self._nearby_error = None
        # 每帧一个后台任务并行取帧，取到一张显示一张，不等整排完成。
        for timestamp in timestamps:
            self._run(
                lambda value=timestamp: self.service.extract_frame_candidate(video, value),
                lambda result, error, value=timestamp: self._nearby_frame_ready(
                    request_generation, value, result, error
                ),
            )

    def _nearby_frame_ready(self, request_generation: int, requested: float, result, error):
        if request_generation != self._nearby_request_generation:
            return
        self._nearby_pending.discard(requested)
        if error:
            self._nearby_error = error
        elif result is not None:
            self._nearby_results.append(result)
            button = min(
                self.nearby_frame_buttons,
                key=lambda item: abs(float(item.property("timestamp") or 0.0) - requested),
            )
            self._show_frame_on_button(button, result, recommended=False)
        if not self._nearby_pending:
            # 整排到齐后再比较画质，标出推荐帧。
            frames = tuple(sorted(self._nearby_results, key=lambda item: item.timestamp))
            self._nearby_frames_ready(request_generation, frames, None if frames else self._nearby_error)

    def _nearby_frames_ready(self, request_generation: int, result, error):
        if request_generation != self._nearby_request_generation:
            return
        if error:
            message = f"附近帧预览失败：{error}"
            self._set_notice(message, "warning")
            self.status_changed.emit(message)
            return
        if not result:
            self._set_notice("没有找到附近可用画面", "warning")
            return
        frames = tuple(result)
        best = recommended_frame(frames)
        for button in self.nearby_frame_buttons:
            target = float(button.property("timestamp") or 0.0)
            frame = min(frames, key=lambda item: abs(item.timestamp - target))
            self._show_frame_on_button(button, frame, recommended=frame is best)
        self._set_notice("")

    @staticmethod
    def _show_frame_on_button(button: QPushButton, frame: CoverFrame, *, recommended: bool) -> None:
        """缩略图、时间和画质说明；推荐只做标记，不替用户换帧。"""

        button.setProperty("timestamp", frame.timestamp)
        button.setText(f"{'★ ' if recommended else ''}{frame.timestamp:.2f}s")
        pixmap = QPixmap(str(frame.path))
        if not pixmap.isNull():
            button.setIcon(QIcon(pixmap))
        metrics = frame.metrics
        lines = [f"{frame.timestamp:.2f} 秒"]
        if metrics is not None:
            lines.append(f"清晰度 {metrics.sharpness:.0%} · 曝光 {metrics.exposure:.0%} · 对比度 {metrics.contrast:.0%}")
            if metrics.subtitle_risk >= 0.3:
                lines.append("画面中下部可能有字幕或文字条")
        if recommended:
            lines.append("推荐：附近画面中画质明显更好；点击后才会换帧")
        button.setToolTip("\n".join(lines))
        button.setProperty("recommended", recommended)
        button.style().unpolish(button)
        button.style().polish(button)

    def _choose_nearby_frame(self, timestamp: float):
        if self.video is None:
            return
        timestamp = max(0.0, float(timestamp))
        self._update_nearby_frame_selection(timestamp)
        self.timestamp_edit.setValue(timestamp)
        self._extract_frame()

    def _toggle_frame_lock(self, checked: bool):
        if self.document is None:
            self.frame_lock_button.setChecked(False)
            return
        before = self.document
        self._frame_locked = bool(checked)
        self.document = self.service.set_frame_locked(self.document, self._frame_locked)
        self.frame_lock_button.setText("已锁帧" if self._frame_locked else "锁帧")
        self._commit_document_change(before)
        message = "已锁定当前帧，后台候选和 AI 不会自动换帧" if self._frame_locked else "已解除帧锁定"
        self.status_changed.emit(message)

    def _find_more_frames(self):
        if self.video is None or not Path(self.video.path).is_file():
            message = "请先在字幕页选择一个包含视频的投稿项目"
            self._set_notice(message, "warning")
            self.status_changed.emit(message)
            return
        center = self.timestamp_edit.value() if self.draft.image_path else self._current_playhead
        request_generation = self._nearby_request_generation + 1
        self._nearby_request_generation = request_generation
        video = self.video
        self.more_frames_button.setEnabled(False)
        self.status_changed.emit("正在按需寻找更大范围画面…")
        self._run(
            lambda: self.service.wider_candidates(video, center),
            lambda result, error: self._wider_frames_ready(request_generation, result, error),
        )

    def _wider_frames_ready(self, request_generation: int, result, error):
        self.more_frames_button.setEnabled(self.video is not None)
        if request_generation != self._nearby_request_generation or error:
            if error:
                message = f"扩大取帧范围失败：{error}"
                self._set_notice(message, "error")
                self.status_changed.emit(message)
            return
        frames = tuple(result or ())
        self._wider_frames = tuple((frame.path, frame.timestamp) for frame in frames)
        if not frames:
            self._set_notice("没有找到更多可用画面", "warning")
            return
        # 旧版经验：后台先按画质筛选，用户最后确认；按时间顺序展示最好的几张。
        count = len(self.nearby_frame_buttons)
        picked = sorted(
            sorted(frames, key=lambda frame: frame.score - frame.subtitle_risk * 20, reverse=True)[:count],
            key=lambda frame: frame.timestamp,
        )
        best = recommended_frame(frames)
        for button, frame in zip(self.nearby_frame_buttons, picked):
            self._show_frame_on_button(button, frame, recommended=frame is best)
        self._update_nearby_frame_selection()
        message = f"已从 {len(frames)} 张更大范围画面中挑出画质较好的 {len(picked)} 张"
        self.status_changed.emit(message)

    def _cycle_copy(self):
        if not self._copy_variants:
            return
        self._copy_variant_index = (self._copy_variant_index + 1) % len(self._copy_variants)
        candidate = self._copy_variants[self._copy_variant_index]
        if self.document is not None:
            before = self.document
            # 只换文字：A/B 各自是独立文本框，位置、字号和行宽沿用当前排版。
            primary = set(self._primary_copy_ids().values())
            for item in before.objects:
                if not isinstance(item, TextObject) or item.id not in primary:
                    continue
                value = candidate.context if item.copy_role == "A" else candidate.headline
                current = object_for_profile(self.document, item.id, self._canvas_key)
                current = current if isinstance(current, TextObject) else item
                self.document = update_text_object(
                    self.document, replace(current, text=value), profile_key=self._canvas_key,
                )
                self.document = set_object_visible(self.document, item.id, bool(value.strip()))
            if self.document != before:
                self._commit_document_change(before)
        else:
            self.title_edit.setPlainText(candidate.headline)
        self.status_changed.emit(
            f"已切换本地基础文案 {self._copy_variant_index + 1}/{len(self._copy_variants)}"
        )
        self._draft_timer.start()
        self._preview_timer.start()

    def set_current_playhead(self, seconds: float):
        """接收字幕页只读播放位置，不触发 seek 或修改字幕状态。"""

        self._current_playhead = max(0.0, float(seconds or 0.0))
        if self.video is not None:
            self.current_frame_button.setToolTip(
                f"使用字幕页当前帧（{self._current_playhead:.2f} 秒），不会改变播放位置"
            )
            if not self.draft.image_path:
                self._refresh_nearby_frame_strip(self._current_playhead)
                if self.isVisible() and not self._frame_extract_pending:
                    QTimer.singleShot(0, self._use_current_frame)

    def set_context(self, project: SubmissionProject | None, video: ProjectVideo | None):
        if project is None or video is None:
            self._context_generation += 1
            self.project = None
            self.video = None
            self.document = None
            self.history.reset(None)
            self._busy = False
            self._frame_extract_pending = False
            self._frame_request_generation += 1
            self._nearby_request_generation += 1
            self._preview_request_generation += 1
            self._copy_variants = ()
            self._copy_variant_index = -1
            self.project_label.setText("请选择项目")
            self.video_label.setText("请选择视频")
            self.draft_status.setText("未加载草稿")
            self._selected_frame_timestamp = None
            self._refresh_nearby_frame_strip(0.0)
            self._update_export_summary()
            self._update_canvas_hint()
            self.canvas.set_preview(QPixmap())
            self.canvas.set_background_pixmap(QPixmap())
            self._background_source = None
            self.canvas.setText("请先在字幕页选择投稿项目和视频")
            self._set_notice("")
            self.extract_button.setEnabled(False)
            self.current_frame_button.setEnabled(False)
            self.asset_menu_button.setEnabled(False)
            self.shape_menu_button.setEnabled(False)
            self.add_text_button.setEnabled(False)
            self._clear_schemes()
            self.export_button.setEnabled(False)
            self.export_both_button.setEnabled(False)
            self.undo_button.setEnabled(False)
            self.redo_button.setEnabled(False)
            self.frame_lock_button.setChecked(False)
            # 无项目时隐藏右侧面板，画布占满
            self._hide_panel()
            self.panel_toggle.setEnabled(False)
            self._show_empty_panel()
            return
        if self.project is not None and self.project.id == project.id and self.video is not None and self.video.path == video.path:
            return
        if self.project is not None and self.video is not None:
            self._save_draft()
        self._context_generation += 1
        self.project, self.video = project, video
        self.project_label.setText(project.title if len(project.title) <= 32 else project.title[:32] + "…")
        self.project_label.setToolTip(project.title)
        self.video_label.setText(video.name)
        self.asset_menu_button.setEnabled(True)
        self.shape_menu_button.setEnabled(True)
        self.add_text_button.setEnabled(True)
        self._set_notice("")
        self._update_export_summary()
        video_path = Path(video.path)
        video_available = video_path.is_file()
        self.extract_button.setEnabled(video_available)
        self.current_frame_button.setEnabled(video_available)
        self.extract_button.setToolTip("从指定时间取帧" if video_available else f"当前视频不存在：{video_path}")
        # 有项目时恢复右侧面板
        self._show_panel()
        self.panel_toggle.setEnabled(True)
        self.panel_toggle.setChecked(False)
        self._preview_path = None
        self._busy = False
        self._frame_extract_pending = False
        self._frame_request_generation += 1
        self._nearby_request_generation += 1
        self._preview_request_generation += 1
        self._canvas_key = "4x3"
        for key, button in self.canvas_ratio_buttons.items():
            button.setChecked(key == self._canvas_key)
        self._update_canvas_hint()
        self._copy_variants = self.service.basic_copy_variants(
            project.title,
            subtitle_context=self.service.subtitle_context(video, self._current_playhead),
        )
        self.document, read = self.service.load_document(project, video)
        self.history.reset(self.document)
        self._frame_locked = bool(self.document.source.frame_locked)
        self.frame_lock_button.blockSignals(True)
        self.frame_lock_button.setChecked(self._frame_locked)
        self.frame_lock_button.setText("已锁帧" if self._frame_locked else "锁帧")
        self.frame_lock_button.blockSignals(False)
        self.draft = CoverDraft.from_document(self.document)
        self._selected_frame_timestamp = (
            float(self.draft.selected_timestamp) if self.draft.image_path else max(0.0, self._current_playhead)
        )
        self._copy_variant_index = next(
            (
                index for index, candidate in enumerate(self._copy_variants)
                if candidate.text == self.draft.title or candidate.headline == self.draft.title
            ),
            -1,
        )
        self._apply_draft()
        # 有项目时默认显示文字面板
        self._canvas_selection_changed(True)
        if not video_available:
            self.draft_status.setText("视频不可用")
            self.draft_status.setToolTip(str(video_path))
            self._set_notice(f"当前视频不可用：{video_path}", "error")
        elif read.status == "ready":
            self.draft_status.setText("已恢复草稿")
            self.draft_status.setToolTip("")
        elif read.status == "missing":
            self.draft_status.setText("新草稿 · 自动保存")
            self.draft_status.setToolTip("")
        else:
            self.draft_status.setText(f"草稿需检查：{read.status}")
            self._set_notice(f"草稿状态需要检查：{read.status}", "warning")
        self._refresh_nearby_frame_strip(
            self.draft.selected_timestamp if self.draft.image_path else self._current_playhead
        )
        self._scheme_batch = 0
        if self.draft.image_path:
            self._render_preview()
            self._queue_nearby_thumbnails(self.draft.selected_timestamp)
            self._refresh_schemes()
        else:
            self._clear_schemes()
            self.canvas.setText("正在准备当前帧…" if self.isVisible() else "进入封面页后自动加载当前帧")
            self.export_button.setEnabled(False)
            if self.isVisible() and not self._frame_extract_pending:
                QTimer.singleShot(0, self._use_current_frame)

    def _apply_draft(self):
        widgets = (self.title_edit, self.font_spin, self.zoom_spin)
        for widget in widgets:
            widget.blockSignals(True)
        try:
            self.title_edit.setPlainText(self.draft.title)
            self.font_spin.setValue(self.draft.font_size)
            self.zoom_spin.setValue(self.draft.background_scale)
            self.timestamp_edit.setValue(self.draft.selected_timestamp)
        finally:
            for widget in widgets:
                widget.blockSignals(False)
        self.canvas.set_zoom(self.draft.background_scale)
        self.canvas.set_background_focus(self.draft.background_x, self.draft.background_y)
        self.canvas.set_document(self.document, self._canvas_key)
        if self.document is not None:
            selected = next(
                (item for item in self.document.objects if item.id == self.document.selected_object_id),
                None,
            )
            if isinstance(selected, TextObject):
                self._selected_text_id = selected.id
                self._sync_selected_text_controls()
            elif self._selected_text_id is None:
                text = next(
                    (item for item in self.document.objects if isinstance(item, TextObject) and item.copy_role == "B"),
                    None,
                ) or next((item for item in self.document.objects if isinstance(item, TextObject)), None)
                self._selected_text_id = text.id if text else None
        self._update_title_rect()

    def _set_text_align(self, align: str):
        if align not in {"left", "center", "right"} or self.document is None:
            return
        text = self._selected_text()
        if text is None:
            return
        current = object_for_profile(self.document, text.id, self._canvas_key)
        current = current if isinstance(current, TextObject) else text
        updated = replace(current, align=align)
        profile = self.document.profiles[self._canvas_key]
        payload = {"transform": updated.transform.to_payload(), "visible": bool(updated.visible), "rect": updated.rect.to_payload(), "wrap": updated.wrap.to_payload(), "align": align, "style": updated.style.to_payload()}
        self.document = replace(self.document, profiles={**self.document.profiles, self._canvas_key: replace(profile, overrides={**profile.overrides, updated.id: payload})})
        self.history.commit(self.document)
        self.undo_button.setEnabled(self.history.can_undo)
        self.redo_button.setEnabled(self.history.can_redo)
        self.canvas.set_document(self.document, self._canvas_key)
        self._draft_timer.start()
        self._preview_timer.start()

    def _pick_font(self):
        source, _ = QFileDialog.getOpenFileName(
            self,
            "选择封面字体",
            "",
            "字体文件 (*.ttf *.ttc *.otf)",
        )
        if not source:
            return
        self.font_path_edit.setText(str(Path(source).resolve()))
        self._draft_changed()

    def _store_text_controls(self):
        if self.document is None:
            return
        text = self._selected_text()
        if text is None:
            return
        current = object_for_profile(self.document, text.id, self._canvas_key)
        if not isinstance(current, TextObject):
            current = text
        # 清空即隐藏该块；不再回填整条投稿标题。
        typed_text = self.title_edit.text().strip()
        fill = self.fill_color_button.color() or current.style.fill_color
        stroke = self.stroke_button.color() or current.style.stroke_color
        updated = replace(
            current,
            text=typed_text,
            visible=bool(typed_text),
            style=replace(
                current.style,
                font_family=(
                    str(Path(self.font_path_edit.text().strip()).resolve())
                    if self.font_path_edit.text().strip() and Path(self.font_path_edit.text().strip()).is_file()
                    else current.style.font_family
                ),
                font_size=self.font_spin.value(),
                fill=fill,
                stroke=stroke,
                stroke_width=self.stroke_spin.value(),
                shadow=self.shadow_check.isChecked(),
                line_spacing=self.line_spacing_spin.value(),
                align=current.align,
                outer_stroke=self.outer_stroke_button.color(),
                outer_stroke_width=self.outer_stroke_spin.value(),
                backdrop=self.backdrop_button.color(),
            ),
        )
        # 文案与样式写回对象本体并同步另一比例，位置和字号只写当前比例。
        self.document = update_text_object(self.document, updated, profile_key=self._canvas_key)
        # 清空即在两个比例中隐藏，重新输入即恢复；这是内容操作，不分比例。
        self.document = set_object_visible(self.document, updated.id, bool(typed_text))

    def _apply_style_preset(self, preset: StylePreset) -> None:
        if self.document is None:
            return
        before = self.document
        for item in self.document.objects:
            if not isinstance(item, TextObject):
                continue
            current = object_for_profile(self.document, item.id, self._canvas_key)
            current = current if isinstance(current, TextObject) else item
            updated = replace(current, style=preset.apply(current.style, item.copy_role))
            self.document = update_text_object(self.document, updated, profile_key=self._canvas_key)
        if self.document != before:
            self._commit_document_change(before)
            self.status_changed.emit(f"已套用样式：{preset.label}")

    def _store_background_controls(self):
        if self.document is None:
            return
        background = next((item for item in self.document.objects if isinstance(item, BackgroundObject)), None)
        if background is None:
            return
        current = object_for_profile(self.document, background.id, self._canvas_key)
        if not isinstance(current, BackgroundObject):
            current = background
        updated = replace(current, scale=self.zoom_spin.value())
        profile = self.document.profiles[self._canvas_key]
        payload = {"transform": updated.transform.to_payload(), "scale": updated.scale, "pan_x": updated.pan_x, "pan_y": updated.pan_y, "fit_mode": updated.fit_mode}
        self.document = replace(self.document, active_profile=self._canvas_key, profiles={**self.document.profiles, self._canvas_key: replace(profile, overrides={**profile.overrides, updated.id: payload})})

    def _read_draft(self) -> CoverDraft:
        self._store_text_controls()
        self._store_background_controls()
        if self.document is not None:
            self.draft = CoverDraft.from_document(self.document)
            return self.draft
        return replace(
            self.draft,
            title=self.title_edit.text().strip() or (self.project.title if self.project else "未命名封面"),
            font_size=self.font_spin.value(),
            background_scale=self.zoom_spin.value(),
        )

    def _draft_changed(self):
        if self.project is None or self.video is None:
            return
        before = self.document
        self._store_text_controls()
        self._store_background_controls()
        self.draft = CoverDraft.from_document(self.document) if self.document else self._read_draft()
        if self.document is not None and before != self.document:
            self.history.commit(self.document)
            self.undo_button.setEnabled(self.history.can_undo)
            self.redo_button.setEnabled(self.history.can_redo)
            self._invalidate_render_requests()
        self._update_title_rect()
        self._draft_timer.start()
        self._preview_timer.start()

    def _title_position_changed(self, x: float, y: float):
        """键盘微调等程序化移动写进文档；鼠标拖动由松手时的 object_changed 提交。"""

        if self.project is None or self.video is None:
            return
        if self.document is None:
            # 兼容没有 v4 文档的旧调用方；这里只更新内存草稿。
            self.draft = replace(self.draft, text_x=x, text_y=y)
        elif getattr(self.canvas, "_mode", None) is None:
            text = self._selected_text()
            current = object_for_profile(self.document, text.id, self._canvas_key) if text else None
            if isinstance(current, TextObject) and (abs(current.transform.x - x) > 1e-6 or abs(current.transform.y - y) > 1e-6):
                moved = replace(current, transform=replace(current.transform, x=x, y=y))
                self.document = update_text_object(self.document, moved, profile_key=self._canvas_key)
                self.draft = CoverDraft.from_document(self.document)

    def _background_position_changed(self, x: float, y: float):
        if self.project is None or self.video is None:
            return
        # Canvas 已经持有拖动中的本地对象；这里只刷新轻量取景显示。
        self.canvas.set_background_focus(x, y)

    def _zoom_changed(self, value: float):
        if self.project is None or self.video is None:
            return
        value = max(BACKGROUND_SCALE_MIN, min(BACKGROUND_SCALE_MAX, float(value)))
        self.zoom_spin.blockSignals(True)
        self.zoom_spin.setValue(value)
        self.zoom_spin.blockSignals(False)
        before = self.document
        if self.document is not None:
            background = next((item for item in self.document.objects if isinstance(item, BackgroundObject)), None)
            if background is not None:
                current = object_for_profile(self.document, background.id, self._canvas_key)
                current = current if isinstance(current, BackgroundObject) else background
                updated = replace(current, scale=value)
                profile = self.document.profiles[self._canvas_key]
                payload = {"transform": updated.transform.to_payload(), "scale": value, "pan_x": updated.pan_x, "pan_y": updated.pan_y, "fit_mode": updated.fit_mode}
                self.document = replace(self.document, profiles={**self.document.profiles, self._canvas_key: replace(profile, overrides={**profile.overrides, updated.id: payload})})
            self.draft = CoverDraft.from_document(self.document)
        else:
            self.draft = replace(self._read_draft(), background_scale=value)
        if self.document is not None and before != self.document:
            self.history.commit(self.document)
            self.undo_button.setEnabled(self.history.can_undo)
            self.redo_button.setEnabled(self.history.can_redo)
        self.canvas.set_zoom(value)
        self._draft_timer.start()
        self._preview_timer.start()

    def _gesture_finished(self):
        # object_changed 已在 release 提交并启动防抖保存；释放时不同步写盘，
        # 连续拖动只在停手后写一次。
        self._draft_timer.start()

    def set_project_list(self, projects) -> None:
        """主窗口扫描后的投稿项目，供批量出图使用。"""

        self._batch_projects = tuple(projects or ())
        self.batch_button.setEnabled(any(project.videos for project in self._batch_projects))

    def _open_batch(self):
        self.flush_draft()
        targets = tuple(
            BatchTarget(
                project, video,
                exported=bool(self.service.existing_exports(project, video)),
                has_draft=self.service.has_draft(project, video),
            )
            for project in self._batch_projects
            for video in project.videos
        )
        if not targets:
            self.status_changed.emit("投稿目录里没有可出图的视频")
            return
        dialog = CoverBatchDialog(targets, self.service.auto_cover, self._run, self)
        dialog.finished_batch.connect(
            lambda done, failed: self.status_changed.emit(f"批量出图完成：成功 {done} 个，失败 {failed} 个")
        )
        dialog.exec()

    def flush_draft(self):
        """关窗或离开前补写尚在防抖中的草稿。"""

        if self._draft_timer.isActive():
            self._draft_timer.stop()
            self._save_draft()

    def _fill_canvas(self):
        self._set_background_transform(0.5, 0.5, 1.0, fit_mode="cover")

    def _fit_canvas(self):
        self._set_background_transform(0.5, 0.5, 1.0, fit_mode="contain")

    def _set_background_transform(self, x: float, y: float, scale: float, *, fit_mode: str | None = None):
        before = self.document
        if self.document is not None:
            background = next((item for item in self.document.objects if isinstance(item, BackgroundObject)), None)
            if background is not None:
                current = object_for_profile(self.document, background.id, self._canvas_key)
                current = current if isinstance(current, BackgroundObject) else background
                updated = replace(current, pan_x=x, pan_y=y, scale=scale, fit_mode=fit_mode or current.fit_mode)
                profile = self.document.profiles[self._canvas_key]
                payload = {"transform": updated.transform.to_payload(), "scale": scale, "pan_x": x, "pan_y": y, "fit_mode": updated.fit_mode}
                self.document = replace(self.document, profiles={**self.document.profiles, self._canvas_key: replace(profile, overrides={**profile.overrides, updated.id: payload})})
            self.draft = CoverDraft.from_document(self.document)
        else:
            self.draft = replace(self._read_draft(), background_x=x, background_y=y, background_scale=scale)
        if self.document is not None and before != self.document:
            self.history.commit(self.document)
            self.undo_button.setEnabled(self.history.can_undo)
            self.redo_button.setEnabled(self.history.can_redo)
        self.zoom_spin.blockSignals(True)
        self.zoom_spin.setValue(scale)
        self.zoom_spin.blockSignals(False)
        self.canvas.set_zoom(scale)
        self.canvas.set_background_focus(x, y)
        self._draft_timer.start()
        self._preview_timer.start()

    def _save_draft(self):
        if self.project is None or self.video is None:
            return
        try:
            if self.document is not None:
                # 文档已经在控件信号/画布手势提交时写入；延迟保存只能消费
                # 这份快照，不能再次从可能过期的控件反向重建它。
                self.draft = CoverDraft.from_document(self.document)
                self.service.save_document(self.project, self.video, self.document)
            else:
                self.draft = self._read_draft()
                self.service.save(self.project, self.video, self.draft)
            self.draft_status.setText("已保存")
        except (OSError, ValueError) as exc:
            message = f"封面草稿保存失败：{exc}"
            self._set_notice(message, "error")
            self.status_changed.emit(message)

    def _run(self, action, callback):
        generation = self._context_generation
        job = _Job(action)
        self._jobs.add(job)

        def done(result, error):
            try:
                if generation == self._context_generation:
                    callback(result, error)
            finally:
                self._jobs.discard(job)

        job.signals.finished.connect(done)
        QThreadPool.globalInstance().start(job)

    def _import_image(self):
        source, _ = QFileDialog.getOpenFileName(self, "选择封面底图", "", "图片 (*.png *.jpg *.jpeg *.webp *.bmp)")
        if not source or self.project is None or self.video is None:
            return
        try:
            path = self.service.import_image(source)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "无法导入底图", str(exc))
            return
        before_document = self.document
        if self.document is not None:
            background = next((item for item in self.document.objects if isinstance(item, BackgroundObject)), None)
            if background is not None:
                self.document = replace(self.document, objects=tuple(replace(item, asset=replace(item.asset, path=str(path)) if item.asset else AssetRef(path=str(path))) if isinstance(item, BackgroundObject) else item for item in self.document.objects))
                self.draft = CoverDraft.from_document(self.document)
            else:
                self.draft = replace(self._read_draft(), image_path=str(path))
        if self.document is not None and before_document != self.document:
            self.history.commit(self.document)
            self.undo_button.setEnabled(self.history.can_undo)
            self.redo_button.setEnabled(self.history.can_redo)
        else:
            self.draft = replace(self._read_draft(), image_path=str(path))
        self._save_draft()
        self._render_preview()

    def _use_current_frame(self):
        if not self.draft.image_path and self._current_playhead < 0.5:
            # 字幕页没有播放过：不取片头黑帧，按旧网页端从全片挑画质好的一张。
            self._auto_pick_frame()
            return
        self.timestamp_edit.setValue(self._current_playhead)
        self._update_nearby_frame_selection(self._current_playhead)
        self._refresh_nearby_frame_strip(self._current_playhead)
        self._extract_frame()

    def _auto_pick_frame(self):
        if self.video is None or not Path(self.video.path).is_file():
            self._extract_frame()
            return
        self._frame_extract_pending = True
        self._frame_request_generation += 1
        request_generation = self._frame_request_generation
        video = self.video
        self.canvas.setText("正在从全片挑选画面…")
        self._set_notice("没有播放位置：正在从全片挑一张画质好的画面…", "info")
        self._run(
            lambda: self.service.overview_candidates(video),
            lambda result, error: self._overview_ready(request_generation, result, error),
        )

    def _overview_ready(self, request_generation: int, result, error):
        if request_generation != self._frame_request_generation:
            return
        self._frame_extract_pending = False
        best = None if error else best_overview_frame(tuple(result or ()))
        timestamp = best.timestamp if best is not None else self._current_playhead
        self.timestamp_edit.setValue(timestamp)
        self._update_nearby_frame_selection(timestamp)
        self._refresh_nearby_frame_strip(timestamp)
        self._extract_frame()

    def _extract_frame(self):
        if self.video is None or not Path(self.video.path).is_file():
            message = "请先在字幕页选择投稿项目和视频"
            self._set_notice(message, "warning")
            self.status_changed.emit(message)
            return
        first_background = not bool(self.draft.image_path)
        self.extract_button.setEnabled(False)
        self.current_frame_button.setEnabled(False)
        for button in self.nearby_frame_buttons:
            button.setEnabled(False)
        self._frame_extract_pending = True
        self._frame_request_generation += 1
        request_generation = self._frame_request_generation
        video = self.video
        timestamp = self.timestamp_edit.value()
        self._set_notice("正在从视频取帧…", "info")
        self.status_changed.emit("正在从视频取帧…")
        service = self.service

        def extract():
            path, actual = service.extract_frame(video, timestamp)
            if first_background:
                # 构图分析在后台预热缓存，回到界面线程的自动构图直接命中。
                service.warm_composition(path)
            return path, actual

        self._run(
            extract,
            lambda result, error: self._frame_ready(
                request_generation, first_background, result, error
            ),
        )

    def _frame_ready(self, request_generation: int, first_background: bool, result, error):
        if request_generation != self._frame_request_generation:
            return
        self._frame_extract_pending = False
        self.extract_button.setEnabled(self.video is not None and Path(self.video.path).is_file())
        self.current_frame_button.setEnabled(self.extract_button.isEnabled())
        self._refresh_nearby_frame_strip(self.timestamp_edit.value())
        if error:
            message = f"取帧失败：{error}"
            self._set_notice(message, "error")
            self.status_changed.emit(message)
            return
        path, timestamp = result
        self._update_nearby_frame_selection(timestamp)
        before_document = self.document
        draft = replace(self._read_draft(), image_path=str(path), selected_timestamp=timestamp)
        if self.document is not None:
            backgrounds = tuple(replace(item, asset=replace(item.asset, path=str(path)) if item.asset else AssetRef(path=str(path))) if isinstance(item, BackgroundObject) else item for item in self.document.objects)
            self.document = replace(self.document, source=replace(self.document.source, selected_timestamp=timestamp, image_asset_id=str(path)), objects=backgrounds)
            draft = CoverDraft.from_document(self.document)
        if first_background and self.document is not None:
            # 新底图的默认构图：两个比例分别保住主体，A/B 避开人脸和杂乱区域。
            self.document = self.service.apply_auto_layout(self.document, path)
            draft = CoverDraft.from_document(self.document)
        elif first_background:
            text_x, text_y = self.service.suggest_text_position(path, draft, canvas_key=self._canvas_key)
            draft = replace(draft, text_x=text_x, text_y=text_y)
        self.draft = draft
        if self.document is not None and before_document != self.document:
            self.history.commit(self.document)
            self.undo_button.setEnabled(self.history.can_undo)
            self.redo_button.setEnabled(self.history.can_redo)
        self.timestamp_edit.setValue(timestamp)
        self._refresh_nearby_frame_strip(timestamp)
        if first_background:
            # 自动排版改了字号和对齐，右侧控件跟着刷新。
            self.canvas.set_document(self.document, self._canvas_key)
            self._sync_selected_text_controls()
        self._save_draft()
        self._set_notice("")
        self.status_changed.emit("已加载当前视频画面")
        self._render_preview()
        self._queue_nearby_thumbnails(timestamp)
        self._refresh_schemes()

    def _render_preview(self):
        if self.video is None or not self.draft.image_path:
            return
        if self._busy:
            self._preview_dirty = True
            return
        self._preview_dirty = False
        self._busy = True
        # 画布本身就是所见即所得的预览；后台渲染静默进行，不打扰编辑。
        self._preview_request_generation += 1
        request_generation = self._preview_request_generation
        canvas_key = self._canvas_key
        document = self.document
        # CoverDocument 是预览的唯一输入；只有兼容旧调用方的无文档路径
        # 才需要读取控件草稿。
        draft = self.draft if document is not None else self._read_draft()
        self.draft = draft
        video = self.video
        self._run(
            lambda: self.service.render_preview_document(video, document, canvas_key=canvas_key) if document is not None else self.service.render_preview(video, draft, canvas_key=canvas_key),
            lambda result, error: self._preview_ready(
                request_generation, canvas_key, result, error
            ),
        )

    def _preview_ready(self, request_generation: int, canvas_key: str, result, error):
        if (
            request_generation != self._preview_request_generation
            or canvas_key != self._canvas_key
        ):
            return
        self._busy = False
        if error:
            self.canvas.setText("预览失败")
            self._set_notice(f"封面预览失败：{error}", "error")
            self.export_button.setEnabled(False)
            return
        self._preview_path = Path(result)
        self._set_preview(self._preview_path)
        self.export_button.setEnabled(True)
        self.export_both_button.setEnabled(True)
        self._set_notice("")
        if self._preview_dirty:
            self._preview_timer.start()
        elif self._pending_export:
            self._pending_export = False
            self._start_export()

    def showEvent(self, event):
        super().showEvent(event)
        if self.video is not None and self.draft.image_path:
            QTimer.singleShot(0, lambda: self._queue_nearby_thumbnails(self.draft.selected_timestamp))

    def _set_preview(self, path: Path):
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            self.canvas.setText("预览图片无法读取")
            return
        self.canvas.set_preview(pixmap)
        if self.draft.image_path and self.draft.image_path != self._background_source:
            # 底图只在换帧时解码一次，改字不再重复读大图。
            source_pixmap = QPixmap(self.draft.image_path)
            if not source_pixmap.isNull():
                self.canvas.set_background_pixmap(source_pixmap)
                self._background_source = self.draft.image_path
        if self.document is not None:
            self.canvas.set_document(self.document, self._canvas_key)
        self._update_title_rect()

    def _update_title_rect(self):
        if self.document is not None:
            self.canvas.set_document(self.document, self._canvas_key)
            self.canvas.update()
            return
        title = self.title_edit.text() if hasattr(self, "title_edit") else self.draft.title
        font_size = self.font_spin.value() if hasattr(self, "font_spin") else self.draft.font_size
        canvas_width = 1440 if self._canvas_key == "4x3" else 1920
        lines = wrap_cover_title(title or "标题", font_size, canvas_width=canvas_width)
        width_units = max((len(line) for line in lines), default=4)
        width = min(0.86, max(0.08, width_units * font_size / canvas_width * 1.18))
        height = min(0.72, max(0.06, len(lines) * font_size * 1.18 / 1080))
        self.canvas.set_title_rect(QRectF(self.draft.text_x, self.draft.text_y, width, height))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_title_rect()

    def _export(self):
        if self.project is None or self.video is None:
            return
        self._save_draft()
        self.export_button.setEnabled(False)
        self._pending_export = True
        self._preview_timer.stop()
        # 强制把当前草稿重新渲染一次；导出从这次预览完成回调启动，确保
        # 导出的 JPG 与画布最后显示的构图使用同一份状态。
        self._render_preview()

    def _export_both(self):
        if self.project is None or self.video is None or self.document is None:
            return
        self._save_draft()
        self.export_button.setEnabled(False)
        self.export_both_button.setEnabled(False)
        document = self.document
        project, video = self.project, self.video
        self._set_notice("正在分别导出 4:3 与 16:9…", "info")
        self.status_changed.emit("正在分别导出 4:3 与 16:9…")
        self._run(
            lambda: self.service.export_both(project, video, document),
            self._export_both_ready,
        )

    def _export_both_ready(self, result, error):
        self.export_button.setEnabled(self._preview_path is not None)
        self.export_both_button.setEnabled(self._preview_path is not None)
        if error:
            message = f"双比例导出失败：{error}"
            self._set_notice(message, "error")
            self.status_changed.emit(message)
            return
        names = "、".join(Path(item).name for item in result)
        self.export_summary.setText(f"已导出 4:3 + 16:9 · 项目目录 · {names}")
        self.export_summary.setToolTip("\n".join(str(Path(item)) for item in result))
        self._set_notice("")
        self.status_changed.emit(f"双比例封面已导出：{names}")

    def _start_export(self):
        if self.project is None or self.video is None:
            self._pending_export = False
            return
        draft = self.draft if self.document is not None else self._read_draft()
        self._set_notice(f"正在导出 {self._canvas_label()} 主封面…", "info")
        self.status_changed.emit(f"正在导出 {self._canvas_label()} 主封面…")
        canvas_key = self._canvas_key
        self._run(
            lambda: self.service.export_document(self.project, self.video, self.document, canvas_key=canvas_key) if self.document is not None else self.service.export(self.project, self.video, draft, canvas_key=canvas_key),
            self._export_ready,
        )

    def _export_ready(self, result, error):
        self.export_button.setEnabled(self._preview_path is not None)
        self.export_both_button.setEnabled(self._preview_path is not None)
        if error:
            message = f"封面导出失败：{error}"
            self._set_notice(message, "error")
            self.status_changed.emit(message)
            return
        output_name = Path(result).name
        size = "1440×1080" if self._canvas_key == "4x3" else "1920×1080"
        output_dir = Path(self.project.directory) if self.project is not None else Path(result).parent
        self.export_summary.setText(
            f"已导出 · {self._canvas_label()} · {size} · 项目目录 · {output_name}"
        )
        self.export_summary.setToolTip(str(output_dir / output_name))
        message = f"封面已导出：{output_name}（{self._canvas_label()} · {size}）"
        self._set_notice("")
        self.status_changed.emit(message)
