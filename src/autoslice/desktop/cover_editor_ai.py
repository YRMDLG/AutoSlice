"""封面 AI：读字幕写文案、在爆点前后挑表情帧、看成品出三套方案；“AI 改一改”给能一键应用的修改。
都由用户点击才运行，不阻塞手动编辑。

混入 CoverEditorWidget；只用 self 上的状态，不单独实例化。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout

from .cover_ai import (
    AICopy,
    AIFrameNotes,
    AIHighlight,
    CoverAIError,
    contact_sheet,
    jpeg_bytes,
)
from .cover_fixes import apply_fix, describe_state
from .cover_model import TextObject, object_for_profile
from .cover_style import streamer_key

# “AI 改一改”弹窗里改前、改后缩略图的宽度。
_PREVIEW_WIDTH = 200
# 选帧：爆点前后取几帧、每帧给看图模型的长边（只看表情，不用太大）。
_PICK_FRAME_COUNT = 6
_PICK_FRAME_SIDE = 512


def _highlight_offsets(highlight: AIHighlight) -> tuple[float, ...]:
    """爆点句开始前一点到说完后一秒多，均匀取几帧（相对爆点开始）；反应常在话说完之后。"""

    start, end = -0.6, max(highlight.end - highlight.start, 1.5) + 1.2
    step = (end - start) / (_PICK_FRAME_COUNT - 1)
    return tuple(round(start + index * step, 2) for index in range(_PICK_FRAME_COUNT))


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
        self._set_notice("AI 正在读字幕写文案、在爆点前后挑表情、看成品出方案（大约一分钟到一分半），这期间可以照常编辑…", "info")

        locked = bool(document.source.frame_locked)
        current_timestamp = float(document.source.selected_timestamp)

        def pick_frame(highlight: AIHighlight):
            """在爆点前后挑表情最有戏的一帧；返回 (那一帧, 表情)，还用原来的帧或取帧、模型失败时返回 None。"""

            try:
                frames = service.nearby_candidates(video, highlight.start, _highlight_offsets(highlight))
            except Exception:  # noqa: BLE001 - 取不到帧就只用当前帧
                return None
            images = (jpeg_bytes(image_path, max_side=_PICK_FRAME_SIDE),) + tuple(
                jpeg_bytes(item.path, max_side=_PICK_FRAME_SIDE) for item in frames
            )
            try:
                picked = service.ai.pick_frame(images, quote=highlight.quote, title=project.title)
            except CoverAIError:
                return None
            return (frames[picked.index - 1], picked.expression) if picked.index > 0 else None

        def work():
            recent = service.works.recent(streamer=streamer, limit=6)
            thumbnails = service.works.thumbnail_bytes(recent[:3])
            frame = jpeg_bytes(image_path)
            # 你的标题提炼出的那句固定放进候选（标题是切片员自己写的总结），AI 文案作补充。
            basic = service.basic_copy_variants(project.title)[0]
            # 写文案（文字模型）和看当前画面给取景框（看图模型）同时发出，不多等。
            with ThreadPoolExecutor(max_workers=2) as pool:
                notes_future = pool.submit(service.ai.inspect, frame)
                analysis = service.ai.analyze(
                    project.title, service.subtitle_cues(video), streamer=streamer, recent=recent,
                    round_index=round_index, title_copy=basic,
                )
                try:
                    notes = notes_future.result()
                except CoverAIError:
                    notes = AIFrameNotes(busy=frozenset(), note="")
            path, timestamp, expression = image_path, current_timestamp, ""
            # 没锁帧：到爆点前后挑表情最有戏的一帧，并对这一帧重新标框——飘过的弹幕、弹窗每帧都不一样。
            picked = pick_frame(analysis.highlight) if not locked and analysis.highlight is not None else None
            if picked is not None:
                chosen, expression = picked
                path, timestamp, frame = str(chosen.path), chosen.timestamp, jpeg_bytes(chosen.path)
                try:
                    notes = service.ai.inspect(frame)
                except CoverAIError:
                    pass  # 看不了新帧就沿用原来那帧的结论
            working = service.with_frame(document, path, timestamp) if path != image_path else document
            from_title = AICopy(basic.context, basic.headline, "标题", "", analysis.title_emphasis)
            copies = (from_title, *(item for item in analysis.copies if item.headline != from_title.headline))
            # 本地按看图标出的脸和界面裁切、排字，先筛掉字压界面压脸的，渲染成总图，AI 看成品挑：看不懂、读不清的一票否决。
            candidates = service.ai_candidates(working, path, copies, notes=notes)
            sheet = contact_sheet(tuple(service.scheme_thumbnail(item.document, width=320) for item in candidates))
            choice = service.ai.choose(
                sheet, len(candidates), title=project.title, frame=frame, recent_thumbnails=thumbnails,
                round_index=round_index,
            )
            schemes = tuple(
                replace(candidates[pick.index], key=f"ai:{pick.direction}", label=f"AI·{pick.direction}", reason=pick.reason)
                for pick in choice.picks
            )
            thumbs = tuple(service.scheme_thumbnail(item.document, width=156) for item in schemes)
            moved = (timestamp, expression) if path != image_path else None
            return analysis, schemes, thumbs, choice, moved

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
        analysis, schemes, thumbnails, choice, moved = result
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
        hints = []
        if analysis.highlight is not None:
            highlight = analysis.highlight
            hints.append(f"爆点：「{highlight.quote}」（{highlight.start:.1f} 秒）{highlight.reason}")
        if moved is not None:
            # 方案用的是 AI 在爆点前后挑的表情帧；套用时底图跟着换，Ctrl+Z 可回到原来的帧。
            timestamp, expression = moved
            hints.append(f"方案换用了 {timestamp:.1f} 秒的画面" + (f"（{expression}）" if expression else ""))
        if hints:
            self.ai_hint.setText("\n".join(hints))
            self.ai_hint.setToolTip("AI 从字幕里找到的爆点，以及在爆点前后挑的表情更有戏的画面")
            self.ai_hint.setVisible(True)
        self._set_notice("")
        self.status_changed.emit("AI 方案已生成：点缩略图套用（Ctrl+Z 可撤销）；“换一版”也会轮换 AI 写的文案")

    def _ai_critique(self):
        """AI 改一改：看当前封面给最能让它更抓眼的修改，每条先算出改后的样子，没变化的不显示。"""

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
            before = service.scheme_thumbnail(document, canvas_key=canvas_key, width=_PREVIEW_WIDTH)
            source = project.title + "\n" + "\n".join(text for _start, _end, text in service.subtitle_cues(video))
            fixes = service.ai.suggest_fixes(
                jpeg_bytes(service.scheme_thumbnail(document, canvas_key=canvas_key, width=480), max_side=480),
                texts=texts, title=project.title, source=source, frame=jpeg_bytes(image_path),
                state=describe_state(document, canvas_key),
            )
            previews = []
            for fix in fixes:
                after = apply_fix(service, document, image_path, fix, canvas_key)
                if after != document:
                    previews.append((fix, service.scheme_thumbnail(after, canvas_key=canvas_key, width=_PREVIEW_WIDTH)))
            return before, tuple(previews)

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
        before, previews = result
        if not previews:
            self.status_changed.emit("AI 没找到能让这张封面更好的修改，可以直接导出")
            QMessageBox.information(self, "AI 改一改", "没找到能让这张封面更好的修改，可以直接导出。")
            return

        def picture(data: bytes) -> QLabel:
            label = QLabel()
            pixmap = QPixmap()
            pixmap.loadFromData(data)
            label.setPixmap(pixmap)
            return label

        dialog = QDialog(self)
        dialog.setWindowTitle("AI 改一改")
        dialog.setModal(False)
        layout = QVBoxLayout(dialog)
        head = QHBoxLayout()
        head.addWidget(picture(before))
        head.addWidget(QLabel("现在的样子\n\n下面每条都是改完的效果，\n点“应用”直接改，Ctrl+Z 可撤销"), 1)
        layout.addLayout(head)
        for fix, thumbnail in previews:
            row = QHBoxLayout()
            row.addWidget(picture(thumbnail))
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
        dialog.resize(560, dialog.sizeHint().height())
        dialog.show()
        self._critique_box = dialog

    def _apply_fix_from(self, button, fix):
        if self._apply_ai_fix(fix):
            button.setEnabled(False)
            button.setText("已应用")

    def _apply_ai_fix(self, fix) -> bool:
        """把一条 AI 修改落到当前文档上（可撤销）；已经是这样就什么都不做。"""

        if self.document is None or not self.draft.image_path:
            return False
        before = self.document
        self.document = apply_fix(self.service, self.document, self.draft.image_path, fix, self._canvas_key)
        if self.document == before:
            return False
        self._commit_document_change(before)
        self.canvas.set_document(self.document, self._canvas_key)
        self._sync_selected_text_controls()
        self.status_changed.emit(f"已应用：{fix.label}（Ctrl+Z 可撤销）")
        return True
