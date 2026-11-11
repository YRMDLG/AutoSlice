"""设置页“AI 接口”：选服务商或填中转地址、密钥和模型，可读模型列表，保存后一键测试文字与看图。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from autoslice.desktop.ai_settings import (
    PROVIDER_PRESETS,
    SUGGESTED_MODELS,
    check_connection,
    is_plain_http,
    list_models,
    preset_for,
    read_ai_settings,
    save_ai_settings,
    split_endpoint,
)
from autoslice.desktop.qt_preview import popups
from autoslice.desktop.qt_preview.icons import icon as desktop_icon
from autoslice.desktop.qt_preview.theme import COLORS, SIZES
from autoslice.desktop.qt_preview.window import label

_API_TYPES = (
    ("openai-responses", "OpenAI Responses（…/responses）"),
    ("openai", "OpenAI 兼容（…/chat/completions）"),
    ("anthropic", "Anthropic（…/messages）"),
)


def _model_box() -> popups.ComboBox:
    box = popups.ComboBox()
    box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    box.setEditable(True)
    box.addItems(SUGGESTED_MODELS)
    box.setMinimumWidth(220)
    return box


class AISettingsPanel(QWidget):
    def __init__(self, run, parent=None):
        """run(action, callback)：在后台执行测试，回调回到界面线程。"""

        super().__init__(parent)
        self._run = run
        self.setObjectName("settingsCard")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 18)
        layout.setSpacing(10)
        layout.addWidget(label("AI 接口", "cardTitle"))
        help_text = label(
            "字幕 AI 检查和封面 AI 共用这里的配置；AI 只在你手动点击时运行，不配也能用其它功能。"
            "选服务商会自动填地址；用中转就选“中转 / 其它”，可以直接粘贴完整地址（如 …/v1/responses）。", "muted",
        )
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        def add_row(text: str, widget) -> None:
            caption = label(text, "formLabel")
            caption.setMinimumWidth(64)
            caption.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            form.addRow(caption, widget)

        self.provider_box = popups.ComboBox()
        self.provider_box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        for key, title, *_rest in PROVIDER_PRESETS:
            self.provider_box.addItem(title, key)
        self.provider_box.activated.connect(self._provider_chosen)
        add_row("服务商", self.provider_box)
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("如 https://api.deepseek.com，或中转的 https://…/v1")
        self.url_edit.editingFinished.connect(self._url_changed)
        self.url_edit.textChanged.connect(self._update_http_warning)
        add_row("接口地址", self.url_edit)
        self.type_box = popups.ComboBox()
        self.type_box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        for key, text in _API_TYPES:
            self.type_box.addItem(text, key)
        add_row("接口类型", self.type_box)
        self.token_edit = QLineEdit()
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        add_row("密钥", self.token_edit)
        self.text_model_box = _model_box()
        text_row = QHBoxLayout()
        text_row.setContentsMargins(0, 0, 0, 0)
        text_row.setSpacing(8)
        text_row.addWidget(self.text_model_box, 1)
        self.models_button = QPushButton("获取模型列表")
        self.models_button.setToolTip("从接口读出能用的模型名，填进两个下拉框（不花额度）")
        self.models_button.clicked.connect(self._fetch_models)
        text_row.addWidget(self.models_button)
        text_holder = QWidget()
        text_holder.setLayout(text_row)
        add_row("文字模型", text_holder)
        self.vision_model_box = _model_box()
        self.vision_model_box.setToolTip("封面选帧、出方案和点评要看图，需支持图片输入的模型")
        add_row("看图模型", self.vision_model_box)
        layout.addLayout(form)
        self.http_check = QCheckBox("允许 HTTP 明文连接")
        self.http_warning = label("这是非本机的 http 地址：密钥和内容不加密传输，只在信任的网络里使用。", "muted")
        self.http_warning.setWordWrap(True)
        layout.addWidget(self.http_check)
        layout.addWidget(self.http_warning)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.status = label("", "muted")
        self.status.setWordWrap(True)
        row.addWidget(self.status, 1)
        self.test_button = QPushButton("测试连接")
        self.test_button.setIcon(desktop_icon("bolt", COLORS.muted, SIZES.tool_icon_size))
        self.test_button.clicked.connect(self._test)
        row.addWidget(self.test_button)
        self.save_button = QPushButton("保存")
        self.save_button.setObjectName("primary")
        self.save_button.setIcon(desktop_icon("save", COLORS.on_accent, SIZES.tool_icon_size))
        self.save_button.clicked.connect(self._save)
        row.addWidget(self.save_button)
        layout.addLayout(row)
        self.refresh()

    def refresh(self) -> None:
        settings = read_ai_settings()
        self.provider_box.setCurrentIndex(max(0, self.provider_box.findData(
            preset_for(settings.base_url) if settings.base_url else "deepseek")))
        self.url_edit.setText(settings.base_url)
        index = self.type_box.findData(settings.api_type)
        self.type_box.setCurrentIndex(max(0, index))
        self.token_edit.clear()
        self.token_edit.setPlaceholderText("已保存（留空则不改）" if settings.has_token else "粘贴你的密钥")
        if not settings.base_url and settings.source != "env":
            # 新用户：先按默认的 DeepSeek 预设填好，只差密钥
            self._provider_chosen()
        else:
            self._set_model_choices(self._preset_models(preset_for(settings.base_url)))
            self.text_model_box.setCurrentText(settings.text_model or SUGGESTED_MODELS[0])
            self.vision_model_box.setCurrentText(settings.vision_model or settings.text_model or SUGGESTED_MODELS[0])
        self.http_check.setChecked(settings.allow_insecure_http)
        from_env = settings.source == "env"
        for widget in (self.provider_box, self.url_edit, self.type_box, self.token_edit, self.text_model_box,
                       self.vision_model_box, self.models_button, self.http_check, self.save_button):
            widget.setEnabled(not from_env)
        if from_env:
            self.status.setText("当前由环境变量 AUTOSLICE_API_* 配置，这里只读。")
        elif settings.error:
            self.status.setText(f"配置有误：{settings.error}")
        else:
            self.status.setText("已配置，可点“测试连接”。" if settings.configured else "还没配置。")
        self._update_http_warning()

    def focus_first_missing(self) -> None:
        """从别处跳过来时，光标放到还没填的那一格（预设已填地址时通常是密钥）。"""

        missing_token = not self.token_edit.text().strip() and not read_ai_settings().has_token
        (self.token_edit if self.url_edit.text().strip() and missing_token else self.url_edit).setFocus()

    @staticmethod
    def _preset_models(key: str) -> tuple[str, ...]:
        return next((models for preset, *_rest, models in PROVIDER_PRESETS if preset == key), ())

    def _set_model_choices(self, text_models, vision_models=None) -> None:
        """换下拉候选但保留已填的名字（下拉框可编辑，候选只是方便选）。"""

        for box, models in ((self.text_model_box, text_models),
                            (self.vision_model_box, text_models if vision_models is None else vision_models)):
            current = box.currentText()
            box.clear()
            box.addItems(list(models))
            box.setCurrentText(current)

    def _provider_chosen(self, _index=None) -> None:
        key = self.provider_box.currentData()
        _key, _title, url, kind, models = next(item for item in PROVIDER_PRESETS if item[0] == key)
        if url:
            self.url_edit.setText(url)
            self.type_box.setCurrentIndex(max(0, self.type_box.findData(kind)))
        elif preset_for(self.url_edit.text()) != "custom":
            # 从官方预设切到中转：清掉官方地址，免得忘了改
            self.url_edit.clear()
        self._set_model_choices(models)
        if models:
            self.text_model_box.setCurrentText(models[0])
            self.vision_model_box.setCurrentText(models[0])
        self._update_http_warning()

    def _fetch_models(self) -> None:
        url, kind, token = self.url_edit.text(), self.type_box.currentData(), self.token_edit.text()
        self.models_button.setEnabled(False)
        self.status.setText("正在读模型列表…")

        def done(result, error):
            self.models_button.setEnabled(self.save_button.isEnabled())
            if error:
                self.status.setText(f"没读到模型列表：{error}")
                return
            ids = [model.id for model in result]
            seeing = [model.id for model in result if model.vision]
            known = any(model.vision is not None for model in result)
            self._set_model_choices(ids, seeing if seeing else ids)
            if self.text_model_box.currentText() not in ids:
                self.text_model_box.setCurrentText(ids[0])
            if seeing and self.vision_model_box.currentText() not in seeing:
                self.vision_model_box.setCurrentText(seeing[0])
            if seeing:
                tail = f"，能看图的：{'、'.join(seeing)}"
            elif known:
                tail = "，都不能看图：封面 AI 会用不了"
            else:
                tail = "，接口没标哪个能看图，看图模型请照服务商说明选"
            self.status.setText(f"找到 {len(ids)} 个模型{tail}。选好后点“保存”。")

        self._run(lambda: list_models(url, kind, token), done)

    def _url_changed(self) -> None:
        base, kind = split_endpoint(self.url_edit.text(), self.type_box.currentData())
        if self.url_edit.text().strip().rstrip("/") != base:
            self.url_edit.setText(base)
        self.type_box.setCurrentIndex(max(0, self.type_box.findData(kind)))
        self.provider_box.setCurrentIndex(max(0, self.provider_box.findData(preset_for(base))))

    def _update_http_warning(self) -> None:
        plain = is_plain_http(self.url_edit.text())
        self.http_check.setVisible(plain)
        self.http_warning.setVisible(plain)

    def _save(self) -> bool:
        if not self.url_edit.text().strip():
            # 原来直接报“API 配置缺少 base_url：<文件路径>”，看不懂
            self.status.setText("先选服务商或填接口地址（例如 https://api.deepseek.com），再保存或测试连接。")
            return False
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
        if not self.url_edit.text().strip() and not read_ai_settings().configured:
            self.status.setText("先填接口地址和密钥，再测试连接。")
            return
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
