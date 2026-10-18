"""封面修改方案的并排对照：原图、效果图、操作说明和可撤销的应用。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .cover_ai import AIFix
from .cover_model import CoverDocument
from .qt_preview import motion


@dataclass(frozen=True)
class CoverFixPreview:
    fix: AIFix
    thumbnail: bytes
    document: CoverDocument


class CoverComparisonImage(QLabel):
    """随可用空间调整预览尺寸，保持比例，避免图片撑大窗口。"""

    def __init__(self, data: bytes, parent=None):
        super().__init__(parent)
        self._original = QPixmap()
        self._original.loadFromData(data)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(120, 90)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.setPixmap(self._original.scaled(
            self.contentsRect().size(), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))


class CoverCritiqueDialog(QDialog):
    def __init__(
        self, before: bytes, previews: tuple[CoverFixPreview, ...],
        apply: Callable[[CoverFixPreview], bool], *, undo: Callable[[], None],
        redo: Callable[[], None], parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("AI 改一改")
        self.setModal(False)
        self.setMinimumSize(540, 400)
        self.previews = previews
        self._apply_callback = apply
        self.apply_buttons: list[QPushButton] = []
        layout = QVBoxLayout(self)
        hint = QLabel("左侧是当前封面，右侧是修改效果。切换方案对照，点“应用”后可撤销。")
        hint.setWordWrap(True)
        hint.setObjectName("subtle")
        layout.addWidget(hint)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        for index, preview in enumerate(previews):
            page = QWidget()
            column = QVBoxLayout(page)
            issue = QLabel(preview.fix.issue)
            issue.setTextFormat(Qt.TextFormat.PlainText)
            issue.setWordWrap(True)
            font = issue.font()
            font.setBold(True)
            issue.setFont(font)
            column.addWidget(issue)
            pictures = QHBoxLayout()
            for title, data in (("当前封面", before), ("方案预览", preview.thumbnail)):
                side = QVBoxLayout()
                side.addWidget(QLabel(title))
                side.addWidget(CoverComparisonImage(data), 1)
                pictures.addLayout(side, 1)
            column.addLayout(pictures, 1)
            operations = QLabel("\n".join(f"• {step}" for step in preview.fix.steps))
            operations.setTextFormat(Qt.TextFormat.PlainText)
            operations.setWordWrap(True)
            column.addWidget(operations)
            row = QHBoxLayout()
            row.addStretch(1)
            button = QPushButton("应用")
            button.clicked.connect(lambda _checked=False, item=preview, sender=button: self._apply(item, sender))
            row.addWidget(button)
            column.addLayout(row)
            self.apply_buttons.append(button)
            self.tabs.addTab(page, f"方案 {index + 1}")
        self.notice = QLabel()
        self.notice.setWordWrap(True)
        self.notice.hide()
        layout.addWidget(self.notice)
        controls = QHBoxLayout()
        for label, callback, sequence in (
            ("撤销", undo, QKeySequence.StandardKey.Undo),
            ("重做", redo, QKeySequence.StandardKey.Redo),
        ):
            button = QPushButton(label)
            button.clicked.connect(lambda _checked=False, action=callback: self._history(action))
            controls.addWidget(button)
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.activated.connect(lambda action=callback: self._history(action))
        controls.addStretch(1)
        close = QPushButton("关闭")
        close.clicked.connect(self.close)
        controls.addWidget(close)
        layout.addLayout(controls)
        available = self.screen().availableGeometry()
        self.resize(min(1000, available.width() - 48), min(720, available.height() - 48))
        motion.install_tree(self)

    def _reset_buttons(self) -> None:
        for button in self.apply_buttons:
            button.setEnabled(True)
            button.setText("应用")

    def _history(self, action: Callable[[], None]) -> None:
        action()
        self._reset_buttons()
        self.notice.hide()

    def _apply(self, preview: CoverFixPreview, button: QPushButton) -> None:
        if self._apply_callback(preview):
            self._reset_buttons()
            button.setText("已应用")
            button.setEnabled(False)
            self.notice.hide()
        else:
            self.notice.setText("封面或画布已变化，请关闭后重新点“AI 改一改”，再预览修改。")
            self.notice.show()
