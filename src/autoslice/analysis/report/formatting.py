"""话题报告中的时间、序号与单话题 Markdown 块格式。"""

from autoslice.analysis import titles as title_analysis

CIRCLED_NUMBERS = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳㉑㉒㉓㉔㉕㉖㉗㉘㉙㉚㉛㉜㉝㉞㉟㊱㊲㊳㊴㊵㊶㊷㊸㊹㊺㊻㊼㊽㊾㊿"


FACADE_EXPORTS = {
    "_CIRCLED_NUMBERS": "CIRCLED_NUMBERS",
    "_format_report_time": "format_report_time",
    "_format_topic_block": "format_topic_block",
    "_topic_index_label": "topic_index_label",
}


def format_report_time(seconds):
    """报告展示用时间：1 小时内用 MM:SS，超过 1 小时用 H:MM:SS。"""
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def topic_index_label(index):
    """返回 1～50 的圆圈序号，超出范围时回退为普通数字。"""
    if 1 <= index <= len(CIRCLED_NUMBERS):
        return CIRCLED_NUMBERS[index - 1]
    return f"{index}."


def format_topic_block(topic, index, streamer_name=None):
    """格式化单个话题块，贴近逐话题时间轴样式。"""
    label = topic_index_label(index) if index else ""
    start = format_report_time(topic["start"])
    end = format_report_time(topic["end"])
    marker = " ✂️" if topic.get("can_slice") else ""
    title = title_analysis._replace_streamer_role(topic["title"], streamer_name)
    lines = [f"{label}[{start}－{end}]{title}{marker}"]
    body = topic.get("body") or []
    lines.extend(title_analysis._replace_streamer_role(line, streamer_name) for line in body)
    return "\n".join(lines)
