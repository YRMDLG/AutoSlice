"""Qt AutoCover vNext 的最小编辑工作区。

这里故意只承接桌面端需要的项目上下文、草稿和快速导出；候选帧提取与
封面渲染继续复用 ``autoslice_cover`` 的服务，避免把 FFmpeg/Pillow 细节
塞进主窗口。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from autoslice.desktop.foundation import DesktopStorage
from autoslice.desktop.projects import ProjectVideo, SubmissionProject

from .cover_service import CoverDraft, CoverService


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


class CoverEditorWidget(QWidget):
    """AutoCover-01 的三栏式最小编辑器。"""

    status_changed = Signal(str)

    def __init__(self, storage: DesktopStorage, parent=None):
        super().__init__(parent)
        self.service = CoverService(storage)
        self.project: SubmissionProject | None = None
        self.video: ProjectVideo | None = None
        self.draft = CoverDraft("")
        self._preview_path: Path | None = None
        self._context_generation = 0
        self._busy = False
        # QThreadPool 不会替 Python 对象保留可用的 signal owner；必须在控件
        # 上持有任务引用，直到 finished 信号回到 UI 线程，否则真实 FFmpeg
        # 任务会完成但结果回调可能被 GC 丢掉。
        self._jobs: set[_Job] = set()
        self._draft_timer = QTimer(self)
        self._draft_timer.setSingleShot(True)
        self._draft_timer.setInterval(500)
        self._draft_timer.timeout.connect(self._save_draft)
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(180)
        self._preview_timer.timeout.connect(self._render_preview)
        self._build()

    def _build(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(14)

        left = QGroupBox("当前投稿")
        left.setMinimumWidth(210)
        left_layout = QVBoxLayout(left)
        self.project_label = QLabel("请选择项目")
        self.project_label.setWordWrap(True)
        self.video_label = QLabel("请选择视频")
        self.video_label.setWordWrap(True)
        self.draft_status = QLabel("尚未加载封面草稿")
        self.draft_status.setWordWrap(True)
        left_layout.addWidget(self.project_label)
        left_layout.addWidget(self.video_label)
        left_layout.addWidget(self.draft_status)
        left_layout.addSpacing(8)
        self.frame_button = QPushButton("从当前视频取帧")
        self.frame_button.clicked.connect(self._extract_frame)
        left_layout.addWidget(self.frame_button)
        self.timestamp = QDoubleSpinBox()
        self.timestamp.setRange(0.0, 24 * 60 * 60)
        self.timestamp.setDecimals(2)
        self.timestamp.setSuffix(" 秒")
        self.timestamp.setToolTip("从当前视频的这个时间点提取一帧")
        left_layout.addWidget(self.timestamp)
        self.import_button = QPushButton("导入本地图")
        self.import_button.clicked.connect(self._import_image)
        left_layout.addWidget(self.import_button)
        left_layout.addStretch(1)
        root.addWidget(left)

        center = QWidget()
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.addWidget(QLabel("封面画布"), alignment=Qt.AlignmentFlag.AlignLeft)
        self.canvas = QLabel("加载底图后在这里预览")
        self.canvas.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.canvas.setMinimumSize(420, 260)
        self.canvas.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.canvas.setStyleSheet("background: #111820; border: 1px solid #2f414d; border-radius: 6px;")
        center_layout.addWidget(self.canvas, 1)
        self.canvas_hint = QLabel("默认使用现有 AutoCover 的标题描边与阴影样式")
        self.canvas_hint.setObjectName("subtle")
        center_layout.addWidget(self.canvas_hint)
        root.addWidget(center, 1)

        right = QGroupBox("文字与输出")
        right.setMinimumWidth(250)
        form = QFormLayout(right)
        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("封面标题")
        self.title_edit.textChanged.connect(self._draft_changed)
        form.addRow("标题", self.title_edit)
        self.x_spin = self._coordinate_spin()
        self.y_spin = self._coordinate_spin()
        self.x_spin.valueChanged.connect(self._draft_changed)
        self.y_spin.valueChanged.connect(self._draft_changed)
        form.addRow("横向位置", self.x_spin)
        form.addRow("纵向位置", self.y_spin)
        self.font_spin = QSpinBox()
        self.font_spin.setRange(24, 320)
        self.font_spin.setSuffix(" px")
        self.font_spin.valueChanged.connect(self._draft_changed)
        form.addRow("字号", self.font_spin)
        form.addRow("效果", QLabel("默认描边 + 阴影"))
        self.save_button = QPushButton("保存草稿")
        self.save_button.clicked.connect(self._save_draft)
        self.export_button = QPushButton("导出 JPG")
        self.export_button.setObjectName("primary")
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self._export)
        form.addRow(self.save_button)
        form.addRow(self.export_button)
        self.ai_button = QPushButton("封面文案/构图建议（手动触发）")
        self.ai_button.setToolTip("AutoCover-01 只保留入口，不会自动调用 AI")
        self.ai_button.clicked.connect(lambda: self.status_changed.emit("AI 建议入口已预留，AutoCover-02 再接入缓存服务"))
        form.addRow(self.ai_button)
        root.addWidget(right)

    @staticmethod
    def _coordinate_spin() -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(0.0, 1.0)
        spin.setSingleStep(0.01)
        spin.setDecimals(2)
        return spin

    def set_context(self, project: SubmissionProject | None, video: ProjectVideo | None):
        if project is None or video is None:
            self._context_generation += 1
            self.project = None
            self.video = None
            self._busy = False
            self.project_label.setText("请选择项目")
            self.video_label.setText("请选择视频")
            self.draft_status.setText("尚未加载封面草稿")
            self.canvas.clear()
            self.canvas.setText("加载底图后在这里预览")
            self.frame_button.setEnabled(False)
            self.frame_button.setToolTip("请先选择包含视频的投稿项目")
            self.export_button.setEnabled(False)
            return
        if (
            self.project is not None
            and self.project.id == project.id
            and self.video is not None
            and self.video.path == video.path
        ):
            return
        if self.project is not None and self.video is not None:
            self._save_draft()
        self._context_generation += 1
        self.project, self.video = project, video
        self.project_label.setText(project.title)
        self.project_label.setToolTip(project.directory)
        self.video_label.setText(video.name)
        video_path = Path(video.path)
        video_available = video_path.is_file()
        self.frame_button.setEnabled(video_available)
        self.frame_button.setToolTip(
            "从当前视频的这个时间点提取一帧"
            if video_available else f"当前视频不存在：{video_path}"
        )
        self._preview_path = None
        self._busy = False
        self.draft, read = self.service.load(project, video)
        self._apply_draft()
        if not video_available:
            self.draft_status.setText(f"当前视频不可用：{video_path}")
        elif read.status == "ready":
            self.draft_status.setText("已恢复封面草稿")
        elif read.status == "missing":
            self.draft_status.setText("暂无草稿，编辑会自动保存")
        else:
            self.draft_status.setText(f"草稿需检查：{read.status}")
        if self.draft.image_path:
            self._render_preview()
        else:
            self.canvas.setText("请导入底图，或从当前视频取帧")
            self.export_button.setEnabled(False)

    def _apply_draft(self):
        widgets = (self.title_edit, self.x_spin, self.y_spin, self.font_spin)
        for widget in widgets:
            widget.blockSignals(True)
        try:
            self.title_edit.setText(self.draft.title)
            self.x_spin.setValue(self.draft.text_x)
            self.y_spin.setValue(self.draft.text_y)
            self.font_spin.setValue(self.draft.font_size)
            self.timestamp.setValue(self.draft.selected_timestamp)
        finally:
            for widget in widgets:
                widget.blockSignals(False)

    def _read_draft(self) -> CoverDraft:
        return replace(
            self.draft,
            title=self.title_edit.text().strip() or (self.project.title if self.project else "未命名封面"),
            text_x=self.x_spin.value(),
            text_y=self.y_spin.value(),
            font_size=self.font_spin.value(),
        )

    def _draft_changed(self):
        if self.project is None or self.video is None:
            return
        self.draft = self._read_draft()
        self._draft_timer.start()
        self._preview_timer.start()

    def _save_draft(self):
        if self.project is None or self.video is None:
            return
        try:
            self.draft = self._read_draft()
            self.service.save(self.project, self.video, self.draft)
            self.draft_status.setText("草稿已保存到本机应用数据")
        except (OSError, ValueError) as exc:
            self.status_changed.emit(f"封面草稿保存失败：{exc}")

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
        self.draft = replace(self._read_draft(), image_path=str(path))
        self._save_draft()
        self._render_preview()

    def _extract_frame(self):
        if self.video is None or not Path(self.video.path).is_file():
            self.status_changed.emit("请先选择投稿项目和视频")
            return
        self.frame_button.setEnabled(False)
        video = self.video
        timestamp = self.timestamp.value()
        self.status_changed.emit("正在从视频取帧…")
        self._run(lambda: self.service.extract_frame(video, timestamp), self._frame_ready)

    def _frame_ready(self, result, error):
        self.frame_button.setEnabled(True)
        if error:
            self.status_changed.emit(f"取帧失败：{error}")
            return
        path, timestamp = result
        self.draft = replace(self._read_draft(), image_path=str(path), selected_timestamp=timestamp)
        self.timestamp.setValue(timestamp)
        self._save_draft()
        self.status_changed.emit("已加载当前视频画面")
        self._render_preview()

    def _render_preview(self):
        if self._busy or self.video is None or not self.draft.image_path:
            return
        self._busy = True
        draft = self._read_draft()
        self.draft = draft
        video = self.video
        self._run(lambda: self.service.render_preview(video, draft), self._preview_ready)

    def _preview_ready(self, result, error):
        self._busy = False
        if error:
            self.canvas.setText(f"预览失败：{error}")
            self.export_button.setEnabled(False)
            return
        self._preview_path = Path(result)
        self._set_preview(self._preview_path)
        self.export_button.setEnabled(True)

    def _set_preview(self, path: Path):
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            self.canvas.setText("预览图片无法读取")
            return
        self.canvas.setPixmap(pixmap.scaled(self.canvas.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._preview_path:
            self._set_preview(self._preview_path)

    def _export(self):
        if self.project is None or self.video is None:
            return
        self._save_draft()
        draft = self._read_draft()
        self.export_button.setEnabled(False)
        self.status_changed.emit("正在导出封面…")
        self._run(lambda: self.service.export(self.project, self.video, draft), self._export_ready)

    def _export_ready(self, result, error):
        self.export_button.setEnabled(self._preview_path is not None)
        if error:
            self.status_changed.emit(f"封面导出失败：{error}")
            return
        self.status_changed.emit(f"封面已导出：{Path(result).name}")
