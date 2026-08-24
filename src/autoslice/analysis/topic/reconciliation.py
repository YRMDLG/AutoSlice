"""首轮相邻分块候选的确定性文本关联与字段合并。"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from autoslice import timecode


class AdjacentTopicReconciler:
    """只对相邻核心块中有充分证据的同一事件候选执行合并。"""

    STRONG_OVERLAP_RATIO = 0.72
    MAX_WEAK_GAP_SEC = 15
    MIN_STRONG_TEXT_SIMILARITY = 0.40
    MIN_WEAK_TEXT_SIMILARITY = 0.62
    BODY_DUPLICATE_SIMILARITY = 0.90

    _GENERIC_TITLES = frozenset({
        "聊天",
        "日常聊天",
        "继续聊天",
        "游戏过程",
        "读弹幕互动",
        "未命名片段",
    })
    _INTERNAL_KEYS = frozenset({
        "_chunk_index",
        "_source_chunk_indexes",
        "_reconcile_order",
    })

    @classmethod
    def reconcile(cls, topics):
        """合并相邻块重复/续写候选，并返回无内部元数据的时间序结果。"""
        prepared = []
        for order, raw_topic in enumerate(topics or []):
            if not isinstance(raw_topic, dict):
                continue
            topic = dict(raw_topic)
            raw_sources = topic.get("_source_chunk_indexes")
            if raw_sources is None:
                raw_sources = (topic.get("_chunk_index"),)
            try:
                source_chunks = tuple(sorted({int(item) for item in raw_sources if item is not None}))
            except TypeError:
                source_chunks = ()
            topic["_source_chunk_indexes"] = source_chunks
            topic["_reconcile_order"] = order
            prepared.append(topic)

        parent = list(range(len(prepared)))
        component_chunks = [set(topic["_source_chunk_indexes"]) for topic in prepared]

        def find(index):
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        def union(left_index, right_index):
            left_root = find(left_index)
            right_root = find(right_index)
            if left_root == right_root:
                return
            if component_chunks[left_root] & component_chunks[right_root]:
                return
            if right_root < left_root:
                left_root, right_root = right_root, left_root
            parent[right_root] = left_root
            component_chunks[left_root].update(component_chunks[right_root])

        merge_edges = []
        for left_index, left in enumerate(prepared):
            for right_index in range(left_index + 1, len(prepared)):
                right = prepared[right_index]
                if left.get("fallback") or right.get("fallback"):
                    continue
                if not cls._has_adjacent_sources(left, right):
                    continue
                evidence = cls._merge_evidence(left, right)
                if evidence is None:
                    continue
                merge_edges.append((*evidence, left_index, right_index))
        merge_edges.sort(reverse=True)
        for _, _, _, _, left_index, right_index in merge_edges:
            union(left_index, right_index)

        groups = {}
        for index, topic in enumerate(prepared):
            groups.setdefault(find(index), []).append(topic)
        reconciled = [cls._merge_group(group) for group in groups.values()]
        return sorted(
            reconciled,
            key=lambda topic: (
                int(topic.get("start", 0)),
                int(topic.get("end", 0)),
                str(topic.get("title", "")),
            ),
        )

    @classmethod
    def _has_adjacent_sources(cls, left, right):
        return any(
            abs(left_chunk - right_chunk) == 1
            for left_chunk in left.get("_source_chunk_indexes", ())
            for right_chunk in right.get("_source_chunk_indexes", ())
        )

    @classmethod
    def _merge_evidence(cls, left, right):
        left_start = int(left.get("start", 0))
        left_end = int(left.get("end", left_start))
        right_start = int(right.get("start", 0))
        right_end = int(right.get("end", right_start))
        overlap = max(0, min(left_end, right_end) - max(left_start, right_start))
        shorter = max(1, min(left_end - left_start, right_end - right_start))
        overlap_ratio = overlap / shorter
        gap = max(0, max(left_start, right_start) - min(left_end, right_end))
        title_similarity = cls._text_similarity(left.get("title"), right.get("title"))
        body_similarity = cls._text_similarity(
            " ".join(left.get("body") or []),
            " ".join(right.get("body") or []),
        )
        text_similarity = max(title_similarity, body_similarity)
        if (
            overlap_ratio >= cls.STRONG_OVERLAP_RATIO
            and text_similarity >= cls.MIN_STRONG_TEXT_SIMILARITY
        ):
            return (2, overlap_ratio, text_similarity, -gap)
        if (
            (overlap > 0 or gap <= cls.MAX_WEAK_GAP_SEC)
            and text_similarity >= cls.MIN_WEAK_TEXT_SIMILARITY
        ):
            return (1, overlap_ratio, text_similarity, -gap)
        return None

    @classmethod
    def _text_similarity(cls, left, right):
        left_key = cls._text_key(left)
        right_key = cls._text_key(right)
        if not left_key or not right_key:
            return 0.0
        if left_key == right_key:
            return 1.0
        return SequenceMatcher(None, left_key, right_key, autojunk=False).ratio()

    @classmethod
    def _text_key(cls, value):
        return re.sub(r"[\W_]+", "", str(value or "").lower(), flags=re.UNICODE)

    @classmethod
    def _select_text_field(cls, topics, field, *, title=False):
        candidates = []
        for position, topic in enumerate(topics):
            value = str(topic.get(field) or "").strip()
            if not value:
                continue
            compact = cls._text_key(value)
            quality = (
                0 if title and compact in cls._GENERIC_TITLES else 1,
                1 if 5 <= len(compact) <= 75 else 0,
                min(len(compact), 100),
                -position,
            )
            candidates.append((quality, value))
        return max(candidates, default=((), ""))[1]

    @classmethod
    def _select_title_hook(cls, topics):
        candidates = []
        for position, topic in enumerate(topics):
            hook = topic.get("title_hook")
            if not isinstance(hook, dict):
                continue
            nonempty = {key: value for key, value in hook.items() if str(value or "").strip()}
            quality = (
                len(nonempty),
                sum(len(cls._text_key(value)) for value in nonempty.values()),
                -position,
            )
            candidates.append((quality, dict(hook)))
        return max(candidates, default=((), None))[1]

    @classmethod
    def _merge_body(cls, topics):
        merged = []
        merged_keys = []
        for topic in topics:
            for raw_line in topic.get("body") or []:
                line = str(raw_line).strip()
                line_key = cls._text_key(line)
                if not line_key:
                    continue
                duplicate_index = next(
                    (
                        index
                        for index, old_key in enumerate(merged_keys)
                        if line_key == old_key
                        or SequenceMatcher(
                            None,
                            line_key,
                            old_key,
                            autojunk=False,
                        ).ratio()
                        >= cls.BODY_DUPLICATE_SIMILARITY
                    ),
                    None,
                )
                if duplicate_index is None:
                    merged.append(line)
                    merged_keys.append(line_key)
                elif len(line_key) > len(merged_keys[duplicate_index]):
                    merged[duplicate_index] = line
                    merged_keys[duplicate_index] = line_key
        return merged

    @classmethod
    def _merge_group(cls, topics):
        ordered = sorted(
            topics,
            key=lambda topic: (
                min(topic.get("_source_chunk_indexes") or (10**9,)),
                int(topic.get("start", 0)),
                int(topic.get("_reconcile_order", 0)),
            ),
        )
        merged = dict(ordered[0])
        for topic in ordered[1:]:
            for key, value in topic.items():
                if key not in merged or merged[key] in (None, "", [], {}):
                    merged[key] = value

        merged["start"] = min(int(topic["start"]) for topic in ordered)
        merged["end"] = max(int(topic["end"]) for topic in ordered)
        merged["start_str"] = timecode.format_elapsed(merged["start"])
        merged["end_str"] = timecode.format_elapsed(merged["end"])
        merged["title"] = cls._select_text_field(ordered, "title", title=True)
        publish_title = cls._select_text_field(ordered, "publish_title")
        if publish_title:
            merged["publish_title"] = publish_title
        merged["can_slice"] = any(bool(topic.get("can_slice")) for topic in ordered)
        merged["body"] = cls._merge_body(ordered)
        title_hook = cls._select_title_hook(ordered)
        if title_hook:
            merged["title_hook"] = title_hook
        else:
            merged.pop("title_hook", None)
        for key in list(merged):
            if key in cls._INTERNAL_KEYS or key.startswith("_chunk"):
                merged.pop(key, None)
        return merged
