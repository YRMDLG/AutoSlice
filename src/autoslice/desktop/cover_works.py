"""封面作品库：每次导出记下这张封面的构图、配色、文案和用了哪个方案。

用途：快速方案避开最近用过的构图和配色（防千篇一律）；以后给 AI 当
“你常做什么样的封面”的范例。只存在本机。
"""

from __future__ import annotations

import json
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from PIL import Image

from .cover_layout import canvas_size, text_layout
from .cover_model import CoverDocument, TextObject, object_for_profile
from .foundation import DesktopStorage

# 只保留最近这么多条；更早的对“最近常做什么”没有帮助。
_KEEP = 500
_THUMB_WIDTH = 320
_ROWS = ("上", "中", "下")
_COLS = ("左", "中", "右")


def _primary_texts(document: CoverDocument, canvas_key: str) -> dict[str, TextObject]:
    """A/B 主文案在该比例里的样子；隐藏或空的不算。"""

    texts: dict[str, TextObject] = {}
    for item in document.objects:
        if not isinstance(item, TextObject) or item.copy_role not in ("A", "B") or item.copy_role in texts:
            continue
        current = object_for_profile(document, item.id, canvas_key)
        if isinstance(current, TextObject) and current.visible and current.text.strip():
            texts[item.copy_role] = current
    return texts


def composition_signature(document: CoverDocument, canvas_key: str = "4x3") -> str:
    """文字块的大致位置，如 “A上中|B下中”；只看 A/B 主文案。"""

    width, height = canvas_size(canvas_key)
    parts = []
    for role, text in sorted(_primary_texts(document, canvas_key).items()):
        area = text_layout(text, (width, height)).area
        cx = (area.left + area.width / 2) / width
        cy = (area.top + area.height / 2) / height
        row = _ROWS[0 if cy < 0.36 else 2 if cy > 0.64 else 1]
        col = _COLS[0 if cx < 0.4 else 2 if cx > 0.6 else 1]
        parts.append(f"{role}{row}{col}")
    return "|".join(parts)


def palette_signature(document: CoverDocument) -> str:
    """B 的填充/描边 + A 的填充；配色相同的封面签名相同。"""

    texts = _primary_texts(document, "4x3")
    parts = []
    if "B" in texts:
        parts.append(f"{texts['B'].style.fill_color}/{texts['B'].style.stroke_color}".lower())
    if "A" in texts:
        parts.append(texts["A"].style.fill_color.lower())
    return "|".join(parts)


_PLACE = {"上": "上方", "中": "中部", "下": "下方"}
_SIDE = {"左": "靠左", "中": "居中", "右": "靠右"}
_SCHEME_NAMES = {
    "recommended": "推荐", "split": "上下分置", "stack": "标题在上", "headline": "大字", "side": "侧边",
    "alternate": "换配色", "batch": "批量出图",
}


def describe_composition(signature: str) -> str:
    """“A上中|B下中” → “A 上方居中，B 下方居中”。"""

    parts = [
        f"{part[0]} {_PLACE.get(part[1], part[1])}{_SIDE.get(part[2], part[2])}"
        for part in signature.split("|") if len(part) == 3
    ]
    return "，".join(parts) or "无文字"


def describe_scheme(key: str) -> str:
    if key.startswith("ai:"):
        return f"AI·{key[3:]}"
    return _SCHEME_NAMES.get(key, key)


@dataclass(frozen=True, slots=True)
class CoverWork:
    work_id: str
    time: str
    streamer: str
    project: str
    video: str
    canvas_keys: tuple[str, ...]
    composition: str
    palette: str
    context: str
    headline: str
    scheme: str = ""
    edits_after_scheme: int = 0
    basic_copy: tuple[str, str] = ("", "")
    thumbnail: str = ""
    outputs: tuple[str, ...] = field(default=())

    @classmethod
    def from_payload(cls, payload: dict) -> "CoverWork":
        basic = payload.get("basic_copy") or ("", "")
        return cls(
            work_id=str(payload.get("work_id", "")), time=str(payload.get("time", "")),
            streamer=str(payload.get("streamer", "")), project=str(payload.get("project", "")),
            video=str(payload.get("video", "")), canvas_keys=tuple(payload.get("canvas_keys") or ()),
            composition=str(payload.get("composition", "")), palette=str(payload.get("palette", "")),
            context=str(payload.get("context", "")), headline=str(payload.get("headline", "")),
            scheme=str(payload.get("scheme", "")), edits_after_scheme=int(payload.get("edits_after_scheme", 0) or 0),
            basic_copy=(str(basic[0]), str(basic[1])) if len(basic) == 2 else ("", ""),
            thumbnail=str(payload.get("thumbnail", "")), outputs=tuple(payload.get("outputs") or ()),
        )

    def to_payload(self) -> dict:
        return {
            "work_id": self.work_id, "time": self.time, "streamer": self.streamer, "project": self.project,
            "video": self.video, "canvas_keys": list(self.canvas_keys), "composition": self.composition,
            "palette": self.palette, "context": self.context, "headline": self.headline, "scheme": self.scheme,
            "edits_after_scheme": self.edits_after_scheme, "basic_copy": list(self.basic_copy),
            "thumbnail": self.thumbnail, "outputs": list(self.outputs),
        }


class CoverWorks:
    def __init__(self, storage: DesktopStorage) -> None:
        self.root = storage.root / "learning" / "cover-works"
        self.path = self.root / "works.json"

    def _read(self) -> list[dict]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        works = payload.get("works") if isinstance(payload, dict) else None
        return [item for item in works if isinstance(item, dict)] if isinstance(works, list) else []

    def record(
        self,
        *,
        streamer: str,
        project: str,
        video: str,
        document: CoverDocument,
        outputs: Iterable[Path],
        canvas_keys: Iterable[str],
        meta: dict | None = None,
    ) -> CoverWork:
        """记一次导出动作（单比例、双比例或批量里的一个视频都只记一条）。"""

        meta = dict(meta or {})
        outputs = tuple(Path(item) for item in outputs)
        texts = _primary_texts(document, "4x3")
        work_id = uuid.uuid4().hex[:12]
        thumbnail = self._thumbnail(outputs[0], work_id) if outputs else ""
        basic = meta.get("basic_copy") or ("", "")
        work = CoverWork(
            work_id=work_id, time=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            streamer=streamer, project=project, video=video, canvas_keys=tuple(canvas_keys),
            composition=composition_signature(document), palette=palette_signature(document),
            context=texts["A"].text if "A" in texts else "", headline=texts["B"].text if "B" in texts else "",
            scheme=str(meta.get("scheme") or ""), edits_after_scheme=int(meta.get("edits_after_scheme") or 0),
            basic_copy=(str(basic[0]), str(basic[1])), thumbnail=thumbnail,
            outputs=tuple(str(item) for item in outputs),
        )
        works = [*self._read(), work.to_payload()][-_KEEP:]
        kept = {item.get("thumbnail") for item in works}
        DesktopStorage._write_json(self.path, {"schema_version": 1, "works": works})
        # 被挤出的旧作品顺手删掉缩略图。
        for stale in self.root.glob("*.jpg"):
            if stale.name not in kept:
                stale.unlink(missing_ok=True)
        return work

    def _thumbnail(self, output: Path, work_id: str) -> str:
        try:
            with Image.open(output) as image:
                image = image.convert("RGB")
                image.thumbnail((_THUMB_WIDTH, _THUMB_WIDTH))
                self.root.mkdir(parents=True, exist_ok=True)
                name = f"{work_id}.jpg"
                image.save(self.root / name, quality=82)
                return name
        except OSError:
            return ""

    def thumbnail_bytes(self, works: Iterable[CoverWork]) -> tuple[bytes, ...]:
        """作品缩略图（JPEG 字节），给看图模型对照；缺失的跳过。"""

        images = []
        for item in works:
            if not item.thumbnail:
                continue
            try:
                images.append((self.root / item.thumbnail).read_bytes())
            except OSError:
                continue
        return tuple(images)

    def recent(self, *, streamer: str | None = None, limit: int = 12) -> tuple[CoverWork, ...]:
        """最近的作品，新的在前；给了主播就只看这个主播的。"""

        works = [CoverWork.from_payload(item) for item in self._read()]
        if streamer:
            works = [item for item in works if item.streamer == streamer]
        return tuple(reversed(works[-limit:]))

    def summary(self) -> dict[str, object]:
        """设置页用：总数、常用构图和方案。"""

        works = [CoverWork.from_payload(item) for item in self._read()]
        return {
            "count": len(works),
            "compositions": Counter(item.composition for item in works if item.composition).most_common(3),
            "schemes": Counter(item.scheme for item in works if item.scheme).most_common(3),
        }
