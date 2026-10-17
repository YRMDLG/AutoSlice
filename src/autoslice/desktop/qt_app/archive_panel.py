"""设置页“学习旧切片”：选旧投稿文件夹、后台扫描、看报告、确认学到的建议、一键清除。

只读档案：不修改、不上传，结果只存在本机应用数据目录。扫描在后台线程里跑，随时能停，下次接着扫。
"""

from __future__ import annotations

import threading
import time

from PySide6.QtCore import QObject, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from autoslice.desktop.archive import ArchiveService, Proposal, ScanOptions, ScanProgress
from autoslice.desktop.qt_preview import motion
from autoslice.desktop.qt_preview.icons import icon as desktop_icon
from autoslice.desktop.qt_preview.theme import COLORS, SIZES
from autoslice.desktop.qt_preview.window import label

_KIND_NAMES = {"replacement": "固定纠错", "glossary": "术语", "titles": "标题范例", "subtitle_limits": "字幕阈值"}
# 这几类默认勾选；术语候选里难免有噪音，默认不勾，由用户挑。
_DEFAULT_CHECKED = {"replacement", "titles", "subtitle_limits"}


class _Bridge(QObject):
    """后台线程把进度发回界面线程。"""

    progress = Signal(object)


class ProposalsDialog(QDialog):
    """学到的建议：勾选后应用，或忽略；应用后才会写进本机的词库、术语表和标题范例。"""

    def __init__(self, service: ArchiveService, proposals: list[Proposal], on_applied, parent=None):
        super().__init__(parent)
        self.setWindowTitle("学到的建议")
        self.resize(760, 560)
        self._service, self._proposals, self._on_applied = service, proposals, on_applied
        layout = QVBoxLayout(self)
        intro = label(
            "这是从你的旧切片里学到的，勾选后点“应用所选”才会生效；不想要的点“忽略所选”，以后不再提。固定纠错写进本机覆盖词库"
            "（每个主播最多 100 条），术语和标题范例只在字幕检查、投稿文案时挑和当前视频有关的用。", "muted")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.list = QListWidget()
        for item in proposals:
            row = QListWidgetItem(f"[{_KIND_NAMES.get(item.kind, item.kind)}] {item.label} · {item.title}　—　{item.evidence}")
            row.setFlags(row.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            row.setCheckState(Qt.CheckState.Checked if item.kind in _DEFAULT_CHECKED else Qt.CheckState.Unchecked)
            row.setData(Qt.ItemDataRole.UserRole, item.id)
            self.list.addItem(row)
        layout.addWidget(self.list, 1)
        self.message = label("", "muted")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        row = QHBoxLayout()
        for text, state in (("全选", Qt.CheckState.Checked), ("全不选", Qt.CheckState.Unchecked)):
            button = QPushButton(text)
            button.setObjectName("quiet")
            button.clicked.connect(lambda _checked=False, value=state: self._set_all(value))
            row.addWidget(button)
        row.addStretch(1)
        self.reject_button = QPushButton("忽略所选")
        self.reject_button.clicked.connect(self._reject)
        row.addWidget(self.reject_button)
        self.apply_button = QPushButton("应用所选")
        self.apply_button.setObjectName("primary")
        self.apply_button.clicked.connect(self._apply)
        row.addWidget(self.apply_button)
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        layout.addLayout(row)
        motion.install_tree(self)

    def _set_all(self, state) -> None:
        for index in range(self.list.count()):
            self.list.item(index).setCheckState(state)

    def checked(self) -> list[Proposal]:
        ids = {
            self.list.item(index).data(Qt.ItemDataRole.UserRole) for index in range(self.list.count())
            if self.list.item(index).checkState() == Qt.CheckState.Checked
        }
        return [item for item in self._proposals if item.id in ids]

    def _remove(self, done: set[str]) -> None:
        for index in reversed(range(self.list.count())):
            if self.list.item(index).data(Qt.ItemDataRole.UserRole) in done:
                self.list.takeItem(index)
        self._proposals = [item for item in self._proposals if item.id not in done]

    def _apply(self) -> None:
        chosen = self.checked()
        if not chosen:
            self.message.setText("还没勾选任何一条。")
            return
        applied, failures = self._service.apply(chosen)
        self._remove(set(applied))
        self.message.setText(f"已应用 {len(applied)} 条。" + (f" {len(failures)} 条没写进去：{failures[0]}" if failures else ""))
        self._on_applied()

    def _reject(self) -> None:
        chosen = self.checked()
        self._service.reject(chosen)
        self._remove({item.id for item in chosen})
        self.message.setText(f"已忽略 {len(chosen)} 条，以后不再提。")


class ArchivePanel(QWidget):
    def __init__(self, service: ArchiveService, run, on_applied=lambda: None, parent=None):
        """run(action, callback)：在后台执行 action，回调回到界面线程（和 AI 设置面板同一个约定）。"""

        super().__init__(parent)
        self.service, self._run, self._on_applied = service, run, on_applied
        self._cancel: threading.Event | None = None
        self._bridge = _Bridge(self)
        self._bridge.progress.connect(self._show_progress)
        self.setObjectName("settingsCard")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 18)
        layout.setSpacing(10)
        layout.addWidget(label("学习旧切片", "cardTitle"))
        help_text = label(
            "扫描你本机保留的旧投稿文件夹，学你的校对习惯、常用术语、标题口吻和封面风格。只读，不修改任何文件、不上传；"
            "结果只存在本机，学到的东西要你确认才会生效。文件夹很多也没关系：可以随时停止，下次接着扫，没变的会直接跳过。",
            "muted",
        )
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        self.folders = QListWidget()
        self.folders.itemSelectionChanged.connect(self._sync)
        layout.addWidget(self.folders)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.add_button = QPushButton("添加文件夹…")
        self.add_button.setIcon(desktop_icon("folder", COLORS.muted, SIZES.tool_icon_size))
        self.add_button.clicked.connect(self._add)
        row.addWidget(self.add_button)
        self.remove_button = QPushButton("移除所选")
        self.remove_button.setObjectName("quiet")
        self.remove_button.clicked.connect(self._remove)
        row.addWidget(self.remove_button)
        self.covers_check = QCheckBox("也分析旧封面（稍慢）")
        self.covers_check.setChecked(True)
        row.addWidget(self.covers_check)
        row.addStretch(1)
        self.stop_button = QPushButton("停止")
        self.stop_button.clicked.connect(self.stop)
        row.addWidget(self.stop_button)
        self.start_button = QPushButton("开始扫描")
        self.start_button.setObjectName("primary")
        self.start_button.setIcon(desktop_icon("refresh", COLORS.on_accent, SIZES.tool_icon_size))
        self.start_button.clicked.connect(self._start)
        row.addWidget(self.start_button)
        layout.addLayout(row)
        self.status = label("", "muted")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        result_row = QHBoxLayout()
        result_row.setSpacing(8)
        self.report_button = QPushButton("查看报告")
        self.report_button.clicked.connect(self._open_report)
        result_row.addWidget(self.report_button)
        self.folder_button = QPushButton("打开摘要文件夹")
        self.folder_button.setToolTip("里面的 digest.json 可以整份交给 GPT：只有统计和少量字幕片段，没有视频和完整路径")
        self.folder_button.clicked.connect(self._open_folder)
        result_row.addWidget(self.folder_button)
        self.proposals_button = QPushButton("学到的建议")
        self.proposals_button.clicked.connect(self._open_proposals)
        result_row.addWidget(self.proposals_button)
        result_row.addStretch(1)
        self.forget_button = QPushButton("清除学到的内容…")
        self.forget_button.setObjectName("quiet")
        self.forget_button.clicked.connect(self._forget)
        result_row.addWidget(self.forget_button)
        layout.addLayout(result_row)
        self.refresh()

    # ── 状态 ──
    @property
    def running(self) -> bool:
        return self._cancel is not None

    def refresh(self) -> None:
        selected = {item.text() for item in self.folders.selectedItems()}
        self.folders.clear()
        for path in self.service.roots():
            item = QListWidgetItem(path)
            self.folders.addItem(item)
            item.setSelected(path in selected)
        # 没添加文件夹时不画一个空的黑框；有了按行数撑开，最多露出四行
        self.folders.setVisible(bool(self.folders.count()))
        self.folders.setFixedHeight(min(4, self.folders.count()) * 30 + 14)
        if not self.running:
            summary = self.service.summary()
            if summary["entries"]:
                pending = self.service.pending_count()
                when = time.strftime("%Y-%m-%d %H:%M", time.localtime(summary["last_scan"])) if summary["last_scan"] else "—"
                self.status.setText(
                    f"已学习 {summary['entries']} 个文件夹，其中 {summary['with_both']} 个有“初稿 + 校对稿”；"
                    f"最近一次扫描 {when}。" + (f" 有 {pending} 条建议等你确认。" if pending else "")
                )
            else:
                self.status.setText("还没扫描过：添加存放旧切片的文件夹（可以添加好几个），点“开始扫描”。")
        self._sync()

    def _sync(self) -> None:
        has_data = self.service.has_data() and bool(self.service.summary()["entries"])
        self.start_button.setEnabled(bool(self.folders.count()) and not self.running)
        self.stop_button.setEnabled(self.running)
        self.add_button.setEnabled(not self.running)
        self.remove_button.setEnabled(bool(self.folders.selectedItems()) and not self.running)
        for button in (self.report_button, self.folder_button, self.proposals_button, self.forget_button):
            button.setEnabled(has_data and not self.running)

    # ── 文件夹 ──
    def _add(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择存放旧切片的文件夹")
        if path:
            self.service.add_root(path)
            self.refresh()

    def _remove(self) -> None:
        for item in self.folders.selectedItems():
            self.service.remove_root(item.text())
        self.refresh()

    # ── 扫描 ──
    def _start(self) -> None:
        if self.running:
            return
        self._cancel = threading.Event()
        cancel, service = self._cancel, self.service
        options = ScanOptions(covers=self.covers_check.isChecked())
        self.status.setText("正在扫描…")
        self._sync()

        def work():
            result = service.scan(None, options, progress=self._bridge.progress.emit, cancel=cancel)
            service.report()
            return result

        self._run(work, self._finished)

    def _show_progress(self, state: ScanProgress) -> None:
        if self.running:
            self.status.setText(
                f"已看 {state.folders} 个文件夹 · 认出 {state.entries} 个 · 新学 {state.changed} · 跳过 {state.skipped}"
                + (f" · 出错 {state.errors}" if state.errors else "")
            )

    def _finished(self, result, error) -> None:
        self._cancel = None
        self.refresh()
        if error is not None:
            self.status.setText(f"扫描失败：{error}")
        elif result.cancelled:
            self.status.setText(f"已停止（已学 {result.entries} 个文件夹，下次接着扫）。" + self.status.text())
        elif result.errors:
            self.status.setText(self.status.text() + f" 有 {len(result.errors)} 个文件夹读取出错，已跳过。")
        self._on_applied()

    def stop(self) -> None:
        if self._cancel is not None:
            self._cancel.set()
            self.status.setText("正在停止…")

    # ── 结果 ──
    def _open_report(self) -> None:
        path = self.service.report_dir / "report.md"
        if not path.is_file():
            self.service.report()
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _open_folder(self) -> None:
        self.service.report_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.service.report_dir)))

    def _open_proposals(self) -> None:
        proposals = self.service.proposals()
        if not proposals:
            QMessageBox.information(self, "学到的建议", "现在没有待确认的建议。")
            return
        dialog = ProposalsDialog(self.service, proposals, self._applied, self)
        dialog.exec()
        self.refresh()

    def _applied(self) -> None:
        self._on_applied()
        self.refresh()

    def _forget(self) -> None:
        answer = QMessageBox.question(
            self, "清除学到的内容",
            "会删掉扫描库、报告、待确认建议和学到的术语/标题范例；不会动你的旧切片文件夹，"
            "也不会撤销已经写进词库的固定纠错。确定吗？",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.service.forget_everything()
            self._on_applied()
            self.refresh()
