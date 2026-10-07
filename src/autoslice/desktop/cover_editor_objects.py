"""对象操作：素材、形状、新建/复制/删除/隐藏/锁定/恢复与层级。

混入 CoverEditorWidget；只用 self 上的状态，不单独实例化。
"""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtGui import (
    QIcon,
    QPixmap,
)
from PySide6.QtWidgets import (
    QFileDialog,
    QMenu,
    QMessageBox,
)

from .cover_asset_dialog import CoverAssetDialog
from .cover_model import (
    AssetRef,
    BackgroundObject,
    ImageObject,
    Rect,
    ShapeObject,
    StickerObject,
    TextObject,
    TextWrap,
    Transform,
    insert_overlay,
    object_for_profile,
    resize_text_style,
    restack_object,
    set_object_locked,
    set_object_visible,
    text_override_payload,
    update_shared_fields,
    update_text_object,
)
from .cover_service import (
    CoverDraft,
)
from .cover_style import streamer_key


class CoverObjectsMixin:
    def _import_asset(self):
        source, _ = QFileDialog.getOpenFileName(self, "选择封面素材", "", "图片 (*.png *.jpg *.jpeg *.webp *.bmp)")
        if not source or self.document is None:
            return
        try:
            asset = self.service.asset_library.import_file(source)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "无法导入素材", str(exc))
            return
        self._insert_overlay(ImageObject(
            id=self._new_object_id("image"),
            asset=AssetRef(path=asset.path, asset_id=asset.asset_id),
            transform=Transform(x=0.62, y=0.54, scale=0.85, rotation=0.0),
        ))
        self.service.asset_library.mark_used(asset.asset_id)

    def _browse_assets(self):
        if self.document is None:
            return
        try:
            preferred = (streamer_key(self.project.title) or self.project.title) if self.project else None
            assets = self.service.asset_library.list_assets(preferred_group=preferred)
        except (OSError, ValueError) as exc:
            message = f"素材库暂不可用：{exc}"
            self._set_notice(message, "error")
            self.status_changed.emit(message)
            return
        if not assets:
            message = "素材库为空；可以先导入一张自定义图片"
            self._set_notice(message, "warning")
            self.status_changed.emit(message)
            return
        dialog = CoverAssetDialog(assets, self)
        if dialog.exec() and (asset := dialog.selected_asset()) is not None:
            self._insert_asset_object(asset)

    def _new_object_id(self, prefix: str) -> str:
        existing = {item.id for item in self.document.objects} if self.document else set()
        index = len(existing) + 1
        while f"{prefix}-{index}" in existing:
            index += 1
        return f"{prefix}-{index}"

    def _insert_overlay(self, overlay) -> None:
        """新对象插在其他素材之上、文字之下，并立即选中。"""

        before = self.document
        self.document = insert_overlay(self.document, overlay)
        self._selected_text_id = None
        self.canvas.set_selected_object(overlay.id)
        self._commit_document_change(before)
        self._canvas_object_selected(overlay.id)

    def _insert_asset_object(self, asset):
        if self.document is None:
            return
        overlay_type = StickerObject if asset.group != "我的导入" else ImageObject
        overlay_kwargs = {
            "id": self._new_object_id("image"),
            "asset": AssetRef(path=asset.path, asset_id=asset.asset_id),
            "transform": Transform(x=0.62, y=0.54, scale=0.85),
        }
        if overlay_type is StickerObject:
            overlay_kwargs["category"] = asset.group
        self._insert_overlay(overlay_type(**overlay_kwargs))
        self.service.asset_library.mark_used(asset.asset_id)

    def _shape_menu(self, parent) -> QMenu:
        menu = QMenu(parent)
        for shape_type, label in (("circle", "圆圈"), ("arrow", "箭头"), ("rect", "矩形框")):
            menu.addAction(label, lambda value=shape_type: self._add_shape(value))
        return menu

    def _add_shape(self, shape_type: str = "circle"):
        if self.document is None or shape_type not in {"circle", "arrow", "rect"}:
            return
        self._insert_overlay(ShapeObject(
            id=self._new_object_id("shape"), shape_type=shape_type,
            transform=Transform(x=0.58, y=0.44, scale=1.0),
        ))
        label = {"circle": "圆圈", "arrow": "箭头", "rect": "矩形框"}[shape_type]
        self.status_changed.emit(f"已添加{label}，可直接在画布上拖动")

    def _add_text(self):
        """新建文本框：沿用主文案样式放在画面中部，选中后直接输入文字。"""

        if self.document is None:
            return
        before = self.document
        source_id = self._primary_copy_ids().get("B")
        source = object_for_profile(self.document, source_id, self._canvas_key) if source_id else None
        style = source.style if isinstance(source, TextObject) else None
        new_id = self._new_object_id("text")
        width = 0.6
        text = TextObject(
            id=new_id,
            copy_role="B",
            text="双击修改文字",
            z_index=max((item.z_index for item in self.document.objects), default=0) + 1,
            transform=Transform(x=0.2, y=0.42),
            rect=Rect(width=width, height=0.16),
            wrap=TextWrap(max_width=width, max_lines=8),
            align="center",
        )
        if style is not None:
            text = replace(text, style=resize_text_style(style, max(48, min(120, round(style.font_size * 0.8)))))
        self.document = replace(self.document, objects=(*self.document.objects, text), selected_object_id=new_id)
        self._selected_text_id = new_id
        self.canvas.set_selected_object(new_id)
        self._commit_document_change(before)
        self._edit_text(new_id)
        self.status_changed.emit("已新建文本框：直接输入文字，拖四角缩放")

    def _fill_asset_menu(self):
        """素材菜单：最近用过的素材一键插入，其余从素材库挑。"""

        menu = self.asset_menu
        menu.clear()
        menu.addAction("从素材库选择…", self._browse_assets)
        menu.addAction("导入图片…", self._import_asset)
        if self.document is None:
            return
        try:
            recent = self.service.asset_library.recent_assets(6)
        except (OSError, ValueError):
            recent = ()
        if not recent:
            return
        menu.addSeparator()
        menu.addSection("最近使用")
        for asset in recent:
            pixmap = QPixmap(asset.path)
            action_icon = QIcon(pixmap.scaled(32, 32, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)) if not pixmap.isNull() else QIcon()
            menu.addAction(action_icon, asset.name, lambda item=asset: self._insert_asset_object(item))

    def _selected_render_object(self):
        if self.document is None:
            return None
        object_id = self.document.selected_object_id
        return next((item for item in self.document.objects if item.id == object_id), None)

    def _sync_overlay_controls(self):
        """同步当前图片、贴图或强调形状的缩放和旋转控件。"""

        selected = self._selected_render_object()
        if self.document is not None and selected is not None:
            effective = object_for_profile(self.document, selected.id, self._canvas_key)
            if isinstance(effective, (ImageObject, StickerObject, ShapeObject)):
                selected = effective
        enabled = isinstance(selected, (ImageObject, StickerObject, ShapeObject))
        for widget in (self.overlay_scale_spin, self.overlay_rotation_spin):
            widget.blockSignals(True)
            widget.setEnabled(enabled)
        try:
            if enabled:
                self.overlay_scale_spin.setValue(float(selected.transform.scale))
                self.overlay_rotation_spin.setValue(float(selected.transform.rotation))
            else:
                self.overlay_scale_spin.setValue(1.0)
                self.overlay_rotation_spin.setValue(0.0)
        finally:
            for widget in (self.overlay_scale_spin, self.overlay_rotation_spin):
                widget.blockSignals(False)
        image = isinstance(selected, (ImageObject, StickerObject))
        shape = isinstance(selected, ShapeObject)
        style_widgets = (self.overlay_opacity_spin, self.shape_stroke_button, self.shape_stroke_spin, self.shape_fill_button)
        for widget in style_widgets:
            widget.blockSignals(True)
        try:
            if image:
                self.overlay_opacity_spin.setValue(round(float(selected.opacity) * 100))
            if shape:
                self.shape_stroke_button.set_color(selected.stroke)
                self.shape_stroke_spin.setValue(int(selected.stroke_width))
                self.shape_fill_button.set_color(selected.fill)
        finally:
            for widget in style_widgets:
                widget.blockSignals(False)
        self._overlay_form.setRowVisible(self.overlay_opacity_spin, image)
        # 第 3 行是“线条”（颜色 + 线宽的组合行，只能按行号控制）。
        self._overlay_form.setRowVisible(3, shape)
        self._overlay_form.setRowVisible(self.shape_fill_button, shape)
        self.lock_asset_button.setChecked(bool(enabled and selected.locked))
        self.hide_asset_button.setEnabled(enabled)
        self.lock_asset_button.setEnabled(enabled)

    def _store_overlay_controls(self):
        """把对象缩放/旋转写入当前比例的 profile override。"""

        if self.document is None:
            return
        selected = self._selected_render_object()
        if not isinstance(selected, (ImageObject, StickerObject, ShapeObject)):
            self._sync_overlay_controls()
            return
        current = object_for_profile(self.document, selected.id, self._canvas_key)
        if not isinstance(current, (ImageObject, StickerObject, ShapeObject)):
            current = selected
        updated = replace(
            current,
            transform=replace(
                current.transform,
                scale=self.overlay_scale_spin.value(),
                rotation=self.overlay_rotation_spin.value(),
            ),
        )
        if updated == current:
            return
        profile = self.document.profiles[self._canvas_key]
        payload = {"transform": updated.transform.to_payload(), "visible": bool(updated.visible)}
        if isinstance(updated, (ImageObject, StickerObject)):
            payload["opacity"] = updated.opacity
        else:
            payload.update({
                "shape_type": updated.shape_type,
                "fill": updated.fill,
                "stroke": updated.stroke,
                "stroke_width": updated.stroke_width,
                "width": updated.width,
                "height": updated.height,
            })
        self.document = replace(
            self.document,
            active_profile=self._canvas_key,
            profiles={
                **self.document.profiles,
                self._canvas_key: replace(profile, overrides={**profile.overrides, updated.id: payload}),
            },
        )
        self.history.commit(self.document)
        self.undo_button.setEnabled(self.history.can_undo)
        self.redo_button.setEnabled(self.history.can_redo)
        self.canvas.set_document(self.document, self._canvas_key)
        self.draft = CoverDraft.from_document(self.document)
        self._invalidate_render_requests()
        self._draft_timer.start()
        self._preview_timer.start()

    def _store_overlay_style(self):
        """透明度和形状颜色两个比例共享，改一处两边都变。"""

        if self.document is None:
            return
        selected = self._selected_render_object()
        if isinstance(selected, (ImageObject, StickerObject)):
            fields = {"opacity": self.overlay_opacity_spin.value() / 100}
        elif isinstance(selected, ShapeObject):
            fields = {
                "stroke": self.shape_stroke_button.color() or selected.stroke,
                "stroke_width": self.shape_stroke_spin.value(),
                "fill": self.shape_fill_button.color(),
            }
        else:
            return
        before = self.document
        self.document = update_shared_fields(self.document, selected.id, **fields)
        if self.document != before:
            self._commit_document_change(before)

    def _toggle_lock(self, checked: bool):
        item = self._selected_render_object()
        if self.document is None or item is None or isinstance(item, BackgroundObject):
            return
        before = self.document
        self.document = set_object_locked(self.document, item.id, bool(checked))
        self._commit_document_change(before)
        self.canvas.set_document(self.document, self._canvas_key)
        self.status_changed.emit("已锁定：不能拖动，快速方案和比例同步也不会挪它" if checked else "已解锁")

    def _hide_selected_object(self):
        item = self._selected_render_object()
        if self.document is None or item is None or isinstance(item, BackgroundObject):
            return
        before = self.document
        self.document = replace(set_object_visible(self.document, item.id, False), selected_object_id=None)
        self._commit_document_change(before)
        self.status_changed.emit("已隐藏，可从工具栏“已隐藏”恢复")

    def _hidden_objects(self) -> list:
        if self.document is None:
            return []
        result = []
        for item in self.document.objects:
            if isinstance(item, BackgroundObject):
                continue
            effective = object_for_profile(self.document, item.id, self._canvas_key)
            if effective is not None and not effective.visible:
                result.append(effective)
        return result

    def _refresh_hidden_button(self):
        count = len(self._hidden_objects())
        self.hidden_button.setVisible(count > 0)
        self.hidden_button.setText(f"已隐藏 {count}")

    def _fill_hidden_menu(self):
        self.hidden_menu.clear()
        names = {ImageObject: "图片", StickerObject: "贴图", ShapeObject: "形状"}
        primary = self._primary_copy_ids()
        for item in self._hidden_objects():
            if isinstance(item, TextObject):
                role = {"A": "上下文 A", "B": "主文案 B"}.get(item.copy_role) if primary.get(item.copy_role) == item.id else "文字"
                preview = item.text.strip().replace("\n", " ")
                label = f"{role}：{preview[:14] or '（空）'}"
            else:
                label = names.get(type(item), "对象")
                if isinstance(item, ShapeObject):
                    label += {"circle": "：圆圈", "arrow": "：箭头", "rect": "：矩形"}.get(item.shape_type, "")
            self.hidden_menu.addAction(label, lambda value=item.id: self._restore_object(value))
        if self.hidden_menu.isEmpty():
            self.hidden_menu.addAction("没有隐藏的对象").setEnabled(False)

    def _restore_object(self, object_id: str):
        """恢复隐藏的对象；空的主文案补回当前候选文字，再直接进入打字。"""

        if self.document is None:
            return
        before = self.document
        self.document = set_object_visible(self.document, object_id, True)
        item = object_for_profile(self.document, object_id, self._canvas_key)
        if isinstance(item, TextObject) and not item.text.strip():
            candidate = self._copy_variants[max(0, self._copy_variant_index)] if self._copy_variants else None
            value = (candidate.context if item.copy_role == "A" else candidate.headline) if candidate else ""
            self.document = update_text_object(
                self.document, replace(item, text=value.strip() or "双击修改文字"), profile_key=self._canvas_key,
            )
        self.document = replace(self.document, selected_object_id=object_id)
        self._commit_document_change(before)
        self._canvas_object_selected(object_id)
        if isinstance(item, TextObject):
            self._edit_text(object_id)
        self.status_changed.emit("已恢复")

    def _delete_object(self, object_id: str):
        """选中框左上角的删除按钮。"""

        if self.document is None:
            return
        self.document = replace(self.document, selected_object_id=object_id)
        self._delete_selected_object()
        background = next((item for item in self.document.objects if isinstance(item, BackgroundObject)), None)
        if background is not None:
            self._canvas_object_selected(background.id)

    def _duplicate_object(self, object_id: str):
        """选中框左下角的复制按钮：文字复制成新的独立文本框。"""

        if self.document is None:
            return
        source = next((item for item in self.document.objects if item.id == object_id), None)
        if isinstance(source, TextObject):
            self._duplicate_text(source)
            return
        self.document = replace(self.document, selected_object_id=object_id)
        self._duplicate_selected_object()

    def _duplicate_text(self, source: TextObject):
        before = self.document
        new_id = self._new_object_id("text")
        z_index = max((item.z_index for item in self.document.objects), default=0) + 1
        profiles = {}
        for key, profile in self.document.profiles.items():
            effective = object_for_profile(self.document, source.id, key)
            effective = effective if isinstance(effective, TextObject) else source
            shifted = replace(
                effective, id=new_id, z_index=z_index,
                transform=replace(effective.transform, x=min(0.92, effective.transform.x + 0.03), y=min(0.92, effective.transform.y + 0.04)),
            )
            profiles[key] = replace(profile, overrides={**profile.overrides, new_id: text_override_payload(shifted)})
        base = object_for_profile(self.document, source.id, self._canvas_key)
        base = base if isinstance(base, TextObject) else source
        duplicate = replace(
            base, id=new_id, z_index=z_index, visible=True,
            transform=replace(base.transform, x=min(0.92, base.transform.x + 0.03), y=min(0.92, base.transform.y + 0.04)),
        )
        self.document = replace(
            self.document,
            objects=(*self.document.objects, duplicate),
            profiles=profiles,
            selected_object_id=new_id,
        )
        self._selected_text_id = new_id
        self.canvas.set_selected_object(new_id)
        self._commit_document_change(before)
        self._canvas_object_selected(new_id)
        self.status_changed.emit("已复制文本框，可直接拖动到新位置")

    def _duplicate_selected_object(self):
        from dataclasses import replace as dc_replace
        item = self._selected_render_object()
        if self.document is None or not isinstance(item, (ImageObject, StickerObject, ShapeObject)):
            self.status_changed.emit("请先在画布上选中图片或强调对象")
            return
        duplicate = dc_replace(
            item, id=self._new_object_id(item.kind),
            transform=dc_replace(item.transform, x=min(0.92, item.transform.x + 0.04), y=min(0.92, item.transform.y + 0.04)),
        )
        self._insert_overlay(duplicate)

    def _delete_selected_object(self):
        item = self._selected_render_object()
        if self.document is None or item is None or item.kind == "background":
            return
        if item.locked:
            self.status_changed.emit("对象已锁定：先在右侧解锁再删除")
            return
        before = self.document
        if isinstance(item, TextObject) and item.id in self._primary_copy_ids().values():
            # A/B 主文案两个比例一起隐藏，可从工具栏“已隐藏”恢复。
            self.document = replace(set_object_visible(self.document, item.id, False), selected_object_id=None)
            self.status_changed.emit("已隐藏，可从工具栏“已隐藏”恢复")
        elif isinstance(item, TextObject):
            # 复制出来的文本框直接移除，连同比例覆盖。
            profiles = {
                key: replace(profile, overrides={k: v for k, v in profile.overrides.items() if k != item.id})
                for key, profile in self.document.profiles.items()
            }
            self.document = replace(
                self.document,
                objects=tuple(obj for obj in self.document.objects if obj.id != item.id),
                profiles=profiles,
                selected_object_id=None,
            )
        else:
            self.document = replace(self.document, objects=tuple(obj for obj in self.document.objects if obj.id != item.id), selected_object_id=None)
        self._commit_document_change(before)

    def _move_selected_layer(self, delta: int):
        item = self._selected_render_object()
        if self.document is None or item is None:
            return
        before = self.document
        # 与相邻对象交换次序，一次点击就能越过文字等相邻层。
        self.document = restack_object(self.document, item.id, delta)
        if self.document != before:
            self._commit_document_change(before)
