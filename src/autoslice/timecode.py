"""视频内时间码的解析与显示格式化。"""

from datetime import timedelta

FACADE_EXPORTS = {
    "_parse_hms": "parse_hms",
    "fmt_time": "format_elapsed",
}


def format_elapsed(seconds):
    """把视频内秒数格式化为 ``H:MM:SS`` 或 ``M:SS`` 兼容文本。"""

    return str(timedelta(seconds=int(seconds)))


def parse_hms(value):
    """解析 ``MM:SS`` 或 ``HH:MM:SS`` 为视频内秒数。"""

    parts = value.split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    return int(parts[0]) * 60 + int(parts[1])
