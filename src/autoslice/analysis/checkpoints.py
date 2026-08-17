"""话题分析与候选复核检查点的唯一持久化实现。"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime

from autoslice.streamer_profiles import current_streamer_profile

TOPIC_ANALYSIS_CHECKPOINT_VERSION = 1
CLIP_REVIEW_POLICY_VERSION = 6

TOPIC_REVIEW_TRANSIENT_KEYS = {
    "can_slice",
    "slice_start",
    "slice_end",
    "slice_anchor",
    "slice_anchor_source",
    "slice_peak_density",
    "peak_density",
    "density_ratio",
    "clip_review_validated",
    "clip_review_rejection",
    "clip_review_attempts",
    "clip_interest_base_score",
    "clip_timeline_star_bonus",
    "clip_interest_score",
    "clip_interest_reason",
    "title_review_validated",
    "title_review_candidates",
    "title_review_reason",
    "title_review_attempts",
}

FACADE_EXPORTS = {
    "CLIP_REVIEW_POLICY_VERSION": "CLIP_REVIEW_POLICY_VERSION",
    "TOPIC_ANALYSIS_CHECKPOINT_VERSION": "TOPIC_ANALYSIS_CHECKPOINT_VERSION",
    "_TOPIC_REVIEW_TRANSIENT_KEYS": "TOPIC_REVIEW_TRANSIENT_KEYS",
    "_analysis_topics_snapshot": "analysis_topics_snapshot",
    "_clip_review_checkpoint_is_complete": "clip_review_checkpoint_is_complete",
    "_clip_review_checkpoint_matches_policy": "clip_review_checkpoint_matches_policy",
    "_write_clip_review_checkpoint": "write_clip_review_checkpoint",
    "_write_completed_clip_review_checkpoint": "write_completed_clip_review_checkpoint",
}


def topic_analysis_prompt_fingerprint(
    prompt: str,
    compact_prompt: str,
    *,
    schema_version: int,
    model: str,
    max_tokens: int,
    compact_max_tokens: int,
) -> str:
    """返回决定首轮响应缓存是否仍可复用的稳定指纹。"""

    payload = "\n".join(
        (
            str(schema_version),
            model,
            str(max_tokens),
            str(compact_max_tokens),
            prompt,
            compact_prompt,
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_topic_analysis_checkpoint(
    path: str | None,
    *,
    schema_version: int,
) -> dict[str, object]:
    """容错读取首轮原始响应检查点；损坏或过期文件按空缓存处理。"""

    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError, TypeError):
        return {}
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != schema_version
        or not isinstance(payload.get("responses"), dict)
    ):
        return {}
    return payload["responses"]


def write_topic_analysis_checkpoint(
    path: str | None,
    responses: dict[str, object],
    chunk_count: int,
    *,
    schema_version: int,
    model: str,
) -> bool:
    """原子保存原始模型响应；写入失败时保留上一个完整检查点。"""

    if not path:
        return True
    payload = {
        "schema_version": schema_version,
        "model": model,
        "chunk_count": int(chunk_count),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "responses": responses,
    }
    temp_path = path + ".tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(temp_path, path)
        return True
    except OSError:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass
        return False


def analysis_topics_snapshot(topics: list[dict] | None) -> list[dict]:
    """复制首轮话题并移除上一次候选和标题复核的瞬态状态。"""

    snapshot = json.loads(json.dumps(topics or [], ensure_ascii=False))
    for topic in snapshot:
        for key in TOPIC_REVIEW_TRANSIENT_KEYS:
            topic.pop(key, None)
    return snapshot


def write_clip_review_checkpoint(
    path: str | None,
    topics: list[dict],
    **status: object,
) -> str | None:
    """原子写入候选复核检查点，API 中断后无需重跑首轮分析。"""

    if not path:
        return None
    payload = {
        "schema_version": 1,
        "review_policy_version": CLIP_REVIEW_POLICY_VERSION,
        "streamer_profile_id": current_streamer_profile().id,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "topics": topics,
    }
    payload.update(status)
    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(temp_path, path)
    return path


def clip_review_checkpoint_matches_policy(checkpoint: object) -> bool:
    """只有当前主播和复核策略生成的检查点才能续跑。"""

    if not isinstance(checkpoint, dict):
        return False
    try:
        version = int(checkpoint.get("review_policy_version"))
    except (TypeError, ValueError):
        return False
    if version != CLIP_REVIEW_POLICY_VERSION:
        return False
    profile_id = checkpoint.get("streamer_profile_id")
    current_profile_id = current_streamer_profile().id
    return profile_id == current_profile_id or (
        not profile_id and current_profile_id == "zeyin"
    )


def clip_review_checkpoint_is_complete(
    checkpoint: object,
    topics: object,
) -> bool:
    """识别当前完成状态及旧版最后一批已完成的兼容状态。"""

    if not isinstance(checkpoint, dict) or not isinstance(topics, list):
        return False
    stage = checkpoint.get("stage")
    legacy_final_batch = (
        stage == "reviewing"
        and int(checkpoint.get("pending_count", -1) or 0) == 0
        and int(checkpoint.get("total_batches", 0) or 0) > 0
        and int(checkpoint.get("batch_index", 0) or 0)
        >= int(checkpoint.get("total_batches", 0) or 0)
    )
    if stage not in {"completed", "title_reviewing"} and not legacy_final_batch:
        return False
    reviewed_topics = [
        topic
        for topic in topics
        if topic.get("clip_review_attempts") is not None
    ]
    return bool(reviewed_topics) and all(
        topic.get("clip_review_validated") is not None for topic in reviewed_topics
    )


def write_completed_clip_review_checkpoint(
    path: str | None,
    topics: list[dict],
    warning: str | None = None,
    source: str = "pipeline",
    completed_at: str | None = None,
) -> str | None:
    """统一写入完整流水线和产物重建的最终复核状态。"""

    return write_clip_review_checkpoint(
        path,
        topics,
        stage="completed" if not warning else "completed_with_warning",
        source=source,
        pending_count=sum(
            1
            for topic in topics or []
            if topic.get("can_slice")
            and topic.get("clip_review_rejection") == "等待独立字幕复核"
        ),
        completed_at=completed_at or datetime.now().isoformat(timespec="seconds"),
    )


__all__ = [
    "CLIP_REVIEW_POLICY_VERSION",
    "FACADE_EXPORTS",
    "TOPIC_ANALYSIS_CHECKPOINT_VERSION",
    "TOPIC_REVIEW_TRANSIENT_KEYS",
    "analysis_topics_snapshot",
    "clip_review_checkpoint_is_complete",
    "clip_review_checkpoint_matches_policy",
    "load_topic_analysis_checkpoint",
    "topic_analysis_prompt_fingerprint",
    "write_clip_review_checkpoint",
    "write_completed_clip_review_checkpoint",
    "write_topic_analysis_checkpoint",
]
