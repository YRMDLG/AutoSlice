"""Desktop 字幕文档状态；文件格式与正式保存沿用旧字幕工作流。"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, replace
from pathlib import Path

from autoslice.desktop.projects import ProjectVideo
from autoslice.subtitle_workflow import (
    load_subtitle_edit_state,
    parse_srt_document,
    save_corrected_srt,
)
from autoslice.transcription.contracts import SubtitleCue, srt_timestamp_seconds


def _fingerprint(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _timestamp(seconds: float) -> str:
    milliseconds = int(round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole, fraction = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole:02d},{fraction:03d}"


@dataclass(frozen=True)
class SubtitleEntry:
    """一个可见字幕及其在源 SRT 中的成员序号。"""

    index: int
    start: str
    end: str
    text: str
    source_indices: tuple[int, ...]
    stable_id: str | None = None


class SubtitleDocument:
    """集中管理加载、内存编辑、dirty、撤销重做及显式保存。"""

    def __init__(
        self,
        video: ProjectVideo,
        source_cues: list[SubtitleCue],
        entries: list[SubtitleEntry],
        saved_state: dict,
    ) -> None:
        self.video = video
        self.source_path = Path(video.srt_path)
        self.corrected_path = Path(video.corrected_srt_path)
        self.source_cues = tuple(source_cues)
        self.entries = entries
        self.saved_state = saved_state
        self.source_fingerprint = _fingerprint(self.source_path)
        self.corrected_fingerprint = _fingerprint(self.corrected_path)
        self._saved_entries = tuple(entries)
        self._undo: list[tuple[SubtitleEntry, ...]] = []
        self._redo: list[tuple[SubtitleEntry, ...]] = []

    @classmethod
    def load(cls, video: ProjectVideo) -> SubtitleDocument:
        if not video.has_source_srt:
            raise ValueError("该视频缺少 SRT 字幕")
        video_directory = Path(video.path).parent.resolve()
        if Path(video.srt_path).resolve().parent != video_directory:
            raise ValueError("字幕路径不在当前视频目录内")
        if Path(video.corrected_srt_path).resolve().parent != video_directory:
            raise ValueError("校对字幕路径不在当前视频目录内")
        source_cues = parse_srt_document(video.srt_path)
        state = {
            "corrections": [],
            "deleted_indices": [],
            "merge_pairs": [],
            "merge_overrides": {},
            "time_overrides": {},
            "split_groups": [],
        }
        corrected_path = Path(video.corrected_srt_path)
        if corrected_path.is_file():
            saved = load_subtitle_edit_state(video.srt_path)
            corrected_cues = parse_srt_document(corrected_path)
            if saved is None:
                state = cls._restore_simple_state(source_cues, corrected_cues)
            else:
                if Path(saved["corrected_srt_path"]).resolve() != corrected_path.resolve():
                    raise ValueError("校对状态指向其他文件，请检查校对字幕")
                state = saved
            entries = cls._entries_from_saved(source_cues, corrected_cues, state)
        else:
            entries = [
                SubtitleEntry(cue.index, cue.start, cue.end, cue.text, (cue.index,))
                for cue in source_cues
            ]
        return cls(video, source_cues, entries, state)

    @staticmethod
    def _restore_simple_state(source_cues: list[SubtitleCue], corrected_cues: list[SubtitleCue]) -> dict:
        """兼容旧页面的无状态校对文件，只接受可无歧义恢复的逐条编辑/删除。"""

        source_by_index = {cue.index: cue for cue in source_cues}
        corrected_indices = [cue.index for cue in corrected_cues]
        source_indices = [cue.index for cue in source_cues]
        if not corrected_indices or len(set(corrected_indices)) != len(corrected_indices):
            raise ValueError("已有校对字幕无法安全恢复，请检查校对文件")
        if corrected_indices != [index for index in source_indices if index in corrected_indices]:
            raise ValueError("已有校对字幕与源字幕序号不匹配，请检查校对文件")
        corrections = []
        for cue in corrected_cues:
            if cue.index not in source_by_index:
                raise ValueError("已有校对字幕与源字幕序号不匹配，请检查校对文件")
            source = source_by_index[cue.index]
            if (cue.start, cue.end, cue.settings) != (
                source.start, source.end, source.settings
            ):
                raise ValueError("已有校对字幕的时间信息无法安全恢复，请检查校对文件")
            if cue.text != source.text:
                corrections.append({
                    "index": cue.index,
                    "original": source.text,
                    "corrected": cue.text,
                })
        return {
            "corrections": corrections,
            "deleted_indices": [index for index in source_indices if index not in corrected_indices],
            "merge_pairs": [],
            "merge_overrides": {},
            "time_overrides": {},
        }

    @staticmethod
    def _entries_from_saved(
        source_cues: list[SubtitleCue], corrected_cues: list[SubtitleCue], state: dict
    ) -> list[SubtitleEntry]:
        split_by_source = {
            int(group["source"]): group.get("segments", [])
            for group in state.get("split_groups", [])
        }
        child_by_parent = {
            int(pair["first"]): int(pair["second"])
            for pair in state.get("merge_pairs", [])
        }
        children = set(child_by_parent.values())
        deleted = set(state.get("deleted_indices", []))
        groups = []
        for cue in source_cues:
            if cue.index in split_by_source:
                groups.append((cue.index,))
                continue
            if cue.index in deleted or cue.index in children:
                continue
            members = [cue.index]
            while members[-1] in child_by_parent:
                members.append(child_by_parent[members[-1]])
            groups.append(tuple(members))
        entries = []
        cursor = 0
        for members in groups:
            if members[0] in split_by_source:
                segments = split_by_source[members[0]]
                if cursor + len(segments) > len(corrected_cues):
                    raise ValueError("已有校对字幕与编辑状态不一致，请检查校对文件")
                for offset, segment in enumerate(segments):
                    cue = corrected_cues[cursor]
                    if offset == 0 and cue.index != members[0]:
                        raise ValueError("已有校对字幕与编辑状态序号不一致，请检查校对文件")
                    entries.append(SubtitleEntry(
                        cue.index, cue.start, cue.end, cue.text, members,
                        str(segment.get("id") or f"{members[0]}:{offset}"),
                    ))
                    cursor += 1
                continue
            if cursor >= len(corrected_cues):
                raise ValueError("已有校对字幕与编辑状态不一致，请检查校对文件")
            cue = corrected_cues[cursor]
            cursor += 1
            if cue.index != members[0]:
                raise ValueError("已有校对字幕与编辑状态序号不一致，请检查校对文件")
            entries.append(SubtitleEntry(cue.index, cue.start, cue.end, cue.text, members))
        if cursor != len(corrected_cues):
            raise ValueError("已有校对字幕与编辑状态不一致，请检查校对文件")
        return entries

    @property
    def dirty(self) -> bool:
        return tuple(self.entries) != self._saved_entries

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def _remember(self) -> None:
        self._undo.append(tuple(self.entries))
        self._redo.clear()

    def edit_text(self, index: int, text: str) -> None:
        for position, entry in enumerate(self.entries):
            if entry.index == index:
                if entry.text != text:
                    self._remember()
                    self.entries[position] = replace(entry, text=text)
                return
        raise ValueError("当前字幕已不存在")

    def change_time(self, index: int, start: float, end: float, *, remember: bool = True) -> None:
        """时间轴修改与文字共用一份文档；预览拖动可延后记录一次撤销。"""

        if not all(isinstance(value, (int, float)) and not isinstance(value, bool)
                   and math.isfinite(value) for value in (start, end)):
            raise ValueError("字幕时间必须是有限秒数")
        start = round(float(start), 3)
        end = round(float(end), 3)
        if start < 0 or end <= start:
            raise ValueError("字幕结束时间必须晚于开始时间")
        for position, entry in enumerate(self.entries):
            if entry.index != index:
                continue
            if position and start < srt_timestamp_seconds(self.entries[position - 1].start):
                raise ValueError("开始时间不能早于上一条字幕")
            if position + 1 < len(self.entries) and start > srt_timestamp_seconds(
                self.entries[position + 1].start
            ):
                raise ValueError("开始时间不能晚于下一条字幕")
            changed = replace(entry, start=_timestamp(start), end=_timestamp(end))
            if changed != entry:
                if remember:
                    self._remember()
                self.entries[position] = changed
            return
        raise ValueError("当前字幕已不存在")

    def split_at(self, index: int, position: float, *, left_text: str | None = None) -> int:
        """在定位线处分成两条内存字幕；序列化前由用户补全右侧正文。"""
        for offset, entry in enumerate(self.entries):
            if entry.index != index:
                continue
            start = srt_timestamp_seconds(entry.start)
            end = srt_timestamp_seconds(entry.end)
            if not start + .05 <= position <= end - .05:
                raise ValueError("定位线太靠近字幕边缘，无法安全拆分")
            self._remember()
            original = entry.text
            left = original if left_text is None else original[:max(0, min(len(original), len(left_text)))]
            right = original[len(left):] if left_text is not None else ""
            new_index = max((item.index for item in self.entries), default=0) + 1
            self.entries[offset:offset + 1] = [
                replace(entry, end=_timestamp(position), text=left,
                        stable_id=f"{entry.source_indices[0]}:0"),
                SubtitleEntry(new_index, _timestamp(position), entry.end, right,
                              entry.source_indices, f"{entry.source_indices[0]}:1"),
            ]
            return new_index
        raise ValueError("当前字幕已不存在")

    def merge_adjacent(self, indices: set[int]) -> int:
        """合并连续选中的字幕，并把整个操作作为一次撤销。"""
        if not indices or len(indices) < 2:
            raise ValueError("至少选择两条字幕")
        positions = [i for i, item in enumerate(self.entries) if item.index in indices]
        if len(positions) != len(indices) or positions != list(range(min(positions), max(positions) + 1)):
            raise ValueError("只能合并连续且相邻的字幕")
        first, last = min(positions), max(positions)
        group = self.entries[first:last + 1]
        self._remember()
        text = ""
        for item in group:
            left, right = text.rstrip(), item.text.lstrip()
            if left and right and left[-1:].isascii() and right[:1].isascii() and left[-1].isalnum() and right[0].isalnum():
                text = left + " " + right
            else:
                text = left + right
        merged = replace(
            group[0], end=group[-1].end, text=text,
            source_indices=tuple(dict.fromkeys(
                value for item in group for value in item.source_indices
            )),
            stable_id=None,
        )
        self.entries[first:last + 1] = [merged]
        return merged.index

    def commit_time_preview(self, before: tuple[SubtitleEntry, ...]) -> None:
        if tuple(self.entries) != before:
            self._undo.append(before)
            self._redo.clear()

    def delete(self, index: int) -> None:
        self.delete_many({index})

    def delete_many(self, indices: set[int]) -> None:
        """一次删除多条，只产生一条撤销记录。"""
        existing = {entry.index for entry in self.entries}
        if not indices or not indices <= existing:
            raise ValueError("所选字幕已不存在")
        if len(indices) >= len(self.entries):
            raise ValueError("至少保留一条字幕，不能删除全部字幕")
        selected_sources = [entry.source_indices for entry in self.entries
                            if entry.index in indices]
        for source_indices in selected_sources:
            group_ids = {entry.index for entry in self.entries
                         if entry.source_indices == source_indices}
            if len(group_ids) > 1 and not group_ids <= indices:
                raise ValueError("拆分字幕请先合并后再删除")
        self._remember()
        self.entries = [entry for entry in self.entries if entry.index not in indices]

    def undo(self) -> bool:
        if not self._undo:
            return False
        self._redo.append(tuple(self.entries))
        self.entries = list(self._undo.pop())
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        self._undo.append(tuple(self.entries))
        self.entries = list(self._redo.pop())
        return True

    def draft_payload(self, selected_index: int | None = None) -> dict:
        """保存完整编辑快照；正式字幕仍只由显式 save 写入。"""

        return {
            "schema_version": 1,
            "selected_index": selected_index,
            "entries": [
                {
                    "index": entry.index,
                    "start": entry.start,
                    "end": entry.end,
                    "text": entry.text,
                    "source_indices": list(entry.source_indices),
                    "stable_id": entry.stable_id,
                }
                for entry in self.entries
            ],
        }

    def restore_draft(self, payload: dict) -> int | None:
        """仅接受当前源字幕可解释的快照，拒绝损坏或跨文件数据。"""

        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise ValueError("字幕草稿版本不兼容")
        raw_entries = payload.get("entries")
        if not isinstance(raw_entries, list) or not raw_entries:
            raise ValueError("字幕草稿内容无效")
        source_order = [cue.index for cue in self.source_cues]
        allowed_groups = {entry.source_indices for entry in self.entries}
        used: list[int] = []
        restored = []
        for item in raw_entries:
            if not isinstance(item, dict):
                raise ValueError("字幕草稿内容无效")
            members = item.get("source_indices")
            if not isinstance(members, list) or not members or any(
                not isinstance(value, int) or isinstance(value, bool) for value in members
            ):
                raise ValueError("字幕草稿成员序号无效")
            split_group = tuple(members) in allowed_groups and sum(
                1 for entry in self.entries if entry.source_indices == tuple(members)
            ) > 1
            if ((not split_group and item.get("index") != members[0])
                    or (not split_group and any(value in used for value in members))):
                raise ValueError("字幕草稿序号冲突")
            if tuple(members) not in allowed_groups:
                raise ValueError("字幕草稿合并关系与正式字幕不一致")
            if not all(isinstance(item.get(key), str) for key in ("start", "end", "text")):
                raise ValueError("字幕草稿字段无效")
            try:
                start = srt_timestamp_seconds(item["start"])
                end = srt_timestamp_seconds(item["end"])
            except ValueError as exc:
                raise ValueError("字幕草稿时间无效") from exc
            if start < 0 or end <= start:
                raise ValueError("字幕草稿时间范围无效")
            used.extend(members)
            restored.append(SubtitleEntry(
                int(item["index"]), item["start"], item["end"], item["text"],
                tuple(members), item.get("stable_id")
            ))
        unique_used = []
        for value in used:
            if value not in unique_used:
                unique_used.append(value)
        if unique_used != [value for value in source_order if value in unique_used]:
            raise ValueError("字幕草稿与当前源字幕不匹配")
        if any(srt_timestamp_seconds(left.start) > srt_timestamp_seconds(right.start)
               for left, right in zip(restored, restored[1:])):
            raise ValueError("字幕草稿时间顺序无效")
        self.entries = restored
        self._undo.clear()
        self._redo.clear()
        selected = payload.get("selected_index")
        return selected if isinstance(selected, int) and any(
            entry.index == selected for entry in restored
        ) else None

    def save(self) -> str:
        if not self.entries:
            raise ValueError("至少保留一条字幕")
        if any(not entry.text.strip() for entry in self.entries):
            raise ValueError("字幕文字不能为空，请补全后保存")
        if _fingerprint(self.source_path) != self.source_fingerprint:
            raise ValueError("源字幕已在外部变化，请重新加载后再保存")
        if _fingerprint(self.corrected_path) != self.corrected_fingerprint:
            raise ValueError("校对字幕已在外部变化，请重新加载后再保存")

        source_by_index = {cue.index: cue for cue in self.source_cues}
        active = {index for entry in self.entries for index in entry.source_indices}
        deleted = [cue.index for cue in self.source_cues if cue.index not in active]
        corrections = {
            int(item["index"]): str(item["corrected"])
            for item in self.saved_state.get("corrections", [])
            if int(item["index"]) in active
        }
        for entry in self.entries:
            if len(entry.source_indices) > 1:
                continue
            if entry.index in source_by_index and entry.text != source_by_index[entry.index].text:
                corrections[entry.index] = entry.text
            elif entry.index in source_by_index:
                corrections.pop(entry.index, None)
        correction_items = [
            {"index": index, "original": source_by_index[index].text, "corrected": text}
            for index, text in sorted(corrections.items())
        ]
        merge_pairs = []
        merge_overrides = {}
        split_groups = []
        seen_split = set()
        for entry in self.entries:
            members = entry.source_indices
            if len(members) > 1:
                for first, second in zip(members, members[1:]):
                    merge_pairs.append({"first": first, "second": second})
                merge_overrides[str(members[0])] = entry.text
        for source in sorted(active):
            parts = [item for item in self.entries if item.source_indices == (source,)]
            if len(parts) < 2:
                continue
            if source in seen_split:
                continue
            seen_split.add(source)
            split_groups.append({
                "source": source,
                "segments": [
                    {"id": item.stable_id or f"{source}:{offset}",
                     "start": srt_timestamp_seconds(item.start),
                     "end": srt_timestamp_seconds(item.end), "text": item.text}
                    for offset, item in enumerate(parts)
                ],
            })
        time_overrides = {}
        split_sources = {int(item["source"]) for item in split_groups}
        for entry in self.entries:
            if entry.source_indices[0] in split_sources:
                continue
            if len(entry.source_indices) != 1 and entry.index not in source_by_index:
                continue
            original_start = source_by_index[entry.source_indices[0]].start
            original_end = source_by_index[entry.source_indices[-1]].end
            if (entry.start, entry.end) != (original_start, original_end):
                time_overrides[str(entry.index)] = {
                    "start": srt_timestamp_seconds(entry.start),
                    "end": srt_timestamp_seconds(entry.end),
                }
        output = save_corrected_srt(
            self.source_path,
            correction_items,
            deleted_indices=deleted,
            merge_pairs=merge_pairs,
            merge_overrides=merge_overrides,
            time_overrides=time_overrides,
            split_groups=split_groups,
        )
        # 旧写入契约可能在写状态时失败；只有两个产物都有效才更新基线。
        saved = load_subtitle_edit_state(self.source_path)
        if saved is None:
            raise OSError("校对字幕已写入，但校对状态保存失败；请检查磁盘并重新加载")
        self.saved_state = saved
        self.corrected_fingerprint = _fingerprint(self.corrected_path)
        self._saved_entries = tuple(self.entries)
        return output
