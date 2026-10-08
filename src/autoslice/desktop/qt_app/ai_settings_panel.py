"""设置页“AI 接口”：填中转地址、密钥和模型，保存后可一键测试文字与看图。"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from autoslice.desktop.ai_settings import (
    SUGGESTED_MODELS,
    check_connection,
    is_plain_http,
    read_ai_settings,
    save_ai_settings,
    split_endpoint,
)
from autoslice.desktop.qt_preview.window import label

_API_TYPES = (
    ("openai-responses", "OpenAI Responses（…/responses）"),
    ("openai", "OpenAI 兼容（…/chat/completions）"),
    ("anthropic", "Anthropic（…/messages）"),
)


def _model_box() -> QComboBox:
    box = QComboBox()
    box.setEditable(True)
    box.addItems(SUGGESTED_MODELS)
    box.setMinimumWidth(220)
    return box


class AISettingsPanel(QWidget):
    def __init__(self, run, parent=None):
        """run(action, callback)：在后台执行测试，回调回到界面线程。"""

        super().__init__(parent)
        self._run = run
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(label("AI 接口", "sectionTitle"))
        help_text = label(
            "字幕 AI 检查和封面 AI 共用这里的配置；AI 只在你手动点击时运行。"
            "接口地址可以直接粘贴完整地址（如 …/v1/responses），会自动识别接口类型。", "muted",
        )
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        form = QFormLayout()
        form.setSpacing(8)
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://你的中转/v1 或完整的 …/v1/responses")
        self.url_edit.editingFinished.connect(self._url_changed)
        self.url_edit.textChanged.connect(self._update_http_warning)
        form.addRow("接口地址", self.url_edit)
        self.type_box = QComboBox()
        for key, text in _API_TYPES:
            self.type_box.addItem(text, key)
        form.addRow("接口类型", self.type_box)
        self.token_edit = QLineEdit()
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("密钥", self.token_edit)
        self.text_model_box = _model_box()
        form.addRow("文字模型", self.text_model_box)
        self.vision_model_box = _model_box()
        self.vision_model_box.setToolTip("封面选帧、出方案和点评要看图，需支持图片输入的模型")
        form.addRow("看图模型", self.vision_model_box)
        layout.addLayout(form)
        self.http_check = QCheckBox("允许 HTTP 明文连接")
        self.http_warning = label("这是非本机的 http 地址：密钥和内容不加密传输，只在信任的网络里使用。", "muted")
        self.http_warning.setWordWrap(True)
        layout.addWidget(self.http_check)
        layout.addWidget(self.http_warning)
        row = QHBoxLayout()
        self.status = label("", "muted")
        self.status.setWordWrap(True)
        row.addWidget(self.status, 1)
        self.save_button = QPushButton("保存")
        self.save_button.setFixedHeight(28)
        self.save_button.clicked.connect(self._save)
        row.addWidget(self.save_button)
        self.test_button = QPushButton("测试连接")
        self.test_button.setObjectName("quiet")
        self.test_button.setFixedHeight(28)
        self.test_button.clicked.connect(self._test)
        row.addWidget(self.test_button)
        layout.addLayout(row)
        self.refresh()

    def refresh(self) -> None:
        settings = read_ai_settings()
        self.url_edit.setText(settings.base_url)
        index = self.type_box.findData(settings.api_type)
        self.type_box.setCurrentIndex(max(0, index))
        self.token_edit.clear()
        self.token_edit.setPlaceholderText("已保存（留空则不改）" if settings.has_token else "粘贴你的密钥")
        self.text_model_box.setCurrentText(settings.text_model or SUGGESTED_MODELS[0])
        self.vision_model_box.setCurrentText(settings.vision_model or settings.text_model or SUGGESTED_MODELS[0])
        self.http_check.setChecked(settings.allow_insecure_http)
        from_env = settings.source == "env"
        for widget in (self.url_edit, self.type_box, self.token_edit, self.text_model_box,
                       self.vision_model_box, self.http_check, self.save_button):
            widget.setEnabled(not from_env)
        if from_env:
            self.status.setText("当前由环境变量 AUTOSLICE_API_* 配置，这里只读。")
        elif settings.error:
            self.status.setText(f"配置有误：{settings.error}")
        else:
            self.status.setText("已配置，可点“测试连接”。" if settings.configured else "还没配置。")
        self._update_http_warning()

    def _url_changed(self) -> None:
        base, kind = split_endpoint(self.url_edit.text(), self.type_box.currentData())
        if self.url_edit.text().strip().rstrip("/") != base:
            self.url_edit.setText(base)
        self.type_box.setCurrentIndex(max(0, self.type_box.findData(kind)))

    def _update_http_warning(self) -> None:
        plain = is_plain_http(self.url_edit.text())
        self.http_check.setVisible(plain)
        self.http_warning.setVisible(plain)

    def _save(self) -> bool:
        try:
            path = save_ai_settings(
                base_url=self.url_edit.text(), api_type=self.type_box.currentData(),
                token=self.token_edit.text(), text_model=self.text_model_box.currentText(),
                vision_model=self.vision_model_box.currentText(),
                allow_insecure_http=self.http_check.isChecked() and is_plain_http(self.url_edit.text()),
            )
        except (OSError, ValueError) as exc:
            self.status.setText(f"没保存：{exc}")
            return False
        self.refresh()
        self.status.setText(f"已保存到 {path}")
        return True

    def _test(self) -> None:
        if self.save_button.isEnabled() and (self.token_edit.text().strip() or self._form_changed()):
            if not self._save():
                return
        self.test_button.setEnabled(False)
        self.status.setText("正在测试文字模型和看图模型…")

        def done(result, error):
            self.test_button.setEnabled(True)
            self.status.setText(str(error) if error else "\n".join(result))

        self._run(check_connection, done)

    def _form_changed(self) -> bool:
        settings = read_ai_settings()
        base, kind = split_endpoint(self.url_edit.text(), self.type_box.currentData())
        return (
            base != settings.base_url or kind != settings.api_type
            or self.text_model_box.currentText() != settings.text_model
            or self.vision_model_box.currentText() != settings.vision_model
            or (self.http_check.isChecked() and is_plain_http(base)) != settings.allow_insecure_http
        )
