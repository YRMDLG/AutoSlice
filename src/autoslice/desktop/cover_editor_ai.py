"""封面 AI：读字幕、看画面出三套方案；看图点评。都由用户点击才运行，不阻塞手动编辑。

混入 CoverEditorWidget；只用 self 上的状态，不单独实例化。
"""

from __future__ import annotations

from PySide6.QtWidgets import QMessageBox

from .cover_ai import CoverAIError, jpeg_bytes
from .cover_model import TextObject, object_for_profile
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
            analysis = service.ai.analyze(
                project.title, service.subtitle_cues(video), streamer=streamer, recent=recent, round_index=round_index,
            )
            ideas = service.ai.design(
                jpeg_bytes(image_path), analysis, recent=recent, recent_thumbnails=thumbnails, round_index=round_index,
            )
            schemes = service.schemes_from_ideas(document, image_path, ideas)
            return analysis, schemes, tuple(service.scheme_thumbnail(item.document, width=156) for item in schemes)

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
        analysis, schemes, thumbnails = result
        # 本地方案若还在生成，结果作废；三张卡换成 AI 方案。
        self._scheme_generation += 1
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
        """AI 点评：按首页小图的尺寸看当前封面，指出读不清、挡脸、主次和重复的问题。"""

        if self.document is None or not self.draft.image_path or self.project is None:
            return
        self._ai_generation += 1
        generation = self._ai_generation
        document, canvas_key, service = self.document, self._canvas_key, self.service
        streamer = streamer_key(self.project.title) or ""
        texts = tuple(
            current.text
            for item in document.objects if isinstance(item, TextObject)
            if isinstance(current := object_for_profile(document, item.id, canvas_key), TextObject) and current.visible
        )
        self.ai_critique_button.setEnabled(False)
        self._set_notice("AI 正在看这张封面…", "info")

        def work():
            cover = jpeg_bytes(service.scheme_thumbnail(document, canvas_key=canvas_key, width=480), max_side=480)
            thumbnails = service.works.thumbnail_bytes(service.works.recent(streamer=streamer, limit=3))
            return service.ai.critique(cover, texts=texts, recent_thumbnails=thumbnails)

        self._run(work, lambda result, error: self._ai_critique_ready(generation, result, error))

    def _ai_critique_ready(self, generation: int, result, error):
        if generation != self._ai_generation:
            return
        self.ai_critique_button.setEnabled(self.document is not None)
        self._set_notice("")
        if error is not None:
            message = str(error) if isinstance(error, CoverAIError) else f"AI 点评失败：{error}"
            self._set_notice(message, "error")
            return
        if not result:
            text = "没发现明显问题，可以直接导出。"
        else:
            text = "\n\n".join(f"• {note.issue}\n  建议：{note.suggestion}" for note in result)
        box = QMessageBox(QMessageBox.Icon.Information, "AI 点评", text, QMessageBox.StandardButton.Ok, self)
        box.setModal(False)
        box.open()
        self._critique_box = box
