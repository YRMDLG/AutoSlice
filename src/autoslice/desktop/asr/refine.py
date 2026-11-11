"""识别后的修整：把断错的边界挪到不拆词的地方，把每句起止贴到真实的说话声上。

分段规划器（新旧流水线共用）只看字数和停顿，常把“的 / 了”甩到下一句开头、把“一模一样”拆成两半；
对齐模型给的字时间在背景音乐里会漂，句尾拖进静音或切掉尾音，相邻两句之间留一两帧的缝（压成视频会闪）。
这里只做小幅修整：边界最多挪两三个字，时间最多挪 0.35 秒，找不到把握就不动。
"""

from __future__ import annotations

import hashlib
import wave
from collections.abc import Sequence
from pathlib import Path

from .types import Word

Segment = tuple[float, float, str]

# 跑到下一句开头就像上一句的尾巴：“引导你 / 的粉丝”“改 / 了改了”
_TAIL_PARTICLES = frozenset("的了吗呢吧啊么呀啦嘛哦呗")
# 这些虽然以上面的字开头，却是下一句自己的词，不往回挪；“的话”整个属于上一句
_PARTICLE_WORDS = ("的确", "了解", "了不起", "了结", "么么")
_TAIL_WORDS = ("的话",)
# 两句之间停顿超过这么久，是说话人自己停的，边界不动
_REAL_PAUSE = 0.4
# 挪字后一句最多可以比字数上限多这么几个字
_LENGTH_SLACK = 2
# 声音判断：每帧 10 毫秒；起止附近声音和静音要差这么多分贝才算有把握
_FRAME = 0.01
_CONTRAST_DB = 10.0
# 安静这么久才算一次停顿；字和字之间几十毫秒的空隙不算
_MIN_PAUSE = 0.12


def _visible(text: str) -> int:
    return sum(1 for char in text if not char.isspace())


def _take(text: str, count: int) -> tuple[str, str]:
    """从开头取 count 个非空白字：(取出的, 剩下的)。"""

    taken = 0
    for index, char in enumerate(text):
        if not char.isspace():
            taken += 1
        if taken == count:
            return text[:index + 1].rstrip(), text[index + 1:].lstrip()
    return text, ""


def _take_tail(text: str, count: int) -> tuple[str, str]:
    """从结尾取 count 个非空白字：(剩下的, 取出的)。"""

    head, tail = _take(text[::-1], count)
    return tail[::-1], head[::-1]


def _char_boundary(words: Sequence[Word], start: float, end: float, text: str, count: int) -> float:
    """这一句里第 count 个字说完的时刻：字级时间对得上就用它，对不上按字数比例估。"""

    plain = [char for char in text if not char.isspace()]
    chars: list[tuple[str, float, float]] = []
    for word in words:
        if word.end < start - 0.05 or word.start > end + 0.05:
            continue
        letters = [char for char in word.text if char.isalnum()]
        if not letters:
            continue
        span = (word.end - word.start) / len(letters)
        chars.extend((char, word.start + i * span, word.start + (i + 1) * span) for i, char in enumerate(letters))
    letters = [char for char in plain if char.isalnum()]
    if chars and len(chars) == len(letters) and 0 < count <= len(chars):
        return chars[count - 1][2]
    return start + (end - start) * count / max(1, len(plain))


def _word_cut(left: str, right: str) -> tuple[int, int] | None:
    """边界切进常用词时，返回 (往右挪的字数, 往左挪的字数)：挪哪边由调用方按长度定。"""

    from autoslice_cover.document_layout import _common_word_spans

    tail, head = left.replace(" ", "")[-6:], right.replace(" ", "")[:6]
    if not tail or not head:
        return None
    joined = tail + head
    inside, _edges = _common_word_spans(joined)
    cut = len(tail)
    if cut not in inside:
        return None
    start = cut
    while start - 1 in inside:
        start -= 1
    end = cut
    while end + 1 in inside:
        end += 1
    # 词是 joined[start-1 : end+1]；往左挪 = 把右边那截拿回上一句
    return cut - (start - 1), end + 1 - cut


def fix_boundaries(segments: Sequence[Segment], words: Sequence[Word], *, max_chars: int) -> list[Segment]:
    """语气词、助词甩到下一句开头，或常用词被拆开时，把边界挪两三个字。"""

    result = [list(item) for item in segments]
    limit = max_chars + _LENGTH_SLACK
    for index in range(1, len(result)):
        previous, current = result[index - 1], result[index]
        if current[0] - previous[1] > _REAL_PAUSE or not previous[2] or not current[2]:
            continue
        back = forward = 0
        head = current[2].replace(" ", "")
        particles = 0
        for char in head:
            if char not in _TAIL_PARTICLES:
                break
            particles += 1
        if head.startswith(_TAIL_WORDS):
            back = 2
        elif head.startswith(_PARTICLE_WORDS):
            continue
        elif 0 < particles <= 2:
            back = particles
        else:
            cut = _word_cut(previous[2], current[2])
            if cut is not None:
                to_left, to_right = cut[1], cut[0]
                back, forward = (to_left, 0) if to_left <= to_right else (0, to_right)
        if back and _visible(previous[2]) + back <= limit and _visible(current[2]) - back >= 2:
            moved, rest = _take(current[2], back)
            boundary = _char_boundary(words, current[0], current[1], current[2], back)
            previous[2], current[2] = previous[2] + moved, rest
            previous[1] = current[0] = max(previous[0] + 0.05, min(current[1] - 0.05, boundary))
        elif forward and _visible(current[2]) + forward <= limit and _visible(previous[2]) - forward >= 2:
            rest, moved = _take_tail(previous[2], forward)
            boundary = _char_boundary(words, previous[0], previous[1], previous[2], _visible(previous[2]) - forward)
            previous[2], current[2] = rest, moved + current[2]
            previous[1] = current[0] = max(previous[0] + 0.05, min(current[1] - 0.05, boundary))
    return [(start, end, text) for start, end, text in result]


def _frame_levels(wav_path: str | Path):
    """16 kHz 单声道 16 位 wav → 每 10 毫秒一帧的响度（dB）；读不了返回 None。"""

    try:
        import numpy as np
    except ImportError:
        return None
    try:
        with wave.open(str(wav_path), "rb") as handle:
            if handle.getsampwidth() != 2:
                return None
            rate, channels = handle.getframerate(), handle.getnchannels()
            samples = np.frombuffer(handle.readframes(handle.getnframes()), dtype=np.int16)
    except (OSError, wave.Error, EOFError):
        return None
    if channels > 1:
        samples = samples[: len(samples) // channels * channels].reshape(-1, channels).mean(axis=1)
    hop = max(1, int(rate * _FRAME))
    frames = len(samples) // hop
    if frames < 10:
        return None
    blocks = samples[: frames * hop].astype(np.float32).reshape(frames, hop) / 32768.0
    return 20 * np.log10(np.sqrt((blocks ** 2).mean(axis=1)) + 1e-6)


def _quiet_runs(levels, low_t: float, high_t: float) -> list[tuple[float, float]] | None:
    """low_t~high_t 之间的停顿（安静至少 _MIN_PAUSE 秒）：[(开始, 结束)]；声音和静音分不清时返回 None。

    阈值按这一小段自己的响度定（背景音乐时大时小）；字中间的短停顿（几十毫秒）不算。
    """

    import numpy as np

    low = max(0, int((low_t - 0.3) / _FRAME))
    high = min(len(levels), int((high_t + 0.3) / _FRAME) + 1)
    local = levels[low:high]
    if len(local) < 8:
        return None
    floor, peak = float(np.percentile(local, 10)), float(np.percentile(local, 95))
    if peak - floor < _CONTRAST_DB:
        return None
    quiet = local < floor + (peak - floor) * 0.35
    runs, begin = [], None
    for index, flag in enumerate(list(quiet) + [False]):
        if flag and begin is None:
            begin = index
        elif not flag and begin is not None:
            if (index - begin) * _FRAME >= _MIN_PAUSE:
                runs.append(((low + begin) * _FRAME, (low + index) * _FRAME))
            begin = None
    return [(a, b) for a, b in runs if b >= low_t and a <= high_t]


def snap_to_speech(segments: Sequence[Segment], wav_path: str | Path, *, lead: float = 0.08, tail: float = 0.2,
                   window: float = 0.35, close_gap: float = 0.3, min_duration: float = 0.6) -> list[Segment]:
    """按两句之间真正的停顿重定边界：上一句结束在停顿开始后一点，下一句开始在停顿结束前一点。

    每个边界只看它附近（前后 window 秒）的停顿；找不到停顿（话连着说）或声音和静音分不清
    （背景音乐太满）就不动。最后把小缝接上，太短的往后补。
    """

    levels = _frame_levels(wav_path)
    result = [list(item) for item in segments]
    if levels is not None and result:
        # 第一句开头、最后一句结尾：只和前面 / 后面的静音比
        runs = _quiet_runs(levels, result[0][0] - window, result[0][0] + window)
        if runs:
            before = [run for run in runs if abs(run[1] - result[0][0]) <= window and run[0] <= result[0][0] + 0.02]
            if before:
                result[0][0] = max(0.0, min(before, key=lambda run: abs(run[1] - result[0][0]))[1] - lead)
        for item, following in zip(result, result[1:]):
            end, start = item[1], following[0]
            runs = _quiet_runs(levels, end - window, start + window)
            if not runs:
                continue
            # 上一句结束：开始点离原终点最近、且一直延续到原终点之后的停顿（句子中间的停顿不算）；
            # 下一句开始同理：停顿要从原起点之前就开始
            near_end = [run for run in runs if abs(run[0] - end) <= window and run[1] >= end - 0.02]
            near_start = [run for run in runs if abs(run[1] - start) <= window and run[0] <= start + 0.02]
            new_end = min(near_end, key=lambda run: abs(run[0] - end))[0] + tail if near_end else end
            new_start = min(near_start, key=lambda run: abs(run[1] - start))[1] - lead if near_start else start
            # 字级时间当护栏：下一句不早于上一句最后一个字说完，上一句不晚于下一句第一个字开口。
            # 念得轻的字（比如“奇怪”的“奇”）响度像停顿，没有护栏会把下一句拉进上一句的字里
            new_start = max(new_start, end)
            if start > end:
                new_end = min(new_end, max(start, new_start))
            if new_end > new_start:
                # 停顿比缓冲还短：两句在停顿里接上
                new_end = new_start = max(min(new_end, new_start), item[0] + 0.2)
            if new_end - item[0] >= 0.2 and following[1] - new_start >= 0.2:
                item[1], following[0] = new_end, new_start
        runs = _quiet_runs(levels, result[-1][1] - window, result[-1][1] + window)
        if runs:
            after = [run for run in runs if abs(run[0] - result[-1][1]) <= window and run[1] >= result[-1][1] - 0.02]
            if after:
                result[-1][1] = min(after, key=lambda run: abs(run[0] - result[-1][1]))[0] + tail
    for index, item in enumerate(result):
        if index + 1 < len(result):
            following = result[index + 1]
            if item[1] > following[0]:
                item[1] = following[0]
            elif following[0] - item[1] < close_gap:
                item[1] = following[0]
            if item[1] - item[0] < min_duration:
                item[1] = min(following[0], item[0] + min_duration)
        elif item[1] - item[0] < min_duration:
            item[1] = item[0] + min_duration
    for item in result:
        # 保证条数不变（字幕页按位置对回去）：极端情况下也留出 0.1 秒
        item[1] = max(item[1], item[0] + 0.1)
    return [(start, end, text) for start, end, text in result]


# 字幕页“贴合声音”的音频缓存最多留几份（6 分钟视频约 11 MB）
_AUDIO_CACHE_KEEP = 6


def speech_audio(cache_root: str | Path, video: str | Path) -> Path:
    """字幕页“贴合声音”用的 16k 单声道音频：按视频路径、大小、修改时间缓存，视频换了才重抽。"""

    from . import audio

    video = Path(video)
    stat = video.stat()
    key = hashlib.sha256(f"{video.resolve()}|{stat.st_size}|{stat.st_mtime_ns}".casefold().encode("utf-8")).hexdigest()[:20]
    root = Path(cache_root)
    target = root / f"{key}.wav"
    if not target.is_file():
        root.mkdir(parents=True, exist_ok=True)
        audio.extract_wav(video, target)
        cached = sorted(root.glob("*.wav"), key=lambda path: path.stat().st_mtime, reverse=True)
        for old in cached[_AUDIO_CACHE_KEEP:]:
            old.unlink(missing_ok=True)
    return target
