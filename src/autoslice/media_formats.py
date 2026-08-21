"""AutoSlice 视频格式能力契约。

所有扫描、分析、切片与输出容器判断都应从本模块的 ``MEDIA_FORMATS``
能力表派生，避免各层维护彼此漂移的扩展名集合。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class MediaFormatCapability:
    """单个视频容器在 AutoSlice 中具备的能力。"""

    extension: str
    can_scan: bool
    can_analyze: bool
    can_slice: bool
    copy_output_extension: str
    legacy_output_extensions: tuple[str, ...] = ()


_MEDIA_FORMAT_ROWS = (
    MediaFormatCapability(".flv", True, True, True, ".flv"),
    MediaFormatCapability(".mp4", True, True, True, ".mp4", (".flv",)),
    MediaFormatCapability(".mkv", True, True, True, ".mkv", (".flv",)),
    MediaFormatCapability(".mov", True, True, True, ".mov", (".flv",)),
    MediaFormatCapability(".avi", True, True, True, ".avi", (".flv",)),
)

# MappingProxyType 防止调用方在运行时改写全局格式契约。
MEDIA_FORMATS: Mapping[str, MediaFormatCapability] = MappingProxyType({
    capability.extension: capability
    for capability in _MEDIA_FORMAT_ROWS
})
SUPPORTED_VIDEO_EXTENSIONS = tuple(MEDIA_FORMATS)


def normalise_video_extension(path_or_extension: str | os.PathLike[str]) -> str:
    """返回小写扩展名，同时兼容 Windows/Posix 路径和原始扩展名。"""

    value = os.fspath(path_or_extension)
    text = os.fsdecode(value).strip()
    if not text:
        return ""
    basename = text.replace("\\", "/").rsplit("/", 1)[-1]
    if not basename or basename.endswith("."):
        return ""
    if basename.startswith(".") and basename.count(".") == 1:
        return basename.casefold()
    dot_index = basename.rfind(".")
    if dot_index <= 0:
        return ""
    return basename[dot_index:].casefold()


def media_format_for(
        path_or_extension: str | os.PathLike[str]) -> MediaFormatCapability | None:
    """查询路径或扩展名对应的格式能力，不支持时返回 ``None``。"""

    return MEDIA_FORMATS.get(normalise_video_extension(path_or_extension))


def is_scannable_video(path: str | os.PathLike[str]) -> bool:
    """判断文件是否可由录播扫描入口发现。"""

    capability = media_format_for(path)
    return bool(capability and capability.can_scan)


def is_analyzable_video(path: str | os.PathLike[str]) -> bool:
    """判断文件是否可作为话题分析输入。"""

    capability = media_format_for(path)
    return bool(capability and capability.can_analyze)


def is_sliceable_video(path: str | os.PathLike[str]) -> bool:
    """判断文件是否可使用当前精确切片流程。"""

    capability = media_format_for(path)
    return bool(capability and capability.can_slice)


def video_filename_stem(path: str | os.PathLike[str]) -> str:
    """仅移除声明为受支持视频格式的后缀，并返回文件名部分。"""

    value = os.fspath(path)
    filename = os.fsdecode(value).replace("\\", "/").rsplit("/", 1)[-1]
    extension = normalise_video_extension(filename)
    if extension not in MEDIA_FORMATS:
        return filename
    return filename[:-len(extension)]


def _slice_capability(
        source_path: str | os.PathLike[str]) -> MediaFormatCapability:
    capability = media_format_for(source_path)
    if capability is None or not capability.can_slice:
        extension = normalise_video_extension(source_path) or "（无后缀）"
        raise ValueError(f"不支持的视频格式：{extension}")
    return capability


def preferred_output_extension(source_path: str | os.PathLike[str]) -> str:
    """返回精确切片首选容器；默认保留受支持的源容器后缀。"""

    capability = _slice_capability(source_path)
    return capability.copy_output_extension


def compatible_output_extensions(
        source_path: str | os.PathLike[str]) -> tuple[str, ...]:
    """返回首选输出后缀及可继续读取的历史输出后缀。"""

    capability = _slice_capability(source_path)
    return tuple(dict.fromkeys((
        capability.copy_output_extension,
        *capability.legacy_output_extensions,
    )))
