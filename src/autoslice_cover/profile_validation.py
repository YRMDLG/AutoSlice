"""AutoCover 专用的主播封面规则引用校验。"""

from __future__ import annotations

from pathlib import Path

from autoslice.streamer_profiles import (
    AUTO_PROFILE_ID,
    StreamerProfile,
    active_streamer_profile,
    resolve_streamer_profile,
)

from .style import PALETTES, TEMPLATES


def resolve_cover_profile(
    title: str,
    *,
    video_path: str | Path | None = None,
    profile: StreamerProfile | str | None = None,
) -> StreamerProfile:
    """按任务路径优先解析主播，并在 AutoCover 边界校验封面规则引用。"""

    selected_profile = profile or active_streamer_profile() or AUTO_PROFILE_ID
    resolved_profile = (
        selected_profile
        if isinstance(selected_profile, StreamerProfile)
        else resolve_streamer_profile(
            selected_profile,
            video_path,
            context_hint=title,
        )
    )
    for index, rule in enumerate(resolved_profile.cover_rules.series_rules):
        if rule.template_key not in TEMPLATES:
            raise ValueError(
                f"主播配置 {resolved_profile.id} 的 "
                f"cover_rules.series_rules[{index}].template_key "
                f"不受 AutoCover 支持: {rule.template_key}"
            )
        if rule.palette_key not in PALETTES:
            raise ValueError(
                f"主播配置 {resolved_profile.id} 的 "
                f"cover_rules.series_rules[{index}].palette_key "
                f"不受 AutoCover 支持: {rule.palette_key}"
            )
    return resolved_profile
