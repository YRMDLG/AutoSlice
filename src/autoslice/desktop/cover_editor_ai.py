"""封面 AI：读字幕写文案、在爆点前后挑表情帧、看成品出三套方案；“AI 改一改”给能一键应用的修改。
都由用户点击才运行，不阻塞手动编辑。

混入 CoverEditorWidget；只用 self 上的状态，不单独实例化。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from .archive.knowledge import Knowledge, profile_id_for
from .cover_ai import (
    AICopy,
    AIFrameNotes,
    AIHighlight,
    CoverAIError,
    contact_sheet,
    jpeg_bytes,
)
from .cover_autolayout import primary_copy_ids
from .cover_copy import BasicCoverCopy
from .cover_critique_dialog import CoverCritiqueDialog, CoverFixPreview
from .cover_effects import burst_id
from .cover_fix_quality import describe_scene, safe_candidate, visually_distinct
from .cover_fixes import apply_fix_profiles, describe_state, describe_text_layers
from .cover_model import TextObject, object_for_profile
from .cover_style import streamer_key
from .qt_preview import motion

# “AI 改一改”弹窗里改前、改后缩略图的宽度。
_PREVIEW_WIDTH = 560
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
        rewrite = self.ai_rewrite_check.isChecked()
        profile_key = self._canvas_key
        project, video, service = self.project, self.video, self.service
        streamer = streamer_key(project.title) or ""
        self.ai_scheme_button.setEnabled(False)
        self.ai_hint.setVisible(False)
        self._set_notice("AI 正在保留你的文字、检查画面并优化排版…" if not rewrite else "AI 正在比较当前文案和标题，复核新文案并优化排版…", "info")

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
            # 以画布当前文案为基准，不从标题重新初始化，更不能把自建文字当成主文案。
            primary = primary_copy_ids(document)
            texts = {role: object_for_profile(document, object_id, profile_key)
                     for role, object_id in primary.items()}
            basic = BasicCoverCopy(
                context=texts["A"].text if "A" in texts and texts["A"].visible else "",
                headline=texts["B"].text if "B" in texts and texts["B"].visible else "",
                emphasis=tuple(dict.fromkeys(word for text in texts.values() for word in text.emphasis)),
            )
            if not basic.headline:
                raise CoverAIError("当前没有可调整的主文案，请先显示主文案；自建文字块会原样保留")
            cues = service.subtitle_cues(video)
            fixed_texts = tuple(
                item.text for base in document.objects
                if isinstance(item := object_for_profile(document, base.id, profile_key), TextObject)
                and item.id not in primary.values() and item.visible and item.text.strip()
            )
            # 写文案（文字模型）和看当前画面给取景框（看图模型）同时发出，不多等。
            with ThreadPoolExecutor(max_workers=2) as pool:
                notes_future = pool.submit(service.ai.inspect, frame)
                analysis = service.ai.analyze(
                    project.title, cues, streamer=streamer, recent=recent,
                    round_index=round_index, title_copy=basic, rewrite=rewrite,
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
            approved = ()
            review_failed = False
            if rewrite:
                try:
                    approved = service.ai.review_copies(
                        project.title, cues, analysis.copies, current=basic, fixed_texts=fixed_texts,
                    )
                except CoverAIError:
                    review_failed = True
            analysis = replace(analysis, copies=approved)
            has_burst = any(item.id in {burst_id(text.id) for text in texts.values()} for item in document.objects)
            current_copy = AICopy(
                basic.context, basic.headline, "当前文案", "保留当前文案",
                basic.emphasis if has_burst else basic.emphasis or analysis.title_emphasis,
            )
            copies = (current_copy, *(item for item in approved if item.as_basic() != basic))
            # 本地按看图标出的脸和界面裁切、排字，先筛掉字压界面压脸的，渲染成总图，AI 看成品挑：看不懂、读不清的一票否决。
            candidates = service.ai_candidates(working, path, copies, notes=notes, preserve_copy=not approved)
            distinct, rendered = [], []
            def render_candidate(candidate):
                return candidate, service.scheme_thumbnail(candidate.document, canvas_key=profile_key, width=320)

            # 候选彼此独立；并行渲染缩短本地筛选等待，质量筛选仍按原顺序执行。
            with ThreadPoolExecutor(max_workers=min(4, len(candidates))) as pool:
                for candidate, thumbnail in pool.map(render_candidate, candidates):
                    if all(visually_distinct(thumbnail, previous) for previous in rendered):
                        distinct.append(candidate)
                        rendered.append(thumbnail)
            candidates = tuple(distinct)
            if not candidates:
                raise CoverAIError("这批排版会挤到已有文字或素材，已全部过滤；当前封面保持原样")
            sheet = contact_sheet(tuple(rendered))
            candidate_texts = []
            for candidate in candidates:
                ids = primary_copy_ids(candidate.document)
                candidate_texts.append({
                    role: text.text if isinstance(text := object_for_profile(candidate.document, object_id, profile_key), TextObject) and text.visible else ""
                    for role, object_id in ids.items()
                })
            choice = service.ai.choose(
                sheet, len(candidates), title=project.title, frame=frame, recent_thumbnails=thumbnails,
                round_index=round_index, candidate_texts=candidate_texts, fixed_texts=fixed_texts,
                current_cover=service.scheme_thumbnail(document, canvas_key=profile_key, width=320),
            )
            schemes = tuple(
                replace(candidates[pick.index], key=f"ai:{pick.direction}", label=f"AI·{pick.direction}", reason=pick.reason)
                for pick in choice.picks
            )
            thumbs = tuple(service.scheme_thumbnail(item.document, width=156) for item in schemes)
            moved = (timestamp, expression) if path != image_path else None
            return analysis, schemes, thumbs, choice, moved, rewrite, review_failed

        self._run(work, lambda result, error: self._ai_schemes_ready(generation, result, error, document))

    def _ai_schemes_ready(self, generation: int, result, error, original=None):
        if generation != self._ai_generation:
            return
        self.ai_scheme_button.setEnabled(self.document is not None)
        if original is not None and self.document is not None and (
            original.objects != self.document.objects or original.profiles != self.document.profiles
            or original.source != self.document.source
        ):
            self._set_notice("生成期间封面已有修改，这批方案已作废；再次点击可按最新封面生成", "warning")
            return
        if error is not None:
            message = str(error) if isinstance(error, CoverAIError) else f"AI 方案失败：{error}"
            self._set_notice(message, "error")
            self.status_changed.emit(message)
            return
        analysis, schemes, thumbnails, choice, moved, rewrite, review_failed = result
        if not schemes:
            message = f"AI 觉得这批候选都不合格（{choice.rejected or '没说原因'}），可以再点一次换一批，或手动调整"
            self._set_notice(message, "warning")
            self.status_changed.emit(message)
            return
        # 本地方案若还在生成，结果作废；先清空卡片（AI 可能少于三张），再换成 AI 挑中的。
        self._clear_schemes()
        self._schemes_ready(self._scheme_generation, (schemes, thumbnails), None)
        self._ai_scheme_states = (original,) if original is not None else ()
        self.scheme_title.setText("AI 方案")
        # 只有用户明确允许、且独立复核通过的文案才进入轮换。
        ai_copies = tuple(item.as_basic() for item in analysis.copies)
        if ai_copies:
            self._copy_variants = tuple(self._copy_variants) + tuple(item for item in ai_copies if item not in self._copy_variants)
        hints = ["保留自建文字块；文字爆炸贴跟随排版。"]
        if not rewrite:
            hints.append("本次保留当前文案，只优化排版与取景。")
        elif not ai_copies:
            hints.append("新文案复核未完成，已保留当前文案。" if review_failed else "没有明确优于当前文字和标题的新文案，已保留当前文案。")
        else:
            hints.append(f"有 {len(ai_copies)} 组新文案通过比较；当前文案也保留在候选中。")
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
        self.status_changed.emit("AI 方案已生成：点缩略图套用，Ctrl+Z 可撤销")

    def _submission_copy(self):
        """投稿文案：AI 按以前的标题口吻写备选标题、简介和标签，弹窗里一键复制；再点一次换一批。"""

        if self.project is None or self.video is None:
            return
        project, video, service = self.project, self.video, self.service
        streamer = streamer_key(project.title) or ""
        past = tuple(item.title for item in getattr(self, "_batch_projects", ()) if item.id != project.id)
        round_index = self._submission_round
        self._submission_round += 1
        self.submission_button.setEnabled(False)
        self._set_notice("AI 正在写投稿标题、简介和标签…", "info")

        def work():
            cues = service.subtitle_cues(video)
            # 学过旧切片的话，先放和这条最像的旧标题，再放当前目录里的
            query = project.title + " " + " ".join(text for _start, _end, text in cues[:60])
            learned = Knowledge(service.storage).title_examples(profile_id_for(video.path, project.title), query)
            return service.ai.submission_copy(
                project.title, cues, streamer=streamer, past_titles=(*learned, *past), round_index=round_index,
            )

        self._run(work, self._submission_copy_ready)

    def _submission_copy_ready(self, result, error):
        self.submission_button.setEnabled(self.project is not None)
        self._set_notice("")
        if error is not None:
            self._set_notice(str(error) if isinstance(error, CoverAIError) else f"投稿文案失败：{error}", "error")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("投稿文案")
        dialog.setModal(False)
        layout = QVBoxLayout(dialog)

        def copy_row(widget, text_of) -> QHBoxLayout:
            row = QHBoxLayout()
            row.addWidget(widget, 1)
            button = QPushButton("复制")
            button.clicked.connect(lambda _checked=False: (
                QGuiApplication.clipboard().setText(text_of()), self.status_changed.emit("已复制到剪贴板"),
            ))
            row.addWidget(button)
            return row

        layout.addWidget(QLabel("标题（可以先改再复制）"))
        for title in result.titles:
            edit = QLineEdit(title)
            layout.addLayout(copy_row(edit, edit.text))
        layout.addWidget(QLabel("简介"))
        description = QPlainTextEdit(result.description)
        description.setMaximumHeight(110)
        layout.addLayout(copy_row(description, description.toPlainText))
        layout.addWidget(QLabel("标签（逗号分隔，B 站里一个一个加）"))
        tags = QLineEdit("，".join(result.tags))
        layout.addLayout(copy_row(tags, tags.text))
        again = QPushButton("换一批")
        again.clicked.connect(lambda _checked=False: (dialog.close(), self._submission_copy()))
        close = QPushButton("关闭")
        close.clicked.connect(dialog.close)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(again)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        dialog.resize(560, dialog.sizeHint().height())
        motion.install_tree(dialog)
        dialog.show()
        self._submission_box = dialog

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
        # 默认先出结果：第一轮能渲染出的候选马上给你对照，看图复核放后台、回来后在窗口里标注。
        # 严格模式（旧行为）等复核通过才给、被否再重出一轮，实测要 2 分多钟还常常什么都不给。
        strict = bool(getattr(self, "_critique_strict", False))
        self.ai_critique_button.setEnabled(False)
        self._set_notice("AI 正在读取字幕并调整 4:3、16:9 封面…（一般 15~40 秒）", "info")

        def work():
            before_profiles = {key: service.scheme_thumbnail(document, canvas_key=key, width=_PREVIEW_WIDTH)
                               for key in document.profiles}
            before = before_profiles[canvas_key]
            cover = jpeg_bytes(before, max_side=480)
            # 原始帧缩到 768：看图请求更小更快
            frame = jpeg_bytes(image_path, max_side=768)
            cues = service.subtitle_cues(video)
            state = describe_state(document, canvas_key)
            layers, scene = describe_text_layers(document, canvas_key), describe_scene(document, canvas_key)
            scenes = {key: describe_scene(document, key) for key in document.profiles}
            feedback, previous_fixes, rendered = "", (), []
            # 首轮失败后仅修正一次，始终以原文档为基准，防止失败改动叠加。
            # 首轮关思考求快（2~3 秒）；关思考时约一半会全被本地检查拦下（越界、挤压），修正轮开 low 补救。
            for _attempt in range(2):
                fixes = service.ai.suggest_fixes(
                    cover, texts=texts, title=project.title, frame=frame, state=state,
                    text_layers=layers, scene=scene, feedback=feedback,
                    previous_fixes=previous_fixes, cues=cues, canvas_key=canvas_key, profile_scenes=scenes,
                    profile_covers=tuple((key, jpeg_bytes(data, max_side=480)) for key, data in before_profiles.items() if key != canvas_key),
                    effort="none" if _attempt == 0 else "low",
                )
                if not fixes:
                    break
                previews, failures = [], []
                for fix in fixes:
                    after = apply_fix_profiles(service, document, image_path, fix)
                    if after == document:
                        failures.append(f"{fix.issue}：执行后没有变化")
                        continue
                    if not safe_candidate(document, after, canvas_key):
                        failures.append(f"{fix.issue}：新增越界、挤压或破坏锁定对象，需调整参数")
                        continue
                    thumbnails = {key: service.scheme_thumbnail(after, canvas_key=key, width=_PREVIEW_WIDTH)
                                  for key in document.profiles}
                    thumbnail = thumbnails[canvas_key]
                    if not any(visually_distinct(before_profiles[key], data) for key, data in thumbnails.items()):
                        failures.append(f"{fix.issue}：小图上看不出变化")
                        continue
                    if any(all(not visually_distinct(dict(item.profile_thumbnails)[key], data)
                               for key, data in thumbnails.items()) for item in (*rendered, *previews)):
                        failures.append(f"{fix.issue}：与本轮或上轮候选几乎重复")
                        continue
                    previews.append(CoverFixPreview(fix, thumbnail, after, tuple(thumbnails.items())))
                previous_fixes += fixes
                rendered.extend(previews)
                if not previews:
                    feedback = "；".join(failures)
                    continue

                def review(previews=tuple(previews)):
                    return service.ai.review_fixes(
                        contact_sheet((before, *(item.thumbnail for item in previews)), labels_outside=True, first_number=0), len(previews),
                        title=project.title, state=state, changes=tuple(item.fix.label for item in previews),
                        cues=cues, canvas_key=canvas_key,
                        profile_states={key: describe_state(document, key) for key in document.profiles},
                        profile_sheets=tuple((key, contact_sheet((data, *(dict(item.profile_thumbnails)[key] for item in previews)), labels_outside=True, first_number=0))
                                             for key, data in before_profiles.items() if key != canvas_key),
                    )

                if not strict:
                    return document, canvas_key, before_profiles, tuple(previews), len(cues), review
                choice = review()
                if choice.picks:
                    selected = tuple(replace(previews[pick.index], fix=replace(previews[pick.index].fix, issue=pick.reason))
                                     for pick in choice.picks)
                    return document, canvas_key, before_profiles, selected, len(cues), None
                feedback = "；".join((*failures, choice.rejected or "候选未体现可靠改善，请重新解决原图的具体问题"))
            return document, canvas_key, before_profiles, (), len(cues), None

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
        original, canvas_key, before_profiles, previews, subtitle_count, review = result
        project, video = self.project, self.video
        if self.document is None or self._canvas_key != canvas_key or (
            original.objects != self.document.objects or original.profiles != self.document.profiles
            or original.source != self.document.source
        ):
            self._set_notice("分析期间封面已有修改，请再次点击“AI 改一改”按最新封面生成。", "warning")
            return
        if not previews:
            message = "这轮未能生成通过检查的改进，未改动你的封面，可以继续编辑。"
            self._set_notice(message, "warning")
            self.status_changed.emit(message)
            return

        def apply_preview(preview: CoverFixPreview) -> bool:
            # 建议都基于同一份封面。允许在这些候选之间切换；手动编辑或换比例后不覆盖新改动。
            if (
                generation != self._ai_generation or self.project != project or self.video != video
                or self._canvas_key != canvas_key or self.document not in (original, *(item.document for item in previews))
            ):
                return False
            if self.document != preview.document:
                previous = self.document
                self.document = preview.document
                self._commit_document_change(previous)
                self.canvas.set_document(self.document, self._canvas_key)
                self._sync_selected_text_controls()
                self.status_changed.emit(f"已同时应用到 4:3 和 16:9：{preview.fix.label}（Ctrl+Z 可撤销）")
            return True

        if getattr(self, "_critique_box", None) is not None:
            self._critique_box.close()
            self._critique_box.deleteLater()
        dialog = CoverCritiqueDialog(
            before_profiles[canvas_key], previews, apply_preview, undo=self._undo, redo=self._redo,
            before_profiles=tuple(before_profiles.items()), canvas_key=canvas_key,
            context_note=f"已读取 {subtitle_count} 条字幕。" if subtitle_count else "未找到可读字幕，本次依据标题和画面。",
            parent=self, reviewing=review is not None,
        )
        dialog.show()
        self._critique_box = dialog
        if review is not None:
            self._run(review, lambda choice, error: self._critique_reviewed(generation, dialog, choice, error))

    def _critique_reviewed(self, generation: int, dialog, choice, error) -> None:
        """后台复核回来：只在同一次“改一改”的窗口还开着时标注，不替用户做决定。"""

        if generation != self._ai_generation or getattr(self, "_critique_box", None) is not dialog:
            return
        if error is not None:
            dialog.set_review({}, failed=str(error) if isinstance(error, CoverAIError) else "复核请求失败")
            return
        dialog.set_review({pick.index: pick.reason for pick in choice.picks}, rejected=choice.rejected)

    def _apply_ai_fix(self, fix) -> bool:
        """把一条 AI 修改落到当前文档上（可撤销）；已经是这样就什么都不做。"""

        if self.document is None or not self.draft.image_path:
            return False
        before = self.document
        self.document = apply_fix_profiles(self.service, self.document, self.draft.image_path, fix)
        if self.document == before:
            return False
        self._commit_document_change(before)
        self.canvas.set_document(self.document, self._canvas_key)
        self._sync_selected_text_controls()
        self.status_changed.emit(f"已应用：{fix.label}（Ctrl+Z 可撤销）")
        return True
