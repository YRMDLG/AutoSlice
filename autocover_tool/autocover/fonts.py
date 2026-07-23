"""默认封面字体选择与本机字体状态。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from PIL import ImageFont


DEFAULT_FONT_LABEL = "濑户体"
FONT_PATH_ENV = "AUTOCOVER_FONT_PATH"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCAL_FONT_PATH = PROJECT_ROOT / "local" / "fonts" / "seto-bilibili.ttf"


@dataclass(frozen=True, slots=True)
class FontStatus:
    """默认字体及其实际回退状态。"""

    label: str
    available: bool
    family: str
    source: str
    font_path: Path | None
    fallback_path: Path | None = None
    warning: str | None = None

    @property
    def render_path(self) -> Path | None:
        """返回 Pillow 实际应使用的字体路径。"""

        return self.font_path or self.fallback_path

    def to_public_dict(self) -> dict[str, str | bool | None]:
        """生成不暴露本机绝对路径的 API 数据。"""

        return {
            "label": self.label,
            "available": self.available,
            "family": self.family,
            "source": self.source,
            "warning": self.warning,
        }


def _expanded_path(value: str | Path) -> Path:
    expanded = os.path.expandvars(str(value))
    return Path(expanded).expanduser().resolve()


def _font_family(path: Path) -> str:
    font = ImageFont.truetype(str(path), size=24)
    family = font.getname()[0]
    if isinstance(family, bytes):
        return family.decode("utf-8", errors="replace")
    return str(family or path.stem)


def _system_font_candidates() -> tuple[Path, ...]:
    windows_dir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    return (
        windows_dir / "Fonts" / "NotoSansSC-VF.ttf",
        windows_dir / "Fonts" / "msyhbd.ttc",
        windows_dir / "Fonts" / "msyh.ttc",
        windows_dir / "Fonts" / "Dengb.ttf",
        windows_dir / "Fonts" / "simhei.ttf",
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
    )


def _first_valid_font(candidates: Iterable[Path]) -> tuple[Path | None, str | None]:
    for candidate in candidates:
        path = candidate.expanduser().resolve()
        if not path.is_file():
            continue
        try:
            return path, _font_family(path)
        except OSError:
            continue
    return None, None


def get_default_font_status() -> FontStatus:
    """检查濑户体是否可用，并给出实际渲染回退。"""

    warnings: list[str] = []
    configured = os.environ.get(FONT_PATH_ENV, "").strip()
    preferred_candidates: list[tuple[str, Path]] = []
    if configured:
        preferred_candidates.append(("environment", _expanded_path(configured)))
    preferred_candidates.append(("local", LOCAL_FONT_PATH.resolve()))

    for source, path in preferred_candidates:
        if not path.is_file():
            if source == "environment":
                warnings.append(f"环境变量 {FONT_PATH_ENV} 指向的字体不存在")
            continue
        try:
            family = _font_family(path)
        except OSError:
            warnings.append(f"字体文件无法加载：{path.name}")
            continue
        return FontStatus(
            label=DEFAULT_FONT_LABEL,
            available=True,
            family=family,
            source=source,
            font_path=path,
            warning="；".join(warnings) or None,
        )

    fallback_path, fallback_family = _first_valid_font(_system_font_candidates())
    warnings.append(
        f"未配置{DEFAULT_FONT_LABEL}，已回退到系统字体"
        if fallback_path
        else f"未配置{DEFAULT_FONT_LABEL}，系统中文字体也不可用"
    )
    return FontStatus(
        label=DEFAULT_FONT_LABEL,
        available=False,
        family=fallback_family or "Pillow 默认字体",
        source="system" if fallback_path else "pillow",
        font_path=None,
        fallback_path=fallback_path,
        warning="；".join(warnings),
    )


def resolve_font_path(custom_path: str | Path | None = None) -> str | None:
    """解析自定义字体或 AutoCover 默认字体，供 Pillow 渲染使用。"""

    if custom_path is not None:
        path = _expanded_path(custom_path)
        if not path.is_file():
            raise FileNotFoundError(f"字体文件不存在：{path}")
        try:
            _font_family(path)
        except OSError as exc:
            raise OSError(f"字体文件无法加载：{path}") from exc
        return str(path)

    selected = get_default_font_status().render_path
    return str(selected) if selected is not None else None


def resolve_font_stack(custom_path: str | Path | None = None) -> tuple[str | None, ...]:
    """返回主字体和系统中文回退字体，按实际渲染优先级排列。"""

    primary = resolve_font_path(custom_path)
    resolved: list[str | None] = []
    seen: set[str] = set()

    def append(path: str | Path | None) -> None:
        if path is None:
            if not resolved:
                resolved.append(None)
            return
        candidate = _expanded_path(path)
        key = str(candidate).casefold()
        if key in seen or not candidate.is_file():
            return
        try:
            _font_family(candidate)
        except OSError:
            return
        seen.add(key)
        resolved.append(str(candidate))

    append(primary)
    for candidate in _system_font_candidates():
        append(candidate)
    if not resolved:
        resolved.append(None)
    return tuple(resolved)
