"""字幕压制样式：内置“默认”（用户原来的压制配置）+ 用户自建的几套，可切换（D-048）。

存在程序数据目录的 subtitle_styles.json，所有项目共用；当前用哪套也记在里面。
默认这套不能改——要调就先“新建”复制一份，原配置永远在。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path

from autoslice.desktop.foundation import DesktopStorage
from autoslice.subtitle_workflow import DEFAULT_SUBTITLE_STYLE, normalise_subtitle_style

DEFAULT_ID = "default"
DEFAULT_NAME = "默认"
SCHEMA_VERSION = 1
NAME_LIMIT = 20


@dataclass(frozen=True)
class StylePreset:
    id: str
    name: str
    style: dict

    @property
    def builtin(self) -> bool:
        return self.id == DEFAULT_ID


class SubtitleStyleStore:
    def __init__(self, storage: DesktopStorage) -> None:
        self.path: Path = storage.root / "subtitle_styles.json"
        self._custom: list[StylePreset] = []
        self._active = DEFAULT_ID
        self._load()

    # ── 读写 ──
    def _load(self) -> None:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            return
        if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
            return
        seen = {DEFAULT_ID}
        for item in value.get("presets") or ():
            if not isinstance(item, dict):
                continue
            preset_id, name = item.get("id"), item.get("name")
            if not isinstance(preset_id, str) or preset_id in seen or not isinstance(name, str) or not name.strip():
                continue
            try:
                style = normalise_subtitle_style(item.get("style"))
            except ValueError:
                # 坏掉的一套跳过，别让整个文件读不出来
                continue
            seen.add(preset_id)
            self._custom.append(StylePreset(preset_id, name.strip()[:NAME_LIMIT], style))
        active = value.get("active")
        self._active = active if active in seen else DEFAULT_ID

    def _save(self) -> None:
        DesktopStorage._write_json(self.path, {
            "schema_version": SCHEMA_VERSION,
            "active": self._active,
            "presets": [{"id": item.id, "name": item.name, "style": item.style} for item in self._custom],
        })

    # ── 查询 ──
    def presets(self) -> list[StylePreset]:
        return [self.default(), *self._custom]

    @staticmethod
    def default() -> StylePreset:
        return StylePreset(DEFAULT_ID, DEFAULT_NAME, normalise_subtitle_style(DEFAULT_SUBTITLE_STYLE))

    def get(self, preset_id: str) -> StylePreset:
        found = next((item for item in self.presets() if item.id == preset_id), None)
        if found is None:
            raise KeyError("这套样式已经不在了")
        return found

    def active(self) -> StylePreset:
        return self.get(self._active)

    # ── 修改 ──
    def set_active(self, preset_id: str) -> StylePreset:
        preset = self.get(preset_id)
        if preset.id != self._active:
            self._active = preset.id
            self._save()
        return preset

    def _unique_name(self, name: str, *, ignore: str | None = None) -> str:
        base = (name or "").strip()[:NAME_LIMIT] or "新样式"
        taken = {item.name for item in self.presets() if item.id != ignore}
        if base not in taken:
            return base
        number = 2
        while f"{base} {number}" in taken:
            number += 1
        return f"{base} {number}"

    def suggest_name(self) -> str:
        return self._unique_name("我的样式")

    def create(self, name: str, style: dict) -> StylePreset:
        """新建一套并切过去（通常复制当前这套再改）。"""

        preset = StylePreset(uuid.uuid4().hex[:10], self._unique_name(name), normalise_subtitle_style(style))
        self._custom.append(preset)
        self._active = preset.id
        self._save()
        return preset

    def _replace(self, preset_id: str, **changes) -> StylePreset:
        if preset_id == DEFAULT_ID:
            raise ValueError("默认样式不能改，先新建一套再调")
        for position, item in enumerate(self._custom):
            if item.id == preset_id:
                updated = StylePreset(item.id, changes.get("name", item.name), changes.get("style", item.style))
                if updated != item:
                    self._custom[position] = updated
                    self._save()
                return updated
        raise KeyError("这套样式已经不在了")

    def update(self, preset_id: str, style: dict) -> StylePreset:
        return self._replace(preset_id, style=normalise_subtitle_style(style))

    def rename(self, preset_id: str, name: str) -> StylePreset:
        if not (name or "").strip():
            raise ValueError("名字不能为空")
        return self._replace(preset_id, name=self._unique_name(name, ignore=preset_id))

    def delete(self, preset_id: str) -> StylePreset:
        """删掉一套；删的是正在用的就回到默认。返回删完后正在用的那套。"""

        if preset_id == DEFAULT_ID:
            raise ValueError("默认样式不能删")
        remaining = [item for item in self._custom if item.id != preset_id]
        if len(remaining) == len(self._custom):
            raise KeyError("这套样式已经不在了")
        self._custom = remaining
        if self._active == preset_id:
            self._active = DEFAULT_ID
        self._save()
        return self.active()
