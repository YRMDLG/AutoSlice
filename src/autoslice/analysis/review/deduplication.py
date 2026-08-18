"""话题与切片标记去重的唯一实现。"""

FACADE_EXPORTS = {
    "_overlap_ratio": "_overlap_ratio",
    "_is_duplicate_topic": "_is_duplicate_topic",
    "_dedupe_clip_marks": "_dedupe_clip_marks",
}


def _overlap_ratio(a_start, a_end, b_start, b_end):
    """按较短区间计算重叠比例。"""
    overlap = max(0, min(a_end, b_end) - max(a_start, b_start))
    shorter = max(1, min(a_end - a_start, b_end - b_start))
    return overlap / shorter


def _is_duplicate_topic(topic, existing_topics):
    """按时间范围去重；同一段被模型换标题复述时只保留第一条。"""
    for old in existing_topics:
        same_range = abs(topic["start"] - old["start"]) <= 3 and abs(topic["end"] - old["end"]) <= 3
        high_overlap = _overlap_ratio(topic["start"], topic["end"], old["start"], old["end"]) >= 0.85
        if same_range or high_overlap:
            return True
    return False



def _dedupe_clip_marks(marks):
    """对 clip_marks 做最终去重，避免旧 JSON 或异常响应导致重复切片。"""
    deduped = []
    seen_topics = []
    for mark in sorted(
            marks,
            key=lambda m: (
                0 if m.get("clip_type") == "stream_outro" else 1,
                int(m.get("topic_start", m.get("start", 0))),
                int(m.get("topic_end", m.get("end", 0))),
                m.get("title", ""),
            )):
        try:
            topic_start = int(float(mark.get("topic_start", mark["start"])))
            topic_end = int(float(mark.get("topic_end", mark["end"])))
            item = dict(mark)
            item["start"] = int(float(mark["start"]))
            item["end"] = int(float(mark["end"]))
            item["title"] = str(mark.get("title", "未命名片段")).strip() or "未命名片段"
        except (KeyError, TypeError, ValueError):
            continue
        if item["end"] <= item["start"] or topic_end <= topic_start:
            continue
        dedupe_topic = {"start": topic_start, "end": topic_end, "title": item["title"]}
        if _is_duplicate_topic(dedupe_topic, seen_topics):
            continue
        if any(
            old.get("title") == item["title"]
            and _overlap_ratio(item["start"], item["end"], old["start"], old["end"]) >= 0.5
            for old in deduped
        ):
            continue
        seen_topics.append(dedupe_topic)
        deduped.append(item)
    # 去重阶段让收播片先参与比较，是为了在尾部范围冲突时优先保留用户
    # 指定的系列片；对外返回仍必须按视频时间排列，否则收播片会变成 01，
    # 还会迫使所有既有切片无意义地整体改号。
    return sorted(
        deduped,
        key=lambda item: (
            int(item.get("start", 0)),
            int(item.get("end", 0)),
            item.get("title", ""),
        ),
    )
