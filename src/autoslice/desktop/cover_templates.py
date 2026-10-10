"""封面模板：把一张排好的封面存成“版式”，换个视频一键套上，日更时版式统一。

模板记 A/B 的字体配色、在两个比例里的位置，以及贴纸、形状、导入的图片和固定的字（比如频道标语）；
不记底图、文案内容，也不记从视频截的拼图小图（每期都不一样）。爆炸贴只记“垫过”，套用时按新文案的
强调词重新垫。模板只存本机。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .cover_autolayout import ensure_context_text, primary_copy_ids
from .cover_effects import add_burst, burst_id
from .cover_model import BackgroundObject, CoverDocument, ImageObject, TextObject

# 套模板加进来的对象 id 前缀：再套一次或换模板时先把上一次加的拿掉。
TEMPLATE_PREFIX = "tpl-"


@dataclass(frozen=True)
class CoverTemplate:
    template_id: str
    name: str
    created: float
    # 模板文档：主文案 A/B 的文字清空，没有底图。
    document: CoverDocument
    # 哪些主文案（A/B）垫过爆炸贴。
    bursts: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.template_id, "name": self.name, "created": self.created,
            "bursts": list(self.bursts), "document": self.document.to_payload(),
        }

    @classmethod
    def from_payload(cls, payload: Any) -> "CoverTemplate | None":
        if not isinstance(payload, dict) or not isinstance(payload.get("document"), dict):
            return None
        try:
            document = CoverDocument.from_payload(payload["document"])
        except (TypeError, ValueError, KeyError):
            return None
        return cls(
            template_id=str(payload.get("id") or uuid.uuid4().hex[:12]), name=str(payload.get("name") or "模板"),
            created=float(payload.get("created") or 0.0), document=document,
            bursts=tuple(role for role in payload.get("bursts") or () if role in ("A", "B")),
        )


def _inside(path: str | None, folder: Path) -> bool:
    try:
        return bool(path) and Path(path).resolve().is_relative_to(folder.resolve())
    except (OSError, ValueError):
        return False


def make_template(document: CoverDocument, name: str, *, frames_dir: Path) -> CoverTemplate:
    """当前封面存成模板。frames_dir 是视频截帧的缓存目录：从那里来的图片是拼图小图，不进模板。"""

    primary = primary_copy_ids(document)
    bursts = tuple(
        role for role, object_id in primary.items()
        if any(item.id == burst_id(object_id) and item.visible for item in document.objects)
    )
    kept = []
    for item in document.objects:
        if isinstance(item, BackgroundObject):
            kept.append(replace(item, asset=None))
        elif not item.visible or item.id.startswith("burst-"):
            continue
        elif isinstance(item, ImageObject) and item.asset is not None and _inside(item.asset.path, frames_dir):
            continue
        elif isinstance(item, TextObject) and item.id in primary.values():
            kept.append(replace(item, text="", emphasis=()))
        else:
            kept.append(item)
    ids = {item.id for item in kept}
    profiles = {
        key: replace(profile, overrides={object_id: value for object_id, value in profile.overrides.items() if object_id in ids})
        for key, profile in document.profiles.items()
    }
    template_document = replace(document, objects=tuple(kept), profiles=profiles, selected_object_id=None)
    return CoverTemplate(uuid.uuid4().hex[:12], name.strip() or "模板", time.time(), template_document, bursts)


def apply_template(document: CoverDocument, template: CoverTemplate, profile_key: str = "4x3") -> CoverDocument:
    """套模板：A/B 的文字和强调词不动，样式和两个比例里的位置换成模板的；加上模板里的贴纸、形状和固定的字。"""

    document = replace(
        document,
        objects=tuple(item for item in document.objects if not item.id.startswith(TEMPLATE_PREFIX)),
        profiles={
            key: replace(profile, overrides={
                object_id: value for object_id, value in profile.overrides.items() if not object_id.startswith(TEMPLATE_PREFIX)
            })
            for key, profile in document.profiles.items()
        },
    )
    source = template.document
    theirs = primary_copy_ids(source)
    if "A" in theirs:
        # 模板有 A 块、当前封面只有 B：先补一个 A 块，A 的文字有了再显示。
        document = ensure_context_text(document)
    mine = primary_copy_ids(document)
    objects = {item.id: item for item in document.objects}
    order = [item.id for item in document.objects]
    profiles = {key: dict(profile.overrides) for key, profile in document.profiles.items()}
    for role, their_id in theirs.items():
        my_id = mine.get(role)
        model = next(item for item in source.objects if item.id == their_id)
        current = objects.get(my_id) if my_id else None
        if not isinstance(current, TextObject) or not isinstance(model, TextObject):
            continue
        objects[my_id] = replace(
            model, id=my_id, copy_role=current.copy_role, text=current.text, emphasis=current.emphasis,
            visible=current.visible, locked=current.locked,
        )
        for key, overrides in profiles.items():
            template_profile = source.profiles.get(key)
            override = template_profile.overrides.get(their_id) if template_profile is not None else None
            if isinstance(override, dict):
                visible = overrides.get(my_id, {}).get("visible", current.visible) if isinstance(overrides.get(my_id), dict) else current.visible
                overrides[my_id] = {**override, "visible": visible}
            else:
                overrides.pop(my_id, None)
    for item in source.objects:
        if isinstance(item, BackgroundObject) or item.id in theirs.values():
            continue
        new_id = TEMPLATE_PREFIX + item.id
        objects[new_id] = replace(item, id=new_id)
        order.append(new_id)
        for key, overrides in profiles.items():
            template_profile = source.profiles.get(key)
            override = template_profile.overrides.get(item.id) if template_profile is not None else None
            if isinstance(override, dict):
                overrides[new_id] = override
    document = replace(
        document,
        objects=tuple(objects[object_id] for object_id in order),
        profiles={key: replace(profile, overrides=profiles[key]) for key, profile in document.profiles.items()},
    )
    for role in template.bursts:
        object_id = primary_copy_ids(document).get(role)
        if object_id:
            document = add_burst(document, object_id, profile_key)
    return document


class CoverTemplateStore:
    """本机模板库：一个 JSON 文件，最近用过的排在前面。"""

    def __init__(self, root: Path | str) -> None:
        self.path = Path(root).expanduser().resolve() / "cover-templates.json"

    def _read(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            return {"templates": []}
        return payload if isinstance(payload, dict) and isinstance(payload.get("templates"), list) else {"templates": []}

    def _write(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def list(self) -> tuple[CoverTemplate, ...]:
        payload = self._read()
        templates = [item for item in (CoverTemplate.from_payload(raw) for raw in payload["templates"]) if item is not None]
        used = payload.get("used") if isinstance(payload.get("used"), dict) else {}
        return tuple(sorted(templates, key=lambda item: (-float(used.get(item.template_id, 0.0)), -item.created)))

    def get(self, template_id: str) -> CoverTemplate | None:
        return next((item for item in self.list() if item.template_id == template_id), None)

    def save(self, template: CoverTemplate) -> None:
        payload = self._read()
        payload["templates"] = [raw for raw in payload["templates"] if not (isinstance(raw, dict) and raw.get("id") == template.template_id)]
        payload["templates"].append(template.to_payload())
        self._write(payload)

    def delete(self, template_id: str) -> None:
        payload = self._read()
        payload["templates"] = [raw for raw in payload["templates"] if not (isinstance(raw, dict) and raw.get("id") == template_id)]
        if isinstance(payload.get("used"), dict):
            payload["used"].pop(template_id, None)
        self._write(payload)

    def mark_used(self, template_id: str) -> None:
        payload = self._read()
        used = payload.get("used") if isinstance(payload.get("used"), dict) else {}
        used[template_id] = time.time()
        payload["used"] = used
        self._write(payload)
