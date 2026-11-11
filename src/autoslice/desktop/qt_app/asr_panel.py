"""字幕识别的界面件：设置页“字幕识别”卡片、字幕页顶部的“识别字幕”提示条。"""

from __future__ import annotations

import json

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from autoslice.desktop.asr import AsrSettings, load_settings, save_settings
from autoslice.desktop.asr.doctor import Check, format_checks
from autoslice.desktop.asr.engines import ENGINES
from autoslice.desktop.asr.jobs import python_command
from autoslice.desktop.asr.setup_text import SETUP_TEXT
from autoslice.desktop.foundation import DesktopStorage
from autoslice.desktop.qt_preview import popups
from autoslice.desktop.qt_preview.icons import icon as desktop_icon
from autoslice.desktop.qt_preview.theme import COLORS, SIZES
from autoslice.desktop.qt_preview.window import label

from .asr_process import ModuleProcess

_FORM_SPACING = 16
_DEVICES = (("auto", "自动（有显卡就用显卡）"), ("cuda", "显卡（CUDA）"), ("cpu", "CPU（很慢）"))


class AsrSettingsPanel(QWidget):
    def __init__(self, storage: DesktopStorage, parent=None):
        super().__init__(parent)
        self.storage = storage
        self.setObjectName("settingsCard")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 18)
        layout.setSpacing(10)
        layout.addWidget(label("字幕识别", "cardTitle"))
        intro = label(
            "视频没有字幕时，在字幕页点“识别字幕”。识别在单独的进程里跑，用的是这台电脑的显卡；"
            "这里只管选引擎和模型。不知道哪个引擎对你的直播更准，点“对比引擎…”选一个有校对字幕的旧切片文件夹，"
            "用你自己的字幕给它们打分。第一次用先看“安装步骤”。", "muted")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        captions: list[QWidget] = []

        def new_form() -> QFormLayout:
            form = QFormLayout()
            form.setHorizontalSpacing(_FORM_SPACING)
            form.setVerticalSpacing(10)
            form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
            return form

        def add_row(form: QFormLayout, text: str, widget) -> QWidget:
            caption = label(text, "formLabel")
            caption.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            captions.append(caption)
            form.addRow(caption, widget)
            return caption

        self.engine_box = popups.ComboBox()
        self.engine_box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        for key, spec in ENGINES.items():
            self.engine_box.addItem(spec.label, key)
        self.engine_box.currentIndexChanged.connect(self._engine_changed)
        engine_form = new_form()
        add_row(engine_form, "识别引擎", self.engine_box)
        layout.addLayout(engine_form)
        # 引擎说明不进表单：表单按宽度算折行高度不准，放格子里会把下拉框压扁、单独一行又被裁字。
        # 放在两段表单之间，左边让出标签列，看着仍挨着下拉框。
        note_row = QHBoxLayout()
        note_row.setContentsMargins(0, 0, 0, 0)
        self.engine_note = label("", "muted")
        self.engine_note.setWordWrap(True)
        note_row.addWidget(self.engine_note, 1)
        layout.addLayout(note_row)
        form = new_form()
        self.model_edit = QLineEdit()
        self.model_edit.setPlaceholderText("Qwen/Qwen3-ASR-1.7B，或本机模型文件夹")
        self.model_caption = add_row(form, "识别模型", self.model_edit)
        self.aligner_edit = QLineEdit()
        self.aligner_edit.setPlaceholderText("Qwen/Qwen3-ForcedAligner-0.6B（留空则时间按字数估算）")
        self.aligner_caption = add_row(form, "对齐模型", self.aligner_edit)
        self.device_box = popups.ComboBox()
        self.device_box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        for key, text in _DEVICES:
            self.device_box.addItem(text, key)
        add_row(form, "运行在", self.device_box)
        python_row = QHBoxLayout()
        python_row.setSpacing(8)
        self.python_edit = QLineEdit()
        self.python_edit.setPlaceholderText("留空 = 当前这个 Python；装了 torch 和识别库的环境填这里")
        python_row.addWidget(self.python_edit, 1)
        browse = QPushButton("选择")
        browse.clicked.connect(self._browse_python)
        # 和输入框同高，不然按钮下沿比输入框低一截；先套上样式表再量，否则量的是默认样式的高度
        self.python_edit.ensurePolished()
        browse.setFixedHeight(self.python_edit.sizeHint().height())
        python_row.addWidget(browse, 0, Qt.AlignmentFlag.AlignVCenter)
        holder = QWidget()
        holder.setLayout(python_row)
        python_row.setContentsMargins(0, 0, 0, 0)
        add_row(form, "识别用的 Python", holder)
        layout.addLayout(form)
        # 两段表单的标签列等宽，输入框左沿才对得齐
        for caption in captions:
            caption.ensurePolished()
        caption_width = max(caption.sizeHint().width() for caption in captions)
        for caption in captions:
            caption.setFixedWidth(caption_width)
        note_row.insertSpacing(0, caption_width + _FORM_SPACING)
        self.hotword_check = QCheckBox("识别时带上热词（主播名、提督/舰长、你确认过的术语）")
        self.filter_check = QCheckBox("识别前先压低背景音乐和底噪")
        layout.addWidget(self.hotword_check)
        layout.addWidget(self.filter_check)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setMaximumHeight(220)
        self.output.hide()
        layout.addWidget(self.output)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.status = label("", "muted")
        self.status.setWordWrap(True)
        row.addWidget(self.status, 1)
        self.setup_button = QPushButton("安装步骤")
        self.setup_button.clicked.connect(self._show_setup)
        row.addWidget(self.setup_button)
        self.bench_button = QPushButton("对比引擎…")
        self.bench_button.setToolTip("用你校对过的旧字幕给各个识别引擎打分：只读旧文件夹，会占显卡，可能要跑几十分钟")
        self.bench_button.clicked.connect(self._bench)
        row.addWidget(self.bench_button)
        self.probe_button = QPushButton("加载试跑")
        self.probe_button.setToolTip("真的把模型加载起来试跑一小段；会占显存，第一次可能要下载模型")
        self.probe_button.clicked.connect(lambda: self._check(probe=True))
        row.addWidget(self.probe_button)
        self.check_button = QPushButton("自检")
        self.check_button.setIcon(desktop_icon("bolt", COLORS.muted, SIZES.tool_icon_size))
        self.check_button.clicked.connect(lambda: self._check(probe=False))
        row.addWidget(self.check_button)
        self.save_button = QPushButton("保存")
        self.save_button.setObjectName("primary")
        self.save_button.setIcon(desktop_icon("save", COLORS.on_accent, SIZES.tool_icon_size))
        self.save_button.clicked.connect(self._save)
        row.addWidget(self.save_button)
        layout.addLayout(row)
        self._runner = ModuleProcess(self)
        self._runner.line.connect(self._collect)
        self._runner.finished.connect(self._checked)
        self._lines: list[str] = []
        # 当前进程在做什么：doctor 只收 JSON 结果，bench 边跑边显示
        self._mode = "doctor"
        self._bench_report = None
        self.refresh()

    def refresh(self) -> None:
        settings = load_settings(self.storage)
        self.engine_box.setCurrentIndex(max(0, self.engine_box.findData(settings.engine)))
        self.model_edit.setText(settings.model)
        self.aligner_edit.setText(settings.aligner)
        self.device_box.setCurrentIndex(max(0, self.device_box.findData(settings.device)))
        self.python_edit.setText(settings.python)
        self.hotword_check.setChecked(settings.hotwords)
        self.filter_check.setChecked(settings.foreground_filter)
        self._engine_changed()

    def settings(self) -> AsrSettings:
        return AsrSettings(
            engine=self.engine_box.currentData() or AsrSettings().engine,
            model=self.model_edit.text().strip(), aligner=self.aligner_edit.text().strip(),
            device=self.device_box.currentData() or "auto", python=self.python_edit.text().strip(),
            hotwords=self.hotword_check.isChecked(), foreground_filter=self.filter_check.isChecked(),
        )

    def _engine_changed(self) -> None:
        spec = ENGINES.get(self.engine_box.currentData() or "")
        self.engine_note.setText(spec.summary if spec else "")
        qwen = (self.engine_box.currentData() == "qwen3-asr")
        for widget in (self.model_edit, self.model_caption, self.aligner_edit, self.aligner_caption):
            widget.setVisible(qwen)

    def _browse_python(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择 python.exe", self.python_edit.text(), "Python (python*.exe python*);;所有文件 (*)")
        if path:
            self.python_edit.setText(path)

    def _save(self) -> bool:
        try:
            path = save_settings(self.storage, self.settings())
        except OSError as exc:
            self.status.setText(f"没保存：{exc}")
            return False
        self.status.setText(f"已保存到 {path}")
        return True

    def _show_setup(self) -> None:
        self.output.setPlainText(SETUP_TEXT)
        self.output.show()

    def _run_buttons(self, enabled: bool) -> None:
        for button in (self.check_button, self.probe_button, self.bench_button):
            button.setEnabled(enabled)

    def _start(self, mode: str, arguments: list[str], message: str) -> None:
        self._run_buttons(False)
        self.status.setText(message)
        self.output.clear()
        self.output.show()
        self._lines = []
        self._mode = mode
        try:
            program, args, environment = python_command("autoslice.desktop.asr", arguments, self.python_edit.text())
        except ValueError as exc:
            self._run_buttons(True)
            self.status.setText(str(exc))
            self.output.hide()
            return
        self._runner.start(program, args, environment)

    def _check(self, *, probe: bool) -> None:
        if self._runner.running or not self._save():
            return
        self._start("doctor", ["doctor", "--json", *(["--probe"] if probe else [])],
                    "正在加载模型试跑，第一次可能要几分钟…" if probe else "正在检查环境…")

    def _bench(self) -> None:
        """原来要自己敲 bench 命令：选旧切片文件夹，后台跑，报告写进数据目录。"""

        if self._runner.running:
            return
        folder = QFileDialog.getExistingDirectory(self, "选一个旧切片文件夹（里面要有视频和“_校对”字幕）")
        if not folder or not self._save():
            return
        report = self.storage.root / "asr" / "引擎对比.md"
        try:
            report.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.status.setText(f"报告目录建不了：{exc}")
            return
        self._bench_report = report
        self._start("bench", ["bench", "--root", folder, "--engines", ",".join(ENGINES), "--out", str(report)],
                    "正在对比引擎：每个引擎都要加载模型跑几段，可能要几十分钟…")

    def _collect(self, text: str) -> None:
        self._lines.append(text)
        if self._mode == "bench":
            self.output.appendPlainText(text)

    def _checked(self, code: int, tail: str, cancelled: bool) -> None:
        self._run_buttons(True)
        if self._mode == "bench":
            self.status.setText(
                "对比已取消。" if cancelled else
                f"对比完成，报告在 {self._bench_report}" if code == 0 else
                "没找到可用的片段：所选文件夹里要有视频和同名的“_校对”字幕。" if code == 2 else
                f"对比没跑完（退出码 {code}），原因见下面。")
            if code not in (0, 2) and tail and not cancelled:
                self.output.appendPlainText(tail)
            return
        payload = next((line for line in reversed(self._lines) if line.lstrip().startswith("[")), "")
        try:
            checks = [Check(**item) for item in json.loads(payload)]
        except (ValueError, TypeError):
            self.output.setPlainText("自检没有跑出结果：\n" + (tail or "\n".join(self._lines[-8:]) or f"退出码 {code}"))
            self.status.setText("自检失败：识别用的 Python 打不开，或没装 AutoSlice 依赖。")
            return
        self.output.setPlainText(format_checks(checks))
        failed = sum(1 for item in checks if item.status == "fail")
        warned = sum(1 for item in checks if item.status == "warn")
        self.status.setText("环境没问题。" if not failed and not warned else
                            f"{failed} 项需要处理、{warned} 项值得留意，详情见下面。")

    def stop(self) -> None:
        if self._runner.running:
            self._runner.cancel()


class AsrBanner(QWidget):
    """字幕页上方的提示条：没有字幕时提示“识别字幕”，识别中显示进度，失败显示原因。"""

    start_clicked = Signal()
    cancel_clicked = Signal()
    settings_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("asrBanner")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        row = QHBoxLayout(self)
        row.setContentsMargins(14, 10, 14, 10)
        row.setSpacing(12)
        column = QVBoxLayout()
        column.setSpacing(4)
        self.title = label("", "sectionTitle")
        self.detail = label("", "muted")
        self.detail.setWordWrap(True)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        column.addWidget(self.title)
        column.addWidget(self.detail)
        column.addWidget(self.bar)
        row.addLayout(column, 1)
        self.settings_button = QPushButton("识别设置")
        self.settings_button.clicked.connect(self.settings_clicked)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.clicked.connect(self.cancel_clicked)
        self.start_button = QPushButton("识别字幕")
        self.start_button.setObjectName("primary")
        self.start_button.setIcon(desktop_icon("bolt", COLORS.on_accent, SIZES.tool_icon_size))
        self.start_button.clicked.connect(self.start_clicked)
        for button in (self.settings_button, self.cancel_button, self.start_button):
            row.addWidget(button, 0, Qt.AlignmentFlag.AlignVCenter)
        self.hide()

    def _set(self, title: str, detail: str, *, progress: bool, start: str = "", cancel: bool = False) -> None:
        self.title.setText(title)
        self.detail.setText(detail)
        self.detail.setVisible(bool(detail))
        self.bar.setVisible(progress)
        self.start_button.setVisible(bool(start))
        self.start_button.setText(start)
        self.cancel_button.setVisible(cancel)
        self.settings_button.setVisible(not progress)
        self.show()

    def show_idle(self, detail: str) -> None:
        self._set("这个视频还没有字幕", detail, progress=False, start="识别字幕")

    def show_running(self, message: str, fraction: float) -> None:
        self._set("正在识别字幕", message, progress=True, cancel=True)
        self.bar.setValue(int(max(0.0, min(1.0, fraction)) * 1000))

    def show_error(self, message: str) -> None:
        self._set("识别没成功", message, progress=False, start="重试")

    def show_cancelled(self) -> None:
        self._set("已取消", "已识别的部分保留着，再点“识别字幕”会接着往下。", progress=False, start="继续识别")
