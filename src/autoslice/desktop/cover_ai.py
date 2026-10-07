"""AutoCover AI Beta 的显式候选契约。

真实模型调用保持关闭；本地 ``mock`` 提供三种可编辑候选，供 UI、缓存和后续
relay 接入使用同一份结构化数据契约。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping

from .cover_model import CoverDocument, TextObject, object_for_profile


@dataclass(frozen=True, slots=True)
class LayoutSuggestion:
    profile_key: str
    object_overrides: dict[str, dict[str, Any]]
    reason: str
    confidence: float = 0.5

    def to_payload(self) -> dict[str, object]:
        return {
            "profile_key": self.profile_key,
            "object_overrides": self.object_overrides,
            "reason": self.reason,
            "confidence": self.confidence,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "LayoutSuggestion":
        return cls(
            profile_key=str(payload.get("profile_key") or "4x3"),
            object_overrides={
                str(key): dict(value)
                for key, value in (payload.get("object_overrides") or {}).items()
                if isinstance(key, str) and isinstance(value, Mapping)
            },
            reason=str(payload.get("reason") or ""),
            confidence=max(0.0, min(1.0, float(payload.get("confidence", 0.5)))),
        )


@dataclass(frozen=True, slots=True)
class CoverAICandidate:
    candidate_id: str
    label: str
    difference: str
    document: CoverDocument
    suggestion: LayoutSuggestion
    recommended: bool = False

    def to_payload(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "label": self.label,
            "difference": self.difference,
            "document": self.document.to_payload(),
            "suggestion": self.suggestion.to_payload(),
            "recommended": self.recommended,
        }


class CoverAIBeta:
    """显式召唤的候选生成器；无 relay 时稳定返回本地 mock。"""

    def __init__(self, *, enabled: bool = False) -> None:
        self.enabled = bool(enabled)

    def suggest(self, document: CoverDocument, *, profile_key: str = "4x3") -> tuple[CoverAICandidate, ...]:
        profile = document.profiles.get(profile_key)
        if profile is None:
            return ()

        text_objects = [item for item in document.objects if isinstance(item, TextObject)]
        text_b = next((item for item in text_objects if item.copy_role == "B"), None)
        if text_b is None:
            text_b = text_objects[0] if text_objects else None
        if text_b is None:
            return ()
        effective_b = object_for_profile(document, text_b.id, profile_key)
        if not isinstance(effective_b, TextObject):
            effective_b = text_b
        text_a = next((item for item in text_objects if item.copy_role == "A"), None)
        effective_a = object_for_profile(document, text_a.id, profile_key) if text_a else None
        if not isinstance(effective_a, TextObject):
            effective_a = text_a

        def clamp(value: float, lower: float = 0.04, upper: float = 0.88) -> float:
            return max(lower, min(upper, float(value)))

        def nudge_side(value: float, amount: float = 0.04) -> float:
            if value <= 0.12:
                return clamp(value + amount)
            if value >= 0.66:
                return clamp(value - amount)
            return clamp(value + amount)

        def override(item: TextObject) -> dict[str, object]:
            return {
                "transform": item.transform.to_payload(),
                "visible": bool(item.visible),
                "rect": item.rect.to_payload(),
                "wrap": item.wrap.to_payload(),
                "align": item.align,
                "style": item.style.to_payload(),
            }

        def with_layout(
            candidate_id: str,
            label: str,
            difference: str,
            reason: str,
            updated_b: TextObject,
            updated_a: TextObject | None,
            confidence: float,
            recommended: bool = False,
        ) -> CoverAICandidate:
            changed: dict[str, dict[str, object]] = {updated_b.id: override(updated_b)}
            if updated_a is not None:
                changed[updated_a.id] = override(updated_a)
            updated_profile = replace(profile, overrides={**profile.overrides, **changed})
            updated = replace(
                document,
                profiles={**document.profiles, profile_key: updated_profile},
                active_profile=profile_key,
            )
            return CoverAICandidate(
                candidate_id,
                label,
                difference,
                updated,
                LayoutSuggestion(profile_key, changed, reason, confidence),
                recommended,
            )

        safe_b_transform = replace(
            effective_b.transform,
            x=nudge_side(effective_b.transform.x),
            y=clamp(effective_b.transform.y + 0.025),
            scale=max(0.01, min(100.0, effective_b.transform.scale * 1.03)),
        )
        safe_b = replace(
            effective_b,
            transform=safe_b_transform,
            rect=replace(effective_b.rect, width=clamp(effective_b.rect.width * 1.02, 0.04, 0.96)),
        )
        safe_a = (
            replace(
                effective_a,
                transform=replace(
                    effective_a.transform,
                    x=safe_b_transform.x,
                    y=clamp(safe_b_transform.y - max(0.10, effective_a.rect.height + 0.035), 0.04, 0.84),
                ),
            )
            if effective_a is not None and effective_a.visible
            else effective_a
        )

        alternate_x = 0.66 if effective_b.transform.x < 0.5 else 0.10
        alternate_b = replace(
            effective_b,
            transform=replace(effective_b.transform, x=alternate_x, y=clamp(effective_b.transform.y + 0.01)),
        )
        alternate_a = (
            replace(
                effective_a,
                transform=replace(
                    effective_a.transform,
                    x=alternate_x,
                    y=clamp(alternate_b.transform.y - max(0.10, effective_a.rect.height + 0.035), 0.04, 0.84),
                ),
            )
            if effective_a is not None and effective_a.visible
            else effective_a
        )

        bold_y = effective_b.transform.y + 0.12
        if bold_y > 0.76:
            bold_y = effective_b.transform.y - 0.10
        bold_size = max(24, min(320, effective_b.style.font_size + 12))
        bold_b = replace(
            effective_b,
            transform=replace(
                effective_b.transform,
                y=clamp(bold_y),
                scale=max(0.01, min(100.0, effective_b.transform.scale * 1.18)),
            ),
            style=replace(effective_b.style, font_size=bold_size),
            rect=replace(
                effective_b.rect,
                width=clamp(effective_b.rect.width * 1.08, 0.04, 0.96),
                height=clamp(effective_b.rect.height * 1.10, 0.04, 0.96),
            ),
        )
        bold_a = (
            replace(
                effective_a,
                transform=replace(
                    effective_a.transform,
                    x=bold_b.transform.x,
                    y=clamp(bold_b.transform.y - max(0.10, effective_a.rect.height + 0.035), 0.04, 0.84),
                ),
                style=replace(effective_a.style, font_size=max(24, min(320, effective_a.style.font_size + 6))),
            )
            if effective_a is not None and effective_a.visible
            else effective_a
        )

        candidates: list[CoverAICandidate] = []
        candidates.append(with_layout("safe", "稳妥", "微调位置和安全区", "保留当前层级，给文字留出更均衡的安全边距", safe_b, safe_a, 0.72, True))
        candidates.append(with_layout("alternate", "换个构图", "把 A/B 主视觉移到另一侧", "让文字避开当前主体区域，保持 A/B 间距", alternate_b, alternate_a, 0.56))
        candidates.append(with_layout("bold", "大胆一点", "放大字号并下移主视觉", "提高主视觉层级，但仍限制在画布安全区", bold_b, bold_a, 0.56))
        return tuple(candidates)

