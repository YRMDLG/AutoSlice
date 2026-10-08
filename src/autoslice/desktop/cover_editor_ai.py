"""封面 AI：读字幕、看画面出三套方案；“AI 改一改”给能一键应用的修改。都由用户点击才运行，不阻塞手动编辑。

混入 CoverEditorWidget；只用 self 上的状态，不单独实例化。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout

from autoslice_cover.document_layout import clamp_background_scale

from .cover_ai import AICopy, AIFrameNotes, CoverAIError, contact_sheet, jpeg_bytes
from .cover_copy import BasicCoverCopy
from .cover_model import (
    BackgroundObject,
    TextObject,
    object_for_profile,
    resize_text_style,
    set_profile_override,
    update_text_object,
)

# “AI 改一改”的一步幅度：拉近/拉远、主文案放大、A 缩小。
_FIX_ZOOM = 1.25
_FIX_GROW = 1.15
_FIX_SHRINK = 0.85
from .cover_style import streamer_key


class CoverAIMixin:
    def _ai_schemes(self):
        """AI 方案：读标题和字幕找爆点、写文案，再看画面选排法和配色，生成三套可编辑方案。"""

        if self.document is None or not self.draft.image_path or self.project is None or self.video is None:
            return
        self._ai_generation += 1
        generation, round_index = self._ai_generation, self._ai_round
        self._ai_round += 1
        document, image_path = self.document, self.draft.image_path
        project, video, service = self.project, self.video, self.service
        streamer = streamer_key(project.title) or ""
        self.ai_scheme_button.setEnabled(False)
        self.ai_hint.setVisible(False)
        self._set_notice("AI 正在读字幕、看画面出方案（通常十几秒到半分钟），这期间可以照常编辑…", "info")

        def work():
            recent = service.works.recent(streamer=streamer, limit=6)
            thumbnails = service.works.thumbnail_bytes(recent[:3])
            frame = jpeg_bytes(image_path)
            # 写文案（文字模型）和看原画面（看图模型）同时发出，不多等。
            with ThreadPoolExecutor(max_workers=2) as pool:
                notes_future = pool.submit(service.ai.inspect, frame)
                analysis = service.ai.analyze(
                    project.title, service.subtitle_cues(video), streamer=streamer, recent=recent,
                    round_index=round_index,
                )
                try:
                    notes = notes_future.result()
                except CoverAIError:
                    notes = AIFrameNotes(busy=frozenset(), note="")
            # 你的标题提炼出的那句固定放进候选（标题是切片员自己写的总结），AI 文案作补充。
            basic = service.basic_copy_variants(project.title)[0]
            from_title = AICopy(basic.context, basic.headline, "标题", "")
            copies = (from_title, *(item for item in analysis.copies if item.headline != from_title.headline))
            # 本地按 AI 的取景框裁切、在它说干净的地方排字，渲染成总图，AI 看成品挑：压脸、压字、看不懂的一票否决。
            candidates = service.ai_candidates(
                document, image_path, copies, busy=notes.busy, views=(notes.box, notes.close), text_zone=notes.text_zone,
            )
            sheet = contact_sheet(tuple(service.scheme_thumbnail(item.document, width=320) for item in candidates))
            choice = service.ai.choose(
                sheet, len(candidates), title=project.title, frame=frame, recent_thumbnails=thumbnails,
                round_index=round_index,
            )
            schemes = tuple(
                replace(candidates[pick.index], key=f"ai:{pick.direction}", label=f"AI·{pick.direction}", reason=pick.reason)
                for pick in choice.picks
            )
            return analysis, schemes, tuple(service.scheme_thumbnail(item.document, width=156) for item in schemes), choice

        self._run(work, lambda result, error: self._ai_schemes_ready(generation, result, error))

    def _ai_schemes_ready(self, generation: int, result, error):
        if generation != self._ai_generation:
            return
        self.ai_scheme_button.setEnabled(self.document is not None)
        if error is not None:
            message = str(error) if isinstance(error, CoverAIError) else f"AI 方案失败：{error}"
            self._set_notice(message, "error")
            self.status_changed.emit(message)
            return
        analysis, schemes, thumbnails, choice = result
        if not schemes:
            message = f"AI 觉得这批候选都不合格（{choice.rejected or '没说原因'}），可以再点一次换一批，或手动调整"
            self._set_notice(message, "warning")
            self.status_changed.emit(message)
            return
        # 本地方案若还在生成，结果作废；先清空卡片（AI 可能少于三张），再换成 AI 挑中的。
        self._clear_schemes()
        self._schemes_ready(self._scheme_generation, (schemes, thumbnails), None)
        self.scheme_title.setText("AI 方案")
        # AI 写的文案并入“换一版”的轮换，排在本地文案前面。
        ai_copies = tuple(item.as_basic() for item in analysis.copies)
        self._copy_variants = ai_copies + tuple(item for item in self._copy_variants if item not in ai_copies)
        self._copy_variant_index = -1
        if analysis.highlight is not None:
            highlight = analysis.highlight
            self.ai_hint.setText(f"爆点：「{highlight.quote}」（{highlight.start:.1f} 秒）{highlight.reason}")
            self.ai_hint.setToolTip("AI 从字幕里找到的这条视频最有看点的一句")
            self.ai_hint.setVisible(True)
        self._set_notice("")
        self.status_changed.emit("AI 方案已生成：点缩略图套用（Ctrl+Z 可撤销）；“换一版”也会轮换 AI 写的文案")

    def _ai_critique(self):
        """AI 改一改：看当前封面挑最影响效果的问题，每条给一个能一键应用的修改。"""

        if self.document is None or not self.draft.image_path or self.project is None or self.video is None:
            return
        self._ai_generation += 1
        generation = self._ai_generation
        document, canvas_key, service = self.document, self._canvas_key, self.service
        image_path, project, video = self.draft.image_path, self.project, self.video
        ids = self._primary_copy_ids()

        def text_of(role: str) -> str:
            current = object_for_profile(document, ids[role], canvas_key) if role in ids else None
            return current.text if isinstance(current, TextObject) and current.visible else ""

        texts = (text_of("A"), text_of("B"))
        self.ai_critique_button.setEnabled(False)
        self._set_notice("AI 正在看这张封面…", "info")

        def work():
            cover = jpeg_bytes(service.scheme_thumbnail(document, canvas_key=canvas_key, width=480), max_side=480)
            source = project.title + "\n" + "\n".join(text for _start, _end, text in service.subtitle_cues(video))
            return service.ai.suggest_fixes(
                cover, texts=texts, title=project.title, source=source, frame=jpeg_bytes(image_path),
            )

        self._run(work, lambda result, error: self._ai_critique_ready(generation, result, error))

    def _ai_critique_ready(self, generation: int, result, error):
        if generation != self._ai_generation:
            return
        self.ai_critique_button.setEnabled(self.document is not None)
        self._set_notice("")
        if error is not None:
            message = str(error) if isinstance(error, CoverAIError) else f"AI 改一改失败：{error}"
            self._set_notice(message, "error")
            return
        if not result:
            self.status_changed.emit("AI 没发现明显问题，可以直接导出")
            QMessageBox.information(self, "AI 改一改", "没发现明显问题，可以直接导出。")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("AI 改一改")
        dialog.setModal(False)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("点“应用”直接改，Ctrl+Z 可撤销："))
        for fix in result:
            row = QHBoxLayout()
            text = QLabel(f"{fix.issue}\n→ {fix.label}")
            text.setWordWrap(True)
            row.addWidget(text, 1)
            button = QPushButton("应用")
            button.clicked.connect(lambda _checked=False, item=fix, sender=button: self._apply_fix_from(sender, item))
            row.addWidget(button)
            layout.addLayout(row)
        close = QPushButton("关闭")
        close.clicked.connect(dialog.close)
        layout.addWidget(close)
        dialog.resize(420, dialog.sizeHint().height())
        dialog.show()
        self._critique_box = dialog

    def _apply_fix_from(self, button, fix):
        if self._apply_ai_fix(fix):
            button.setEnabled(False)
            button.setText("已应用")

    def _apply_ai_fix(self, fix) -> bool:
        """把一条 AI 修改落到文档上（可撤销）。"""

        if self.document is None or not self.draft.image_path:
            return False
        key = self._canvas_key
        if fix.action == "rewrite":
            before = self.document
            self._replace_copy(BasicCoverCopy(context=fix.context, headline=fix.headline))
            changed = self.document != before
        else:
            before = self.document
            if fix.action in ("move_bottom", "move_top"):
                # 只重排字，AI 或用户调好的取景不动。
                self.document = self.service.apply_auto_layout(
                    self.document, self.draft.image_path, mode="stack",
                    position="bottom" if fix.action == "move_bottom" else "top", reframe=False,
                )
            elif fix.action in ("zoom_in", "zoom_out"):
                background = next((item for item in self.document.objects if isinstance(item, BackgroundObject)), None)
                current = object_for_profile(self.document, background.id, key) if background else None
                if isinstance(current, BackgroundObject):
                    factor = _FIX_ZOOM if fix.action == "zoom_in" else 1 / _FIX_ZOOM
                    scale = clamp_background_scale(max(1.0, current.scale * factor))
                    self.document = set_profile_override(self.document, key, replace(current, scale=scale))
            elif fix.action in ("bigger", "smaller_context"):
                role, factor = ("B", _FIX_GROW) if fix.action == "bigger" else ("A", _FIX_SHRINK)
                object_id = self._primary_copy_ids().get(role)
                current = object_for_profile(self.document, object_id, key) if object_id else None
                if isinstance(current, TextObject) and current.visible:
                    size = max(24, round(current.style.font_size * factor))
                    ratio = size / max(1, current.style.font_size)
                    self.document = update_text_object(self.document, replace(
                        current, style=resize_text_style(current.style, size),
                        rect=replace(current.rect, width=min(3.0, current.rect.width * ratio),
                                     height=min(3.0, current.rect.height * ratio)),
                        wrap=replace(current.wrap, max_width=min(1.0, current.wrap.max_width * ratio)),
                    ), profile_key=key)
            changed = self.document != before
            if changed:
                self._commit_document_change(before)
        if changed:
            self.canvas.set_document(self.document, key)
            self._sync_selected_text_controls()
            self.status_changed.emit(f"已应用：{fix.label}（Ctrl+Z 可撤销）")
        return changed
