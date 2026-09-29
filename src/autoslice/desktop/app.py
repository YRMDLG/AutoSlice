"""Desktop vNext 的轻量 Tk 外壳。"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

from autoslice.desktop.projects import ProjectSnapshot, SubmissionProject, SubmissionProjectService
from autoslice.desktop.subtitles import SubtitleDocument


class DesktopApp:
    """两个功能页共享项目快照；字幕文档在切页时保留。"""

    PAGES = ("字幕校对", "封面制作", "设置")

    def __init__(self, window: tk.Tk, service: SubmissionProjectService | None = None) -> None:
        self.window = window
        self.service = service or SubmissionProjectService()
        self.page = "字幕校对"
        self.selection: dict[str, str | None] = {page: None for page in self.PAGES}
        self.subtitle_document: SubtitleDocument | None = None
        self.selected_cue_index: int | None = None
        self._rendering_projects = False
        self.window.title("AutoSlice Desktop vNext")
        self.window.minsize(840, 520)
        self.window.protocol("WM_DELETE_WINDOW", self._close)
        self.window.bind("<Control-z>", self._undo)
        self.window.bind("<Control-y>", self._redo)
        self.window.bind("<Delete>", self._delete_selected)

        shell = ttk.Frame(window, padding=14)
        shell.pack(fill="both", expand=True)
        header = ttk.Frame(shell)
        header.pack(fill="x")
        ttk.Label(header, text="AutoSlice", font=("Segoe UI", 15, "bold")).pack(side="left")
        self.navigation = ttk.Frame(header)
        self.navigation.pack(side="left", padx=24)
        for page in self.PAGES:
            ttk.Button(
                self.navigation, text=page, command=lambda target=page: self.show_page(target)
            ).pack(side="left", padx=3)
        self.refresh_button = ttk.Button(header, text="刷新", command=self.refresh)
        self.refresh_button.pack(side="right")
        self.path_label = ttk.Label(shell, text=str(self.service.root), foreground="#666666")
        self.path_label.pack(anchor="w", pady=(10, 2))
        self.status_label = ttk.Label(shell, text="未扫描")
        self.status_label.pack(anchor="w", pady=(0, 8))
        self.body = ttk.Frame(shell)
        self.body.pack(fill="both", expand=True)
        self.show_page(self.page)
        self.refresh()

    def show_page(self, page: str) -> None:
        self.page = page
        for widget in self.body.winfo_children():
            widget.destroy()
        if page == "设置":
            ttk.Label(self.body, text="投稿目录", font=("Segoe UI", 13, "bold")).pack(anchor="w")
            ttk.Label(self.body, text=str(self.service.root)).pack(anchor="w", pady=10)
            ttk.Label(self.body, text="目录配置与更多设置将在后续任务开放。").pack(anchor="w")
            return
        list_frame = ttk.Frame(self.body, width=240)
        list_frame.pack(side="left", fill="y", padx=(0, 16))
        list_frame.pack_propagate(False)
        ttk.Label(list_frame, text="投稿项目", font=("Segoe UI", 12, "bold")).pack(anchor="w")
        self.project_list = tk.Listbox(list_frame, exportselection=False)
        self.project_list.pack(fill="both", expand=True, pady=(8, 0))
        self.project_list.bind("<<ListboxSelect>>", self._select_project)
        workspace = ttk.Frame(self.body)
        workspace.pack(side="left", fill="both", expand=True)
        ttk.Label(workspace, text=page, font=("Segoe UI", 14, "bold")).pack(anchor="w")
        if page == "字幕校对":
            self._build_subtitle_workspace(workspace)
        else:
            self.detail_label = ttk.Label(workspace, text="请选择项目", justify="left", wraplength=450)
            self.detail_label.pack(anchor="w", pady=18)
        self._render_projects(self.service.snapshot)

    def _build_subtitle_workspace(self, workspace: ttk.Frame) -> None:
        controls = ttk.Frame(workspace)
        controls.pack(fill="x", pady=(10, 5))
        self.video_choice = ttk.Combobox(controls, state="readonly", width=35)
        self.video_choice.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.video_choice.bind("<<ComboboxSelected>>", self._select_video)
        self.save_button = ttk.Button(controls, text="保存校对字幕", command=self._save_subtitles)
        self.save_button.pack(side="right")
        self.subtitle_status = ttk.Label(workspace, text="请选择项目", wraplength=650)
        self.subtitle_status.pack(anchor="w", pady=(0, 8))
        self.subtitle_count = ttk.Label(workspace, text="")
        self.subtitle_count.pack(anchor="w")
        columns = ttk.Frame(workspace)
        columns.pack(fill="x", pady=(8, 0))
        ttk.Label(columns, text="序号", width=5).pack(side="left")
        ttk.Label(columns, text="开始 → 结束", width=28).pack(side="left")
        ttk.Label(columns, text="字幕文字").pack(side="left")
        holder = ttk.Frame(workspace)
        holder.pack(fill="both", expand=True, pady=(6, 0))
        self.cue_canvas = tk.Canvas(holder, highlightthickness=0)
        scroll = ttk.Scrollbar(holder, orient="vertical", command=self.cue_canvas.yview)
        self.cue_canvas.configure(yscrollcommand=scroll.set)
        self.cue_canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.cue_rows = ttk.Frame(self.cue_canvas)
        self.cue_window = self.cue_canvas.create_window((0, 0), window=self.cue_rows, anchor="nw")
        self.cue_rows.bind(
            "<Configure>",
            lambda _event: self.cue_canvas.configure(scrollregion=self.cue_canvas.bbox("all")),
        )
        self.cue_canvas.bind(
            "<Configure>",
            lambda event: self.cue_canvas.itemconfigure(self.cue_window, width=event.width),
        )
        self.cue_canvas.bind("<MouseWheel>", self._scroll_cues)
        self._update_subtitle_status()

    def refresh(self) -> None:
        if not self._resolve_unsaved():
            return
        self.subtitle_document = None
        self.selected_cue_index = None
        snapshot = self.service.refresh()
        self.status_label.configure(text=f"{snapshot.status} · {len(snapshot.projects)} 个项目")
        if snapshot.message:
            self.status_label.configure(text=f"{snapshot.status}：{snapshot.message}")
        if self.page != "设置":
            self._render_projects(snapshot)

    def _render_projects(self, snapshot: ProjectSnapshot) -> None:
        self._rendering_projects = True
        try:
            self.project_list.delete(0, tk.END)
            for project in snapshot.projects:
                self.project_list.insert(tk.END, f"{project.title}  ·  {project.status}")
            selected_id = self.selection[self.page]
            selected_index = next(
                (i for i, project in enumerate(snapshot.projects) if project.id == selected_id), None
            )
            if selected_index is None:
                self.selection[self.page] = None
                if self.page == "字幕校对":
                    self._render_subtitles()
                    self.subtitle_status.configure(
                        text=snapshot.status if not snapshot.projects else "请选择项目"
                    )
                else:
                    self.detail_label.configure(
                        text=snapshot.status if not snapshot.projects else "请选择项目"
                    )
                return
            self.project_list.selection_set(selected_index)
            self._show_project(snapshot.projects[selected_index])
        finally:
            self._rendering_projects = False

    def _select_project(self, _event: tk.Event) -> None:
        if self._rendering_projects:
            return
        selected = self.project_list.curselection()
        if not selected:
            return
        project = self.service.snapshot.projects[selected[0]]
        if project.id != self.selection[self.page] and not self._resolve_unsaved():
            self._restore_project_selection()
            return
        if project.id != self.selection[self.page]:
            self.subtitle_document = None
            self.selected_cue_index = None
        self.selection[self.page] = project.id
        self._show_project(project)

    def _restore_project_selection(self) -> None:
        self._rendering_projects = True
        try:
            self.project_list.selection_clear(0, tk.END)
            for index, project in enumerate(self.service.snapshot.projects):
                if project.id == self.selection[self.page]:
                    self.project_list.selection_set(index)
                    break
        finally:
            self._rendering_projects = False

    def _show_project(self, project: SubmissionProject) -> None:
        if self.page == "字幕校对":
            self.current_videos = project.videos
            self.video_choice["values"] = [video.name for video in project.videos]
            if not project.videos:
                self.video_choice.set("")
                self.subtitle_document = None
                self._render_subtitles()
                self.subtitle_status.configure(text=project.error or "该项目缺少视频")
                return
            current_path = self.subtitle_document.video.path if self.subtitle_document else None
            index = next((i for i, video in enumerate(project.videos) if video.path == current_path), 0)
            self.video_choice.current(index)
            if self.subtitle_document is None:
                self._load_video(project.videos[index])
            else:
                self._render_subtitles()
            return
        detail = f"{project.title}\n{project.directory}\n{project.status} · {len(project.videos)} 个视频"
        if project.error:
            detail += f"\n{project.error}"
        self.detail_label.configure(text=detail + "\n\n工作台将在后续任务实现。")

    def _select_video(self, _event: tk.Event) -> None:
        selected = self.video_choice.current()
        if selected < 0 or selected >= len(self.current_videos):
            return
        video = self.current_videos[selected]
        if self.subtitle_document and video.path == self.subtitle_document.video.path:
            return
        if not self._resolve_unsaved():
            if self.subtitle_document:
                current = next(
                    (i for i, item in enumerate(self.current_videos)
                     if item.path == self.subtitle_document.video.path), 0
                )
                self.video_choice.current(current)
            return
        self._load_video(video)

    def _load_video(self, video) -> None:
        self.subtitle_document = None
        self.selected_cue_index = None
        try:
            self.subtitle_document = SubtitleDocument.load(video)
        except (OSError, ValueError, KeyError) as exc:
            self.subtitle_status.configure(text=f"字幕无法加载：{exc}")
        self._render_subtitles()

    def _render_subtitles(self) -> None:
        if self.page != "字幕校对":
            return
        for widget in self.cue_rows.winfo_children():
            widget.destroy()
        self._cue_labels: dict[int, tuple[tk.Label, tk.Label]] = {}
        self._cue_editors: dict[int, tk.Text] = {}
        document = self.subtitle_document
        if document is None:
            self.subtitle_count.configure(text="")
            self.save_button.state(["disabled"])
            return
        for entry in document.entries:
            row = ttk.Frame(self.cue_rows, padding=(0, 5))
            row.pack(fill="x")
            number = tk.Label(row, text=str(entry.index), width=5, anchor="w", takefocus=1)
            number.pack(side="left", anchor="n")
            timing = tk.Label(
                row, text=f"{entry.start} → {entry.end}", width=28, anchor="w", takefocus=1
            )
            timing.pack(side="left", anchor="n")
            self._cue_labels[entry.index] = (number, timing)
            editor = tk.Text(
                row, height=max(2, min(4, entry.text.count("\n") + 1)),
                wrap="word", font=("Segoe UI", 10), undo=False
            )
            editor.insert("1.0", entry.text)
            editor.pack(side="left", fill="x", expand=True)
            self._cue_editors[entry.index] = editor
            for label in (number, timing):
                label.bind(
                    "<Button-1>",
                    lambda _event, index=entry.index, widget=label: (
                        widget.focus_set(), self._focus_cue(index)
                    ),
                )
            editor.bind("<FocusIn>", lambda _event, index=entry.index: self._focus_cue(index))
            editor.bind("<Button-1>", lambda _event, index=entry.index: self._focus_cue(index))
            editor.bind(
                "<KeyRelease>",
                lambda _event, index=entry.index, widget=editor: self._edit_cue(index, widget),
            )
            editor.bind("<Control-z>", self._undo)
            editor.bind("<Control-y>", self._redo)
            editor.bind("<Delete>", self._delete_selected)
            editor.bind("<MouseWheel>", self._scroll_cues)
        if self.selected_cue_index in self._cue_labels:
            self._focus_cue(self.selected_cue_index)
        else:
            self.selected_cue_index = None
        self._update_subtitle_status()

    def _focus_cue(self, index: int) -> None:
        self.selected_cue_index = index
        for cue_index, labels in self._cue_labels.items():
            for label in labels:
                label.configure(bg="#d8e9ff" if cue_index == index else "SystemButtonFace")

    def _scroll_cues(self, event: tk.Event) -> str:
        self.cue_canvas.yview_scroll(-int(event.delta / 120), "units")
        return "break"

    def _edit_cue(self, index: int, editor: tk.Text) -> None:
        if self.subtitle_document is None or not editor.winfo_exists():
            return
        self.subtitle_document.edit_text(index, editor.get("1.0", "end-1c"))
        self._update_subtitle_status()

    def _update_subtitle_status(self) -> None:
        if self.page != "字幕校对":
            return
        document = self.subtitle_document
        if document is None:
            self.save_button.state(["disabled"])
            return
        self.save_button.state(["!disabled"])
        self.subtitle_count.configure(text=f"{len(document.entries)} 条字幕 · 时间只读")
        state = "有未保存修改" if document.dirty else "已保存 / 无未保存修改"
        self.subtitle_status.configure(
            text=f"{document.video.name} · {state} · 点击文字直接修改；选中后 Delete 删除，Ctrl+Z / Ctrl+Y 撤销重做"
        )

    def _delete_selected(self, _event: tk.Event | None = None) -> str | None:
        if self.page != "字幕校对" or not self.subtitle_document or self.selected_cue_index is None:
            return None
        focus = self.window.focus_get()
        if _event is not None and (focus is None or not str(focus).startswith(str(self.cue_rows))):
            return None
        try:
            self.subtitle_document.delete(self.selected_cue_index)
        except ValueError as exc:
            self.subtitle_status.configure(text=str(exc))
        else:
            self.selected_cue_index = None
            self._render_subtitles()
        return "break"

    def _undo(self, _event: tk.Event | None = None) -> str | None:
        if self.page == "字幕校对" and self.subtitle_document:
            if self.subtitle_document.undo():
                self._render_subtitles()
                if self.selected_cue_index is not None:
                    self._cue_editors[self.selected_cue_index].focus_set()
            return "break"
        return None

    def _redo(self, _event: tk.Event | None = None) -> str | None:
        if self.page == "字幕校对" and self.subtitle_document:
            if self.subtitle_document.redo():
                self._render_subtitles()
                if self.selected_cue_index is not None:
                    self._cue_editors[self.selected_cue_index].focus_set()
            return "break"
        return None

    def _save_subtitles(self) -> bool:
        if self.subtitle_document is None:
            return False
        try:
            output = self.subtitle_document.save()
        except (OSError, ValueError) as exc:
            if self.page == "字幕校对":
                self.subtitle_status.configure(text=f"保存失败：{exc}")
            else:
                self.status_label.configure(text=f"保存失败：{exc}")
            return False
        if self.page == "字幕校对":
            self._update_subtitle_status()
            self.subtitle_status.configure(text=f"已保存校对字幕：{output}")
        else:
            self.status_label.configure(text=f"已保存校对字幕：{output}")
        return True

    def _resolve_unsaved(self) -> bool:
        if self.subtitle_document is None or not self.subtitle_document.dirty:
            return True
        answer = messagebox.askyesnocancel(
            "未保存的字幕修改",
            "当前字幕有未保存修改。\n是：保存校对字幕后继续\n否：放弃修改后继续\n取消：留在当前项目",
            parent=self.window,
        )
        if answer is None:
            return False
        return self._save_subtitles() if answer else True

    def _close(self) -> None:
        if self._resolve_unsaved():
            self.window.destroy()


def main() -> None:
    """独立启动 vNext，不启动旧 Web 或 AutoCover UI。"""

    window = tk.Tk()
    DesktopApp(window)
    window.mainloop()
