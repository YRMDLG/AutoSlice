"""设置页分区（设计稿）：标题和说明在框外，框里一行一项、行与行之间一道细线，左边 96px 的灰色标签。

设置页各块原来都是“卡片里放标题、说明和内容”（qt_preview.window.card 和各设置面板）。这里把这些卡片
就地改成设计稿的分区，不改各块自己的构造代码：控件对象原样保留，只换外层容器和边距。
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QFormLayout, QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

LABEL_WIDTH = 96


class _RowWatcher(QObject):
    """行里的控件显隐变化时，重算哪些行和分隔线该露出来（藏起来的行不留空行和多余的线）。"""

    def __init__(self, box: QWidget) -> None:
        super().__init__(box)
        self._box = box

    def eventFilter(self, obj, event) -> bool:
        if event.type() in (QEvent.Type.ShowToParent, QEvent.Type.HideToParent):
            sync_rows(self._box)
        return False


def restyle(card: QWidget) -> None:
    """把一张设置卡改成设计稿分区；重复调用无副作用。"""

    layout = card.layout()
    if layout is None or card.property("section"):
        return
    card.setProperty("section", True)
    card.style().unpolish(card)
    card.style().polish(card)
    items = []
    while layout.count():
        items.append(layout.takeAt(0))
    title = note = None
    rest = []
    for item in items:
        widget = item.widget()
        if title is None and isinstance(widget, QLabel) and widget.objectName() == "cardTitle":
            title = widget
            continue
        if (title is not None and note is None and not rest and isinstance(widget, QLabel)
                and widget.objectName() == "muted" and widget.wordWrap()):
            note = widget
            continue
        if item.spacerItem() is not None:
            continue
        rest.append(item)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(10)
    header = QVBoxLayout()
    header.setContentsMargins(0, 0, 0, 0)
    header.setSpacing(2)
    if title is not None:
        title.setObjectName("sectionHeading")
        header.addWidget(title)
    if note is not None:
        note.setObjectName("sectionNote")
        header.addWidget(note)
    layout.addLayout(header)
    box = QWidget()
    box.setObjectName("settingsBox")
    box.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    column = QVBoxLayout(box)
    column.setContentsMargins(16, 0, 16, 0)
    column.setSpacing(0)
    watcher = _RowWatcher(box)
    for item in rest:
        form = item.layout()
        if isinstance(form, QFormLayout):
            for row in _form_rows(form):
                _add_row(column, row, watcher)
        else:
            _add_row(column, item, watcher)
    # 只有标题和说明的块（如“播放器”）不画空框
    if column.count():
        layout.addWidget(box)
        sync_rows(box)
    else:
        box.deleteLater()


def _form_rows(form: QFormLayout):
    """表单拆成一行一行：左边标签（固定宽、靠左），右边控件。"""

    rows = []
    while form.rowCount():
        taken = form.takeRow(0)
        label_item, field_item = taken.labelItem, taken.fieldItem
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        caption = label_item.widget() if label_item is not None else None
        if caption is not None:
            row.addWidget(caption)
        if field_item is not None:
            if field_item.widget() is not None:
                row.addWidget(field_item.widget(), 1)
            elif field_item.layout() is not None:
                row.addLayout(field_item.layout(), 1)
        rows.append(row)
    return rows


def _add_row(column: QVBoxLayout, item, watcher: _RowWatcher) -> None:
    holder = QWidget()
    holder.setObjectName("settingsRow")
    line = QFrame()
    line.setObjectName("rowLine")
    line.setFixedHeight(1)
    holder.setProperty("rowLine", line)
    inner = QVBoxLayout(holder)
    inner.setContentsMargins(0, 10, 0, 10)
    inner.setSpacing(0)
    # 既可能是从卡片里取下的布局项，也可能是表单拆出来的横排（QLayout 本身也是布局项）
    widget = item.widget()
    layout = item.layout()
    if widget is not None:
        # 保留原来的对齐（靠左的单个按钮不能被拉满一行）
        alignment = item.alignment() if not isinstance(item, (QHBoxLayout, QVBoxLayout)) else Qt.AlignmentFlag(0)
        inner.addWidget(widget, 0, alignment)
        holder.setProperty("rowContent", widget)
        widget.installEventFilter(watcher)
        _tidy_labels(widget.layout())
    elif layout is not None:
        _tidy_labels(layout)
        inner.addLayout(layout)
    column.addWidget(line)
    column.addWidget(holder)


def _tidy_labels(layout) -> None:
    """横排里打头的灰色标签：固定 96px 宽、靠左，和设计稿的标签列对齐。"""

    if not isinstance(layout, QHBoxLayout) or layout.count() == 0:
        return
    first = layout.itemAt(0).widget()
    # 只认表单标签；行首的说明 / 状态文字（muted）是要占满剩余宽度的，不能定宽
    if isinstance(first, QLabel) and first.objectName() == "formLabel":
        first.setFixedWidth(LABEL_WIDTH)
        first.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)


def sync_rows(box: QWidget) -> None:
    """单个控件的行跟着控件显隐；第一条露出来的行上面不画线。"""

    shown_before = False
    for holder in box.findChildren(QWidget, "settingsRow", Qt.FindChildOption.FindDirectChildrenOnly):
        content = holder.property("rowContent")
        visible = content is None or not content.isHidden()
        holder.setVisible(visible)
        line = holder.property("rowLine")
        if line is not None:
            line.setVisible(visible and shown_before)
        shown_before = shown_before or visible
