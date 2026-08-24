"""基于精确任务字幕生成封面 AI 文案，失败时确定性回退。"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from autoslice.llm import transport as llm_transport
from autoslice.streamer_profiles import resolve_streamer_profile

from .style import PALETTES, TEMPLATES, get_palette, get_template
from .titles import CoverLine, LayoutVariant, recommend_layout_variants, visual_units

DEFAULT_ANALYSIS_MODEL = "gpt-5.6-luna"
DEFAULT_REVIEW_MODEL = "gpt-5.6-terra"
MAX_SRT_CHARS = 18_000
MAX_CONTEXT_TEXT = 500
MAX_CANDIDATES = 3
MAX_LABEL_LENGTH = 32
MAX_REASON_LENGTH = 240
ALLOWED_ROLES = frozenset({"context", "emphasis", "quote", "neutral"})
AI_PALETTE_KEYS = frozenset(
    key for key, palette in PALETTES.items() if palette.context_color.upper().startswith("#FFE")
)
_SRT_TIMECODE_RE = re.compile(
    r"\d{1,2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{1,2}:\d{2}:\d{2}[,.]\d{3}"
)


@dataclass(frozen=True, slots=True)
class CopyRecommendationResult:
    """可直接返回给前端的三组文案候选。"""

    candidates: tuple[LayoutVariant, ...]
    selected_index: int
    source: str
    warning: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "selected_index": self.selected_index,
            "selected_key": self.candidates[self.selected_index].key,
            "source": self.source,
            "warning": self.warning,
        }


def _analysis_model(environ: Mapping[str, str]) -> str:
    return str(environ.get("AUTOSLICE_ANALYSIS_MODEL") or "").strip() or DEFAULT_ANALYSIS_MODEL


def _review_model(environ: Mapping[str, str]) -> str:
    return (
        str(environ.get("AUTOSLICE_LLM_MODEL") or "").strip()
        or str(environ.get("AUTOSLICE_LLM_REVIEW_MODEL") or "").strip()
        or DEFAULT_REVIEW_MODEL
    )


def resolve_task_srt_path(
    video_path: str | Path,
    corrected_srt_path: str | Path | None = None,
) -> Path | None:
    """只接受精确契约路径，或视频同目录同 stem 的确定性 SRT。"""

    if corrected_srt_path:
        exact = Path(corrected_srt_path).expanduser().resolve()
        if exact.is_file():
            return exact
    video = Path(video_path).expanduser().resolve()
    sibling = video.with_suffix(".srt")
    return sibling if sibling.is_file() else None


def _read_limited_srt(path: Path) -> str | None:
    last_error: UnicodeDecodeError | None = None
    content = ""
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            content = path.read_text(encoding=encoding)
            break
        except UnicodeDecodeError as error:
            last_error = error
        except OSError:
            return None
    else:
        if last_error is not None:
            return None
    cleaned = content.replace("\x00", "").strip()
    if not cleaned or not _SRT_TIMECODE_RE.search(cleaned):
        return None
    if len(cleaned) <= MAX_SRT_CHARS:
        return cleaned
    half = (MAX_SRT_CHARS - 80) // 2
    return f"{cleaned[:half]}\n\n[字幕中段因长度限制省略]\n\n{cleaned[-half:]}"


def _bounded_text(value: Any, maximum: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:maximum]


def _validated_text(value: Any, maximum: int, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是字符串")
    cleaned = re.sub(r"\s+", " ", value).strip()
    if not cleaned or len(cleaned) > maximum:
        raise ValueError(f"{field} 为空或超过长度限制")
    return cleaned


def _fallback_candidates(title: str, editorial_reason: str | None) -> tuple[LayoutVariant, ...]:
    cleaned_title = _bounded_text(title, MAX_CONTEXT_TEXT) or "未命名切片"
    reason = _bounded_text(editorial_reason, 160)
    base_variants = recommend_layout_variants(cleaned_title)
    enriched_variants = (
        recommend_layout_variants(f"{cleaned_title}，{reason}")
        if reason and reason.casefold() not in cleaned_title.casefold()
        else []
    )
    ordered = [base_variants[0]]
    if enriched_variants:
        ordered.append(enriched_variants[0])
    ordered.extend(base_variants[1:])
    ordered.extend(enriched_variants[1:])

    pool: list[LayoutVariant] = []
    signatures: set[tuple[str, ...]] = set()
    for variant in ordered:
        signature = (
            variant.template_key,
            variant.palette_key,
            *(f"{line.role}:{line.text.casefold()}" for line in variant.lines),
        )
        if signature in signatures:
            continue
        signatures.add(signature)
        pool.append(variant)

    selected = pool[:MAX_CANDIDATES]
    if len(selected) < MAX_CANDIDATES:
        raise RuntimeError("确定性封面候选不足")
    return tuple(
        LayoutVariant(
            key=f"fallback-{index + 1}",
            label=("标题稳妥版", "理由强化版", "精简点击版")[index],
            template_key=variant.template_key,
            palette_key=variant.palette_key,
            reason=(
                "使用当前任务标题确定性生成"
                if index != 1 or not reason
                else "结合当前任务标题与编辑价值理由确定性生成"
            ),
            lines=variant.lines,
        )
        for index, variant in enumerate(selected)
    )


def _candidate_from_payload(item: Any, index: int) -> LayoutVariant:
    if not isinstance(item, dict):
        raise ValueError("候选必须是对象")
    label = _validated_text(item.get("label"), MAX_LABEL_LENGTH, "label")
    reason = _validated_text(item.get("reason"), MAX_REASON_LENGTH, "reason")
    template_key = _validated_text(item.get("template_key"), 64, "template_key")
    palette_key = _validated_text(item.get("palette_key"), 64, "palette_key")
    if template_key not in TEMPLATES or palette_key not in AI_PALETTE_KEYS:
        raise ValueError("候选模板或调色板不受支持")
    template = get_template(template_key)
    get_palette(palette_key)
    raw_lines = item.get("lines")
    if not isinstance(raw_lines, list) or not 1 <= len(raw_lines) <= template.max_lines:
        raise ValueError("候选行数不合法")
    lines: list[CoverLine] = []
    seen_text: set[str] = set()
    for raw_line in raw_lines:
        if not isinstance(raw_line, dict):
            raise ValueError("候选行必须是对象")
        text = _validated_text(raw_line.get("text"), 80, "line.text")
        role = _validated_text(raw_line.get("role"), 16, "line.role")
        normalized = text.casefold()
        if (
            not text
            or role not in ALLOWED_ROLES
            or normalized in seen_text
            or visual_units(text) > template.max_line_units
        ):
            raise ValueError("候选行文字或 role 不合法")
        seen_text.add(normalized)
        lines.append(CoverLine(text=text, role=role))
    roles = {line.role for line in lines}
    if len(lines) > 1 and not {"context", "emphasis"}.issubset(roles):
        raise ValueError("多行候选必须包含主题与爆点角色")
    return LayoutVariant(
        key=f"ai-{index + 1}",
        label=label,
        template_key=template_key,
        palette_key=palette_key,
        reason=reason,
        lines=tuple(lines),
    )


def validate_candidate_payload(payload: Any) -> tuple[LayoutVariant, ...]:
    """严格校验 Luna 返回的三组候选。"""

    if not isinstance(payload, dict) or not isinstance(payload.get("candidates"), list):
        raise ValueError("缺少 candidates")
    raw_candidates = payload["candidates"]
    if len(raw_candidates) != MAX_CANDIDATES:
        raise ValueError("AI 必须返回三组候选")
    candidates = tuple(
        _candidate_from_payload(item, index) for index, item in enumerate(raw_candidates)
    )
    signatures = {
        tuple((line.text.casefold(), line.role) for line in candidate.lines)
        for candidate in candidates
    }
    if len(signatures) != MAX_CANDIDATES:
        raise ValueError("AI 候选不能重复")
    return candidates


def _validate_review_payload(payload: Any) -> int:
    if not isinstance(payload, dict):
        raise ValueError("复核结果必须是对象")
    selected = payload.get("selected_index")
    reviews = payload.get("reviews")
    if isinstance(selected, bool) or not isinstance(selected, int) or not 0 <= selected < 3:
        raise ValueError("复核选择不合法")
    if not isinstance(reviews, list) or len(reviews) != 3:
        raise ValueError("复核必须覆盖三组候选")
    indexed: dict[int, dict[str, Any]] = {}
    for review in reviews:
        if not isinstance(review, dict):
            raise ValueError("复核项必须是对象")
        index = review.get("candidate_index")
        score = review.get("clickability_score")
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or not 0 <= index < 3
            or isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not 1 <= float(score) <= 5
            or review.get("original_accuracy") not in {"pass", "fail"}
            or review.get("hook_consequence") not in {"pass", "fail"}
            or not _validated_text(review.get("reason"), MAX_REASON_LENGTH, "review.reason")
        ):
            raise ValueError("复核项字段不合法")
        indexed[index] = review
    if set(indexed) != {0, 1, 2}:
        raise ValueError("复核候选索引重复或缺失")
    chosen = indexed[selected]
    if chosen["original_accuracy"] != "pass" or chosen["hook_consequence"] != "pass":
        raise ValueError("Terra 选择了未通过准确性或爆点检查的候选")
    return selected


def _analysis_prompt(context: dict[str, Any]) -> str:
    schema = {
        "candidates": [
            {
                "label": "候选标签",
                "reason": "为什么适合点击且不歪曲原话",
                "template_key": "现有模板 key",
                "palette_key": "现有调色板 key",
                "lines": [{"text": "封面大字", "role": "context|emphasis|quote|neutral"}],
            }
        ]
    }
    return (
        "你是 AutoCover 的 Luna 文案生成阶段。只依据给定的最终校对字幕和任务元数据，"
        "生成恰好 3 组彼此不同、可直接用于封面的结构化候选。不得编造字幕没有的事实、"
        "说话人、后果或原话；必须优先写出真正爆点、具体后果/反差和有点击欲的大字。\n"
        "role 语义固定：context=主题背景黄字；emphasis=真正爆点/后果强调色；"
        "quote 与 neutral=不同对话角色，只有能可靠区分说话方时使用。每组多行候选必须同时"
        "包含 context 与 emphasis。只能使用给出的模板和调色板 key，并遵守各模板行数与"
        "单行宽度限制。只输出 JSON，不要 Markdown。\n"
        f"允许模板：{json.dumps({key: {'max_lines': value.max_lines, 'max_line_units': value.max_line_units} for key, value in TEMPLATES.items()}, ensure_ascii=False)}\n"
        f"允许调色板：{json.dumps(sorted(AI_PALETTE_KEYS), ensure_ascii=False)}\n"
        f"输出结构：{json.dumps(schema, ensure_ascii=False)}\n"
        f"任务输入：{json.dumps(context, ensure_ascii=False)}"
    )


def _review_prompt(context: dict[str, Any], candidates: tuple[LayoutVariant, ...]) -> str:
    candidate_payload = [candidate.to_dict() for candidate in candidates]
    schema = {
        "selected_index": 0,
        "reviews": [
            {
                "candidate_index": 0,
                "original_accuracy": "pass|fail",
                "hook_consequence": "pass|fail",
                "clickability_score": 1,
                "reason": "独立复核结论",
            }
        ],
    }
    return (
        "你是 AutoCover 的 Terra 独立复核阶段。重新阅读最终校对字幕，不采信 Luna 的理由，"
        "逐组检查：1) 原话、事实和说话人是否准确；2) 是否抓到真正爆点以及具体后果/反差；"
        "3) 封面大字是否有点击欲且简洁。必须覆盖 3 组候选，并只从原话准确且爆点/后果通过"
        "的候选中自动选择一组。只输出 JSON，不要改写候选，不要 Markdown。\n"
        f"输出结构：{json.dumps(schema, ensure_ascii=False)}\n"
        f"任务输入：{json.dumps(context, ensure_ascii=False)}\n"
        f"待复核候选：{json.dumps(candidate_payload, ensure_ascii=False)}"
    )


def _safe_warning(stage: str, error: BaseException | None = None) -> str:
    status = llm_transport.llm_http_status(error) if error is not None else None
    detail = f"（HTTP {status}）" if status is not None else ""
    return f"{stage}{detail}，已使用不联网的确定性文案候选。"


def generate_copy_recommendations(
    *,
    video_path: str | Path,
    current_title: str,
    publish_title: str | None = None,
    editorial_interest_reason: str | None = None,
    corrected_srt_path: str | Path | None = None,
    runner: Callable[..., str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> CopyRecommendationResult:
    """显式执行 Luna 生成 + Terra 复核；任何正常失败均安全回退。"""

    fallback = _fallback_candidates(current_title, editorial_interest_reason)
    srt_path = resolve_task_srt_path(video_path, corrected_srt_path)
    srt_content = _read_limited_srt(srt_path) if srt_path is not None else None
    if not srt_content:
        return CopyRecommendationResult(
            candidates=fallback,
            selected_index=0,
            source="fallback",
            warning="未找到可靠的最终校对 SRT，未调用 AI，已使用确定性文案候选。",
        )

    try:
        profile = resolve_streamer_profile("auto", video_path, context_hint=current_title)
        streamer = {
            "canonical_name": profile.canonical_name,
            "report_name": profile.report_name,
            "aliases": list(profile.aliases[:8]),
        }
    except (OSError, TypeError, ValueError):
        streamer = {
            "canonical_name": "主播",
            "report_name": "主播",
            "aliases": ["主播"],
        }
    context = {
        "corrected_srt": srt_content,
        "editorial_interest_reason": _bounded_text(
            editorial_interest_reason, MAX_CONTEXT_TEXT
        ),
        "publish_title": _bounded_text(publish_title, MAX_CONTEXT_TEXT),
        "current_task_title": _bounded_text(current_title, MAX_CONTEXT_TEXT),
        "streamer": streamer,
    }
    active_runner = runner or llm_transport.call_llm_with_retry
    active_environ = os.environ if environ is None else environ
    try:
        generated_text = active_runner(
            _analysis_prompt(context),
            compact_prompt=None,
            max_tokens=3200,
            compact_max_tokens=2800,
            require_json=True,
            model_override=_analysis_model(active_environ),
            reasoning_stage="analysis",
        )
        candidates = validate_candidate_payload(
            llm_transport.extract_json_payload(generated_text)
        )
    except Exception as error:
        return CopyRecommendationResult(
            candidates=fallback,
            selected_index=0,
            source="fallback",
            warning=_safe_warning("Luna 文案生成失败或输出不合法", error),
        )

    try:
        review_text = active_runner(
            _review_prompt(context, candidates),
            compact_prompt=None,
            max_tokens=2200,
            compact_max_tokens=1800,
            require_json=True,
            model_override=_review_model(active_environ),
            reasoning_stage="review",
        )
        selected_index = _validate_review_payload(
            llm_transport.extract_json_payload(review_text)
        )
    except Exception as error:
        return CopyRecommendationResult(
            candidates=fallback,
            selected_index=0,
            source="fallback",
            warning=_safe_warning("Terra 独立复核失败或输出不合法", error),
        )

    return CopyRecommendationResult(
        candidates=candidates,
        selected_index=selected_index,
        source="ai",
    )


__all__ = [
    "ALLOWED_ROLES",
    "CopyRecommendationResult",
    "generate_copy_recommendations",
    "resolve_task_srt_path",
    "validate_candidate_payload",
]
