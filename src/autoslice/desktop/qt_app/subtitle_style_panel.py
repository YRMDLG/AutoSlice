"""字幕样式面板（D-048）：调压制用的字体、字号、颜色、描边、阴影、位置，存成几套随时切换。

面板放在字幕页右列（临时替换字幕列表和 AI 检查），左边视频照常显示：每改一下，播放器上的字幕预览
就按新样式重画（预览和压制用的是同一份 ASS 生成逻辑，所见即所得）。
"""

from __future__ import annotations

from PySide6.QtCore import QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from autoslice.desktop.cover_widgets import _ColorButton
from autoslice.desktop.qt_preview.window import label
from autoslice.desktop.subtitle_styles import DEFAULT_ID, SubtitleStyleStore
from autoslice.subtitle_workflow import EXACT_SUBTITLE_FONT

LABEL_WIDTH = 64
SAVE_DELAY_MS = 400

# (键, 标签, 最小, 最大, 步长, 说明)；单位沿用剪映，和原来的压制配置一一对应
NUMBERS = (
    ("font_size", "字号", 1.0, 30.0, 0.5, "剪映字号：20 约等于 1080p 画面上 135 像素高"),
    ("outline_width", "描边粗细", 0.0, 100.0, 1.0, "剪映描边：100 约等于 1080p 下 5 像素"),
    ("shadow", "阴影", 0.0, 100.0, 1.0, "黑色阴影的偏移，0 是不要阴影"),
    ("x", "水平位置", -1000.0, 1000.0, 1.0, "剪映坐标：0 是正中，-1000 最左、1000 最右"),
    ("y", "垂直位置", -1000.0, 1000.0, 1.0, "剪映坐标：0 是正中，-1000 最下、1000 最上"),
)


class _NumberField(QWidget):
    """滑块 + 数字框，两边同步；拖滑块时连续出值，方便边看预览边调。"""

    value_changed = Signal(float)

    def __init__(self, minimum: float, maximum: float, step: float, parent=None) -> None:
        super().__init__(parent)
        self._scale = round(1 / step) if step < 1 else 1
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(round(minimum * self._scale), round(maximum * self._scale))
        self.spin = QDoubleSpinBox()
        self.spin.setRange(minimum, maximum)
        self.spin.setSingleStep(step)
        self.spin.setDecimals(1 if step < 1 else 0)
        # 右侧留了 22px 给上下箭头：要放得下“-1000”“20.5”
        self.spin.setFixedWidth(96)
        self.spin.setKeyboardTracking(False)
        row.addWidget(self.slider, 1)
        row.addWidget(self.spin)
        self.slider.valueChanged.connect(lambda raw: self._set(raw / self._scale, from_slider=True))
        self.spin.valueChanged.connect(lambda value: self._set(value, from_slider=False))

    def _set(self, value: float, *, from_slider: bool) -> None:
        with QSignalBlocker(self.spin), QSignalBlocker(self.slider):
            if from_slider:
                self.spin.setValue(value)
            else:
                self.slider.setValue(round(value * self._scale))
        self.value_changed.emit(float(self.spin.value()))

    def value(self) -> float:
        return float(self.spin.value())

    def set_value(self, value: float) -> None:
        with QSignalBlocker(self.spin), QSignalBlocker(self.slider):
            self.spin.setValue(value)
            self.slider.setValue(round(value * self._scale))


def _hex(value: str) -> str:
    return value.strip().lstrip("#").lower()


class SubtitleStylePanel(QWidget):
    style_changed = Signal(dict)      # 生效的样式变了（切换、调整）：刷新预览
    close_requested = Signal()
    step_requested = Signal(int)      # 换一句看：-1 上一句、1 下一句
    font_check_requested = Signal(str, bool)

    def __init__(self, store: SubtitleStyleStore, storage=None, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("subtitleStylePanel")
        self.store = store
        self._style = dict(store.active().style)
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(SAVE_DELAY_MS)
        self._save_timer.timeout.connect(self._save_now)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 头部：标题 + 完成
        header = QWidget()
        header.setMinimumHeight(56)
        heading = QHBoxLayout(header)
        heading.setContentsMargins(18, 8, 12, 8)
        heading.setSpacing(8)
        heading.addWidget(label("字幕样式", "cardHeading"))
        heading.addStretch()
        self.done_button = QPushButton("完成")
        self.done_button.setObjectName("tool")
        self.done_button.setToolTip("回到字幕列表（样式已经自动保存）")
        self.done_button.clicked.connect(self.close_requested)
        heading.addWidget(self.done_button)
        outer.addWidget(header)

        scroll = QScrollArea()
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName("styleBody")
        column = QVBoxLayout(body)
        column.setContentsMargins(18, 0, 18, 14)
        column.setSpacing(10)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        # 选哪套 + 新建 / 删除
        presets = QHBoxLayout()
        presets.setSpacing(6)
        self.preset_combo = QComboBox()
        self.preset_combo.setAccessibleName("压制样式")
        self.preset_combo.activated.connect(self._choose_preset)
        presets.addWidget(self.preset_combo, 1)
        self.new_button = QPushButton("新建")
        self.new_button.setToolTip("复制当前这套，另存一套新的再调")
        self.new_button.clicked.connect(self._create)
        presets.addWidget(self.new_button)
        self.delete_button = QPushButton("删除")
        self.delete_button.setObjectName("tool")
        self.delete_button.setProperty("danger", True)
        self.delete_button.clicked.connect(self._delete)
        presets.addWidget(self.delete_button)
        column.addLayout(presets)
        self.default_note = label("默认就是你现在的压制配置，不能直接改；点“新建”复制一份再调。", "muted")
        self.default_note.setWordWrap(True)
        column.addWidget(self.default_note)

        box = QWidget()
        box.setObjectName("settingsBox")
        box.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        rows = QVBoxLayout(box)
        rows.setContentsMargins(14, 0, 14, 0)
        rows.setSpacing(0)
        self._rows = rows

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("给这套起个名字")
        self.name_edit.setMaxLength(20)
        self.name_edit.editingFinished.connect(self._rename)
        self.name_row = self._row("名称", self.name_edit)

        self.font_combo = QComboBox()
        self.font_combo.setEditable(True)
        self.font_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.font_combo.setToolTip("本机装了的中文字体；也可以直接输入字体名")
        families = [EXACT_SUBTITLE_FONT] + sorted(
            {name for name in QFontDatabase.families(QFontDatabase.WritingSystem.SimplifiedChinese)
             if not name.startswith("@")} - {EXACT_SUBTITLE_FONT})
        self.font_combo.addItems(families)
        self.font_combo.activated.connect(lambda _index: self._font_committed())
        self.font_combo.lineEdit().editingFinished.connect(self._font_committed)
        font_column = QVBoxLayout()
        font_column.setSpacing(4)
        font_column.addWidget(self.font_combo)
        self.font_status = label("", "muted")
        self.font_status.setWordWrap(True)
        font_column.addWidget(self.font_status)
        self._row("字体", font_column)

        # 普通勾选框：开关样式没有圆点，开 / 关不好分辨
        self.bold_check = QCheckBox("加粗")
        self.bold_check.toggled.connect(lambda value: self._change("bold", bool(value)))
        self._row("字重", self.bold_check)

        self.color_buttons: dict[str, _ColorButton] = {}
        for key, caption in (("font_color", "文字颜色"), ("outline_color", "描边颜色")):
            button = _ColorButton(storage=storage)
            button.color_changed.connect(lambda key=key, button=button: self._change(key, _hex(button.color())))
            self.color_buttons[key] = button
            holder = QWidget()
            holder_row = QHBoxLayout(holder)
            holder_row.setContentsMargins(0, 0, 0, 0)
            holder_row.addWidget(button)
            holder_row.addStretch()
            self._row(caption, holder)

        self.numbers: dict[str, _NumberField] = {}
        for key, caption, minimum, maximum, step, tip in NUMBERS:
            field = _NumberField(minimum, maximum, step)
            field.setToolTip(tip)
            field.value_changed.connect(lambda value, key=key: self._change(key, value))
            self.numbers[key] = field
            self._row(caption, field)
        column.addWidget(box)

        actions = QHBoxLayout()
        actions.setSpacing(6)
        self.center_button = QPushButton("水平居中")
        self.center_button.setObjectName("tool")
        self.center_button.clicked.connect(lambda: self._set_number("x", 0.0))
        actions.addWidget(self.center_button)
        self.reset_button = QPushButton("恢复成默认值")
        self.reset_button.setObjectName("tool")
        self.reset_button.setToolTip("把这套的数值改回默认样式（名字不变）")
        self.reset_button.clicked.connect(self._reset_to_default)
        actions.addWidget(self.reset_button)
        actions.addStretch()
        column.addLayout(actions)

        # 预览要停在有字幕的地方才看得到：给个换句的入口
        steps = QHBoxLayout()
        steps.setSpacing(6)
        steps.addWidget(label("预览", "formLabel"))
        self.prev_button = QPushButton("上一句")
        self.next_button = QPushButton("下一句")
        for button, delta in ((self.prev_button, -1), (self.next_button, 1)):
            button.setObjectName("tool")
            button.clicked.connect(lambda _checked=False, delta=delta: self.step_requested.emit(delta))
            steps.addWidget(button)
        steps.addStretch()
        column.addLayout(steps)
        drag_note = label("也可以直接在画面上点字幕：拖框改位置，拖四个角改大小（靠近正中会吸住，按住 Alt 不吸）。", "muted")
        drag_note.setWordWrap(True)
        column.addWidget(drag_note)
        self.preview_hint = label("", "muted")
        self.preview_hint.setWordWrap(True)
        column.addWidget(self.preview_hint)
        column.addStretch()

        self.status = label("", "subtle")
        self.status.setWordWrap(True)
        status_row = QHBoxLayout()
        status_row.setContentsMargins(18, 8, 18, 12)
        status_row.addWidget(self.status)
        outer.addLayout(status_row)
        self._reload()

    # ── 布局 ──
    def _row(self, caption: str, content) -> QWidget:
        if self._rows.count():
            divider = QFrame()
            divider.setObjectName("rowLine")
            divider.setFixedHeight(1)
            self._rows.addWidget(divider)
        holder = QWidget()
        holder.setObjectName("settingsRow")
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 8, 0, 8)
        row.setSpacing(10)
        text = label(caption, "formLabel")
        text.setFixedWidth(LABEL_WIDTH)
        if isinstance(content, QWidget):
            row.addWidget(text, 0, Qt.AlignmentFlag.AlignVCenter)
        else:
            # 控件下面还有一行说明（如字体检查结果）：标签和第一行控件对齐，不跟整块居中
            first = content.itemAt(0).widget() if content.count() else None
            if first is not None:
                text.setMinimumHeight(first.sizeHint().height())
            row.addWidget(text, 0, Qt.AlignmentFlag.AlignTop)
        if isinstance(content, QWidget):
            row.addWidget(content, 1)
        else:
            row.addLayout(content, 1)
        self._rows.addWidget(holder)
        return holder

    # ── 状态 ──
    def current_style(self) -> dict:
        return dict(self._style)

    def _editable(self) -> bool:
        return self.store.active().id != DEFAULT_ID

    def _reload(self) -> None:
        """按当前这套刷新整个面板（切换、新建、删除之后）。"""

        active = self.store.active()
        self._style = dict(active.style)
        with QSignalBlocker(self.preset_combo):
            self.preset_combo.clear()
            for preset in self.store.presets():
                self.preset_combo.addItem(preset.name, preset.id)
            self.preset_combo.setCurrentIndex(self.preset_combo.findData(active.id))
        editable = self._editable()
        self.default_note.setVisible(not editable)
        self.delete_button.setVisible(editable)
        self.reset_button.setVisible(editable)
        self.name_row.setVisible(editable)
        self.name_edit.setText(active.name)
        with QSignalBlocker(self.font_combo):
            self.font_combo.setCurrentText(self._style["font_name"])
        with QSignalBlocker(self.bold_check):
            self.bold_check.setChecked(bool(self._style["bold"]))
        for key, button in self.color_buttons.items():
            button.set_color(f"#{self._style[key]}")
        for key, field in self.numbers.items():
            field.set_value(float(self._style[key]))
        for widget in (self.font_combo, self.bold_check, *self.color_buttons.values(),
                       *self.numbers.values(), self.center_button):
            widget.setEnabled(editable)
        self._update_status()
        self.font_status.setText("")
        self.font_check_requested.emit(self._style["font_name"], bool(self._style["bold"]))

    def _update_status(self) -> None:
        active = self.store.active()
        if active.id == DEFAULT_ID:
            self.status.setText("压制会用“默认”这套（你原来的压制配置）。")
        else:
            self.status.setText(f"压制会用“{active.name}”这套 · 改动自动保存。")

    def set_font_status(self, font_name: str, problem: str) -> None:
        """后台查完字体回来：只认当前还选着的那个字体。"""

        if font_name != self._style["font_name"]:
            return
        self.font_status.setObjectName("danger" if problem else "muted")
        self.font_status.style().unpolish(self.font_status)
        self.font_status.style().polish(self.font_status)
        self.font_status.setText(problem or "本机已装，压制能用上。")

    def set_preview_hint(self, text: str) -> None:
        self.preview_hint.setText(text)
        self.preview_hint.setVisible(bool(text))

    # ── 修改 ──
    def _change(self, key: str, value) -> None:
        if not self._editable() or self._style.get(key) == value:
            return
        self._style[key] = value
        self._save_timer.start()
        self.style_changed.emit(dict(self._style))
        if key == "bold":
            # 粗细不同，libass 选到的字体文件也不同：重查一次
            self.font_check_requested.emit(self._style["font_name"], bool(value))

    def _set_number(self, key: str, value: float) -> None:
        self.numbers[key].set_value(value)
        self._change(key, value)

    def editable(self) -> bool:
        return self._editable()

    def set_numbers(self, values: dict) -> bool:
        """视频上拖字幕框时整批改位置 / 字号：按各项的范围和步长取整，滑块数字跟着动，只刷新一次预览。"""

        if not self._editable():
            return False
        changed = False
        for key, value in values.items():
            field = self.numbers.get(key)
            if field is None:
                continue
            step = field.spin.singleStep()
            value = round(round(float(value) / step) * step, 3)
            value = min(max(value, field.spin.minimum()), field.spin.maximum())
            field.set_value(value)
            if self._style.get(key) != value:
                self._style[key] = value
                changed = True
        if changed:
            self._save_timer.start()
            self.style_changed.emit(dict(self._style))
        return changed

    def _font_committed(self) -> None:
        name = self.font_combo.currentText().strip()
        if not name or name == self._style["font_name"] or not self._editable():
            return
        # 名字带逗号、大括号这类会把 ASS 搞坏，先挡住
        if any(char in name for char in ",{}\\"):
            with QSignalBlocker(self.font_combo):
                self.font_combo.setCurrentText(self._style["font_name"])
            self.font_status.setText("字体名不能带逗号、大括号或反斜杠")
            return
        self._change("font_name", name)
        self.font_status.setText("正在检查本机有没有这个字体…")
        self.font_check_requested.emit(name, bool(self._style["bold"]))

    def _save_now(self) -> None:
        self._save_timer.stop()
        if not self._editable():
            return
        try:
            self.store.update(self.store.active().id, self._style)
        except (KeyError, ValueError) as exc:
            self.status.setText(f"没保存上：{exc}")

    def flush(self) -> None:
        """面板收起或窗口关闭前，把还在等的改动立刻写下。"""

        if self._save_timer.isActive():
            self._save_now()

    def _choose_preset(self, index: int) -> None:
        preset_id = self.preset_combo.itemData(index)
        if preset_id is None or preset_id == self.store.active().id:
            return
        self.flush()
        self.store.set_active(preset_id)
        self._reload()
        self.style_changed.emit(dict(self._style))

    def _create(self) -> None:
        self.flush()
        self.store.create(self.store.suggest_name(), self._style)
        self._reload()
        self.style_changed.emit(dict(self._style))
        # 新建完直接让人改名字
        self.name_edit.setFocus()
        self.name_edit.selectAll()

    def _rename(self) -> None:
        if not self._editable():
            return
        active = self.store.active()
        name = self.name_edit.text().strip()
        if not name or name == active.name:
            self.name_edit.setText(active.name)
            return
        renamed = self.store.rename(active.id, name)
        self.name_edit.setText(renamed.name)
        with QSignalBlocker(self.preset_combo):
            self.preset_combo.setItemText(self.preset_combo.findData(renamed.id), renamed.name)
        self._update_status()

    def _delete(self) -> None:
        if not self._editable():
            return
        active = self.store.active()
        choice = QMessageBox.question(
            self, "删除这套样式", f"删除“{active.name}”？删掉后压制回到“默认”这套。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if choice != QMessageBox.StandardButton.Yes:
            return
        self._save_timer.stop()
        self.store.delete(active.id)
        self._reload()
        self.style_changed.emit(dict(self._style))

    def _reset_to_default(self) -> None:
        if not self._editable():
            return
        self._style = dict(self.store.default().style)
        self._save_now()
        self._reload()
        self.style_changed.emit(dict(self._style))
