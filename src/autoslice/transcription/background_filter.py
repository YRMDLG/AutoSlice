"""背景音过滤模式的唯一契约与策略 owner。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

BACKGROUND_FILTER_OFF = "off"
BACKGROUND_FILTER_SOFT = "soft"
BACKGROUND_FILTER_STRICT = "strict"
BACKGROUND_FILTER_MODES = (
    BACKGROUND_FILTER_OFF,
    BACKGROUND_FILTER_SOFT,
    BACKGROUND_FILTER_STRICT,
)
TECHNICAL_FILTER_OFF = "off"
TECHNICAL_FILTER_GATE = "adaptive_gate"
TECHNICAL_FILTER_SPEAKER = "speaker_diarization"


@dataclass(frozen=True)
class BackgroundFilterPolicy:
    """描述一个模式允许启用的处理能力。"""

    mode: str
    label: str
    description: str
    enabled: bool
    apply_audio_gate: bool
    request_speaker_model: bool
    discard_non_primary_speakers: bool
    risk_hint: str = ""


_POLICIES = {
    BACKGROUND_FILTER_OFF: BackgroundFilterPolicy(
        mode=BACKGROUND_FILTER_OFF,
        label="关闭",
        description="完整音轨；不加临时音频门限，不请求说话人模型。",
        enabled=False,
        apply_audio_gate=False,
        request_speaker_model=False,
        discard_non_primary_speakers=False,
    ),
    BACKGROUND_FILTER_SOFT: BackgroundFilterPolicy(
        mode=BACKGROUND_FILTER_SOFT,
        label="软过滤",
        description="保守降低背景音误识别；检测到的非主要说话人仍保留在字幕中。",
        enabled=True,
        apply_audio_gate=True,
        request_speaker_model=True,
        discard_non_primary_speakers=False,
    ),
    BACKGROUND_FILTER_STRICT: BackgroundFilterPolicy(
        mode=BACKGROUND_FILTER_STRICT,
        label="严格过滤",
        description="仅适合确认是单人直播；CAM++ 可用时删除非主要说话人。",
        enabled=True,
        apply_audio_gate=True,
        request_speaker_model=True,
        discard_non_primary_speakers=True,
        risk_hint="可能吞掉有效人声，仅单人直播使用。",
    ),
}


def normalise_background_filter_mode(
    background_filter_mode: Any = None,
    *,
    foreground_only: Any = None,
    default: str = BACKGROUND_FILTER_OFF,
) -> str:
    """规范化新模式和旧布尔参数，并拒绝含义冲突的双字段调用。"""

    if default not in _POLICIES:
        raise ValueError(f"无效的背景音默认模式：{default}")
    legacy_mode = None
    if foreground_only is not None:
        if not isinstance(foreground_only, bool):
            raise ValueError("foreground_only 必须是布尔值")
        legacy_mode = (
            BACKGROUND_FILTER_SOFT
            if foreground_only
            else BACKGROUND_FILTER_OFF
        )

    if background_filter_mode is None:
        return legacy_mode or default
    if not isinstance(background_filter_mode, str):
        raise ValueError("background_filter_mode 必须是字符串")
    mode = background_filter_mode.strip().casefold()
    if mode not in _POLICIES:
        allowed = "、".join(BACKGROUND_FILTER_MODES)
        raise ValueError(
            f"background_filter_mode 必须是以下值之一：{allowed}"
        )
    if legacy_mode is not None and legacy_mode != mode:
        raise ValueError(
            "background_filter_mode 与 foreground_only 含义不一致"
        )
    return mode


def background_filter_policy(
    background_filter_mode: Any = None,
    *,
    foreground_only: Any = None,
    default: str = BACKGROUND_FILTER_OFF,
) -> BackgroundFilterPolicy:
    """返回规范化后的不可变模式策略。"""

    return _POLICIES[
        normalise_background_filter_mode(
            background_filter_mode,
            foreground_only=foreground_only,
            default=default,
        )
    ]


def legacy_foreground_only(mode: str) -> bool:
    """把三模式投影到旧布尔元数据；反向读取 True 仍只映射 soft。"""

    return background_filter_policy(mode).enabled


def resolve_actual_mode(mode: str, *, speaker_model_used: bool) -> str:
    """严格模式缺少 CAM++ 时实际只能执行软过滤。"""

    policy = background_filter_policy(mode)
    if policy.discard_non_primary_speakers and not speaker_model_used:
        return BACKGROUND_FILTER_SOFT
    return policy.mode


def technical_filter_mode(mode: str, *, speaker_model_used: bool) -> str:
    """返回旧结果字段使用的技术模式名称。"""

    policy = background_filter_policy(mode)
    if not policy.enabled:
        return TECHNICAL_FILTER_OFF
    return (
        TECHNICAL_FILTER_SPEAKER
        if speaker_model_used
        else TECHNICAL_FILTER_GATE
    )


def technical_mode_uses_speaker_model(mode: Any) -> bool:
    """兼容读取旧 ``foreground_filter_mode`` 字段。"""

    return str(mode or "").strip() == TECHNICAL_FILTER_SPEAKER


def filter_model_label(mode: str, *, speaker_model_used: bool) -> str:
    """返回不含本机路径的公开过滤实现名称。"""

    policy = background_filter_policy(mode)
    if speaker_model_used:
        return "CAM++"
    if policy.apply_audio_gate:
        return "基础降噪/门限"
    return "未使用"


def filter_fallback_reason(
    mode: str,
    *,
    speaker_model_ready: bool,
    speaker_model_used: bool,
    speaker_model_load_failed: bool = False,
) -> str:
    """说明请求能力与实际能力之间的降级，不伪称完成说话人分离。"""

    policy = background_filter_policy(mode)
    if not policy.request_speaker_model or speaker_model_used:
        return ""
    if speaker_model_load_failed:
        if policy.discard_non_primary_speakers:
            return (
                "CAM++ 文件已检测到但加载失败，严格过滤已回退为软过滤："
                "仅应用基础降噪/门限，未区分或删除非主要说话人。"
            )
        return (
            "CAM++ 文件已检测到但加载失败，已仅应用基础降噪/门限；"
            "未区分说话人。"
        )
    if not speaker_model_ready:
        if policy.discard_non_primary_speakers:
            return (
                "CAM++ 所需本地文件缺失或不完整，严格过滤已回退为软过滤："
                "仅应用基础降噪/门限，未区分或删除非主要说话人。"
            )
        return (
            "CAM++ 所需本地文件缺失或不完整，已仅应用基础降噪/门限；"
            "未区分说话人。"
        )
    if policy.discard_non_primary_speakers:
        return (
            "CAM++ 未实际启用，严格过滤已回退为软过滤："
            "仅应用基础降噪/门限，未区分或删除非主要说话人。"
        )
    return "CAM++ 未实际启用，已仅应用基础降噪/门限；未区分说话人。"


def public_background_filter_modes() -> list[dict[str, Any]]:
    """返回供 Web 状态接口展示的稳定三模式能力。"""

    return [
        {
            "mode": policy.mode,
            "label": policy.label,
            "description": policy.description,
            "enabled": policy.enabled,
            "uses_audio_gate": policy.apply_audio_gate,
            "requests_speaker_model": policy.request_speaker_model,
            "removes_non_primary_speakers": (
                policy.discard_non_primary_speakers
            ),
            "risk_hint": policy.risk_hint,
        }
        for policy in _POLICIES.values()
    ]


def build_background_filter_result(
    mode: str,
    *,
    speaker_model_ready: bool,
    speaker_model_used: bool,
    speaker_model_load_failed: bool = False,
    detected_speaker_count: int = 0,
    removed_segment_count: int = 0,
    removed_seconds: float = 0.0,
    candidate_segment_count: int = 0,
    candidate_seconds: float = 0.0,
    speaker_filtered_chunk_count: int = 0,
    device: str = "",
) -> dict[str, Any]:
    """构造检查点、任务结果和页面共同使用的公开统计契约。"""

    policy = background_filter_policy(mode)
    actual_mode = resolve_actual_mode(
        policy.mode,
        speaker_model_used=speaker_model_used,
    )
    return {
        "requested_mode": policy.mode,
        "actual_mode": actual_mode,
        "enabled": policy.enabled,
        "speaker_model_ready": bool(speaker_model_ready),
        "speaker_model_used": bool(speaker_model_used),
        "speaker_model_load_failed": bool(speaker_model_load_failed),
        "used": bool(speaker_model_used),
        "detected_speaker_count": max(0, int(detected_speaker_count)),
        "detected_speaker_count_scope": "max_per_chunk",
        "removed_segment_count": max(0, int(removed_segment_count)),
        "removed_seconds": round(max(0.0, float(removed_seconds)), 3),
        "candidate_segment_count": max(0, int(candidate_segment_count)),
        "candidate_seconds": round(max(0.0, float(candidate_seconds)), 3),
        "model": filter_model_label(
            policy.mode,
            speaker_model_used=speaker_model_used,
        ),
        "device": str(device or ""),
        "fallback_reason": filter_fallback_reason(
            policy.mode,
            speaker_model_ready=speaker_model_ready,
            speaker_model_used=speaker_model_used,
            speaker_model_load_failed=speaker_model_load_failed,
        ),
        # 旧字段继续保留：mode 是技术实现，不是用户请求模式。
        "mode": technical_filter_mode(
            policy.mode,
            speaker_model_used=speaker_model_used,
        ),
        "speaker_filtered_segment_count": max(
            0,
            int(removed_segment_count),
        ),
        "speaker_filtered_chunk_count": max(
            0,
            int(speaker_filtered_chunk_count),
        ),
    }


__all__ = [
    "BACKGROUND_FILTER_MODES",
    "BACKGROUND_FILTER_OFF",
    "BACKGROUND_FILTER_SOFT",
    "BACKGROUND_FILTER_STRICT",
    "TECHNICAL_FILTER_GATE",
    "TECHNICAL_FILTER_OFF",
    "TECHNICAL_FILTER_SPEAKER",
    "BackgroundFilterPolicy",
    "background_filter_policy",
    "build_background_filter_result",
    "filter_fallback_reason",
    "filter_model_label",
    "legacy_foreground_only",
    "normalise_background_filter_mode",
    "public_background_filter_modes",
    "resolve_actual_mode",
    "technical_filter_mode",
    "technical_mode_uses_speaker_model",
]
