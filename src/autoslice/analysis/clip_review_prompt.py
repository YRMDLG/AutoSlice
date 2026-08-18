"""独立候选复核的证据载荷与提示词渲染。"""

import json

from autoslice import timecode
from autoslice.analysis import clip_policy
from autoslice.analysis.topic import titles as title_analysis
from autoslice.llm.prompts import (
    ClipCandidatePromptEvidence,
)
from autoslice.llm.prompts import (
    build_clip_candidate_review_prompt as render_clip_candidate_review_prompt,
)

FACADE_EXPORTS = {
    "_build_clip_candidate_review_prompt": "build_clip_candidate_review_prompt",
}


def build_clip_candidate_review_prompt(
    candidates,
    streamer_name=None,
    compact=False,
):
    """构造独立复核提示；只把原字幕、峰值和原始人工记录作为证据。"""
    payload = []
    for index, candidate in enumerate(candidates, 1):
        evidence_limit = 10 if compact else 24
        evidence = [
            title_analysis._strip_body_prefix(line)
            for line in (candidate.get("body") or [])[:evidence_limit]
            if title_analysis._strip_body_prefix(line)
        ]
        subtitle_evidence = [
            line for line in evidence if line.startswith("字幕核查：")
        ]
        manual_evidence = [
            line
            for line in evidence
            if line.startswith(("人工时间轴", "时间轴："))
        ]
        density_evidence = [
            line for line in evidence if line.startswith("弹幕依据：")
        ]
        payload.append(
            {
                "id": index,
                "reference_start": timecode.format_elapsed(candidate["start"]),
                "reference_end": timecode.format_elapsed(candidate["end"]),
                "candidate_anchor": timecode.format_elapsed(
                    candidate.get("slice_anchor", candidate["start"])
                ),
                "candidate_sources": list(
                    candidate.get("clip_candidate_sources") or []
                ),
                "provisional_title": candidate.get(
                    "title",
                    "待核查高能片段",
                ),
                "reference_publish_titles": (
                    title_analysis._clip_candidate_reference_publish_titles(
                        candidate
                    )
                ),
                "publish_title_locked": bool(
                    candidate.get("publish_title_locked")
                ),
                "manual_star_count": max(
                    0,
                    int(candidate.get("manual_stars", 0) or 0),
                ),
                "danmaku_evidence": (
                    title_analysis._clip_candidate_danmaku_prompt_evidence(
                        candidate
                    )
                ),
                "evidence": evidence,
                "subtitle_evidence": subtitle_evidence,
                "core_subtitle_evidence": (
                    candidate.get("core_subtitle_evidence") or []
                ),
                "manual_evidence": manual_evidence,
                "density_evidence": density_evidence,
            }
        )
    context = title_analysis._prompt_context(
        streamer_name,
        context_text=json.dumps(payload, ensure_ascii=False),
        compact=compact,
        publish_title_example_text="具体事件与原话",
    )
    return render_clip_candidate_review_prompt(
        ClipCandidatePromptEvidence(
            context=context,
            candidates=tuple(payload),
            focus_max_seconds=clip_policy.TOPIC_REVIEW_FOCUS_MAX_SEC,
            minimum_interest_score=clip_policy.CLIP_MIN_INTEREST_SCORE,
        )
    )
