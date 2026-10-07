"""旧版 CoverDraft 兼容层：读取 v1～v3 草稿、给旧调用方提供平面值，以及旧渲染需要的换行与位置换算。"""

from __future__ import annotations

from dataclasses import dataclass

from .cover_migration import (
    document_from_draft_values,
    document_from_payload,
    draft_values_from_document,
)
from .cover_model import (
    CoverDocument,
)


@dataclass(frozen=True)
class CoverDraft:
    """可恢复的封面编辑状态。坐标使用画布归一化值。"""

    title: str
    image_path: str | None = None
    selected_timestamp: float = 0.0
    text_x: float = 0.06
    text_y: float = 0.18
    font_size: int = 104
    background_x: float = 0.5
    background_y: float = 0.5
    background_scale: float = 1.0

    def to_payload(self) -> dict[str, object]:
        return {
            "version": 3,
            "title": self.title,
            "image_path": self.image_path,
            "selected_timestamp": self.selected_timestamp,
            "text_x": self.text_x,
            "text_y": self.text_y,
            "font_size": self.font_size,
            "background_x": self.background_x,
            "background_y": self.background_y,
            "background_scale": self.background_scale,
        }

    def to_document(self) -> CoverDocument:
        return document_from_draft_values(
            title=self.title,
            image_path=self.image_path,
            selected_timestamp=self.selected_timestamp,
            text_x=self.text_x,
            text_y=self.text_y,
            font_size=self.font_size,
            background_x=self.background_x,
            background_y=self.background_y,
            background_scale=self.background_scale,
        )

    @classmethod
    def from_document(cls, document: CoverDocument) -> "CoverDraft":
        values = draft_values_from_document(document)
        return cls(**values)

    @classmethod
    def from_payload(cls, payload: object, fallback_title: str) -> "CoverDraft":
        if isinstance(payload, dict) and payload.get("version") == 4:
            try:
                document, _migrated = document_from_payload(payload, fallback_title)
                return cls.from_document(document)
            except (TypeError, ValueError):
                return cls(fallback_title)
        if not isinstance(payload, dict) or payload.get("version") not in {1, 2, 3}:
            return cls(fallback_title)
        title = str(payload.get("title") or fallback_title).strip() or fallback_title
        try:
            timestamp = max(0.0, float(payload.get("selected_timestamp", 0.0)))
            text_x = min(1.0, max(0.0, float(payload.get("text_x", 0.06))))
            text_y = min(1.0, max(0.0, float(payload.get("text_y", 0.18))))
            font_size = min(320, max(24, int(payload.get("font_size", 104))))
            background_x = min(1.0, max(0.0, float(payload.get("background_x", 0.5))))
            background_y = min(1.0, max(0.0, float(payload.get("background_y", 0.5))))
            background_scale = min(2.5, max(1.0, float(payload.get("background_scale", 1.0))))
        except (TypeError, ValueError):
            return cls(fallback_title)
        image_path = payload.get("image_path")
        if not isinstance(image_path, str) or not image_path.strip():
            image_path = None
        return cls(
            title, image_path, timestamp, text_x, text_y, font_size,
            background_x, background_y, background_scale,
        )


