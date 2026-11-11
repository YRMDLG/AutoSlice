"""封面修改方案的并排对照：原图、效果图、操作说明和可撤销的应用。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
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
    profile_thumbnails: tuple[tuple[str, bytes], ...] = ()


class CoverComparisonImage(QLabel):
    """随可用空间调整预览尺寸，保持比例，避免图片撑大窗口。"""

    def __init__(self, data: bytes, parent=None):
        super().__init__(parent)
        self._original = QPixmap()
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(120, 90)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.set_image(data)

    def set_image(self, data: bytes) -> None:
        self._original.loadFromData(data)
        self._refresh_image()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refresh_image()

    def _refresh_image(self) -> None:
        self.setPixmap(self._original.scaled(
            self.contentsRect().size(), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))


class CoverCritiqueDialog(QDialog):
    def __init__(
        self, before: bytes, previews: tuple[CoverFixPreview, ...],
        apply: Callable[[CoverFixPreview], bool], *, undo: Callable[[], None],
        redo: Callable[[], None], before_profiles: tuple[tuple[str, bytes], ...] = (),
        canvas_key: str = "4x3", context_note: str = "", parent=None, reviewing: bool = False,
    ):
        super().__init__(parent)
        self.setWindowTitle("AI 改一改")
        self.setModal(False)
        self.setMinimumSize(540, 400)
        self.previews = previews
        self._apply_callback = apply
        self._before = dict(before_profiles) or {canvas_key: before}
        self._comparisons: list[tuple[str, QWidget]] = []
        self._apply_label = "应用到两个比例" if len(self._before) > 1 else "应用"
        self.apply_buttons: list[QPushButton] = []
        # 每个方案一行“AI 复核”：复核在后台进行时先写“复核中”，结果回来再标推荐/不建议
        self.review_labels: list[QLabel] = []
        layout = QVBoxLayout(self)
        hint = QLabel(context_note + ("左侧是当前封面，右侧是修改效果。上面可切换比例或两个比例一起看；应用后两个比例一起更新，Ctrl+Z 一起撤销。"
                                     if len(self._before) > 1 else "左侧是当前封面，右侧是修改效果。切换方案对照，点“应用”后可撤销。"))
        hint.setWordWrap(True)
        hint.setObjectName("subtle")
        layout.addWidget(hint)
        self.profile_combo = QComboBox()
        for key in self._before:
            self.profile_combo.addItem(f"看 {key.replace('x', ':')}", key)
        if len(self._before) > 1:
            self.profile_combo.addItem("两个比例一起看", "all")
        # 默认只看正在编辑的比例：两个比例上下挤着时预览只有一百多像素高，看不清细节
        self.profile_combo.setCurrentIndex(max(0, self.profile_combo.findData(canvas_key)))
        self.profile_combo.setVisible(len(self._before) > 1)
        self.profile_combo.currentIndexChanged.connect(self._change_profile)
        layout.addWidget(self.profile_combo)
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
            review = QLabel("AI 复核中…不影响现在对照和应用" if reviewing else "")
            review.setObjectName("subtle")
            review.setWordWrap(True)
            review.setVisible(reviewing)
            column.addWidget(review)
            self.review_labels.append(review)
            for key, original_image in self._before.items():
                section = QWidget()
                section_layout = QVBoxLayout(section)
                section_layout.setContentsMargins(0, 0, 0, 0)
                section_layout.addWidget(QLabel(key.replace("x", ":")))
                pictures = QHBoxLayout()
                data = dict(preview.profile_thumbnails).get(key, preview.thumbnail)
                for title, image in (("当前封面", original_image), ("方案预览", data)):
                    side = QVBoxLayout()
                    side.addWidget(QLabel(title))
                    picture = CoverComparisonImage(image)
                    picture.setProperty("profile_key", key)
                    side.addWidget(picture, 1)
                    pictures.addLayout(side, 1)
                section_layout.addLayout(pictures, 1)
                self._comparisons.append((key, section))
                column.addWidget(section, 1)
            operations = QLabel("\n".join(f"• {step}" for step in preview.fix.steps))
            operations.setTextFormat(Qt.TextFormat.PlainText)
            operations.setWordWrap(True)
            details = QScrollArea()
            details.setWidgetResizable(True)
            details.setMaximumHeight(110)
            details.setWidget(operations)
            column.addWidget(details)
            row = QHBoxLayout()
            row.addStretch(1)
            button = QPushButton(self._apply_label)
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
        self._change_profile()
        available = self.screen().availableGeometry()
        self.resize(min(1280, available.width() - 48), min(860, available.height() - 48))
        motion.install_tree(self)

    def set_review(self, picks: dict[int, str], rejected: str = "", failed: str = "") -> None:
        """后台复核结果：picks 是 {方案序号(0 起): 理由}；不在里面的标“不建议”，失败时说明可自行判断。"""

        for index, label in enumerate(self.review_labels):
            if failed:
                text = f"AI 复核没完成（{failed}），可以自己对照判断。"
            elif index in picks:
                text = f"✓ AI 复核推荐：{picks[index]}"
                self.tabs.setTabText(index, f"方案 {index + 1} ✓")
            else:
                text = "AI 复核不建议（理由见窗口下方），可以自己对照判断。" if rejected else "AI 复核不建议，可以自己对照判断。"
            label.setText(text)
            label.setVisible(True)
        if rejected and not failed:
            # 发给 AI 的总图里原图标 0、方案从 1 编起，意见里的“方案 N”就是这里的方案 N
            self.notice.setText(f"AI 复核意见：{rejected}")
            self.notice.show()

    def _change_profile(self, _index: int = 0) -> None:
        key = self.profile_combo.currentData()
        for profile, section in self._comparisons:
            section.setVisible(key == "all" or key == profile)

    def _reset_buttons(self) -> None:
        for button in self.apply_buttons:
            button.setEnabled(True)
            button.setText(self._apply_label)

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
