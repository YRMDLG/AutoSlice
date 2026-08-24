"""基于精确任务字幕生成封面 AI 文案，失败时确定性回退。"""

from __future__ import annotations

import json
import math
import os
import re
import unicodedata
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
MAX_REASON_LENGTH = 120
MAX_EVIDENCE_LENGTH = 80
MIN_EVIDENCE_LENGTH = 2
ALLOWED_ROLES = frozenset({"context", "emphasis", "quote", "neutral"})
AI_PALETTE_KEYS = frozenset(
    key for key, palette in PALETTES.items() if palette.context_color.upper().startswith("#FFE")
)
_SRT_TIMECODE_RE = re.compile(
    r"\d{1,2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{1,2}:\d{2}:\d{2}[,.]\d{3}"
)
_INTEGER_STRING_RE = re.compile(r"[0-9]+")
_SCORE_STRING_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_REPEATED_ASCII_RE = re.compile(r"([A-Za-z])\1{2,}")
_OMISSION_MARKER = "[字幕中段因长度限制省略]"
_REVIEW_CHECK_VALUES = {
    "pass": "pass",
    "true": "pass",
    "通过": "pass",
    "fail": "fail",
    "false": "fail",
    "不通过": "fail",
}


class _NoPassingReviewError(ValueError):
    """Terra 已完整复核，但没有事实与爆点检查均通过的候选。"""


class _ReviewValidationError(ValueError):
    """Terra 复核结果的安全、可展示失败分类。"""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


class _CandidateContentValidationError(ValueError):
    """Luna 候选内容护栏失败时使用的安全分类。"""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


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


def _validated_reason(value: Any, field: str) -> str:
    """校验 reason 必须非空，过长解释只做确定性截断。"""

    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是字符串")
    cleaned = re.sub(r"\s+", " ", value).strip()
    if not cleaned:
        raise ValueError(f"{field} 不能为空")
    return cleaned[:MAX_REASON_LENGTH]


def _has_suspect_content(value: str) -> bool:
    """保守拦截明确乱码，不把正常中文重复语气词当成异常。"""

    if "\ufffd" in value or _REPEATED_ASCII_RE.search(value):
        return True
    if any(unicodedata.category(character) == "Cc" for character in value):
        return True
    return not any(character.isalnum() for character in value)


def _normalized_source_segments(source_text: str) -> tuple[str, ...]:
    """返回实际保留的字幕正文，避免把时间码或省略段当成证据。"""

    segments: list[str] = []
    for raw_line in source_text.splitlines():
        line = raw_line.strip()
        if (
            not line
            or line == _OMISSION_MARKER
            or line.isdigit()
            or _SRT_TIMECODE_RE.fullmatch(line)
        ):
            continue
        normalized = re.sub(r"\s+", " ", line).strip()
        if normalized:
            segments.append(normalized)
    return tuple(segments)


def _validated_evidence_quotes(item: Any, source_text: str | None, index: int) -> tuple[str, ...]:
    if not isinstance(item, list) or not 1 <= len(item) <= 2:
        raise _CandidateContentValidationError(
            "内容无字幕依据", f"候选 {index + 1} 的 evidence_quotes 数量不合法"
        )
    quotes: list[str] = []
    for quote in item:
        if not isinstance(quote, str):
            raise _CandidateContentValidationError(
                "内容无字幕依据", f"候选 {index + 1} 的 evidence_quotes 必须是字符串"
            )
        normalized = re.sub(r"\s+", " ", quote).strip()
        if not MIN_EVIDENCE_LENGTH <= len(normalized) <= MAX_EVIDENCE_LENGTH:
            raise _CandidateContentValidationError(
                "内容无字幕依据", f"候选 {index + 1} 的证据长度不合法"
            )
        if _OMISSION_MARKER in normalized or "省略" in normalized:
            raise _CandidateContentValidationError(
                "内容无字幕依据", f"候选 {index + 1} 的证据引用了省略段"
            )
        if _has_suspect_content(normalized):
            raise _CandidateContentValidationError(
                "文案疑似乱码", f"候选 {index + 1} 的证据疑似乱码"
            )
        if normalized.casefold() in {quote.casefold() for quote in quotes}:
            raise _CandidateContentValidationError(
                "内容无字幕依据", f"候选 {index + 1} 的证据不能重复"
            )
        if source_text is not None and not any(
            normalized in segment for segment in _normalized_source_segments(source_text)
        ):
            raise _CandidateContentValidationError(
                "内容无字幕依据", f"候选 {index + 1} 的证据不在最终校对字幕中"
            )
        quotes.append(normalized)
    return tuple(quotes)


def _fallback_candidates(
    title: str,
    editorial_reason: str | None,
    *,
    video_path: str | Path | None = None,
    srt_content: str | None = None,
) -> tuple[LayoutVariant, ...]:
    source_sentences = _readable_srt_sentences(srt_content)
    if source_sentences:
        fallback_titles = [source_sentences[0], source_sentences[-1]]
        if len(source_sentences) > 1:
            fallback_titles.append(f"{source_sentences[0]}，{source_sentences[-1]}")
        else:
            fallback_titles.append(source_sentences[0])
        cleaned_title = fallback_titles[0]
        reason = ""
        base_variants = []
        for fallback_title in fallback_titles:
            base_variants.extend(
                recommend_layout_variants(fallback_title, video_path=video_path)
            )
        enriched_variants: list[LayoutVariant] = []
    else:
        cleaned_title = _bounded_text(title, MAX_CONTEXT_TEXT) or "未命名切片"
        if _has_suspect_content(cleaned_title):
            cleaned_title = "未命名切片"
        reason = _bounded_text(editorial_reason, 160)
        if _has_suspect_content(reason):
            reason = ""
        base_variants = recommend_layout_variants(cleaned_title, video_path=video_path)
        enriched_variants = (
            recommend_layout_variants(
                f"{cleaned_title}，{reason}",
                video_path=video_path,
            )
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
                "使用最终校对字幕确定性生成"
                if source_sentences
                else (
                    "使用当前任务标题确定性生成"
                    if index != 1 or not reason
                    else "结合当前任务标题与编辑价值理由确定性生成"
                )
            ),
            lines=variant.lines,
        )
        for index, variant in enumerate(selected)
    )


def _readable_srt_sentences(srt_content: str | None) -> list[str]:
    """从实际保留的 SRT 正文提取可读字幕句子，绝不使用省略标记。"""

    if not srt_content:
        return []
    sentences: list[str] = []
    for raw_line in srt_content.splitlines():
        line = raw_line.strip()
        if not line or line == _OMISSION_MARKER or line.isdigit() or _SRT_TIMECODE_RE.fullmatch(line):
            continue
        line = re.sub(r"\s+", " ", line)
        if not 2 <= len(line) <= MAX_EVIDENCE_LENGTH or _has_suspect_content(line):
            continue
        if line.casefold() not in {sentence.casefold() for sentence in sentences}:
            sentences.append(line)
    return sentences


def _candidate_from_payload(
    item: Any, index: int, source_text: str | None = None
) -> LayoutVariant:
    if not isinstance(item, dict):
        raise ValueError("候选必须是对象")
    label = _validated_text(item.get("label"), MAX_LABEL_LENGTH, "label")
    reason = _validated_reason(item.get("reason"), "reason")
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
        raw_text = raw_line.get("text")
        if isinstance(raw_text, str) and _has_suspect_content(raw_text):
            raise _CandidateContentValidationError("文案疑似乱码", "候选行疑似乱码")
        text = _validated_text(raw_text, 80, "line.text")
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
    _validated_evidence_quotes(item.get("evidence_quotes"), source_text, index)
    return LayoutVariant(
        key=f"ai-{index + 1}",
        label=label,
        template_key=template_key,
        palette_key=palette_key,
        reason=reason,
        lines=tuple(lines),
    )


def validate_candidate_payload(
    payload: Any, source_text: str | None = None
) -> tuple[LayoutVariant, ...]:
    """严格校验 Luna 返回的三组候选；生产调用必须传入最终校对字幕正文。"""

    if not isinstance(payload, dict) or not isinstance(payload.get("candidates"), list):
        raise ValueError("缺少 candidates")
    raw_candidates = payload["candidates"]
    if len(raw_candidates) != MAX_CANDIDATES:
        raise ValueError("AI 必须返回三组候选")
    candidates = tuple(
        _candidate_from_payload(item, index, source_text)
        for index, item in enumerate(raw_candidates)
    )
    signatures = {
        tuple((line.text.casefold(), line.role) for line in candidate.lines)
        for candidate in candidates
    }
    if len(signatures) != MAX_CANDIDATES:
        raise ValueError("AI 候选不能重复")
    return candidates


def _normalized_review_index(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise _ReviewValidationError("reviews/索引不合法", f"{field} 不合法")
    if isinstance(value, int):
        index = value
    elif isinstance(value, str) and _INTEGER_STRING_RE.fullmatch(value.strip()):
        index = int(value.strip())
    else:
        raise _ReviewValidationError("reviews/索引不合法", f"{field} 不合法")
    if not 0 <= index < MAX_CANDIDATES:
        raise _ReviewValidationError("reviews/索引不合法", f"{field} 超出范围")
    return index


def _normalized_review_score(value: Any) -> float:
    if isinstance(value, bool):
        raise _ReviewValidationError("分数不合法", "clickability_score 不合法")
    if isinstance(value, (int, float)):
        score = float(value)
    elif isinstance(value, str) and _SCORE_STRING_RE.fullmatch(value.strip()):
        score = float(value.strip())
    else:
        raise _ReviewValidationError("分数不合法", "clickability_score 不合法")
    if not math.isfinite(score) or not 1 <= score <= 5:
        raise _ReviewValidationError("分数不合法", "clickability_score 超出范围")
    return score


def _normalized_review_check(value: Any, field: str) -> str:
    if isinstance(value, bool):
        return "pass" if value else "fail"
    if isinstance(value, str):
        normalized = _REVIEW_CHECK_VALUES.get(value.strip().casefold())
        if normalized is not None:
            return normalized
    raise _ReviewValidationError("状态字段不合法", f"{field} 不合法")


def _validate_review_payload(payload: Any) -> int:
    if not isinstance(payload, dict):
        raise _ReviewValidationError("reviews/索引不合法", "复核结果必须是对象")
    selected = _normalized_review_index(payload.get("selected_index"), "selected_index")
    reviews = payload.get("reviews")
    if not isinstance(reviews, list) or len(reviews) != MAX_CANDIDATES:
        raise _ReviewValidationError("reviews/索引不合法", "复核必须覆盖三组候选")
    indexed: dict[int, dict[str, Any]] = {}
    for review in reviews:
        if not isinstance(review, dict):
            raise _ReviewValidationError("reviews/索引不合法", "复核项必须是对象")
        index = _normalized_review_index(review.get("candidate_index"), "candidate_index")
        if index in indexed:
            raise _ReviewValidationError("reviews/索引不合法", "复核候选索引重复")
        try:
            reason = _validated_reason(review.get("reason"), "review.reason")
        except (TypeError, ValueError) as error:
            raise _ReviewValidationError("reason 不合法/为空", "review.reason 不合法") from error
        indexed[index] = {
            "original_accuracy": _normalized_review_check(
                review.get("original_accuracy"), "original_accuracy"
            ),
            "hook_consequence": _normalized_review_check(
                review.get("hook_consequence"), "hook_consequence"
            ),
            "clickability_score": _normalized_review_score(
                review.get("clickability_score")
            ),
            "reason": reason,
        }
    if set(indexed) != set(range(MAX_CANDIDATES)):
        raise _ReviewValidationError("reviews/索引不合法", "复核候选索引重复或缺失")
    chosen = indexed[selected]
    if chosen["original_accuracy"] == "pass" and chosen["hook_consequence"] == "pass":
        return selected
    passing = [
        (index, review)
        for index, review in indexed.items()
        if review["original_accuracy"] == "pass"
        and review["hook_consequence"] == "pass"
    ]
    if not passing:
        raise _NoPassingReviewError("Terra 没有给出双通过候选")
    return min(
        passing,
        key=lambda item: (-item[1]["clickability_score"], item[0]),
    )[0]


def _analysis_prompt(context: dict[str, Any]) -> str:
    schema = {
        "candidates": [
            {
                "label": "候选标签",
                "reason": "为什么适合点击且不歪曲原话",
                "template_key": "现有模板 key",
                "palette_key": "现有调色板 key",
                "evidence_quotes": ["最终校对字幕中的逐字连续短句"],
                "lines": [{"text": "封面大字", "role": "context|emphasis|quote|neutral"}],
            }
        ]
    }
    return (
        "你是 AutoCover 的 Luna 文案生成阶段。只依据给定的最终校对字幕和任务元数据，"
        "生成恰好 3 组彼此不同、可直接用于封面的结构化候选。不得编造字幕没有的事实、"
        "说话人、后果或原话；必须优先保留原话、诱因、冲突、后果或收尾，再做简短改写，"
        "不得发明事实。每组必须提供 1 到 2 条 evidence_quotes；每条都是从最终校对 SRT"
        "正文逐字复制的连续短句，不能为空、不能来自省略段，证据只用于校验且不会展示给前端。\n"
        "role 语义固定：context=主题背景黄字；emphasis=真正爆点/后果强调色；"
        "quote 与 neutral=不同对话角色，只有能可靠区分说话方时使用。每组多行候选必须同时"
        "包含 context 与 emphasis。只能使用给出的模板和调色板 key，并遵守各模板行数与"
        "单行宽度限制。reason 必须是非空字符串且不超过 120 字，解释过长时请直接压缩。"
        "只输出 JSON，不要 Markdown。\n"
        f"允许模板：{json.dumps({key: {'max_lines': value.max_lines, 'max_line_units': value.max_line_units} for key, value in TEMPLATES.items()}, ensure_ascii=False)}\n"
        f"允许调色板：{json.dumps(sorted(AI_PALETTE_KEYS), ensure_ascii=False)}\n"
        f"输出结构：{json.dumps(schema, ensure_ascii=False)}\n"
        f"任务输入：{json.dumps(context, ensure_ascii=False)}"
    )


def _review_prompt(
    context: dict[str, Any],
    candidates: tuple[LayoutVariant, ...],
    evidence_quotes: tuple[tuple[str, ...], ...] | None = None,
) -> str:
    candidate_payload = [candidate.to_dict() for candidate in candidates]
    if evidence_quotes is not None:
        for candidate, quotes in zip(candidate_payload, evidence_quotes):
            candidate["evidence_quotes"] = list(quotes)
    schema = {
        "selected_index": 0,
        "reviews": [
            {
                "candidate_index": index,
                "original_accuracy": "pass",
                "hook_consequence": "pass",
                "clickability_score": 5 - index,
                "reason": f"候选 {index + 1} 的独立复核结论",
            }
            for index in range(MAX_CANDIDATES)
        ],
    }
    return (
        "你是 AutoCover 的 Terra 独立复核阶段。重新阅读最终校对字幕，不采信 Luna 的理由，"
        "逐组独立检查：1) 每条 evidence_quotes 是否确实支持候选文字；2) 原话、事实和说话人"
        "是否准确；3) 文案是否语义通顺，是否出现乱码、无意义拼接或重复 ASCII 字母；4) 是否"
        "抓到真正爆点以及具体后果/反差；5) 封面大字是否有点击欲且简洁。任何一项失败都必须"
        "标记 fail；不能把所有候选默认 pass。必须覆盖 3 组候选，并只从证据充分、内容通顺、"
        "原话准确且爆点/后果通过的候选中自动选择一组。"
        "reviews 必须按 candidate_index 0、1、2 各输出一次；"
        "original_accuracy 与 hook_consequence 只允许使用具体字符串 pass 或 fail，"
        "不得输出组合占位符；clickability_score 必须是 1 到 5 的数字；reason 必须是非空"
        "字符串且不超过 120 字，解释过长时请直接压缩。只输出 JSON，不要改写候选，"
        "不要 Markdown。\n"
        f"输出结构：{json.dumps(schema, ensure_ascii=False)}\n"
        f"任务输入：{json.dumps(context, ensure_ascii=False)}\n"
        f"待复核候选：{json.dumps(candidate_payload, ensure_ascii=False)}"
    )


def _review_correction_prompt(
    context: dict[str, Any],
    candidates: tuple[LayoutVariant, ...],
    evidence_quotes: tuple[tuple[str, ...], ...] | None = None,
) -> str:
    """构造不回显上次回答的固定格式纠正请求。"""

    schema = {
        "selected_index": 0,
        "reviews": [
            {
                "candidate_index": index,
                "original_accuracy": "pass",
                "hook_consequence": "fail",
                "clickability_score": 1,
                "reason": "非空且不超过 120 字的复核理由",
            }
            for index in range(MAX_CANDIDATES)
        ],
    }
    candidate_payload = [candidate.to_dict() for candidate in candidates]
    if evidence_quotes is not None:
        for candidate, quotes in zip(candidate_payload, evidence_quotes):
            candidate["evidence_quotes"] = list(quotes)
    return (
        "你是 AutoCover 的 Terra 独立复核格式纠正阶段。请重新依据最终校对字幕和待复核候选"
        "完成复核；独立检查 evidence_quotes 是否支持候选、文字是否语义通顺、是否有乱码或"
        "无意义拼接；任何一项失败都标记 fail。不要引用、猜测或复述任何上一次回答，只输出"
        "下面契约中的 JSON。必须输出"
        "selected_index 为明确的 0、1 或 2；reviews 恰好三项，并按 candidate_index 0、1、2"
        "各出现一次。original_accuracy 与 hook_consequence 只能是字符串 pass 或 fail；"
        "clickability_score 必须是 1 到 5 的数字；reason 必须是非空字符串且不超过 120 字。"
        "只有证据充分、内容通顺、原话准确和爆点/后果均为 pass 的候选才可以被 selected_index 选中；没有双通过候选"
        "时必须如实全部标记为 fail，不得凭空放行。不要 Markdown，不要改写候选。\n"
        f"输出结构：{json.dumps(schema, ensure_ascii=False)}\n"
        f"任务输入：{json.dumps(context, ensure_ascii=False)}\n"
        f"待复核候选：{json.dumps(candidate_payload, ensure_ascii=False)}"
    )


def _safe_warning(
    stage: str,
    category: str,
    error: BaseException | None = None,
) -> str:
    status = llm_transport.llm_http_status(error) if error is not None else None
    detail = f"HTTP {status}（{category}）" if status is not None else category
    return f"{stage}：{detail}，已使用不联网的确定性文案候选。"


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

    srt_path = resolve_task_srt_path(video_path, corrected_srt_path)
    srt_content = _read_limited_srt(srt_path) if srt_path is not None else None
    fallback = _fallback_candidates(
        current_title,
        editorial_interest_reason,
        video_path=video_path,
        srt_content=srt_content,
    )
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
    except Exception as error:
        return CopyRecommendationResult(
            candidates=fallback,
            selected_index=0,
            source="fallback",
            warning=_safe_warning("Luna 文案生成", "调用失败", error),
        )
    generated_payload = llm_transport.extract_json_payload(generated_text)
    if generated_payload is None:
        return CopyRecommendationResult(
            candidates=fallback,
            selected_index=0,
            source="fallback",
            warning=_safe_warning("Luna 文案生成", "输出 JSON 不合法"),
        )
    try:
        candidates = validate_candidate_payload(generated_payload, source_text=srt_content)
        evidence_quotes = tuple(
            tuple(item["evidence_quotes"])
            for item in generated_payload["candidates"]
        )
    except _CandidateContentValidationError as error:
        return CopyRecommendationResult(
            candidates=fallback,
            selected_index=0,
            source="fallback",
            warning=_safe_warning("Luna 文案生成", error.category),
        )
    except (TypeError, ValueError):
        return CopyRecommendationResult(
            candidates=fallback,
            selected_index=0,
            source="fallback",
            warning=_safe_warning("Luna 文案生成", "输出字段不合法"),
        )

    def run_review(prompt: str) -> str:
        return active_runner(
            prompt,
            compact_prompt=None,
            max_tokens=2200,
            compact_max_tokens=1800,
            require_json=True,
            model_override=_review_model(active_environ),
            reasoning_stage="review",
        )

    def fallback_with_warning(
        category: str, error: BaseException | None = None
    ) -> CopyRecommendationResult:
        return CopyRecommendationResult(
            candidates=fallback,
            selected_index=0,
            source="fallback",
            warning=_safe_warning("Terra 独立复核", category, error),
        )

    try:
        review_text = run_review(_review_prompt(context, candidates, evidence_quotes))
    except Exception as error:
        return fallback_with_warning("调用失败", error)

    review_payload = llm_transport.extract_json_payload(review_text)
    correction_category = "输出 JSON 不合法"
    if review_payload is not None:
        try:
            selected_index = _validate_review_payload(review_payload)
        except _NoPassingReviewError:
            return fallback_with_warning("没有通过项")
        except _ReviewValidationError as error:
            correction_category = error.category
        except (TypeError, ValueError):
            correction_category = "reviews/索引不合法"
        else:
            return CopyRecommendationResult(
                candidates=candidates,
                selected_index=selected_index,
                source="ai",
            )

    try:
        corrected_review_text = run_review(
            _review_correction_prompt(context, candidates, evidence_quotes)
        )
    except Exception as error:
        return fallback_with_warning("调用失败", error)

    corrected_payload = llm_transport.extract_json_payload(corrected_review_text)
    if corrected_payload is None:
        return fallback_with_warning("输出 JSON 不合法")
    try:
        selected_index = _validate_review_payload(corrected_payload)
    except _NoPassingReviewError:
        return fallback_with_warning("没有通过项")
    except _ReviewValidationError as error:
        return fallback_with_warning(error.category)
    except (TypeError, ValueError):
        return fallback_with_warning(correction_category)

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
