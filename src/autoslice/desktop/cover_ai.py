"""AutoCover AI Beta 的显式候选契约。

真实模型调用保持关闭；本地 ``mock`` 提供三种可编辑候选，供 UI、缓存和后续
relay 接入使用同一份结构化数据契约。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping

from .cover_model import CoverDocument, TextObject


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
        text = next((item for item in document.objects if isinstance(item, TextObject) and item.copy_role == "B"), None)
        if text is None:
            return ()
        current = text.transform
        variants = (
            ("safe", "稳妥", "沿用当前风格，仅调整安全区", current, "保留当前验证过的层级"),
            ("alternate", "换个构图", "把主视觉移向另一侧", replace(current, x=0.58 if current.x < 0.5 else 0.08), "减少与近期构图重复"),
            ("bold", "大胆一点", "放大主视觉并下移", replace(current, y=min(0.72, current.y + 0.18), scale=min(1.35, current.scale * 1.12)), "允许更强的主视觉变化"),
        )
        candidates: list[CoverAICandidate] = []
        for candidate_id, label, difference, transform, reason in variants:
            override = {"transform": transform.to_payload()}
            suggestion = LayoutSuggestion(profile_key, {text.id: override}, reason, 0.72 if candidate_id == "safe" else 0.56)
            profile = document.profiles.get(profile_key)
            if profile is None:
                continue
            updated_profile = replace(profile, overrides={**profile.overrides, text.id: override})
            updated = replace(document, profiles={**document.profiles, profile_key: updated_profile}, active_profile=profile_key)
            candidates.append(CoverAICandidate(candidate_id, label, difference, updated, suggestion, candidate_id == "safe"))
        return tuple(candidates)

