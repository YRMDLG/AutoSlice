"""AutoCover 编辑器的小部件：后台任务、文案框、就地输入框、颜色按钮与预设图标。"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QColorDialog,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QStyle,
    QStyleOptionButton,
    QStylePainter,
    QWidget,
)

from autoslice_cover.document_render import rgba

from .cover_style import StylePreset
from .qt_preview import popups


class _TitleEdit(QPlainTextEdit):
    """允许手动换行，并保留旧测试/调用方使用的 ``text()``。"""

    def text(self) -> str:
        return self.toPlainText()


class _InlineTextEdit(_TitleEdit):
    """画布上的就地输入框：回车换行，Ctrl+回车或点别处完成，Esc 放弃。"""

    finished = Signal(bool)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.finished.emit(False)
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.finished.emit(True)
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        if self.isVisible():
            self.finished.emit(True)


class ToolbarButton(QPushButton):
    """工具栏按钮：窄窗口下只画图标；text() 不变，完整说明仍在提示里。"""

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self._compact = False

    @property
    def compact(self) -> bool:
        return self._compact

    def set_compact(self, compact: bool, *, force: bool = False) -> None:
        """force：图标稍后才设置的按钮（帧缩略图）也先进入只画图标状态。"""

        compact = bool(compact) and (force or not self.icon().isNull())
        if compact != self._compact:
            self._compact = compact
            self.updateGeometry()
            self.update()

    def full_width(self) -> int:
        """不收起时需要的宽度。"""

        return QPushButton.sizeHint(self).width()

    def compact_width(self) -> int:
        """只显示图标时需要的宽度。"""

        option = QStyleOptionButton()
        self.initStyleOption(option)
        option.text = ""
        return self.style().sizeFromContents(QStyle.ContentsType.CT_PushButton, option, self.iconSize(), self).width()

    def _option(self) -> QStyleOptionButton:
        option = QStyleOptionButton()
        self.initStyleOption(option)
        if self._compact:
            option.text = ""
        return option

    def sizeHint(self) -> QSize:
        if not self._compact:
            return super().sizeHint()
        return self.style().sizeFromContents(QStyle.ContentsType.CT_PushButton, self._option(), self.iconSize(), self)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def paintEvent(self, _event) -> None:
        painter = QStylePainter(self)
        painter.drawControl(QStyle.ControlElement.CE_PushButton, self._option())


class FitToolbar(QWidget):
    """一行工具栏：放不下时，把带图标的按钮收成只显示图标；宽了再展开。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._groups: list[tuple[ToolbarButton, ...]] = []

    def set_collapsible(self, *groups) -> None:
        """按收起的先后顺序传入若干组按钮：先收第一组，还放不下再收第二组。"""

        self._groups = [tuple(group) for group in groups]

    def fit(self) -> None:
        layout = self.layout()
        if layout is None or not self._groups:
            return
        # 先按“全部展开”估算需要的宽度（不用来回切换状态试探），再逐组收起直到放得下
        need = layout.sizeHint().width()
        for group in self._groups:
            need += sum(b.full_width() - b.sizeHint().width() for b in group if b.compact and b.isVisibleTo(self))
        level = 0
        for group in self._groups:
            if need <= self.width():
                break
            need -= sum(b.full_width() - b.compact_width() for b in group if b.isVisibleTo(self))
            level += 1
        for index, group in enumerate(self._groups):
            for button in group:
                button.set_compact(index < level)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.fit()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.fit()


class _ColorButton(QPushButton):
    """带色块的颜色按钮；可选“无”与透明度，替代手填十六进制。"""

    color_changed = Signal()

    def __init__(self, *, allow_none: bool = False, allow_alpha: bool = False, none_label: str = "无", parent=None):
        super().__init__(parent)
        self._color = ""
        self._allow_none = allow_none
        self._allow_alpha = allow_alpha
        self._none_label = none_label
        self.setFixedHeight(26)
        self.setIconSize(QSize(14, 14))
        if allow_none:
            menu = QMenu(self)
            popups.MenuFx(menu)
            menu.addAction("选择颜色…", self._pick)
            menu.addAction(none_label, lambda: self._choose(""))
            self.setProperty("hasMenu", True)
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
        self.setText(self._color or self._none_label)
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
