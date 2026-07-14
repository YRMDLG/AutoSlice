"""
话题分析 + 智能切片引擎

流水线: FunASR转录 → 弹幕密度分析 → SRT分块 → DeepSeek Pro分析 → 报告 + 切片标记

用法:
  from topic_engine import run_pipeline
  result = run_pipeline(flv_path, ass_path, progress_callback=cb)
  # result: {"report": "...", "clip_marks": [...], "json_path": "..."}
"""

import html
import math
import bisect
import difflib
import os, re, json, time, zipfile, requests, threading
from collections import defaultdict
from datetime import datetime, timedelta


# ============================================================
# 配置
# ============================================================
CHUNK_SEC = 600          # 每块 10 分钟：减少 API 调用，降低话题被硬切碎的概率
LLM_MODEL = "deepseek-v4-pro"
LLM_MAX_TOKENS = 16000
LLM_COMPACT_MAX_TOKENS = 12000
LLM_FULL_TEXT_CHARS = 8000
LLM_COMPACT_TEXT_CHARS = 2200
LLM_RETRY_DELAYS = (3, 8, 20, 45)
LLM_REQUEST_TIMEOUT = (30, 600)
MAX_INITIAL_FAILED_CHUNKS = 3
FUNASR_MODEL = "iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch"
FUNASR_DEFAULT_DEVICE = os.environ.get("AUTOSLICE_FUNASR_DEVICE", "cpu")
FUNASR_CACHE_MODEL_DIR = os.path.expanduser(
    r"~\.cache\modelscope\hub\models\iic\speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch"
)
DANMAKU_WINDOW = 60
DANMAKU_WINDOW_STEP = 15  # 每 15 秒采样一个 60 秒窗口，兼顾峰值定位和全场覆盖
CLIP_DENSITY_RATIO = 1.20  # 话题切片至少需要达到全场平均的 1.2 倍
TOPIC_PRE_CONTEXT_SEC = 45      # 话题切片向前保留前因，避免二剪素材拖得过长
TOPIC_POST_CONTEXT_SEC = 60     # 话题切片向后保留反应和收尾
TOPIC_MIN_CLIP_SEC = 75         # 太短的高能点至少扩成 1.25 分钟上下文
TOPIC_MAX_CLIP_SEC = 300        # 单个实际切片最长 5 分钟
TOPIC_REQUIRED_CONTEXT_OVERFLOW_SEC = 15  # 为保住 SC/明确触发句和完整收尾，最多允许额外 15 秒
TOPIC_DIRECT_SLICE_MAX_SEC = 240  # 4 分钟内的连贯话题保留完整语义核心；更长话题才截弹幕峰值核心段
TOPIC_FOCUS_PRE_SEC = 0         # 长话题核心从弹幕峰值窗口开始，前因由 TOPIC_PRE_CONTEXT_SEC 补
TOPIC_FOCUS_POST_SEC = DANMAKU_WINDOW  # 长话题核心覆盖完整弹幕峰值窗口
TOPIC_CONTEXT_GAP = 4.0         # SRT 语句间隔边界
TOPIC_NATURAL_BOUNDARY_PRE_MAX_SEC = 30
TOPIC_NATURAL_BOUNDARY_POST_MAX_SEC = 45
TOPIC_AI_FOCUS_PRE_CONTEXT_SEC = 20
TOPIC_AI_FOCUS_POST_CONTEXT_SEC = 20
TOPIC_AI_FOCUS_EDGE_PRE_CONTEXT_SEC = 45
TOPIC_AI_FOCUS_EDGE_POST_CONTEXT_SEC = 60
TOPIC_AI_FOCUS_NATURAL_PRE_BOUNDARY_SEC = 5
TOPIC_AI_FOCUS_NATURAL_POST_BOUNDARY_SEC = 10
TOPIC_LEAD_IN_RECOVERY_MIN_SEC = 180
TOPIC_LEAD_IN_LOOKBACK_SEC = 180
SC_CONTEXT_LOOKBACK_SEC = 180   # 话题前 3 分钟内的 SC/礼物触发点会纳入切片
SC_FALLBACK_GIFT_LOOKBACK_SEC = 15  # 仅凭“感谢礼物”推断 SC 时避免回溯到无关互动
SRT_ABNORMAL_CHARS_PER_SEC = 18 # 超过该语速视为 ASR 时间戳异常
SRT_ESTIMATED_CHARS_PER_SEC = 7 # 异常长字幕按该语速估算结束时间
SRT_MAX_ESTIMATED_SEG_SEC = 300 # 单条异常字幕最多估算 5 分钟
SRT_REPEAT_REPAIR_MIN_ENTRIES = 8  # 旧版把整段全文按每个字重复写入时的识别下限
SUBTITLE_TARGET_CHARS = 18
SUBTITLE_MAX_CHARS = 28
SUBTITLE_MAX_DURATION_SEC = 7.0
SUBTITLE_PAUSE_BREAK_SEC = 0.65
TOPIC_MIN_REPORT_SEC = 60       # 正文较多但模型给出几秒时，报告至少扩到 1 分钟
TOPIC_MAX_REPAIRED_REPORT_SEC = 180
MANUAL_TIMELINE_DIR = r"F:\切片时间轴"
MANUAL_TIMELINE_CHUNK_MARGIN_SEC = 180
MANUAL_TIMELINE_TOPIC_PRE_SEC = 30
MANUAL_TIMELINE_TOPIC_POST_SEC = 150
MANUAL_TIMELINE_STAR_DENSITY_RATIO = 0.80
MANUAL_TIMELINE_END_MARGIN_SEC = 15
MANUAL_TIMELINE_OPTIMIZE_GAP_SEC = 180
MANUAL_TIMELINE_OPTIMIZE_MAX_GROUP_SEC = 600
MANUAL_TIMELINE_OPTIMIZE_BATCH_SIZE = 6
MANUAL_TIMELINE_ALIGNMENT_SEARCH_SEC = 600
MANUAL_TIMELINE_ALIGNMENT_WINDOW_SEC = 80
MANUAL_TIMELINE_ALIGNMENT_STEP_SEC = 20
MANUAL_TIMELINE_ALIGNMENT_MIN_SCORE = 0.12

SC_TRIGGER_KEYWORDS = (
    "sc", "s c", "super chat", "superchat", "醒目留言", "醒目", "付费留言",
    "舰长", "上舰", "总督", "提督", "舰团", "礼物", "打赏", "投喂",
    "爱心抱枕", "告白花束", "棉花糖", "牛哇牛哇", "充电",
)

THANKS_TRIGGER_RE = re.compile(r'(谢谢|感谢|谢[谢了]?|多谢).{0,24}(送|的|老板|老公|礼物|留言|支持)')

STREAMER_NICKNAME_MAP = {
    "泽音Melody": "音音",
    "泽音": "音音",
}

STREAMER_FAN_ALIASES = ("音姐", "麻麻", "音音")

STREAMER_ASR_LITERAL_REPLACEMENTS = (
    ("英英", "音音"),
    ("莹莹", "音音"),
    ("盈盈", "音音"),
    ("应应", "音音"),
    ("音因", "音音"),
    ("音乐生", "音悦生"),
    ("英悦生", "音悦生"),
    ("音悦声", "音悦生"),
    ("晚安音乐声", "晚安音悦生"),
)

PUBLISH_TITLE_PREFIX = "【泽音】"
MAX_PUBLISH_TITLE_CHARS = 80
TITLE_STYLE_PROFILE_PATH = os.path.join(os.path.dirname(__file__), "title_style_profile.json")
TITLE_STYLE_EXAMPLE_LIMIT = 8
DEFAULT_REFINEMENT_QUEUE_DIR = os.environ.get(
    "AUTOSLICE_REFINEMENT_QUEUE_DIR",
    r"F:\Videos\自动切片",
)
UNIFIED_REFINEMENT_QUEUE_JSON = "精调任务总清单.json"
UNIFIED_REFINEMENT_QUEUE_MD = "精调任务总清单.md"
_UNIFIED_REFINEMENT_QUEUE_LOCK = threading.Lock()
_PUBLISH_TITLE_PREFIX_RE = re.compile(
    r'^\s*[【\[]\s*泽音(?:Melody)?\s*[】\]]\s*',
    re.IGNORECASE,
)
_PUBLISH_TITLE_META_KEYWORDS = (
    "publish_title", "投稿标题建议", "标题建议如下", "根据要求", "按照要求",
    "只输出", "最终JSON", "最终 JSON", "can_slice", "points", "作为模型",
    "作为AI", "无法生成", "信息不足", "仅供参考",
)
_GENERIC_PUBLISH_TITLES = {
    "直播精彩片段", "精彩直播片段", "直播高光", "精彩片段",
    "日常聊天", "日常闲聊", "游戏过程", "互动片段",
}

_TITLE_STYLE_TAG_KEYWORDS = {
    "SC": ("sc", "醒目留言", "付费留言", "红sc", "留言"),
    "观众互动": ("观众", "弹幕", "音悦生", "舰长", "礼物", "感谢", "互动"),
    "游戏": ("游戏", "关卡", "过关", "失败", "挑战", "节奏天国", "躲猫猫"),
    "看视频": ("看视频", "视频", "二创", "连线", "回放"),
    "唱歌": ("唱歌", "点歌", "演唱", "歌曲", "舞台"),
    "温情": ("晚安", "陪伴", "鼓励", "谢谢大家", "温柔", "感动"),
    "日常": ("出差", "下飞机", "打车", "外卖", "妈妈", "音妈", "线下", "日常"),
}

REFINEMENT_WORKFLOW_STEPS = (
    ("verify_context", "核查前后文"),
    ("trim_breath", "剪气口与停顿"),
    ("correct_subtitles", "导入片段同名校对字幕并检查专名"),
    ("add_intro_outro", "添加片头片尾"),
    ("export_video", "导出精调成片"),
    ("make_cover", "用 AutoCover 制作封面"),
    ("publish_bilibili", "在 B 站网页投稿"),
)


def fmt_time(seconds):
    return str(timedelta(seconds=int(seconds)))


def _infer_streamer_name(video_path):
    """从录播路径推断主播名；例如 1947277414-泽音Melody -> 泽音Melody。"""
    parts = re.split(r'[\\/]+', video_path or "")
    basename = os.path.basename(video_path or "")
    for known_name in sorted(STREAMER_NICKNAME_MAP, key=len, reverse=True):
        if known_name in basename:
            return known_name
    for part in parts:
        match = re.match(r'^\d{4,}-(.+)$', part)
        if match:
            name = match.group(1).strip()
            if name:
                return name
    return "主播"


def _streamer_report_name(streamer_name):
    """报告展示用粉丝称呼，避免正式名太生硬。"""
    return STREAMER_NICKNAME_MAP.get(streamer_name, streamer_name or "主播")


def _text_len_for_timing(text):
    """估算语速用长度：去掉空白，保留中文/数字/字母。"""
    return len(re.sub(r'\s+', '', text or ""))


def _repair_srt_end_time(start_s, end_s, text):
    """修复 FunASR 偶发的“几百字压到零点几秒”时间戳。"""
    duration = max(0.001, end_s - start_s)
    text_len = _text_len_for_timing(text)
    if text_len < 80:
        return end_s
    if text_len / duration <= SRT_ABNORMAL_CHARS_PER_SEC:
        return end_s
    estimated = min(SRT_MAX_ESTIMATED_SEG_SEC, max(duration, text_len / SRT_ESTIMATED_CHARS_PER_SEC))
    return start_s + estimated


def _join_asr_tokens(tokens):
    """拼接 FunASR 字/词 token；中文不加空格，连续英文词保留分隔。"""
    result = ""
    for token in (str(item).strip() for item in tokens):
        if not token:
            continue
        if result and re.search(r'[A-Za-z0-9]$', result) and re.match(r'^[A-Za-z0-9]', token):
            result += " "
        result += token
    return result.strip()


def _normalise_asr_text(text, streamer_name="主播"):
    """清理 ASR 分词空格，并对泽音录播应用低歧义专名纠错。"""
    if isinstance(text, (list, tuple)):
        tokens = text
    else:
        tokens = re.split(r'\s+', str(text or "").replace("\n", " ").strip())
    clean = _join_asr_tokens(tokens)
    return _normalise_streamer_terms(clean, streamer_name=streamer_name)


def _normalise_streamer_terms(text, streamer_name="主播"):
    """统一字幕和 AI 报告中的主播/粉丝专名，不改动其它排版。"""
    clean = str(text or "")
    if _streamer_report_name(streamer_name) != "音音":
        return clean
    for source, target in STREAMER_ASR_LITERAL_REPLACEMENTS:
        clean = clean.replace(source, target)
    clean = re.sub(
        r'音乐声(?=(?:们|宝宝|晚上好|晚安|好[呀啊]?|来|在|都|才|说|看|见|发|给|喜欢|想))',
        '音悦生',
        clean,
    )
    clean = re.sub(r'(?<=感谢)音乐声', '音悦生', clean)
    clean = re.sub(r'(?<=见)音乐声', '音悦生', clean)
    return clean


def _subtitle_text_size(text):
    return len(re.sub(r'\s+', '', text or ""))


def _segment_timed_tokens(timed_tokens, streamer_name="主播"):
    """把字/词时间戳整理成适合阅读和边界吸附的短句字幕。"""
    if not timed_tokens:
        return []
    segments = []
    current = []
    current_chars = 0
    sentence_end_tokens = "。！？!?；;"

    def flush():
        nonlocal current, current_chars
        if not current:
            return
        text = _normalise_asr_text([item[2] for item in current], streamer_name=streamer_name)
        if text:
            segments.append((current[0][0], current[-1][1], text))
        current = []
        current_chars = 0

    for index, (start_s, end_s, token) in enumerate(timed_tokens):
        token = str(token).strip()
        if not token:
            continue
        current.append((float(start_s), float(end_s), token))
        current_chars += _subtitle_text_size(token)
        duration = current[-1][1] - current[0][0]
        next_gap = 0.0
        if index + 1 < len(timed_tokens):
            next_gap = max(0.0, float(timed_tokens[index + 1][0]) - float(end_s))
        should_break = (
            (token[-1] in sentence_end_tokens and current_chars >= 4)
            or (next_gap >= SUBTITLE_PAUSE_BREAK_SEC and current_chars >= 2)
            or (current_chars >= SUBTITLE_TARGET_CHARS and (next_gap >= 0.15 or duration >= 4.5))
            or current_chars >= SUBTITLE_MAX_CHARS
            or duration >= SUBTITLE_MAX_DURATION_SEC
        )
        if should_break:
            flush()
    flush()
    return segments


def _segments_from_funasr_result(text, timestamps, offset=0.0, streamer_name="主播"):
    """把单个 FunASR 结果转成短句，避免把整段全文复制到每个字时间戳。"""
    timestamps = [item for item in (timestamps or []) if isinstance(item, (list, tuple)) and len(item) == 2]
    if not text or not timestamps:
        return []
    tokens = str(text).strip().split()
    if len(tokens) != len(timestamps):
        compact = re.sub(r'\s+', '', str(text))
        if len(compact) == len(timestamps):
            tokens = list(compact)
        else:
            start_s = offset + float(timestamps[0][0]) / 1000.0
            end_s = offset + float(timestamps[-1][1]) / 1000.0
            clean = _normalise_asr_text(text, streamer_name=streamer_name)
            return [(start_s, max(end_s, start_s + 0.1), clean)] if clean else []
    timed_tokens = [
        (
            offset + float(timestamp[0]) / 1000.0,
            offset + float(timestamp[1]) / 1000.0,
            token,
        )
        for token, timestamp in zip(tokens, timestamps)
    ]
    return _segment_timed_tokens(timed_tokens, streamer_name=streamer_name)


def _read_srt_entries(srt_path):
    """读取原始 SRT 条目，不提前修正时间，供异常结构识别使用。"""
    if not srt_path or not os.path.exists(srt_path):
        return []
    with open(srt_path, encoding="utf-8") as f:
        content = f.read()
    pattern = r'(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})\s*\n(.*?)(?=\n\n|\Z)'
    entries = []
    for start_str, end_str, text in re.findall(pattern, content, re.DOTALL):
        clean_text = text.strip().replace("\n", " ").strip()
        if not clean_text:
            continue
        entries.append((
            _parse_srt_timestamp(start_str),
            _parse_srt_timestamp(end_str),
            clean_text,
        ))
    return entries


def _load_repaired_srt_segments(srt_path):
    """加载健康 SRT，并无损还原旧版 FunASR 的“全文逐字重复”异常文件。"""
    entries = _read_srt_entries(srt_path)
    if not entries:
        return []
    streamer_name = _infer_streamer_name(srt_path)
    segments = []
    index = 0
    while index < len(entries):
        raw_text = entries[index][2]
        group_end = index + 1
        while group_end < len(entries) and entries[group_end][2] == raw_text:
            group_end += 1
        group = entries[index:group_end]
        tokens = raw_text.split()
        is_repeated_funasr_block = (
            len(group) >= SRT_REPEAT_REPAIR_MIN_ENTRIES
            and len(tokens) == len(group)
            and _subtitle_text_size(raw_text) >= 20
        )
        if is_repeated_funasr_block:
            timed_tokens = [
                (entry[0], entry[1], token)
                for entry, token in zip(group, tokens)
            ]
            segments.extend(_segment_timed_tokens(timed_tokens, streamer_name=streamer_name))
        else:
            for start_s, end_s, text in group:
                clean_text = _normalise_asr_text(text, streamer_name=streamer_name)
                if not clean_text:
                    continue
                repaired_end = _repair_srt_end_time(start_s, end_s, clean_text)
                if (
                    segments
                    and clean_text == segments[-1][2]
                    and start_s - segments[-1][1] <= TOPIC_CONTEXT_GAP
                ):
                    segments[-1] = (
                        segments[-1][0],
                        max(segments[-1][1], repaired_end),
                        clean_text,
                    )
                else:
                    segments.append((start_s, repaired_end, clean_text))
        index = group_end
    return sorted(segments, key=lambda item: (item[0], item[1]))


def export_corrected_srt(source_srt_path):
    """在源字幕旁生成可导入剪映的校对版，不覆盖原始 SRT。"""
    segments = _load_repaired_srt_segments(source_srt_path)
    if not segments:
        return None
    output_path = os.path.splitext(source_srt_path)[0] + "_校对字幕.srt"
    with open(output_path, "w", encoding="utf-8") as f:
        for index, (start_s, end_s, text) in enumerate(segments, 1):
            f.write(
                f"{index}\n{_srt_time(start_s)} --> {_srt_time(max(end_s, start_s + 0.1))}\n"
                f"{text}\n\n"
            )
    return output_path


def _repair_short_topic_end(start_s, end_s, body_lines, chunk_end):
    """模型给出极短时间但正文很多时，修正报告话题结束时间。"""
    duration = end_s - start_s
    body_len = sum(_text_len_for_timing(line) for line in body_lines)
    if duration >= 10 or body_len < 40:
        return end_s
    estimated = min(TOPIC_MAX_REPAIRED_REPORT_SEC, max(TOPIC_MIN_REPORT_SEC, body_len / SRT_ESTIMATED_CHARS_PER_SEC))
    return int(min(chunk_end, start_s + estimated))


def _extract_video_start_datetime(video_path):
    """从录播文件名/目录名提取视频起始墙钟时间，用于换算人工时间轴。"""
    basename = os.path.basename(video_path or "")
    candidates = [basename] + re.split(r'[\\/]+', video_path or "")
    patterns = (
        r'(?P<y>\d{4})年(?P<m>\d{1,2})月(?P<d>\d{1,2})[日号][-_\s]*'
        r'(?P<h>\d{1,2})点(?P<mi>\d{1,2})分(?:(?P<s>\d{1,2})秒)?',
        r'(?P<y>\d{4})[-.](?P<m>\d{1,2})[-.](?P<d>\d{1,2})[-_\s]+'
        r'(?P<h>\d{1,2})[-点:](?P<mi>\d{1,2})[-分:](?P<s>\d{1,2})',
    )
    for text in candidates:
        for pattern in patterns:
            match = re.search(pattern, text)
            if not match:
                continue
            parts = {
                key: int(value) if value is not None else 0
                for key, value in match.groupdict().items()
            }
            try:
                return datetime(parts["y"], parts["m"], parts["d"], parts["h"], parts["mi"], parts["s"])
            except ValueError:
                continue
    return None


def _manual_timeline_doc_candidates(video_start, timeline_dir=MANUAL_TIMELINE_DIR):
    """按录播日期生成可能的人工时间轴 docx 路径。"""
    if not video_start:
        return []
    compact = video_start.strftime("%Y%m%d")
    dotted = f"{video_start.year}.{video_start.month}.{video_start.day}"
    names = [
        f"{compact}.docx",
        f"{compact}切片文档.docx",
        f"{dotted}.docx",
        f"{dotted}切片文档.docx",
    ]
    return [os.path.join(timeline_dir, name) for name in names]


def _find_manual_timeline_doc(video_path, timeline_dir=MANUAL_TIMELINE_DIR):
    """自动查找 F:\切片时间轴 下和录播日期匹配的 docx。"""
    video_start = _extract_video_start_datetime(video_path)
    for path in _manual_timeline_doc_candidates(video_start, timeline_dir):
        if os.path.exists(path):
            return path
    if not video_start or not os.path.isdir(timeline_dir):
        return None
    compact = video_start.strftime("%Y%m%d")
    dotted = f"{video_start.year}.{video_start.month}.{video_start.day}"
    matches = []
    for name in os.listdir(timeline_dir):
        if not name.lower().endswith(".docx"):
            continue
        if name.startswith(compact) or name.startswith(dotted):
            matches.append(os.path.join(timeline_dir, name))
    if not matches:
        return None
    matches.sort(key=lambda p: ("切片文档" in os.path.basename(p), os.path.basename(p)))
    return matches[0]


def _read_docx_lines(docx_path):
    """读取 docx 段落文本；优先 python-docx，失败时用 zip XML 兜底。"""
    try:
        import docx  # type: ignore

        document = docx.Document(docx_path)
        return [p.text.strip() for p in document.paragraphs if p.text.strip()]
    except Exception:
        try:
            with zipfile.ZipFile(docx_path) as zf:
                xml = zf.read("word/document.xml").decode("utf-8", "ignore")
            texts = re.findall(r"<w:t[^>]*>(.*?)</w:t>", xml)
            merged = []
            current = []
            for item in texts:
                text = html.unescape(item).strip()
                if not text:
                    continue
                current.append(text)
                if re.search(r'[。！？!?]$', text) or re.match(r'^\d{1,2}:\d{2}', text):
                    merged.append("".join(current).strip())
                    current = []
            if current:
                merged.append("".join(current).strip())
            return [line for line in merged if line]
        except Exception:
            return []


def _parse_manual_timeline_lines(lines, video_start):
    """解析朋友整理的时间轴文档，把墙钟时间换算成视频内秒数。"""
    if not video_start:
        return []
    entries = []
    period_start = None
    period_end = None
    header_re = re.compile(
        r'(\d{4})-(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2}):(\d{2})'
        r'\s*至\s*'
        r'(\d{4})-(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2}):(\d{2})'
    )
    event_re = re.compile(r'^\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s+(.+?)\s*$')
    expanded_lines = []
    event_marker_re = re.compile(r'(?<!\d)\d{1,2}:\d{2}(?::\d{2})?(?=\s)')
    for raw in lines or []:
        line = re.sub(r'\s+', ' ', str(raw or "")).strip()
        if not line:
            continue
        header = header_re.search(line)
        if header and "记录如下" in line:
            try:
                values = list(map(int, header.groups()))
                period_start = datetime(*values[:6])
                period_end = datetime(*values[6:])
            except ValueError:
                period_start = None
                period_end = None
            continue
        markers = list(event_marker_re.finditer(line))
        if not markers:
            continue
        for index, marker in enumerate(markers):
            end = markers[index + 1].start() if index + 1 < len(markers) else len(line)
            expanded_lines.append((
                line[marker.start():end].strip(),
                period_start,
                period_end,
            ))

    for line, line_period_start, line_period_end in expanded_lines:
        match = event_re.match(line)
        if not match:
            continue
        h, minute, sec, text = match.groups()
        second = int(sec or 0)
        event_time = (int(h), int(minute), second)
        try:
            if line_period_start and line_period_end:
                event_dt = datetime(
                    line_period_start.year, line_period_start.month, line_period_start.day,
                    *event_time,
                )
                if event_dt < line_period_start:
                    event_dt += timedelta(days=1)
            else:
                candidates = [
                    datetime.combine(video_start.date() + timedelta(days=offset), datetime.min.time()).replace(
                        hour=event_time[0], minute=event_time[1], second=event_time[2]
                    )
                    for offset in (-1, 0, 1)
                ]
                event_dt = min(candidates, key=lambda item: abs((item - video_start).total_seconds()))
        except ValueError:
            continue
        elapsed = int((event_dt - video_start).total_seconds())
        if elapsed < 0:
            continue
        stars = text.count("⭐") + text.count("★")
        clean_text = re.sub(r'[⭐★]+', '', text).strip()
        clean_text = clean_text.strip(" -—，,。")
        if not clean_text:
            continue
        entries.append({
            "start": elapsed,
            "clock": f"{event_dt:%Y-%m-%d %H:%M:%S}",
            "text": clean_text,
            "stars": stars,
            "highlight": stars > 0,
            "source": "manual_timeline",
        })
    return entries


def _parse_elapsed_timeline_report_lines(lines):
    """解析明确声明为视频内时间的旧报告，作为低权重参考候选。"""
    normalized_lines = [str(line or "").strip() for line in lines or []]
    has_elapsed_time_basis = any(
        "时间基准" in line and ("视频内时间" in line or "播放进度" in line)
        for line in normalized_lines
    )
    if not has_elapsed_time_basis:
        return []

    time_pattern = r'\d{1,3}:\d{2}(?::\d{2})?'
    topic_re = re.compile(
        rf'^\s*(?:[①-⑳㉑-㊿]|\d+[.、)])?\s*'
        rf'\[\s*(?P<start>{time_pattern})\s*[－—–~-]\s*'
        rf'(?P<end>{time_pattern})\s*\]\s*(?P<title>.+?)\s*$'
    )
    entries = []
    current = None
    for line in normalized_lines:
        match = topic_re.match(line)
        if match:
            try:
                start = _parse_hms(match.group("start"))
                end = _parse_hms(match.group("end"))
            except (TypeError, ValueError):
                current = None
                continue
            if end <= start:
                current = None
                continue
            raw_title = match.group("title")
            stars = raw_title.count("⭐") + raw_title.count("★")
            if "✂" in raw_title:
                stars = max(stars, 1)
            title = re.sub(r'[✂⭐★]\ufe0f?', '', raw_title).strip(" -—，,。")
            if not title:
                current = None
                continue
            current = {
                "start": start,
                "end": end,
                "clock": "视频内时间",
                "text": title,
                "stars": stars,
                "highlight": stars > 0,
                "source": "elapsed_report_reference",
                "time_basis": "video_elapsed_seconds",
                "explicit_range": True,
            }
            entries.append(current)
            continue
        if current and line.startswith("【泽音】"):
            current["reference_publish_title"] = line
    return entries


def _filter_manual_timeline_entries(entries, video_duration, end_margin_sec=MANUAL_TIMELINE_END_MARGIN_SEC):
    """只保留当前分段视频范围内的人工时间轴记录。"""
    if not video_duration or video_duration <= 0:
        return list(entries or [])
    max_start = float(video_duration) + max(0, end_margin_sec)
    return [item for item in entries or [] if 0 <= float(item.get("start", -1)) <= max_start]


def _probe_video_duration(video_path):
    """用 ffprobe 获取当前分段视频的精确时长；失败时返回 None。"""
    if not video_path or not os.path.isfile(video_path):
        return None
    import subprocess as sp
    try:
        result = sp.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", video_path,
            ],
            check=True,
            stdout=sp.PIPE,
            stderr=sp.PIPE,
            encoding="utf-8",
            errors="replace",
        )
        duration = float(result.stdout.strip())
        return duration if duration > 0 else None
    except (OSError, ValueError, sp.CalledProcessError):
        return None


def load_manual_timeline(video_path, timeline_dir=MANUAL_TIMELINE_DIR, manual_timeline_path=None):
    """加载人工时间轴 docx；manual_timeline_path=None 自动匹配，'__none__' 禁用。"""
    video_start = _extract_video_start_datetime(video_path)
    if manual_timeline_path == "__none__":
        return {"path": None, "entries": [], "video_start": video_start, "mode": "disabled"}
    if manual_timeline_path:
        doc_path = manual_timeline_path if os.path.isfile(manual_timeline_path) else None
    else:
        doc_path = _find_manual_timeline_doc(video_path, timeline_dir)
    if not doc_path:
        return {"path": None, "entries": [], "video_start": video_start, "mode": "manual" if manual_timeline_path else "auto"}
    lines = _read_docx_lines(doc_path)
    entries = _parse_manual_timeline_lines(lines, video_start)
    time_basis = "wall_clock_converted_to_video_elapsed_seconds" if entries else None
    if not entries:
        entries = _parse_elapsed_timeline_report_lines(lines)
        if entries:
            time_basis = "video_elapsed_seconds"
    return {
        "path": doc_path,
        "entries": entries,
        "video_start": video_start,
        "mode": "manual" if manual_timeline_path else "auto",
        "time_basis": time_basis,
    }


def _manual_timeline_summary(manual_timeline):
    """返回可 JSON 序列化的人工时间轴摘要，避免 Web SSE 返回 datetime。"""
    manual_timeline = manual_timeline or {}
    entries = manual_timeline.get("entries") or []
    video_start = manual_timeline.get("video_start")
    if isinstance(video_start, datetime):
        video_start = video_start.strftime("%Y-%m-%d %H:%M:%S")
    return {
        "path": manual_timeline.get("path"),
        "entry_count": len(entries),
        "source_entry_count": manual_timeline.get("source_entry_count", len(entries)),
        "raw_entry_count": manual_timeline.get("raw_entry_count", len(entries)),
        "optimized_entry_count": manual_timeline.get("optimized_entry_count"),
        "optimized_json_path": manual_timeline.get("optimized_json_path"),
        "optimized_md_path": manual_timeline.get("optimized_md_path"),
        "optimization_warning": manual_timeline.get("optimization_warning"),
        "star_count": sum(1 for item in entries if item.get("stars", 0) > 0),
        "video_start": video_start,
        "time_basis": manual_timeline.get("time_basis") or (
            entries[0].get("time_basis", "wall_clock_converted_to_video_elapsed_seconds")
            if entries else None
        ),
    }


def _format_manual_entry_for_prompt(entry):
    stars = "⭐" * min(int(entry.get("stars", 0)), 5)
    prefix = f"{stars} " if stars else ""
    clock = entry.get("clock")
    elapsed_label = fmt_time(entry["start"])
    if entry.get("end") is not None and int(entry["end"]) > int(entry["start"]):
        elapsed_label = f"{elapsed_label}-{fmt_time(entry['end'])}"
    time_label = f"{elapsed_label} / {clock}" if clock else elapsed_label
    summary = "；".join(
        _strip_body_prefix(item)
        for item in (entry.get("summary") or [])[:2]
        if _strip_body_prefix(item)
    )
    summary_suffix = f" | {summary}" if summary else ""
    return f"- [{time_label}] {prefix}{entry.get('text', '')}{summary_suffix}"


def _manual_timeline_info_for_chunk(entries, chunk_start, chunk_end, limit=12):
    """取当前分块附近的人工时间轴，供 LLM 参考。"""
    nearby = [
        item for item in entries or []
        if chunk_start - MANUAL_TIMELINE_CHUNK_MARGIN_SEC <= item["start"] <= chunk_end + MANUAL_TIMELINE_CHUNK_MARGIN_SEC
    ]
    if not nearby:
        return "无"
    starred = [item for item in nearby if item.get("stars", 0) > 0]
    selected = starred[:limit]
    if len(selected) < limit:
        selected.extend([item for item in nearby if item not in selected][:limit - len(selected)])
    selected.sort(key=lambda item: item["start"])
    return "\n".join(_format_manual_entry_for_prompt(item) for item in selected)


def _attach_manual_timeline_to_chunks(chunks, entries):
    """把人工时间轴摘要挂到每个 SRT 分块上。"""
    for ch in chunks:
        ch["manual_timeline_info"] = _manual_timeline_info_for_chunk(
            entries, int(ch["start"]), int(ch.get("end", ch["start"] + CHUNK_SEC))
        )
    return chunks


def _manual_title_from_text(text):
    """从人工时间轴一句话生成短标题。"""
    clean = re.sub(r'[“”"（）()\[\]【】]', '', text or "")
    clean = re.sub(r'《(.+?)》', r'\1', clean)
    parts = [part.strip() for part in re.split(r'[，。；;：:、]', clean) if part.strip()]
    clean = parts[0] if parts else clean.strip()
    if len(clean) < 5 and len(parts) > 1:
        clean = f"{parts[0]}{parts[1]}"
    if len(clean) < 4:
        clean = re.sub(r'\s+', '', text or "")[:MAX_TOPIC_TITLE_CHARS]
    return (clean[:MAX_TOPIC_TITLE_CHARS] or "人工时间轴重点").strip()


def _manual_entry_matches_topic(entry, topic, margin=0):
    start = int(topic["start"]) - margin
    end = int(topic["end"]) + margin
    entry_start = int(entry["start"])
    entry_end = max(entry_start + 1, int(entry.get("end", entry_start + 1)))
    return entry_start < end and entry_end > start


def _is_manual_merge_target(topic):
    """人工重点只合并到真实话题；兜底/泛话题会吞掉重点，必须单独补话题。"""
    if topic.get("fallback"):
        return False
    if topic.get("source") == "manual_timeline":
        return True
    if _is_bad_topic_title(topic.get("title", "")):
        return False
    if topic.get("title") in _GENERIC_TOPIC_TITLES:
        return False
    text = " ".join([topic.get("title", "")] + list(topic.get("body") or []))
    compact = re.sub(r'\s+', '', text)
    if any(keyword in compact for keyword in _UNCUTTABLE_CONTENT_KEYWORDS):
        return False
    return True


def _merge_manual_timeline_topics(topics, entries):
    """后置对照优化时间轴；命中只附证据，遗漏候选必须再次复核。"""
    if not entries:
        return topics
    for topic in topics:
        if not _is_manual_merge_target(topic):
            continue
        matched = [entry for entry in entries if _manual_entry_matches_topic(entry, topic)]
        if not matched:
            continue
        existing_entries = list(topic.get("manual_timeline") or [])
        for entry in matched:
            if entry not in existing_entries:
                existing_entries.append(entry)
        topic["manual_stars"] = max(
            [topic.get("manual_stars", 0)]
            + [entry.get("stars", 0) for entry in existing_entries]
        )
        topic["manual_timeline"] = existing_entries
        body = list(topic.get("body") or [])
        for entry in matched:
            if entry.get("stars", 0) <= 0:
                continue
            stars = "⭐" * min(entry.get("stars", 0), 5)
            line = f"●人工时间轴{stars}：{fmt_time(entry['start'])} {entry['text']}"
            if line not in body:
                body.append(line)
        topic["body"] = body

    for entry in entries:
        optimized = entry.get("source") == "optimized_manual_timeline"
        if entry.get("stars", 0) <= 0 and not optimized:
            continue
        if any(entry in (topic.get("manual_timeline") or []) for topic in topics):
            continue
        if any(_is_manual_merge_target(topic) and _manual_entry_matches_topic(entry, topic) for topic in topics):
            continue
        topic_start = (
            max(0, int(entry["start"]))
            if optimized
            else max(0, int(entry["start"]) - MANUAL_TIMELINE_TOPIC_PRE_SEC)
        )
        topic_end = (
            max(topic_start + 1, int(entry.get("end", topic_start + 1)))
            if optimized
            else int(entry["start"]) + MANUAL_TIMELINE_TOPIC_POST_SEC
        )
        topic = {
            "start": topic_start,
            "end": topic_end,
            "start_str": fmt_time(topic_start),
            "end_str": fmt_time(topic_end),
            "title": _manual_title_from_text(entry["text"]),
            "can_slice": False,
            "body": list(entry.get("summary") or []) + [
                f"●人工时间轴{'⭐' * min(entry.get('stars', 0), 5)}："
                f"{fmt_time(entry['start'])} {entry['text']}"
            ],
            "manual_stars": entry.get("stars", 0),
            "manual_timeline": [entry],
            "source": entry.get("source", "manual_timeline"),
            # 时间轴优化阶段只负责整理候选。首轮遗漏后必须再做一次独立复核，
            # 成功前不能把优化阶段的 ai_enriched 当作切片许可。
            "ai_enriched": False if optimized else bool(entry.get("ai_enriched")),
            "ai_focus_validated": False if optimized else bool(entry.get("ai_focus_validated")),
            "postcheck_pending": optimized,
            "reference_only": optimized,
            "publish_title": entry.get("publish_title"),
        }
        if not _is_duplicate_topic(topic, [old for old in topics if _is_manual_merge_target(old)]):
            topics.append(topic)
    topics.sort(key=lambda item: (item["start"], item["end"]))
    return topics


def _topic_srt_summary_lines(start, end, srt_segments, limit=12, bucket_sec=30):
    """把碎片字幕聚成带时间范围的短窗口，供 AI 核对事件与边界。"""
    if not srt_segments:
        return []
    related = [
        (seg_start, seg_end, text)
        for seg_start, seg_end, text in srt_segments
        if seg_end >= start and seg_start <= end
    ]
    if not related:
        return []

    buckets = {}
    for seg_start, seg_end, text in related:
        key = max(0, int((max(start, seg_start) - start) // bucket_sec))
        bucket = buckets.setdefault(key, {
            "start": max(start, seg_start),
            "end": min(end, seg_end),
            "texts": [],
        })
        bucket["start"] = min(bucket["start"], max(start, seg_start))
        bucket["end"] = max(bucket["end"], min(end, seg_end))
        compact = re.sub(r'\s+', '', text or '')
        if compact and (not bucket["texts"] or bucket["texts"][-1] != compact):
            bucket["texts"].append(compact)

    windows = [buckets[key] for key in sorted(buckets)]
    if len(windows) <= limit:
        selected = windows
    elif limit <= 1:
        selected = [windows[len(windows) // 2]]
    else:
        indexes = sorted({round(i * (len(windows) - 1) / (limit - 1)) for i in range(limit)})
        selected = [windows[index] for index in indexes]

    lines = []
    seen = set()
    for window in selected:
        compact = "".join(window["texts"])
        if not compact or compact in seen:
            continue
        seen.add(compact)
        if len(compact) > 180:
            compact = compact[:180] + "…"
        lines.append(
            f"·字幕核查：{fmt_time(window['start'])}-{fmt_time(window['end'])} {compact}"
        )
    return lines


def _topic_danmaku_reference_line(start, end, peaks):
    """生成话题附近弹幕峰值依据。"""
    if not peaks:
        return None
    candidates = [
        (peak_start, density)
        for peak_start, density in peaks
        if peak_start + DANMAKU_WINDOW >= start and peak_start <= end
    ]
    if not candidates:
        return None
    peak_start, density = max(candidates, key=lambda item: item[1])
    return f"·弹幕依据：{fmt_time(peak_start)} 附近峰值约 {int(density)} 条/分钟"


def _topic_danmaku_reference_lines(start, end, peaks, limit=3):
    """保留相隔较远的多个峰值，让 AI 能识别人工记录中的并列事件。"""
    candidates = [
        (peak_start, density)
        for peak_start, density in peaks or []
        if peak_start + DANMAKU_WINDOW >= start and peak_start <= end
    ]
    selected = []
    for peak_start, density in sorted(candidates, key=lambda item: item[1], reverse=True):
        if any(abs(peak_start - old_start) < DANMAKU_WINDOW for old_start, _ in selected):
            continue
        selected.append((peak_start, density))
        if len(selected) >= limit:
            break
    return [
        f"·弹幕依据：{fmt_time(peak_start)} 附近峰值约 {int(density)} 条/分钟"
        for peak_start, density in sorted(selected)
    ]


def _topics_from_manual_timeline(
        entries, srt_segments=None, peaks=None, max_gap_sec=240,
        max_group_duration_sec=None):
    """基于字幕/弹幕生成话题，人工时间轴只作为辅助参考和校准。"""
    sorted_entries = sorted(entries or [], key=lambda item: item["start"])
    groups = []
    current = []
    for entry in sorted_entries:
        if entry.get("explicit_range"):
            if current:
                groups.append(current)
                current = []
            groups.append([entry])
            continue
        if not current:
            current = [entry]
            continue
        same_hour = int(entry["start"] // 3600) == int(current[-1]["start"] // 3600)
        within_group_duration = (
            max_group_duration_sec is None
            or entry["start"] - current[0]["start"] <= max_group_duration_sec
        )
        if (
            same_hour
            and within_group_duration
            and entry["start"] - current[-1]["start"] <= max_gap_sec
        ):
            current.append(entry)
        else:
            groups.append(current)
            current = [entry]
    if current:
        groups.append(current)

    topics = []
    for group in groups:
        starred_entries = [item for item in group if item.get("stars", 0) > 0]
        if starred_entries and any(item.get("alignment_score") is not None for item in starred_entries):
            title_entry = max(
                starred_entries,
                key=lambda item: (
                    float(item.get("alignment_score") or 0),
                    len(str(item.get("text", ""))),
                ),
            )
        else:
            title_entry = starred_entries[0] if starred_entries else group[0]
        explicit_end = group[0].get("end") if len(group) == 1 and group[0].get("explicit_range") else None
        if explicit_end is not None:
            start = max(0, int(group[0]["start"]))
            end = max(start + 1, int(explicit_end))
        else:
            start = max(0, int(group[0]["start"]) - (MANUAL_TIMELINE_TOPIC_PRE_SEC if title_entry.get("stars", 0) else 0))
            end = int(group[-1]["start"]) + (MANUAL_TIMELINE_TOPIC_POST_SEC if title_entry.get("stars", 0) else 120)
        body = []
        body.extend(_topic_danmaku_reference_lines(start, end, peaks or []))
        body.extend(_topic_srt_summary_lines(start, end, srt_segments or []))
        for item in group:
            time_label = fmt_time(item["start"])
            if item.get("stars", 0) > 0:
                stars = "⭐" * min(item.get("stars", 0), 5)
                body.append(f"●人工时间轴{stars}：{time_label} {item['text']}")
            else:
                body.append(f"·时间轴：{time_label} {item['text']}")
            if item.get("reference_publish_title"):
                body.append(f"·参考投稿标题（仅供核对）：{item['reference_publish_title']}")
        topic = {
            "start": start,
            "end": end,
            "start_str": fmt_time(start),
            "end_str": fmt_time(end),
            "title": _manual_title_from_text(title_entry["text"]),
            "can_slice": False,
            "body": body,
            "manual_stars": max(item.get("stars", 0) for item in group),
            "manual_timeline": group,
            "source": "subtitle_danmaku_with_manual_reference",
        }
        topics.append(topic)
    return topics


def load_api_config():
    """读取 API 配置，优先用 AutoSlice 自己的，否则用 Claude 的"""
    # 1. 先试 AutoSlice 独立配置
    auto_cfg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "api_config.json")
    if os.path.exists(auto_cfg):
        with open(auto_cfg) as f:
            cfg = json.load(f)
        return cfg["base_url"], cfg["token"], cfg.get("model", LLM_MODEL)

    # 2. 回退到 Claude 配置
    cfg_path = os.path.expanduser(r"~\.claude\settings.json")
    with open(cfg_path) as f:
        cfg = json.load(f)
    return cfg["env"]["ANTHROPIC_BASE_URL"], cfg["env"]["ANTHROPIC_AUTH_TOKEN"], LLM_MODEL


# ============================================================
# Step 1: FunASR 自动转录 (复用 core.py 逻辑，降级到 CPU)
# ============================================================

def _prepare_funasr_environment():
    """FunASR 模型只使用本地缓存，避免 Web 任务在网络下载失败时长时间卡住。"""
    os.environ.setdefault("MODELSCOPE_LOCAL_ONLY", "1")


def _funasr_model_cache_candidates():
    return [FUNASR_CACHE_MODEL_DIR]


def _resolve_funasr_model_source():
    """优先返回本地缓存目录，避免 AutoModel 用模型 ID 访问 ModelScope API。"""
    for model_dir in _funasr_model_cache_candidates():
        if model_dir and os.path.isfile(os.path.join(model_dir, "model.pt")):
            return model_dir
    return FUNASR_MODEL


def _load_funasr_model(AutoModel, progress_callback=None, device=None):
    """加载 FunASR 模型；本地无缓存时抛出带排查提示的异常。"""
    _prepare_funasr_environment()
    selected_device = device or FUNASR_DEFAULT_DEVICE
    model_source = _resolve_funasr_model_source()
    try:
        return AutoModel(
            model=model_source,
            device=selected_device,
            disable_update=True,
        )
    except Exception as exc:
        message = (
            "FunASR 模型加载失败：本地 ModelScope 缓存不可用，或模型下载被网络/SSL 中断。"
            "请先生成同名 SRT，或在网络正常时预下载 FunASR 模型后重试。"
        )
        if progress_callback:
            progress_callback(f"{message} 原始错误: {exc}", 0, 100)
        raise RuntimeError(message) from exc


def ensure_srt(video_path, progress_callback=None):
    """确保 SRT 存在，没有则用 FunASR 生成"""
    srt_path = video_path[:-4] + ".srt"
    if os.path.exists(srt_path) and os.path.getsize(srt_path) > 0:
        if progress_callback:
            progress_callback("SRT 已存在，跳过转录", 5, 100)
        return srt_path

    if progress_callback:
        progress_callback("FunASR 转录中...", 5, 100)

    import subprocess as sp, json as _json, uuid

    try:
        from funasr import AutoModel
    except ImportError:
        if progress_callback:
            progress_callback("FunASR 未安装", 0, 100)
        return None

    wav_path = video_path[:-4] + f"_asr_{uuid.uuid4().hex[:6]}.wav"

    # 提取音频
    sp.run(["ffmpeg", "-i", video_path, "-vn", "-acodec", "pcm_s16le",
            "-ar", "16000", "-ac", "1", "-y", wav_path],
           check=True, stdout=sp.PIPE, stderr=sp.DEVNULL,
           encoding="utf-8", errors="replace")

    try:
        if progress_callback:
            progress_callback(f"加载 FunASR 模型({FUNASR_DEFAULT_DEVICE})...", 10, 100)

        model = _load_funasr_model(AutoModel, progress_callback=progress_callback)
    except Exception:
        if os.path.exists(wav_path):
            os.remove(wav_path)
        raise

    # 获取时长
    try:
        probe = sp.run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", wav_path],
                       stdout=sp.PIPE, stderr=sp.DEVNULL)
        dur = float(_json.loads(probe.stdout.decode("utf-8", errors="replace"))
                    .get("format", {}).get("duration", 0))
    except:
        dur = os.path.getsize(wav_path) / (16000 * 2)

    # 分段转录
    chunk_dur = 120.0
    all_segs = []
    n_chunks = max(1, int((dur + chunk_dur - 0.001) / chunk_dur))
    streamer_name = _infer_streamer_name(video_path)

    for i in range(n_chunks):
        start_t = i * chunk_dur
        if progress_callback:
            pct = 10 + int((i / n_chunks) * 80)
            progress_callback(f"转录中 ({i+1}/{n_chunks})...", pct, 100)

        chunk_file = wav_path if n_chunks == 1 else video_path[:-4] + f"_chunk_{i}.wav"
        if n_chunks > 1:
            sp.run(["ffmpeg", "-y", "-ss", str(start_t), "-i", wav_path,
                    "-t", str(chunk_dur), "-acodec", "pcm_s16le",
                    "-ar", "16000", "-ac", "1", chunk_file],
                   check=True, stdout=sp.PIPE, stderr=sp.DEVNULL,
                   encoding="utf-8", errors="replace")

        try:
            result = model.generate(input=chunk_file, batch_size_s=60, disable_pbar=True)
            if result:
                for item in result:
                    text = item.get("text", "").strip()
                    ts = item.get("timestamp", [])
                    if text and ts:
                        all_segs.extend(_segments_from_funasr_result(
                            text,
                            ts,
                            offset=start_t,
                            streamer_name=streamer_name,
                        ))
            if chunk_file != wav_path:
                os.remove(chunk_file)
        except:
            if chunk_file != wav_path and os.path.exists(chunk_file):
                os.remove(chunk_file)

    os.remove(wav_path)

    if not all_segs:
        return None

    # 写 SRT
    with open(srt_path, "w", encoding="utf-8") as f:
        for idx, (ss, se, txt) in enumerate(all_segs, 1):
            if len(txt) < 2:
                continue
            f.write(f"{idx}\n{_srt_time(ss)} --> {_srt_time(se)}\n{txt}\n\n")

    if progress_callback:
        progress_callback(f"转录完成 ({len(all_segs)} 条)", 90, 100)

    return srt_path


def _srt_time(s):
    h, m = divmod(int(s), 3600)
    m, sec = divmod(m, 60)
    ms = int((s - int(s)) * 1000)
    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"


# ============================================================
# Step 2: 弹幕密度分析
# ============================================================

class DanmakuDensitySeries(list):
    """等间隔弹幕密度窗口，并保留按整场时长计算的真实平均值。"""

    def __init__(self, windows=(), average_density=0.0, message_count=0, duration=0.0):
        super().__init__(windows)
        self.average_density = float(average_density)
        self.message_count = int(message_count)
        self.duration = float(duration)


def _average_danmaku_density(windows):
    """优先读取整场真实均值；普通列表继续兼容既有测试和调用方。"""
    if hasattr(windows, "average_density"):
        return float(windows.average_density)
    densities = [density for _, density in windows or []]
    return sum(densities) / len(densities) if densities else 0.0


def analyze_danmaku(ass_path):
    """按固定步长统计 60 秒滑动窗口，不丢弃低密度样本。"""
    if not ass_path or not os.path.exists(ass_path):
        return DanmakuDensitySeries()

    timestamps = []
    with open(ass_path, encoding="utf-8") as f:
        for line in f:
            if line.startswith("Dialogue:"):
                parts = line.split(",", 3)
                if len(parts) < 2:
                    continue
                try:
                    h, m, s = parts[1].strip().split(":")
                    timestamps.append(int(h) * 3600 + int(m) * 60 + float(s))
                except (TypeError, ValueError):
                    continue

    if not timestamps:
        return DanmakuDensitySeries()

    timestamps.sort()
    duration = max(float(timestamps[-1]), float(DANMAKU_WINDOW))
    average_density = len(timestamps) * 60.0 / duration
    windows = []
    for start in range(0, int(math.floor(timestamps[-1])) + 1, DANMAKU_WINDOW_STEP):
        left = bisect.bisect_left(timestamps, start)
        right = bisect.bisect_left(timestamps, start + DANMAKU_WINDOW)
        windows.append((start, right - left))

    return DanmakuDensitySeries(
        windows,
        average_density=average_density,
        message_count=len(timestamps),
        duration=duration,
    )


# ============================================================
# Step 3: SRT 解析 + 分块
# ============================================================

def parse_srt_text(srt_path):
    """解析 SRT，去空格，返回 [(start_s, end_s, text), ...]，并修复明显异常时间戳。"""
    return [
        (start_s, end_s, text)
        for start_s, end_s, text in _load_repaired_srt_segments(srt_path)
        if _subtitle_text_size(text) >= 2
    ]


def chunk_srt(segs, peaks, chunk_sec=CHUNK_SEC):
    """将 SRT 按时间分块，每块附带弹幕密度信息"""
    if not segs:
        return []
    avg_density = _average_danmaku_density(peaks)

    chunks = []
    chunk_start = segs[0][0]
    current_texts = []

    for item in segs:
        if len(item) == 3:
            start_s, end_s, text = item
        else:
            start_s, text = item
            end_s = start_s
        if start_s - chunk_start > chunk_sec:
            if current_texts:
                chunks.append(_make_chunk(chunk_start, current_texts, peaks, avg_density))
            chunk_start = start_s
            current_texts = []
        time_label = fmt_time(start_s) if end_s <= start_s + 1 else f"{fmt_time(start_s)}－{fmt_time(end_s)}"
        current_texts.append(f"[{time_label}] {text}")

    if current_texts:
        chunks.append(_make_chunk(chunk_start, current_texts, peaks, avg_density))

    return chunks


def _make_chunk(chunk_start, texts, peaks, avg_density=0):
    text_block = "\n".join(texts)
    chunk_end = chunk_start + CHUNK_SEC
    nearby_peaks = [(s, d) for s, d in peaks if chunk_start - 60 <= s <= chunk_end + 60]
    if nearby_peaks:
        max_d = max(d for _, d in nearby_peaks)
        ratio = max_d / avg_density if avg_density > 0 else 1.0
        danmaku_info = f"[弹幕: 本段峰值{max_d}条/分钟 = {ratio:.1f}倍平均 | 全场平均={avg_density:.0f}]"
    else:
        danmaku_info = f"[弹幕: 本段无峰值, 远低于全场平均{avg_density:.0f}]"
    return {
        "start": chunk_start,
        "end": chunk_end,
        "text": text_block,
        "danmaku_info": danmaku_info,
        "has_peaks": len(nearby_peaks) > 0,
    }


def _load_title_style_profile(profile_path=None):
    """读取历史投稿标题风格配置；配置损坏时安全降级为空配置。"""
    path = profile_path or TITLE_STYLE_PROFILE_PATH
    empty = {"source": {}, "rules": [], "examples": []}
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError, TypeError):
        return empty
    if not isinstance(payload, dict):
        return empty

    rules = []
    for rule in payload.get("rules") or []:
        text = re.sub(r'\s+', ' ', str(rule)).strip()
        if text and text not in rules:
            rules.append(text)

    examples = []
    seen_titles = set()
    for item in payload.get("examples") or []:
        if isinstance(item, str):
            item = {"title": item}
        if not isinstance(item, dict):
            continue
        title = re.sub(r'\s+', ' ', str(item.get("title", ""))).strip()
        if (
            not title.startswith(PUBLISH_TITLE_PREFIX)
            or "直播回放" in title
            or title in seen_titles
            or len(title) > MAX_PUBLISH_TITLE_CHARS
        ):
            continue
        tags = [
            re.sub(r'\s+', ' ', str(tag)).strip()
            for tag in item.get("tags") or []
            if str(tag).strip()
        ]
        examples.append({
            "title": title,
            "tags": tags,
            "source": str(item.get("source", "history")).strip() or "history",
        })
        seen_titles.add(title)
    return {
        "source": payload.get("source") if isinstance(payload.get("source"), dict) else {},
        "rules": rules,
        "examples": examples,
    }


def _select_title_style_examples(context_text, profile=None, limit=TITLE_STYLE_EXAMPLE_LIMIT):
    """按当前话题语义选择少量同类真实标题，避免把全部历史标题塞进提示词。"""
    profile = profile or _load_title_style_profile()
    examples = profile.get("examples") or []
    if not examples or limit <= 0:
        return []
    context = str(context_text or "").lower()
    active_tags = {
        tag
        for tag, keywords in _TITLE_STYLE_TAG_KEYWORDS.items()
        if any(keyword.lower() in context for keyword in keywords)
    }
    scored = []
    for index, item in enumerate(examples):
        tags = set(item.get("tags") or [])
        score = len(active_tags & tags) * 10
        if item.get("source") == "recent":
            score += 2
        elif item.get("source") == "high_play":
            score += 1
        scored.append((-score, index, item))
    scored.sort(key=lambda row: (row[0], row[1]))
    return [item for _, _, item in scored[:limit]]


def _build_title_style_prompt(context_text="", compact=False):
    """把账号历史标题规律压缩成可复用的提示词片段。"""
    profile = _load_title_style_profile()
    rule_limit = 4 if compact else 8
    example_limit = 4 if compact else TITLE_STYLE_EXAMPLE_LIMIT
    rules = (profile.get("rules") or [])[:rule_limit]
    examples = _select_title_style_examples(context_text, profile=profile, limit=example_limit)
    if not rules and not examples:
        return ""
    source = profile.get("source") or {}
    reviewed_count = source.get("reviewed_submission_count")
    basis = f"已审阅账号 {reviewed_count} 条投稿后归纳" if reviewed_count else "由账号历史投稿归纳"
    lines = [
        f"{basis}。下面只给少量同类真实标题用于学习语气和结构，禁止照抄旧事件：",
    ]
    lines.extend(f"- 规则：{rule}" for rule in rules)
    lines.extend(f"- 样本：{item['title']}" for item in examples)
    return "\n".join(lines)


# ============================================================
# Step 4: LLM 分析
# ============================================================

SYSTEM_PROMPT = """你是直播内容时间轴整理+切片决策助手。你只能分析【当前分块】里给出的字幕和弹幕密度，不要引用、复述或补写当前分块之外的内容。

## 目标风格

输出要像人工整理的“逐话题时间轴”：每个话题有时间范围，下面用 ·/● 写详细要点。不要写空洞总结，要写出具体发生了什么、主播怎么说、观众/弹幕有什么反应。

## 覆盖范围：全程时间轴，不是只挑爆点

- 当前分块里只要有连续讲话，就整理成 1-2 个核心话题；内容特别密集时最多 3 个
- 普通聊天、过渡、游戏过程、读弹幕、感谢礼物也要写进时间轴
- 不要因为“弹幕不高/不适合切”就输出“无明显话题”
- 只有当前分块几乎没有有效讲话、全是沉默/音乐/无法理解的碎词时，才允许输出“无明显话题”
- ✂️ 只表示“值得自动切片”，不是“是否写进报告”；不值得切也必须写进报告
- 禁止输出草稿、分析过程、候选列表、话题划分说明；只输出最终条目

## 核心原则：相对密度判断

- 密度 > 全场平均 → 观众活跃 → 话题标题末尾加 ✂️
- 密度 ≈ 或 < 全场平均 → 常态/冷场 → 不加 ✂️
- 如果字幕内容平淡、只有游戏台词/沉默/机械复读，即使有短暂弹幕也谨慎不切

## 时间范围硬约束

- 所有时间都是视频内时间/播放进度（从 0:00:00 开始），不是真实钟点时间

- 输出的每个话题时间必须落在本次提示给出的“允许时间范围”内
- 不允许输出历史分块、示例分块、其它视频片段的时间戳
- 如果事件跨越分块，只写当前分块内能确认的部分
- 不要漏掉当前分块的主要讲话内容；能归纳就归纳成“日常闲聊/游戏过程/读弹幕互动”等普通话题
- 话题开始必须包含触发事件：由 SC、观众长留言、礼物、提问或外部视频引发的讨论，要从念出触发内容或明确引出问题处开始；结束要覆盖最后一轮回应，不能只框弹幕爆点一句

## 输出格式：只输出 JSON，不要输出 Markdown

**关键要求：**
- 只输出一个 JSON 对象，不要输出解释、草稿、分析过程、代码块或 Markdown
- JSON 格式严格如下：
{"topics":[{"start":"0:04:00","end":"0:08:00","title":"话题标题","publish_title":"【泽音】具体事件钩子👀结果或反差","can_slice":false,"points":["具体要点，写清楚事情经过","补充细节"]}]}
- 时间戳精确到秒，格式 `H:MM:SS`，例如 `0:04:00`
- 标题 5-15 字，概括核心内容，可加合适 emoji
- 每个话题都要给出 publish_title，供程序在弹幕筛选后直接用于投稿；它不影响 can_slice 判断
- publish_title 固定以“【泽音】”开头，根据当前事件选择“事件+原话”“SC+回应”“观看对象+反应”“短句头条”或温情原话等结构
- 不要机械地让每个 publish_title 都使用“结果、随后、当场”；适量使用符合语义的 emoji，具体账号风格和真实样本见当前提示末尾
- 禁止把 publish_title 写成“直播精彩片段”“日常聊天”等空标题；不得编造字幕和要点中没有的事件、原话或结果
- 每个话题 2-6 条 points；礼物、弹幕爆点、观众金句可直接写进 points
- 遇到 SC/醒目留言/观众长留言时，尽量保留观众开头对主播的称呼，例如“音姐……”“音音……”“麻麻……”
- 不要编造字幕里没有的信息
- 不要输出任何示例内容
- 不要解释为什么切或不切，不要在 points 里写弹幕密度判断、格式说明、推理过程；切片只用 can_slice 表示
- 不要写“我决定/现在写/标题可以/只能基于字幕/注意起始时间”等模型思考过程"""


class LLMResponseTruncatedError(RuntimeError):
    """LLM 因输出额度耗尽而未返回完整结构化结果。"""


class LLMStructuredOutputError(RuntimeError):
    """LLM 返回了文本，但没有可解析的完整 JSON。"""


def _llm_response_has_complete_json(content):
    """判断响应中是否包含可解析的完整 JSON。"""
    return bool(content and _extract_json_payload(content) is not None)


def call_llm(prompt, max_tokens=LLM_MAX_TOKENS):
    base_url, token, model = load_api_config()
    # 自动判断 API 格式：sk- 开头 = OpenAI 兼容，否则 = Anthropic
    if token.startswith("sk-"):
        # OpenAI 兼容格式 (opencode.ai 等)
        resp = requests.post(
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "temperature": 0.3,
            },
            timeout=LLM_REQUEST_TIMEOUT,
            proxies={"http": None, "https": None},
        )
        resp.raise_for_status()
        data = resp.json()
        # 兼容不同 OpenAI 响应格式
        choice = data.get("choices", [{}])[0]
        finish_reason = choice.get("finish_reason")
        content = choice.get("message", {}).get("content", "")
        if not content:
            reasoning_content = choice.get("message", {}).get("reasoning_content", "")
            # DeepSeek Pro 可能把主要 token 用在 reasoning_content。
            # 只允许 reasoning_content 中的完整 JSON 进入后续结构化解析，禁止把普通推理文本当报告。
            if _llm_response_has_complete_json(reasoning_content):
                content = reasoning_content
        if not content:
            content = choice.get("text", "")
        if finish_reason == "length" and not _llm_response_has_complete_json(content):
            raise LLMResponseTruncatedError(
                f"DeepSeek Pro 输出被截断(max_tokens={max_tokens})，将缩短提示后重试"
            )
        if not content:
            raise RuntimeError(f"API 返回格式不兼容: {json.dumps(data)[:300]}")
        return content
    else:
        # Anthropic 兼容格式
        resp = requests.post(
            f"{base_url}/messages",
            headers={
                "x-api-key": token,
                "Content-Type": "application/json",
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "temperature": 0.3,
            },
            timeout=LLM_REQUEST_TIMEOUT,
            proxies={"http": None, "https": None},
        )
        resp.raise_for_status()
        data = resp.json()
        content = ""
        for block in data.get("content", []):
            if block["type"] == "text":
                content = block["text"]
                break
        if data.get("stop_reason") == "max_tokens" and not _llm_response_has_complete_json(content):
            raise LLMResponseTruncatedError(
                f"DeepSeek Pro 输出被截断(max_tokens={max_tokens})，将缩短提示后重试"
            )
        return content


def _short_llm_error(error):
    """把 LLM/API 异常压缩成适合进度显示的一行。"""
    if isinstance(error, requests.HTTPError) and error.response is not None:
        text = (error.response.text or "").replace("\n", " ").strip()
        return f"HTTP {error.response.status_code}: {text[:160]}"
    return str(error)[:200]


def _is_retryable_llm_error(error):
    """判断是否适合重试：服务端 5xx、限流 429、连接/超时。"""
    if isinstance(error, (LLMResponseTruncatedError, LLMStructuredOutputError)):
        return True
    if isinstance(error, (requests.Timeout, requests.ConnectionError)):
        return True
    if isinstance(error, requests.HTTPError) and error.response is not None:
        status = error.response.status_code
        return status == 429 or 500 <= status < 600
    return False


def _call_llm_with_retry(prompt, compact_prompt=None, max_tokens=LLM_MAX_TOKENS,
                         compact_max_tokens=LLM_COMPACT_MAX_TOKENS, attempts=None,
                         sleep_func=time.sleep, progress_callback=None,
                         progress_label="API", progress_step=0, require_json=False):
    """对临时性 LLM/API 错误做退避重试；连续失败后再抛出。"""
    total_attempts = attempts or (len(LLM_RETRY_DELAYS) + 1)
    last_error = None
    for attempt in range(total_attempts):
        use_compact = compact_prompt is not None and (
            attempt >= 2 or isinstance(last_error, (LLMResponseTruncatedError, LLMStructuredOutputError))
        )
        active_prompt = compact_prompt if use_compact else prompt
        active_tokens = compact_max_tokens if use_compact else max_tokens
        try:
            result = call_llm(active_prompt, max_tokens=active_tokens)
            if require_json and _extract_json_payload(result) is None:
                raise LLMStructuredOutputError("DeepSeek Pro 未返回完整 JSON，将改用紧凑提示重试")
            return result
        except Exception as e:
            last_error = e
            if not _is_retryable_llm_error(e) or attempt >= total_attempts - 1:
                raise
            delay = LLM_RETRY_DELAYS[min(attempt, len(LLM_RETRY_DELAYS) - 1)]
            compact_note = "，改用紧凑提示" if use_compact else ""
            if progress_callback:
                progress_callback(
                    f"{progress_label} 失败{compact_note}，{delay}s 后重试 "
                    f"({attempt + 1}/{total_attempts}): {_short_llm_error(e)}",
                    progress_step, 100,
                )
            sleep_func(delay)
    raise last_error


def _build_chunk_prompt(ch, index, total, compact=False, streamer_name="主播"):
    """构造字幕/弹幕首轮 prompt；人工时间轴不得参与这一轮。"""
    chunk_start = ch["start"]
    chunk_end = ch.get("end", ch["start"] + CHUNK_SEC)
    text_limit = LLM_COMPACT_TEXT_CHARS if compact else LLM_FULL_TEXT_CHARS
    title_style_prompt = _build_title_style_prompt(ch.get("text") or "", compact=compact)
    if compact:
        prompt_head = (
            "你是直播逐话题时间轴整理助手。只分析当前分块，只输出最终话题条目；"
            "当前分块有连续讲话时只整理成1-2个核心话题，内容特别密集最多3个；普通闲聊/游戏过程也要写；"
            "只有几乎无有效讲话才输出“无明显话题”。"
            "can_slice只给值得自动切片的段，不值得切也要写进报告。"
            "SC、长留言、礼物或提问引发的讨论必须从触发内容开始，到最后一轮回应结束。"
            "每个话题都要给publish_title：固定以【泽音】开头，根据历史风格选择事件+原话、SC+回应、"
            "观看反应或短句头条等合适结构，不要每条都机械写‘结果/当场’；禁止空泛标题和编造。"
            "不要解释规则、不要写弹幕密度判断、不要写推理过程、不要写候选列表。"
            "只输出JSON对象：{\"topics\":[{\"start\":\"0:00:00\",\"end\":\"0:05:00\",\"title\":\"话题标题\","
            "\"publish_title\":\"【泽音】具体事件钩子👀结果或反差\",\"can_slice\":false,\"points\":[\"具体要点\"]}]}。\n\n"
        )
    else:
        prompt_head = SYSTEM_PROMPT
    prompt = (
        f"{prompt_head}\n\n"
        f"## 当前分块\n"
        f"- 分块编号: 第{index + 1}/{total}块\n"
        f"- 允许时间范围: {fmt_time(chunk_start)} - {fmt_time(chunk_end)}\n"
        f"- 主播展示称呼: {streamer_name or '主播'}（报告里不要写泛称“主播”，用这个称呼代替）\n"
        f"- 粉丝常用称呼: {'、'.join(STREAMER_FAN_ALIASES)}；如果观众留言/SC 原句以这些称呼开头，要保留原话称呼\n"
        f"- 弹幕信息: {ch['danmaku_info']}\n\n"
        f"## 账号历史投稿标题风格\n{title_style_prompt or '无可用历史样本；只根据当前证据写具体标题'}\n\n"
        f"## 字幕:\n{ch['text'][:text_limit]}"
    )
    return prompt, chunk_start, chunk_end

# ============================================================
# LLM 响应解析与去重
# ============================================================

_HEADING_RE = re.compile(
    r'^\s*(?:#{1,6}\s*)?(?:[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳]|\d+[.)、])?\s*\['
    r'(\d{1,2}:\d{2}(?::\d{2})?)\s*[-–—－~～至]+\s*'
    r'(\d{1,2}:\d{2}(?::\d{2})?)\]\s*(.+?)\s*$'
)
_NO_SLICE_HINTS = ("不切", "不加标记", "不建议切", "不要切", "不适合切")
_PLACEHOLDER_TITLES = (
    "无明显话题", "话题标题", "下一个话题", "未命名片段", "从字幕看",
    "其他话题", "下一段", "可能的切分", "根据要求",
)
_GENERIC_TOPIC_TITLES = ("日常聊天互动", "感谢礼物互动", "视频评论讨论", "游戏关卡挑战")
MAX_TOPIC_TITLE_CHARS = 24
_META_TITLE_KEYWORDS = (
    "考虑分成", "考虑输出", "更好的方式", "更合理", "我们仔细", "仔细分析",
    "每个时间段", "提取可理解", "然后紧接着", "第二个话题", "第一个话题",
    "可能的划分", "话题划分", "标题：", "标题:", "基于时间顺序",
    "建议这样", "字幕原文", "让我们详细解析", "我们还需要", "先理解字幕",
    "所以整体", "大致内容", "可能的整理", "从字幕看", "主要内容",
    "第一part", "第二part", "第三个短", "话题一", "话题二",
    "最佳方式", "我们仔细看", "时间线变化", "我们分析", "连续讲话",
    "规划话题结构", "输出时不要写Part", "一个合理的方法", "合理的方法",
    "观察事件",
    "中文（问候等）", "现在规划", "可能的最佳划分", "最佳划分",
    "具体分段", "梳理字幕", "连续意思", "输出最终条目", "读懂字幕",
    "具体要点", "比如",
    "这部分明显", "继续讨论这个视频", "继续这段剧情", "總結話題",
    "总结话题", "根據字幕", "可以劃分", "劃分為", "输出内容",
    "严格按照格式", "标题加emoji", "最终输出", "感谢一个礼物",
    "礼物、弹幕爆点", "确保时间戳", "让我们仔细构建", "最终输出示例",
    "注意称呼",
    "points:", "points：", "title:", "title：", "要点：", "要点:",
    "重新考虑分块内容", "我们先把内容分几个话题", "那么我们定义",
    "整体时间段", "让我们尝试提取话题", "我们确保每个话题",
    "这段讨论", "这段继续", "内容有些混乱",
    "约2分", "划分建议", "先构思", "topic1", "topic2",
    "观察内容", "更仔细看",
    "我们规划话题", "先考虑can", "建议分成两个话题",
    "先整理出具体的时间段", "根据人工时间轴", "最终JSON", "最终 JSON",
    "我们尝试解读字幕",
    "人工时间轴参考", "观察时间戳", "需要写点", "我们看内容",
    "我们来看内容", "根据内容推断边界",
    "其他话题", "这些人工时间轴", "与上一段有重叠", "下一段",
    "可能的切分", "我们来确定话题", "根据要求", "可能音音",
    "观众可能", "一个合理的划分",
)
_META_BODY_KEYWORDS = (
    "但注意", "注意：", "注意:", "我们需要", "我们应该", "我应该", "我倾向", "是否应该",
    "输出格式", "输出如下", "不要输出", "程序会自动", "允许时间范围", "当前分块",
    "时间范围", "时间戳必须", "格式：", "格式`", "根据原则", "指令说", "题目说", "不能假设",
    "只需输出", "只需要输出", "最后，检查", "Markdown代码块", "Markdown 代码块",
    "这里有一段字幕", "后面没有字幕", "所以我们", "因此，输出", "考虑一下",
    "例如：", "例如:", "由于弹幕密度", "因为弹幕密度", "弹幕密度", "全场平均", "本段无峰值",
    "所以输出", "现在写输出", "要点要写", "所以整理信息", "所以话题标题", "目标风格",
    "但具体", "具体有哪些点", "从字幕中提取", "再看弹幕信息", "弹幕反应？", "没有具体弹幕内容",
    "不加✂️", "加✂️", "可能是", "似乎", "或许", "我们可以", "最好合并", "时间太短",
    "内容要点", "我们还需要考虑", "其他可能性", "可以提一句", "由于字幕", "所以只有一个话题",
    "根据格式", "如果有礼物", "才用●", "不用写", "如果无明显话题", "没有弹幕爆点",
    "弹幕爆点信息", "无爆点", "弹幕高能", "密度达", "峰值", "弹幕信息", "低于平均",
    "高于平均", "不活跃", "这里有明显话题", "最后，如果", "尽量简洁",
    "只能写基于字幕", "基于字幕", "标题可以", "标题更简洁", "优先简洁",
    "现在写", "我决定", "决定输出", "注意起始时间", "弹幕高密度",
    "要点用", "没有特别弹幕爆点", "这里没有明显", "需要确保", "有依据",
    "很好地覆盖", "再检查", "写要点", "最终答案", "规则要求", "按照示例",
    "很难分开", "检查要求", "要点2", "可以考虑更具体", "也可以分", "也许可以写",
    "更符合实际", "原字幕没有说完", "忠实于数据", "不符合常识", "每条对应真实时间",
    "字幕未显示", "我们谨慎", "我们写", "可以不用●",
    "看第二段", "第一段", "第二段", "第三段", "第四段", "同样，", "同样，1:",
    "我们说", "这显然", "时间重叠", "重新组织", "按时间顺序梳理", "接着在",
    "从“", "开始到", "我们取到", "最好重新", "约4:", "约3:", "约2:", "约1:",
    "根据字幕", "话题可以", "话题划分", "可能的划分", "通常做法", "首先，决定",
    "分析字幕内容", "从内容看", "关键词", "不能输出", "建议3个话题", "建议3个",
    "考虑时间顺序", "考虑实际讲话内容", "第五段", "时间轴整合", "自然分段",
    "注意1:", "允许时间", "超出范围", "我们尽量", "有很多讲话",
    "考虑分成", "考虑输出", "输出两个话题", "更好的方式", "更合理", "更合理的是", "更合理地", "然后紧接着",
    "我们仔细分析", "仔细分析每个时间段", "提取可理解", "这里明显", "我可以这样",
    "可以这样", "整体上，这是", "第二个话题", "第一个话题", "标题：", "标题:",
    "整个分块", "前部分", "我们只能", "不能用", "超过", "最后一段开始",
    "首先，覆盖", "覆盖从", "要注意", "直接输出最终条目", "最好基于时间顺序",
    "基于时间顺序整理", "建议这样划分", "子部分：", "子部分:", "字幕原文",
    "但内容不确定", "写具体", "从语义看", "可以作为一个整体话题", "为了简洁",
    "注意，我们", "分话题", "建议分成以下", "字幕分析", "总体来说",
    "比较好的做法", "我建议", "我考虑", "我们也可以", "但中间有间隔",
    "我们还需要写出具体要点", "让我们详细解析", "提取关键点", "可能游戏相关",
    "先理解字幕", "基于此", "要点要具体", "要点内容要具体", "思考如何写",
    "输出中不要", "更精确", "我们可用", "话题一", "话题二", "可能的话题",
    "大致内容", "评论文本", "原文：", "原文:", "整体来看", "注意时间戳",
    "可能的整理", "不合要求", "第三个短", "主要内容:", "主要内容：",
    "第一part", "第二part", "部分:", "部分：",
    "最佳方式", "我们仔细看", "时间线变化", "我们分析", "有哪些连续讲话",
    "规划话题结构", "输出时不要写Part", "现在我们来组织", "字幕内容:",
    "字幕内容：", "一个合理的方法", "合理的方法", "实际上，看字幕文本",
    "观察事件",
    "现在规划", "可能的最佳划分", "最佳划分", "这样就", "具体分段",
    "梳理字幕", "连续意思", "输出最终条目", "让我们仔细整理", "读懂字幕",
    "具体要点", "比如：", "比如:", "我认为合理的划分", "我们可能还需要涵盖",
    "然后要点", "话题A", "话题B",
    "这部分明显", "继续讨论这个视频", "继续这段剧情", "總結話題",
    "总结话题", "根據字幕", "根据字幕", "我認為", "我认为", "可以劃分",
    "可以划分", "劃分為", "划分为", "输出内容要严格按照格式", "严格按照格式",
    "标题加emoji", "最终输出", "礼物、弹幕爆点", "确保时间戳",
    "让我们仔细构建", "最终输出示例", "注意称呼", "如果有）",
    "points:", "points：", "title:", "title：", "重新考虑分块内容",
    "我们先把内容分几个话题", "那么我们定义", "整体时间段",
    "让我们尝试提取话题", "我们确保每个话题",
    "我们仔细阅读字幕", "整体看", "我们试着划分", "可能乱码",
    "后面还有", "这些时间段有重叠", "观察内容", "更仔细看",
    "划分建议", "我们还须注意", "先构思", "topic1", "topic2",
    "我们规划话题", "仔细看字幕", "先考虑can", "建议分成两个话题",
    "最终JSON", "最终 JSON", "先整理出具体的时间段", "查看字幕时间戳",
    "注意时间有重叠", "根据人工时间轴", "再分析字幕", "我们尝试解读字幕",
    "can_slice", "points", "\"topics\"", "\"start\"", "\"end\"", "\"title\"",
    "人工时间轴参考", "观察时间戳", "需要写点", "我们看内容",
    "我们来看内容", "对于话题", "根据内容推断边界", "我们看字幕的时间戳",
    "这些人工时间轴", "与上一段有重叠", "其他话题", "另一个思路",
    "我计划", "虽然弹幕低", "必须整理", "考虑话题", "提示说",
    "不需要特别重视", "可以作为参考", "所以生成JSON", "我们整理一下",
    "根据要求", "我们考虑", "先仔细解析字幕", "一个合理的划分",
    "我们来做分析", "我们来确定话题", "从人工时间轴和字幕",
    "输出JSON模板", "可能的切分", "或者：",
)

_FRAGMENT_BODY_LINES = {
    "要点", "补充细节", "具体要点", "另一个事件", "例如", "例如：", "例如:", "等等。", "等等",
    "内容要点", "内容要点：", "内容要点:", "输出", "主播", "加盟商", "店主", "连麦者",
    "但", "但是", "然后", "因为", "所以", "因此", "不过", "最后", "另外", "同时", "继续",
    "现在规划", "具体要点", "具体要点：", "具体要点:", "比如", "比如：", "比如:",
    "points", "points:", "points：", "title", "title:", "title：", "要点", "要点：", "要点:",
    "更好的划分", "更好的划分：", "那么我们定义", "那么我们定义：", "整体时间段", "整体时间段：",
    "观察内容", "观察内容：", "更仔细看", "更仔细看：", "划分建议", "划分建议：",
    "整体看", "整体看，内容涉及：", "我们试着划分", "我们试着划分：",
    "我们规划话题", "我们规划话题：", "仔细看字幕", "仔细看字幕：",
    "先考虑can", "先考虑can：", "最终JSON", "最终 JSON", "最终 JSON：",
    "根据人工时间轴", "根据人工时间轴：", "再分析字幕详细内容", "再分析字幕详细内容：",
    "人工时间轴参考", "人工时间轴参考：", "观察时间戳", "观察时间戳：",
    "需要写点", "需要写点：", "我们看内容", "我们看内容：",
    "我们来看内容", "我们来看内容：", "我们看字幕的时间戳", "我们看字幕的时间戳：",
    "其他话题", "其他话题：", "另一个思路", "另一个思路：",
    "我计划", "我计划：", "所以生成JSON", "所以生成JSON：",
    "根据要求", "根据要求，", "我们考虑", "我们考虑：",
    "先仔细解析字幕", "先仔细解析字幕：", "一个合理的划分", "一个合理的划分：",
    "我们来做分析", "我们来做分析：", "我们来确定话题", "我们来确定话题。",
    "输出JSON模板", "输出JSON模板：", "可能的切分", "可能的切分：",
    "或者", "或者：",
    "弹幕/礼物高光", "弹幕礼物高光", "…", "...", "……",
}

_DANMAKU_META_KEYWORDS = (
    "弹幕反应平静", "无爆点", "弹幕高能", "密度达", "峰值", "全场平均", "低于平均", "高于平均",
    "弹幕倍数", "弹幕信息", "弹幕爆点信息", "没有弹幕爆点", "不活跃", "反应不活跃",
    "弹幕高密度", "反应活跃", "可能弹幕", "字幕未显示", "我们谨慎",
    "弹幕互动平淡", "观众反应较少", "弹幕较少", "观众活跃度不高",
)

_UNCUTTABLE_CONTENT_KEYWORDS = (
    "未发言", "仅播放", "只是播放", "游戏角色对话语音", "背景语音", "游戏画面/语音",
    "具体内容不清晰", "字幕识别较碎", "未形成稳定可切片主题", "暂不标记为自动切片",
    "无有效讲话", "全是沉默", "全是音乐", "机械复读", "游戏开头动画",
)


def _strip_code_fence(response):
    """去掉 LLM 可能包裹的 Markdown 代码块。"""
    response = (response or "").strip()
    if response.startswith("```"):
        response = re.sub(r'^```\w*\n?', '', response)
        response = re.sub(r'\n?```$', '', response)
    return response.strip()


def _clean_topic_title(raw_title):
    """清理标题里的切片标记和模型推理说明，保留可读标题。"""
    title = raw_title.replace("✂️", "").replace("✂", "")
    title = _strip_title_meta(title)
    title = re.sub(r'\s+', ' ', title)
    return title.strip(' -—：:？?。；;，,') or "未命名片段"


def _is_bad_topic_title(title):
    """识别模型把整段字幕当标题的情况。"""
    clean = re.sub(r'\s+', '', title or "")
    if not clean:
        return True
    if clean in {
        "内容", "等", "根据人工时间轴", "划分建议", "先考虑can", "我们规划话题",
        "人工时间轴参考", "观察时间戳", "其他话题", "下一段", "可能的切分",
        "根据要求",
    }:
        return True
    if any(keyword in title for keyword in _META_TITLE_KEYWORDS):
        return True
    if re.match(r'^(所以|其实|可能|先|大致|关于|这部分|继续).*?(整体|字幕|话题|整理|内容|弹幕|留言|剧情|礼物)', title):
        return True
    if len(clean) > MAX_TOPIC_TITLE_CHARS:
        return True
    if any(keyword in clean for keyword in ("感谢我有十八岁的音乐", "感个CH的声音好", "但是下一次我不确定", "从字幕看", "然后从3", "嗯好了会了会了")):
        return True
    if re.fullmatch(r'[A-Za-z]{12,}', clean):
        return True
    return False


def _compact_topic_phrase(text, max_chars=MAX_TOPIC_TITLE_CHARS):
    """从正文提取一个短标题片段。"""
    clean = _strip_body_prefix(text)
    clean = re.sub(r'^(这段|这里|音音|主播|她|他|继续)?(在)?(说|提到|聊到|表示|分析|吐槽|感谢|读弹幕|回应)', '', clean)
    clean = re.sub(r'[“”"`]', '', clean)
    clean = re.split(r'[，。；;：:、（）()\s]', clean, maxsplit=1)[0]
    clean = clean.strip(' -—：:？?。；;，,、')
    return clean[:max_chars] if clean else ""


def _derive_topic_title(title, body_lines):
    """长标题兜底：优先从正文关键词/第一条要点生成短标题。"""
    body_text = " ".join(_strip_body_prefix(line) for line in body_lines)
    if _is_bad_topic_title(title):
        manual_match = re.search(r'人工时间轴[⭐★]*[:：]\s*(?:\d{1,2}:\d{2}(?::\d{2})?\s*)?(.+?)(?:\s+人工时间轴|$)', body_text)
        if manual_match:
            manual_title = _manual_title_from_text(manual_match.group(1))
            if manual_title and not _is_bad_topic_title(manual_title):
                return manual_title
    keyword_titles = (
        (("300万", "石头"), "翡翠切石与包装"),
        (("柳师傅", "包装"), "翡翠切石与包装"),
        (("石头", "包装"), "翡翠切石与包装"),
        (("眼睛", "鲁鲁修"), "角色画风与番剧回忆"),
        (("英兰", "男公关"), "樱兰高校番剧回忆"),
        (("字母A",), "字母A关卡挑战"),
        (("a特别难",), "字母A关卡挑战"),
        (("闭着眼", "这一关"), "闭眼关卡挑战"),
        (("前女友", "回礼"), "前女友回礼吐槽"),
        (("出轨",), "出轨玩笑互动"),
        (("期末", "晚安"), "期末成绩与晚安互动"),
        (("十年前", "手机"), "十年前视频感慨"),
        (("千万", "播放"), "千万播放视频评论"),
        (("像素风",), "像素风古早感"),
        (("朱鹮", "新闻"), "读新闻吐槽朱鹮"),
        (("妈妈", "奶茶"), "奶茶晚安互动"),
        (("晚安", "音乐生"), "晚安收尾互动"),
        (("哼唱练习", "拍子"), "唱歌练习找拍子"),
        (("武士", "关卡"), "武士关卡挑战"),
        (("店铺", "亏损"), "连麦分析店铺亏损"),
        (("咖啡", "加盟"), "咖啡加盟经营分析"),
        (("银宝生日快乐",), "生日祝福与视频回顾"),
        (("生日祝福", "视频"), "生日祝福与视频回顾"),
        (("永远", "生日快乐"), "生日祝福与感悟"),
        (("礼物",), "感谢礼物互动"),
        (("评论",), "读评论与感想"),
    )
    for keywords, fallback_title in keyword_titles:
        if all(keyword in body_text for keyword in keywords):
            if _is_bad_topic_title(title) or title in _GENERIC_TOPIC_TITLES:
                return fallback_title
    if not _is_bad_topic_title(title):
        return title
    for line in body_lines:
        phrase = _compact_topic_phrase(line)
        if phrase and len(phrase) >= 4:
            return phrase
    if _is_bad_topic_title(title):
        return ""
    return "日常聊天互动"

def _strip_title_meta(title):
    """去掉模型写进标题里的自我判断尾巴，避免污染报告和文件名。"""
    title = re.sub(r'\s+', ' ', title).strip()
    # 常见污染："标题 ？但时间太短。最好合并。"、"标题，但..."、"标题。例如..."
    title = re.split(r'\s*[？?。；;，,]\s*(?:但|不过|最好|可能|例如|所以|因为|由于|是否|应该|可以)', title, maxsplit=1)[0]
    title = re.split(r'\s+(?:但|不过|最好|可能|例如|所以|因为|由于|是否|应该|可以)', title, maxsplit=1)[0]
    title = re.sub(r'[（(]\s*(?:但|因为|由于|弹幕|时间|不切|不加标记|不建议切|不要切).*?[）)]', '', title)
    title = re.sub(r'[？?。；;，,：:]+$', '', title)
    return title.strip(' -—：:？?。；;，,')

def _is_slice_marked(raw_title):
    """判断标题是否显式标记为可切。"""
    if any(hint in raw_title for hint in _NO_SLICE_HINTS):
        return False
    return "✂" in raw_title


def _is_topic_in_chunk(start_s, end_s, chunk_start, chunk_end, tolerance=90):
    """只接受当前分块时间范围附近的话题，过滤模型复读旧示例。"""
    if end_s <= start_s:
        return False
    if start_s < chunk_start - tolerance:
        return False
    if end_s > chunk_end + tolerance:
        return False
    return True


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


def _strip_body_prefix(line):
    """去掉正文要点符号，便于判断是否是模型自我说明。"""
    stripped = line.strip()
    while stripped.startswith(("·", "●", "-", "*", "•")):
        stripped = stripped[1:].strip()
    return stripped


def _is_placeholder_title(title):
    """过滤模型占位标题和“无明显话题”。"""
    clean = _strip_body_prefix(title)
    if not clean:
        return True
    if any(placeholder in clean for placeholder in _PLACEHOLDER_TITLES):
        return True
    return clean in ("标题", "（标题）", "(标题)")


def _is_meta_body_line(line):
    """过滤模型思考过程、规则复述、弹幕密度解释和占位半句。"""
    raw = line.strip()
    clean = _strip_body_prefix(line)
    if not clean:
        return True
    if "```" in clean:
        return True

    normalized = clean.strip(' （）()[]【】「」『』：:。；;，,、.!！?？')
    if re.fullmatch(r'\[?\d{1,2}:\d{2}(?::\d{2})?\s*', clean):
        return True
    if clean.startswith(("字幕核查：", "字幕核查:", "弹幕依据：", "弹幕依据:", "切片核心：", "切片核心:")):
        return False
    if clean.startswith(("“", "”", "\"", "‘", "'")) and ("– 说" in clean or "- 说" in clean or len(clean) > 80):
        return True
    if "->" in clean:
        return True
    if clean in _FRAGMENT_BODY_LINES or normalized in _FRAGMENT_BODY_LINES:
        return True
    if clean.startswith((
        "标题：", "标题:", "第一个话题", "第二个话题", "第三个话题", "字幕原文",
        "话题一", "话题二", "话题三", "話題1", "話題2", "話題3",
        "第一part", "第二part", "第三个短", "{", "}", '"topics"',
        '"start"', '"end"', '"title"', '"can_slice"', '"points"',
    )):
        return True
    if re.match(r'^(points|title)\s*[:：]', clean, re.IGNORECASE):
        return True
    if clean.startswith((
        "首先，覆盖", "覆盖从", "要注意", "注意字幕", "然后从", "另外，前部分", "整个分块",
        "注意最后一段", "更好的方式", "更合理", "其实我们最好", "建议这样",
        "子部分", "从语义看", "为了简洁", "注意，我们", "字幕分析", "总体来说",
        "比较好的做法", "我建议", "我考虑", "我们也可以", "但中间有间隔",
        "让我们详细解析", "先理解字幕", "基于此", "要点要具体", "要点内容",
        "思考如何写", "输出中不要", "更精确", "我们可用", "可能的话题",
        "大致内容", "从字幕看", "整体来看", "注意时间戳", "可能的整理",
        "主要内容", "部分:", "最佳方式", "我们仔细看", "我们分析", "输出时不要写Part",
        "现在我们来组织", "字幕内容", "一个合理的方法", "实际上，看字幕文本",
        "观察事件", "现在规划", "可能的最佳划分", "具体分段", "梳理字幕",
        "输出最终条目", "让我们仔细整理", "读懂字幕", "具体要点", "比如",
        "我认为合理的划分", "我们可能还需要涵盖", "然后要点",
        "这部分明显", "继续讨论这个视频", "继续这段剧情", "總結話題",
        "根據字幕", "根据字幕", "输出内容要严格按照格式", "标题加emoji",
        "最终输出", "礼物、弹幕爆点", "确保时间戳", "让我们仔细构建",
        "最终输出示例", "注意称呼", "由于是音音自言自语",
        "重新考虑分块内容", "我们先把内容分几个话题", "那么我们定义",
        "整体时间段", "让我们尝试提取话题", "我们确保每个话题",
        "我们仔细阅读字幕", "整体看", "我们试着划分", "这些时间段有重叠",
        "观察内容", "更仔细看", "划分建议", "我们还须注意", "先构思",
        "我们规划话题", "仔细看字幕", "先考虑can", "建议分成两个话题",
        "或者可以合并", "最终 JSON", "最终JSON", "先整理出具体的时间段",
        "查看字幕时间戳", "注意时间有重叠", "根据人工时间轴", "再分析字幕",
        "我们尝试解读字幕",
        "人工时间轴参考", "观察时间戳", "需要写点", "我们看内容",
        "我们来看内容", "对于话题", "根据内容推断边界", "我们看字幕的时间戳",
        "这些人工时间轴", "与上一段有重叠", "其他话题", "另一个思路",
        "我计划", "虽然弹幕低", "必须整理", "考虑话题", "提示说",
        "不需要特别重视", "可以作为参考", "所以生成JSON", "我们整理一下",
        "根据要求", "我们考虑", "先仔细解析字幕", "一个合理的划分",
        "我们来做分析", "我们来确定话题", "从人工时间轴和字幕",
        "输出JSON模板", "可能的切分", "或者",
    )):
        return True
    if re.match(r'^topic\d+\s*[:：]', clean, re.IGNORECASE):
        return True
    if re.match(r'^\[?\d{1,2}:\d{2}(?::\d{2})?\]?\s*["“”]', clean):
        return True
    if re.match(r'^["“”]?(start|end|title|can_slice|points|topics)["“”]?\s*[:：]', clean, re.IGNORECASE):
        return True
    if clean in {"{", "}", "[", "]", "},", "],", "{"}:
        return True
    if re.match(r'^\[\d{1,2}:\d{2}(?::\d{2})?\s*/\s*\d{4}-\d{2}-\d{2}', clean):
        return True
    if re.match(r'^\d+[.)、]\s*(聊|讨论|观看|感谢|游戏|生日)', clean):
        return True
    if re.match(r'^\d+\.\s*\d{1,2}:\d{2}', clean):
        return True
    if re.match(r'^\d{1,2}:\d{2}(?::\d{2})?\s*[-－]\s*\d{1,2}:\d{2}', clean):
        return True
    if re.match(r'^\d{1,2}:\d{2}(?::\d{2})?\s*[（(]', clean):
        return True
    if re.match(r'^\d{1,2}:\d{2}(?::\d{2})?\s*[：:]', clean):
        return True
    if re.match(r'^\d+[.)、]\s*', clean) and re.search(r'(表演|评论|讨论|话题|話題|游戏|感谢|朗读|观看|吐槽|礼物)', clean):
        return True
    if clean.startswith(("然后", "从")) and re.search(r'\d{1,2}:\d{2}(?::\d{2})?', clean):
        return True
    if "##" in clean or "规划话题结构" in clean:
        return True
    if clean.startswith("[开始") or clean.startswith("开始－结束") or clean.startswith("开始-结束"):
        return True
    if re.match(r'^\d+[.、]\s*', clean) and re.search(r'\d{1,2}:\d{2}(?::\d{2})?\s*[-－]\s*\d{1,2}:\d{2}', clean):
        return True
    if re.match(r'^\d+[.、]\s*', clean) and re.search(r'(话题|話題|关于|讨论|音音|主播|弹幕|感谢|游戏|时间|内容)', clean):
        return True
    if re.match(r'^(话题|話題|第[一二三四五六七八九十]+段|第\d+段)\s*\d*[:：]', clean):
        return True
    if re.search(r'\d{1,2}:\d{2}(?::\d{2})?\s*[-－]\s*\d{1,2}:\d{2}', clean) and re.search(r'(话题|时间|开始|结束|取到|部分|阶段)', clean):
        return True
    if re.search(r'\d{1,2}:\d{2}(?::\d{2})?\s*[-－]\s*\d{1,2}:\d{2}', clean) and re.search(r'(注意|但是|但|我们|考虑|更好|更合理|然后|这里|标题|划分|输出|合并)', clean):
        return True
    if re.match(r'^\d{1,2}:\d{2}(?::\d{2})?\s*(?:开始|继续)', clean) and re.search(r'(评论文本|讨论|抱怨|感谢|开始)', clean):
        return True
    # 被 max_tokens 截断时常出现“·主播”“·加盟商”“·但”这类无法独立理解的半句。
    if len(normalized) <= 3 and normalized in {"主播", "观众", "弹幕", "店主", "对方", "加盟商", "但", "输出"}:
        return True
    # ● 只保留礼物、观众金句等具体事件；泛泛的弹幕强弱/密度判断不进报告。
    if raw.startswith("●") and any(keyword in clean for keyword in _DANMAKU_META_KEYWORDS):
        return True
    if any(keyword in clean for keyword in _META_BODY_KEYWORDS):
        return True
    if re.search(r'(应该|不应该|可以只输出|是否|格式|指令|原则|分块|代码块|我们可以|所以输出|要点要写|具体有哪些点)', clean) and (
        clean.startswith(("但", "另外", "所以", "因此", "这里", "如果", "最后", "检查", "考虑"))
        or "我们" in clean
    ):
        return True
    if clean.startswith(("但", "但是", "不过", "所以", "因此", "此外", "按照", "检查", "现在", "这里", "因为", "另外", "也许", "也可以", "为了")) and re.search(
        r'(规则|要求|字幕|依据|输出|话题|标题|要点|检查|示例|时间|数据|写|分成|可以|覆盖|常识)',
        clean,
    ):
        return True
    if clean.startswith(("所以", "另外", "因此", "现在", "再看")) and re.search(r'(输出|整理|标题|弹幕|要点|具体|密度)', clean):
        return True
    if re.match(r'^(弹幕|密度|由于弹幕|因为弹幕)[:：]', clean):
        return True
    return False


def _clean_body_content(line):
    """保留有效信息，同时去掉模型常见的总结式开头。"""
    clean = _strip_body_prefix(line)
    clean = re.sub(r'^(?:所以整体是|大致内容[:：]?|主要内容[:：]?|首先[，,]\s*)', '', clean).strip()
    clean = re.sub(r'^[\"“”](.*?)[\"”]?\s*,?$', r'\1', clean).strip()
    clean = re.sub(r'^内容有些混乱[，,。；;：:但是\s]*', '', clean).strip()
    clean = re.sub(r'^但是可以归纳出话题[:：]?', '', clean).strip()
    clean = re.sub(r'^可以归纳出话题[:：]?', '', clean).strip()
    clean = re.sub(r'^要点\s*[:：]\s*', '', clean).strip()
    clean = re.sub(r'^这段(?:讨论|继续解释|继续)?', '', clean).strip()
    clean = clean.replace("音音音音", "音音")
    return clean


def _normalise_body_line(line):
    """规范正文要点前缀，让报告接近人工时间轴。"""
    raw = line.strip()
    line = _clean_body_content(raw)
    if not line or _is_meta_body_line(line):
        return ""
    if raw.startswith("●"):
        return "●" + line
    return "·" + line


def _extract_json_payload(response):
    """从 LLM 响应中提取 JSON 对象/数组；提取失败返回 None。"""
    text = _strip_code_fence(response)
    if not text:
        return None
    candidates = []
    if "{" in text and "}" in text:
        candidates.append(text[text.find("{"):text.rfind("}") + 1])
    if "[" in text and "]" in text:
        candidates.append(text[text.find("["):text.rfind("]") + 1])
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except Exception:
            continue
    return None


def _json_points_to_body(points):
    """把 JSON points/body 字段转换成报告正文要点。"""
    if points is None:
        return []
    if isinstance(points, str):
        raw_items = [line for line in re.split(r'[\r\n]+', points) if line.strip()]
    elif isinstance(points, (list, tuple)):
        raw_items = []
        for item in points:
            if isinstance(item, (list, tuple)):
                raw_items.extend(str(sub) for sub in item)
            else:
                raw_items.append(str(item))
    else:
        raw_items = [str(points)]
    body_lines = [_normalise_body_line(item) for item in raw_items]
    return [line for line in body_lines if line]


def _json_can_slice(value, raw_title):
    """解析 JSON 里的 can_slice 字段；兼容字符串布尔值。"""
    if any(hint in str(raw_title) for hint in _NO_SLICE_HINTS):
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "y", "1", "可切", "切", "是"}
    return "✂" in str(raw_title)


def _fallback_publish_title(topic_title):
    """模型标题缺失或受污染时，生成不会泄漏推理文字的安全投稿标题。"""
    clean_title = _clean_topic_title(str(topic_title or ""))
    if not clean_title or _is_bad_topic_title(clean_title):
        clean_title = "值得留意的直播片段"
    return f"{PUBLISH_TITLE_PREFIX}{clean_title}"[:MAX_PUBLISH_TITLE_CHARS]


def _normalise_publish_title(raw_title, topic_title):
    """清理投稿标题并统一账号前缀；不合格时回退到话题短标题。"""
    raw_text = "" if raw_title is None else str(raw_title)
    title = raw_text.replace("**", "").replace("`", "")
    title = re.sub(r'^\s*(?:publish_title|投稿标题(?:建议)?)\s*[：:]\s*', '', title, flags=re.IGNORECASE)
    title = re.sub(r'\s+', ' ', title).strip(' \t\r\n-—')
    title = _PUBLISH_TITLE_PREFIX_RE.sub('', title, count=1).strip()
    if (
        not title
        or len(title) + len(PUBLISH_TITLE_PREFIX) > MAX_PUBLISH_TITLE_CHARS
        or len(re.sub(r'\s+', '', title)) < 4
        or title in _GENERIC_PUBLISH_TITLES
        or any(keyword.lower() in title.lower() for keyword in _PUBLISH_TITLE_META_KEYWORDS)
        or any(token in title for token in ('{"topics"', '```', '\\n'))
    ):
        return _fallback_publish_title(topic_title)
    return f"{PUBLISH_TITLE_PREFIX}{title}"


def _parse_json_topics_response(response, chunk_start, chunk_end, accepted_topics):
    """优先解析结构化 JSON 响应；不是 JSON 时返回 None，由旧 Markdown 解析兜底。"""
    payload = _extract_json_payload(response)
    if payload is None:
        return None
    if isinstance(payload, dict):
        raw_topics = payload.get("topics", [])
    elif isinstance(payload, list):
        raw_topics = payload
    else:
        return [], []
    if not isinstance(raw_topics, list):
        return [], []

    parsed_topics = []
    clip_marks = []
    for item in raw_topics:
        if not isinstance(item, dict):
            continue
        try:
            start_str = str(item.get("start", "")).strip()
            end_str = str(item.get("end", "")).strip()
            start_s = _parse_hms(start_str)
            end_s = _parse_hms(end_str)
        except Exception:
            continue
        if not _is_topic_in_chunk(start_s, end_s, chunk_start, chunk_end):
            continue
        raw_title = str(item.get("title", "")).strip()
        if _is_placeholder_title(raw_title):
            continue
        body_lines = _json_points_to_body(
            item.get("points", item.get("body", item.get("summary", item.get("details"))))
        )
        if not body_lines:
            continue
        end_s = _repair_short_topic_end(start_s, end_s, body_lines, chunk_end)
        title = _derive_topic_title(_clean_topic_title(raw_title), body_lines)
        if not title:
            continue
        topic = {
            "start": start_s,
            "end": end_s,
            "start_str": start_str,
            "end_str": fmt_time(end_s),
            "title": title,
            "publish_title": _normalise_publish_title(item.get("publish_title"), title),
            "can_slice": _json_can_slice(item.get("can_slice", False), raw_title),
            "body": body_lines,
        }
        if _is_duplicate_topic(topic, accepted_topics):
            continue
        accepted_topics.append(topic)
        parsed_topics.append(topic)
        if topic["can_slice"]:
            clip_marks.append({"start": topic["start"], "end": topic["end"], "title": topic["title"]})

    report_blocks = [_format_topic_block(topic, idx + 1) for idx, topic in enumerate(parsed_topics)]
    return report_blocks, _dedupe_clip_marks(clip_marks)


def _build_manual_topic_enrichment_prompt(topics, streamer_name="音音", compact=False):
    """把规则聚合候选压缩成一次批量 AI 复核请求。"""
    candidates = []
    for index, topic in enumerate(topics or [], 1):
        body_limit = 8 if compact else 18
        evidence = [
            _strip_body_prefix(line)
            for line in (topic.get("body") or [])[:body_limit]
            if _strip_body_prefix(line)
        ]
        candidates.append({
            "id": index,
            "start": fmt_time(topic["start"]),
            "end": fmt_time(topic["end"]),
            "current_title": topic.get("title", "未命名片段"),
            "evidence": evidence,
        })
    title_style_prompt = _build_title_style_prompt(
        json.dumps(candidates, ensure_ascii=False),
        compact=compact,
    )
    guide = (
        "投稿标题固定以【泽音】开头，按当前证据选择事件+原话、SC+回应、观看对象+反应、"
        "短句头条或温情原话等合适结构；不要每项都机械使用‘结果/当场’，"
        "也不能照抄历史事件或编造证据中不存在的信息。"
    )
    return (
        "你是泽音Melody录播的资深切片编辑。下面候选由字幕、弹幕和人工时间轴共同聚合；"
        "人工时间轴只是线索，不是可直接照抄的结论。请逐项核对证据，改善短标题和内容要点，"
        "并生成可直接投稿的publish_title。不得修改id，不得决定是否切片。"
        f"主播在正文中称为{streamer_name}，不要写泛称‘主播’。{guide}"
        f"\n\n账号历史投稿标题风格：\n{title_style_prompt or '无可用历史样本，只按当前证据写具体标题。'}\n\n"
        "每个候选通常输出一个前因、事件、反应完整且最值得二剪的连贯事件，不要把两个独立话题硬拼成一个。"
        "如果current_title或字幕明确包含两个独立事件（例如用‘与/和/及’并列），且各自附近都有不同弹幕峰值，"
        "必须把同一个id输出为两项，每项只写一个事件；同一个id最多两项，禁止为了凑数拆分连续对话。"
        "focus_start和focus_end必须位于候选start/end内，精确到字幕证据中的时间，完整包住标题所写事件；"
        "如果事件由SC、观众长留言、礼物、提问或外部视频触发，focus_start必须从念出触发内容或明确引出问题处开始；"
        "ASR没有识别出SC字样时，要结合感谢、复述留言和紧随其后的回答判断；focus_end必须覆盖最后一轮回应。"
        "优先控制在30秒到4分钟，不能只框一句爆点，也不能夹带前后无关话题。"
        "弹幕依据只有密度，没有弹幕正文；除非字幕或人工记录明确写出，否则禁止编造‘观众刷屏、直呼、"
        "调侃、笑称、齐刷、赞叹’等具体反应。每项写2-5条有证据的具体points；"
        "禁止模型分析过程、规则说明、弹幕密度判断和空泛描述。"
        "只输出JSON对象："
        "{\"topics\":[{\"id\":1,\"title\":\"5-15字具体短标题\","
        "\"publish_title\":\"【泽音】具体事件钩子👀结果或原话\","
        "\"focus_start\":\"0:03:40\",\"focus_end\":\"0:05:30\","
        "\"points\":[\"具体发生了什么\",\"音音如何回应\"]}]}。\n\n"
        "候选数据：\n"
        + json.dumps(candidates, ensure_ascii=False, separators=(",", ":"))
    )


_UNSUPPORTED_AI_AUDIENCE_REACTION_RE = re.compile(
    r'(?:观众|弹幕).{0,18}(?:刷屏|刷|直呼|调侃|笑称|齐刷|赞叹|赞|起哄|反应活跃|疯狂)'
)


def _filter_unsupported_ai_points(points):
    """弹幕密度不能证明具体弹幕内容，过滤模型自行补写的观众反应。"""
    return [
        line for line in points or []
        if not _UNSUPPORTED_AI_AUDIENCE_REACTION_RE.search(_strip_body_prefix(line))
    ]


def _validated_ai_focus_range(item, topic):
    """校验 AI 建议的语义核心范围；越界或过长时忽略，继续使用程序候选范围。"""
    try:
        focus_start = _parse_hms(str(item.get("focus_start", "")))
        focus_end = _parse_hms(str(item.get("focus_end", "")))
    except (TypeError, ValueError):
        return None
    source_start = int(topic["start"])
    source_end = int(topic["end"])
    duration = focus_end - focus_start
    if not source_start <= focus_start < focus_end <= source_end:
        return None
    if duration < 10 or duration > TOPIC_MAX_CLIP_SEC:
        return None
    return focus_start, focus_end


def _enriched_manual_topic_from_item(topic, item):
    """把一项 AI 复核结果应用到候选副本；无有效正文时返回 None。"""
    points = _filter_unsupported_ai_points(_json_points_to_body(item.get("points")))
    if not points:
        return None
    enriched = dict(topic)
    title = _derive_topic_title(
        _clean_topic_title(str(item.get("title", topic.get("title", "")))),
        points,
    )
    if not title:
        title = topic.get("title", "未命名片段")
    preserved_evidence = [
        line
        for line in topic.get("body") or []
        if line.startswith("·弹幕依据：") or line.startswith("●人工时间轴")
    ]
    body = list(points)
    for line in preserved_evidence:
        if line not in body:
            body.append(line)
    enriched["title"] = title
    enriched["publish_title"] = _normalise_publish_title(
        item.get("publish_title"), title
    )
    enriched["body"] = body
    enriched["ai_enriched"] = True
    enriched["postcheck_pending"] = False
    enriched["postcheck_validated"] = True
    enriched.pop("reference_only", None)
    focus_range = _validated_ai_focus_range(item, topic)
    if focus_range:
        source_start = int(topic["start"])
        source_end = int(topic["end"])
        enriched["reference_start"] = source_start
        enriched["reference_end"] = source_end
        enriched["start"], enriched["end"] = focus_range
        enriched["start_str"] = fmt_time(enriched["start"])
        enriched["end_str"] = fmt_time(enriched["end"])
        enriched["ai_focus_validated"] = True
    return enriched


def _enrich_manual_topics_with_llm(topics, streamer_name="音音", progress_callback=None):
    """用一次 DeepSeek 请求批量复核人工候选，并允许并列事件拆成两项。"""
    if not topics:
        return 0
    prompt = _build_manual_topic_enrichment_prompt(topics, streamer_name=streamer_name)
    compact_prompt = _build_manual_topic_enrichment_prompt(
        topics,
        streamer_name=streamer_name,
        compact=True,
    )
    response = _call_llm_with_retry(
        prompt,
        compact_prompt=compact_prompt,
        require_json=True,
        progress_callback=progress_callback,
        progress_label="人工时间轴 AI 复核",
        progress_step=75,
    )
    payload = _extract_json_payload(response)
    raw_topics = payload.get("topics", []) if isinstance(payload, dict) else []
    if not isinstance(raw_topics, list):
        raise LLMStructuredOutputError("人工时间轴 AI 复核未返回 topics 数组")

    grouped_items = defaultdict(list)
    for item in raw_topics:
        if not isinstance(item, dict):
            continue
        try:
            topic_index = int(item.get("id")) - 1
        except (TypeError, ValueError):
            continue
        if not 0 <= topic_index < len(topics):
            continue
        if len(grouped_items[topic_index]) < 2:
            grouped_items[topic_index].append(item)

    updated = 0
    enriched_topics = []
    for topic_index, topic in enumerate(topics):
        items = grouped_items.get(topic_index, [])
        replacements = []
        for item in items:
            enriched = _enriched_manual_topic_from_item(topic, item)
            if not enriched:
                continue
            if len(items) > 1 and not enriched.get("ai_focus_validated"):
                continue
            if _is_duplicate_topic(enriched, replacements):
                continue
            replacements.append(enriched)
        if replacements:
            replacements.sort(key=lambda value: (value["start"], value["end"]))
            enriched_topics.extend(replacements)
            updated += len(replacements)
        else:
            enriched_topics.append(topic)
    if not updated:
        raise LLMStructuredOutputError("人工时间轴 AI 复核没有返回可用话题")
    topics[:] = sorted(enriched_topics, key=lambda value: (value["start"], value["end"]))
    return updated


def _try_enrich_manual_topics(topics, streamer_name="音音", progress_callback=None):
    """AI 复核失败时保留规则候选，返回适合写入报告的警告。"""
    try:
        _enrich_manual_topics_with_llm(
            topics,
            streamer_name=streamer_name,
            progress_callback=progress_callback,
        )
        return None
    except Exception as exc:
        return f"人工时间轴 AI 复核失败，已保留字幕/弹幕规则结果：{_short_llm_error(exc)}"


def _enrich_manual_topics_in_batches(
        topics, streamer_name="音音", progress_callback=None,
        batch_size=MANUAL_TIMELINE_OPTIMIZE_BATCH_SIZE):
    """分批优化复杂人工时间轴，避免一次请求塞入整场证据。"""
    optimized_topics = []
    warnings = []
    total_batches = max(1, math.ceil(len(topics or []) / max(1, batch_size)))
    for batch_index, offset in enumerate(range(0, len(topics or []), max(1, batch_size)), 1):
        batch = list(topics[offset:offset + max(1, batch_size)])
        if progress_callback:
            progress_callback(
                f"字幕校准人工时间轴 ({batch_index}/{total_batches})...",
                22,
                100,
            )
        try:
            _enrich_manual_topics_with_llm(
                batch,
                streamer_name=streamer_name,
                progress_callback=progress_callback,
            )
        except Exception as exc:
            warning = f"第 {batch_index}/{total_batches} 批优化失败：{_short_llm_error(exc)}"
            warnings.append(warning)
            for topic in batch:
                topic["reference_only"] = True
        optimized_topics.extend(batch)
    topics[:] = sorted(optimized_topics, key=lambda item: (item["start"], item["end"]))
    if not warnings:
        return None
    return "人工时间轴部分未完成字幕校准，相关条目仅作低权重参考：" + "；".join(warnings)


def _manual_alignment_text(text):
    return "".join(re.findall(r'[\u4e00-\u9fffA-Za-z0-9]+', str(text or ""))).lower()


def _manual_alignment_score(reference, candidate):
    """用二元字组和最长公共片段匹配人工概述与噪声 ASR。"""
    reference = _manual_alignment_text(reference)
    candidate = _manual_alignment_text(candidate)
    if len(reference) < 2 or len(candidate) < 2:
        return 0.0
    reference_grams = {reference[index:index + 2] for index in range(len(reference) - 1)}
    candidate_grams = {candidate[index:index + 2] for index in range(len(candidate) - 1)}
    overlap = len(reference_grams & candidate_grams)
    if not overlap:
        return 0.0
    f1 = 2 * overlap / (len(reference_grams) + len(candidate_grams))
    recall = overlap / len(reference_grams)
    longest = difflib.SequenceMatcher(
        None,
        reference,
        candidate,
        autojunk=False,
    ).find_longest_match().size
    longest_ratio = longest / max(8, min(len(reference), 40))
    return 0.35 * f1 + 0.35 * recall + 0.30 * min(1.0, longest_ratio)


def _srt_alignment_windows(srt_segments):
    """把整场字幕预聚合为固定窗口，供人工时间轴做宽范围模糊校时。"""
    segments = sorted(srt_segments or [], key=lambda item: (item[0], item[1]))
    if not segments:
        return []
    duration = int(math.ceil(max(end for _, end, _ in segments)))
    windows = []
    left = 0
    right = 0
    for start in range(0, duration + 1, MANUAL_TIMELINE_ALIGNMENT_STEP_SEC):
        end = start + MANUAL_TIMELINE_ALIGNMENT_WINDOW_SEC
        while left < len(segments) and segments[left][1] < start:
            left += 1
        right = max(right, left)
        while right < len(segments) and segments[right][0] <= end:
            right += 1
        text = "".join(item[2] for item in segments[left:right])
        if text:
            windows.append((start, end, text))
    return windows


def _align_manual_timeline_entries_to_srt(entries, srt_segments):
    """在原墙钟点前后十分钟搜索字幕证据，修正人工记录的粗略锚点。"""
    windows = _srt_alignment_windows(srt_segments)
    if not windows:
        return [dict(entry) for entry in entries or []]
    aligned_entries = []
    for entry in entries or []:
        raw_start = int(entry.get("start", 0))
        nearby = [
            window for window in windows
            if abs((window[0] + MANUAL_TIMELINE_ALIGNMENT_WINDOW_SEC / 2) - raw_start)
            <= MANUAL_TIMELINE_ALIGNMENT_SEARCH_SEC
        ]
        best_window = None
        best_score = 0.0
        for window in nearby:
            content_score = _manual_alignment_score(entry.get("text", ""), window[2])
            distance = abs((window[0] + MANUAL_TIMELINE_ALIGNMENT_WINDOW_SEC / 2) - raw_start)
            proximity_bonus = 0.02 * max(
                0.0,
                1.0 - distance / MANUAL_TIMELINE_ALIGNMENT_SEARCH_SEC,
            )
            score = content_score + proximity_bonus
            if score > best_score:
                best_window = window
                best_score = score
        fixed = dict(entry)
        fixed["original_start"] = raw_start
        fixed["alignment_score"] = round(best_score, 4)
        if best_window and best_score >= MANUAL_TIMELINE_ALIGNMENT_MIN_SCORE:
            fixed["start"] = int(best_window[0])
            fixed["alignment_shift_sec"] = int(best_window[0] - raw_start)
            fixed["alignment_source"] = "subtitle_fuzzy_match"
        else:
            fixed["alignment_shift_sec"] = 0
            fixed["alignment_source"] = "wall_clock_fallback"
        aligned_entries.append(fixed)
    return sorted(aligned_entries, key=lambda item: (item["start"], item.get("original_start", 0)))


def _optimized_manual_entries_from_topics(topics):
    """把字幕复核话题转换成供后续分块分析使用的简洁时间轴。"""
    entries = []
    for topic in topics or []:
        original_entries = [
            {
                "start": int(item.get("start", 0)),
                "original_start": int(item.get("original_start", item.get("start", 0))),
                "clock": item.get("clock"),
                "text": item.get("text", ""),
                "stars": int(item.get("stars", 0)),
                "alignment_score": item.get("alignment_score"),
                "alignment_shift_sec": int(item.get("alignment_shift_sec", 0)),
            }
            for item in topic.get("manual_timeline") or []
        ]
        stars = max(
            [int(topic.get("manual_stars", 0))]
            + [item["stars"] for item in original_entries]
        )
        summary = []
        for line in topic.get("body") or []:
            clean = _strip_body_prefix(line)
            if not clean:
                continue
            if str(line).startswith(("·弹幕依据：", "·字幕核查：", "·时间轴：", "●人工时间轴")):
                continue
            if clean not in summary:
                summary.append(clean)
            if len(summary) >= 4:
                break
        entry = {
            "start": int(topic["start"]),
            "end": int(topic["end"]),
            "clock": original_entries[0].get("clock") if original_entries else None,
            "text": topic.get("title", "人工时间轴重点"),
            "summary": summary,
            "stars": stars,
            "highlight": stars > 0,
            "source": "optimized_manual_timeline",
            "ai_enriched": bool(topic.get("ai_enriched")),
            "ai_focus_validated": bool(topic.get("ai_focus_validated")),
            "reference_only": bool(topic.get("reference_only")),
            "publish_title": topic.get("publish_title"),
            "original_entries": original_entries,
        }
        entries.append(entry)
    return entries


def _optimize_manual_timeline(
        entries, srt_segments, peaks, streamer_name="音音", progress_callback=None):
    """先用字幕/弹幕聚合人工记录，再由 AI 改写标题、要点和语义范围。"""
    if not entries:
        return [], None
    aligned_entries = _align_manual_timeline_entries_to_srt(entries, srt_segments)
    topics = _topics_from_manual_timeline(
        aligned_entries,
        srt_segments=srt_segments,
        peaks=peaks,
        max_gap_sec=MANUAL_TIMELINE_OPTIMIZE_GAP_SEC,
        max_group_duration_sec=MANUAL_TIMELINE_OPTIMIZE_MAX_GROUP_SEC,
    )
    warning = _enrich_manual_topics_in_batches(
        topics,
        streamer_name=streamer_name,
        progress_callback=progress_callback,
    )
    return _optimized_manual_entries_from_topics(topics), warning


def _optimized_timeline_paths(video_base):
    return video_base + "_优化时间轴.json", video_base + "_优化时间轴.md"


def _write_optimized_timeline_files(
        video_base, source_path, raw_entries, optimized_entries, warning=None):
    """保存可审阅的优化时间轴，便于判断人工参考如何被字幕校准。"""
    json_path, md_path = _optimized_timeline_paths(video_base)
    payload = {
        "video_path": video_base + ".flv",
        "source_path": source_path,
        "raw_entry_count": len(raw_entries or []),
        "optimized_entry_count": len(optimized_entries or []),
        "warning": warning,
        "entries": optimized_entries or [],
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    lines = [
        "# 字幕校准后的人工时间轴",
        "",
        f"> 原始文件: {source_path or '无'}",
        f"> 原始 {len(raw_entries or [])} 条 → 优化 {len(optimized_entries or [])} 个话题候选",
    ]
    if warning:
        lines.append(f"> 警告: {warning}")
    lines.extend(["", "---", ""])
    for index, entry in enumerate(optimized_entries or [], 1):
        stars = " ⭐" * min(int(entry.get("stars", 0)), 5)
        confidence = "字幕/AI已复核" if entry.get("ai_enriched") else "低权重参考"
        lines.append(
            f"## {index:02d} [{fmt_time(entry['start'])}－{fmt_time(entry['end'])}] "
            f"{entry.get('text', '未命名话题')}{stars}"
        )
        lines.append(f"- 状态: {confidence}")
        adjustments = [
            f"{fmt_time(item.get('original_start', item.get('start', 0)))}→"
            f"{fmt_time(item.get('start', 0))} ({int(item.get('alignment_shift_sec', 0)):+d}秒)"
            for item in entry.get("original_entries") or []
            if int(item.get("alignment_shift_sec", 0)) != 0
        ]
        if adjustments:
            lines.append(f"- 字幕校时: {'；'.join(adjustments[:4])}")
        for point in entry.get("summary") or []:
            lines.append(f"- {point}")
        lines.append("")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")
    return json_path, md_path


def _load_optimized_timeline_artifact(
        artifact_path, flv_path, manual_timeline_path=None):
    """加载独立优化产物，并核对录播及原始 DOCX，避免串用时间轴。"""
    if not artifact_path or not os.path.isfile(artifact_path):
        raise FileNotFoundError(f"优化时间轴文件不存在: {artifact_path or '未选择'}")
    try:
        with open(artifact_path, encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"优化时间轴 JSON 无法读取: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("entries"), list):
        raise ValueError("优化时间轴 JSON 缺少 entries 数组")

    def normalized(path):
        return os.path.normcase(os.path.abspath(str(path or "")))

    artifact_video_path = payload.get("video_path")
    if not artifact_video_path or normalized(artifact_video_path) != normalized(flv_path):
        raise ValueError("优化时间轴不属于当前选择的录播文件")
    source_path = payload.get("source_path")
    if manual_timeline_path and normalized(source_path) != normalized(manual_timeline_path):
        raise ValueError("优化时间轴与当前选择的人工 DOCX 不一致")

    return {
        "path": source_path,
        "entries": payload["entries"],
        "source_entry_count": int(payload.get("raw_entry_count", 0)),
        "raw_entry_count": int(payload.get("raw_entry_count", 0)),
        "optimized_entry_count": int(payload.get("optimized_entry_count", len(payload["entries"]))),
        "optimized_json_path": artifact_path,
        "optimized_md_path": os.path.splitext(artifact_path)[0] + ".md",
        "optimization_warning": payload.get("warning"),
        "mode": "optimized_artifact",
        "video_start": _extract_video_start_datetime(flv_path),
    }


def _prepare_optimized_manual_timeline(
        flv_path, video_base, srt_segments, peaks, video_duration,
        manual_timeline_path, streamer_name="音音", progress_callback=None):
    """加载、过滤并优化人工时间轴，返回后续可直接使用的结构。"""
    manual_timeline = load_manual_timeline(
        flv_path,
        manual_timeline_path=manual_timeline_path,
    )
    all_entries = manual_timeline.get("entries") or []
    raw_entries = _filter_manual_timeline_entries(all_entries, video_duration)
    manual_timeline["source_entry_count"] = len(all_entries)
    manual_timeline["raw_entry_count"] = len(raw_entries)
    manual_timeline["entries"] = raw_entries
    if not raw_entries:
        return manual_timeline

    optimized_entries, warning = _optimize_manual_timeline(
        raw_entries,
        srt_segments=srt_segments,
        peaks=peaks,
        streamer_name=streamer_name,
        progress_callback=progress_callback,
    )
    optimized_json_path, optimized_md_path = _write_optimized_timeline_files(
        video_base,
        manual_timeline.get("path"),
        raw_entries,
        optimized_entries,
        warning=warning,
    )
    manual_timeline["entries"] = optimized_entries
    manual_timeline["optimized_entry_count"] = len(optimized_entries)
    manual_timeline["optimized_json_path"] = optimized_json_path
    manual_timeline["optimized_md_path"] = optimized_md_path
    manual_timeline["optimization_warning"] = warning
    return manual_timeline


def optimize_manual_timeline_for_video(
        flv_path, manual_timeline_path, ass_path=None, progress_callback=None):
    """仅优化人工时间轴，不启动整场话题分析或自动切片。"""
    if not os.path.isfile(flv_path):
        raise FileNotFoundError(f"录播文件不存在: {flv_path}")
    if not manual_timeline_path or not os.path.isfile(manual_timeline_path):
        raise FileNotFoundError(f"人工时间轴文件不存在: {manual_timeline_path or '未选择'}")

    if progress_callback:
        progress_callback("检查完整版字幕...", 0, 100)
    source_srt_path = ensure_srt(flv_path, progress_callback)
    if not source_srt_path:
        raise RuntimeError("无法生成 SRT 字幕")
    corrected_srt_path = export_corrected_srt(source_srt_path)
    srt_path = corrected_srt_path or source_srt_path
    srt_segments = parse_srt_text(srt_path)
    if not srt_segments:
        raise RuntimeError("字幕文件没有可用于校时的有效内容")

    if ass_path is None:
        candidate_ass_path = os.path.splitext(flv_path)[0] + ".ass"
        ass_path = candidate_ass_path if os.path.isfile(candidate_ass_path) else None
    if progress_callback:
        progress_callback("计算人工时间轴附近弹幕依据...", 15, 100)
    peaks = analyze_danmaku(ass_path) if ass_path and os.path.isfile(ass_path) else DanmakuDensitySeries()
    srt_duration = max((end for _, end, _ in srt_segments), default=None)
    video_duration = _probe_video_duration(flv_path) or srt_duration
    streamer_name = _streamer_report_name(_infer_streamer_name(flv_path))
    video_base = os.path.splitext(flv_path)[0]
    manual_timeline = _prepare_optimized_manual_timeline(
        flv_path,
        video_base,
        srt_segments,
        peaks,
        video_duration,
        manual_timeline_path,
        streamer_name=streamer_name,
        progress_callback=progress_callback,
    )
    if not manual_timeline.get("raw_entry_count"):
        raise ValueError("所选人工时间轴没有落在当前完整版录播范围内的记录")
    if progress_callback:
        progress_callback(
            f"完成! 原始 {manual_timeline['raw_entry_count']} 条 → "
            f"优化 {manual_timeline.get('optimized_entry_count', 0)} 个候选",
            100,
            100,
        )
    return {
        "video_path": flv_path,
        "source_srt_path": source_srt_path,
        "corrected_srt_path": corrected_srt_path,
        "srt_path": srt_path,
        "optimized_json_path": manual_timeline.get("optimized_json_path"),
        "optimized_md_path": manual_timeline.get("optimized_md_path"),
        "warning": manual_timeline.get("optimization_warning"),
        "manual_timeline": _manual_timeline_summary(manual_timeline),
    }


def _parse_llm_response(response, chunk_start, chunk_end, accepted_topics=None, allow_markdown_fallback=True):
    """
    解析单个分块的 LLM 输出。

    返回: (report_blocks, clip_marks)
    - report_blocks: 单话题时间轴块，主要用于测试和调试
    - clip_marks: 去重后的可切片段列表
    """
    accepted_topics = accepted_topics if accepted_topics is not None else []
    json_result = _parse_json_topics_response(response, chunk_start, chunk_end, accepted_topics)
    if json_result is not None:
        return json_result
    if not allow_markdown_fallback:
        return [], []

    response = _strip_code_fence(response)
    if not response or response.strip() == "无明显话题":
        return [], []

    parsed_topics = []
    current = None

    def flush_current():
        if not current:
            return
        start_s = current["start"]
        end_s = current["end"]
        if not _is_topic_in_chunk(start_s, end_s, chunk_start, chunk_end):
            return

        if _is_placeholder_title(current["title"]):
            return
        body_lines = [_normalise_body_line(line) for line in current["body"]]
        body_lines = [line for line in body_lines if line]
        if not body_lines:
            return
        end_s = _repair_short_topic_end(start_s, end_s, body_lines, chunk_end)
        title = _derive_topic_title(current["title"], body_lines)
        if not title:
            return
        topic = {
            "start": start_s,
            "end": end_s,
            "start_str": current["start_str"],
            "end_str": fmt_time(end_s),
            "title": title,
            "can_slice": current["can_slice"],
            "body": body_lines,
        }
        if _is_duplicate_topic(topic, accepted_topics):
            return
        accepted_topics.append(topic)
        parsed_topics.append(topic)

    for raw_line in response.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if re.match(r'^Part\s*\d+\s*[:：]', line, re.IGNORECASE):
            # 分块分析不接受 LLM 自己输出 Part，避免最终报告 Part 重复。
            continue
        match = _HEADING_RE.match(line)
        if match:
            flush_current()
            start_str, end_str, raw_title = match.groups()
            current = {
                "start_str": start_str,
                "end_str": end_str,
                "start": _parse_hms(start_str),
                "end": _parse_hms(end_str),
                "title": _clean_topic_title(raw_title),
                "can_slice": _is_slice_marked(raw_title),
                "body": [],
            }
        elif current:
            current["body"].append(line)

    flush_current()

    report_blocks = [_format_topic_block(topic, idx + 1) for idx, topic in enumerate(parsed_topics)]
    clip_marks = [
        {"start": topic["start"], "end": topic["end"], "title": topic["title"]}
        for topic in parsed_topics
        if topic["can_slice"]
    ]
    return report_blocks, clip_marks


def _strip_prompt_time_labels(text):
    """去掉分块字幕里的 [time] 标签，生成兜底摘要时使用。"""
    lines = []
    for raw in (text or "").splitlines():
        line = re.sub(r'^\[[^\]]+\]\s*', '', raw).strip()
        if line:
            lines.append(line)
    return " ".join(lines)


def _fallback_title_from_text(text):
    """LLM 漏分块时，根据字幕关键词生成保底话题标题。"""
    if not text:
        return "日常聊天互动"
    rules = (
        (("人体比例",), "人体比例讨论"),
        (("痔疮",), "奇怪广告吐槽"),
        (("猫咪",), "猫咪内容互动"),
        (("像素风",), "像素风古早感"),
        (("节奏", "天国"), "节奏天国游戏"),
        (("手感", "火热"), "节奏天国游戏"),
        (("武士",), "武士关卡游戏"),
        (("关卡",), "游戏关卡挑战"),
        (("游戏", "关卡"), "游戏过程互动"),
        (("咖啡", "店"), "咖啡店经营讨论"),
        (("加盟",), "加盟经营讨论"),
        (("礼物",), "感谢礼物互动"),
        (("生日",), "生日相关聊天"),
        (("晚安",), "晚安收尾互动"),
        (("弹幕",), "读弹幕互动"),
        (("视频", "评论"), "视频评论讨论"),
        (("新闻",), "新闻内容吐槽"),
    )
    for keywords, title in rules:
        if all(keyword in text for keyword in keywords):
            return title
    return "日常聊天互动"


def _make_fallback_topic_from_chunk(ch, streamer_name="音音"):
    """当 LLM 对分块没有有效输出时，生成非切片兜底时间轴，避免整场直播空白。"""
    text = _strip_prompt_time_labels(ch.get("text", ""))
    text = re.sub(r'\s+', '', text)
    if len(text) < 20:
        return None
    title = _fallback_title_from_text(text)
    topic = {
        "start": int(ch["start"]),
        "end": int(ch.get("end", ch["start"] + CHUNK_SEC)),
        "start_str": fmt_time(ch["start"]),
        "end_str": fmt_time(ch.get("end", ch["start"] + CHUNK_SEC)),
        "title": title,
        "can_slice": False,
        "body": [
            f"·本段为{streamer_name}的连续聊天/互动，字幕识别较碎，已保留在时间轴中",
            "·该段未形成稳定可切片主题，暂不标记为自动切片",
        ],
        "fallback": True,
    }
    return topic


def _dedupe_clip_marks(marks):
    """对 clip_marks 做最终去重，避免旧 JSON 或异常响应导致重复切片。"""
    deduped = []
    seen_topics = []
    for mark in sorted(marks, key=lambda m: (int(m.get("topic_start", m.get("start", 0))), int(m.get("topic_end", m.get("end", 0))), m.get("title", ""))):
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
    return deduped


def _nearest_safe_srt_boundary(candidate, minimum, maximum, srt_segments):
    """在允许范围内寻找最接近候选点、且不落在任何字幕句内部的整数秒。"""
    minimum = math.ceil(minimum)
    maximum = math.floor(maximum)
    if minimum > maximum:
        return None
    candidate = max(minimum, min(int(candidate), maximum))
    if not srt_segments:
        return candidate

    def is_safe(point):
        return not any(start < point < end for start, end, _ in srt_segments)

    max_distance = max(candidate - minimum, maximum - candidate)
    for distance in range(max_distance + 1):
        options = [candidate - distance]
        if distance:
            options.append(candidate + distance)
        for point in options:
            if minimum <= point <= maximum and is_safe(point):
                return point
    return None


def _merge_expanded_clip_marks(marks, srt_segments=None):
    """处理扩展后的重叠：核心重叠才合并，仅上下文相碰则按语义边界拆开。"""
    def titles_of(mark):
        titles = mark.get("merged_titles") or [mark.get("title", "")]
        result = []
        for title in titles:
            title = str(title).strip()
            if title and title not in result:
                result.append(title)
        return result

    merged = []
    for mark in sorted(_dedupe_clip_marks(marks), key=lambda m: (m["start"], m["end"])):
        item = _cap_expanded_clip_mark(dict(mark))
        if not merged:
            merged.append(item)
            continue
        prev = merged[-1]
        if item["start"] >= prev["end"]:
            merged.append(item)
            continue

        prev_topic_start = prev.get("topic_start", prev["start"])
        prev_topic_end = prev.get("topic_end", prev["end"])
        item_topic_start = item.get("topic_start", item["start"])
        item_topic_end = item.get("topic_end", item["end"])
        core_overlap = _overlap_ratio(
            prev_topic_start, prev_topic_end, item_topic_start, item_topic_end
        )
        same_title = prev.get("title") == item.get("title")
        if not same_title and core_overlap < 0.5:
            actual_overlap_start = max(int(prev["start"]), int(item["start"]))
            actual_overlap_end = min(int(prev["end"]), int(item["end"]))
            if prev_topic_end <= item_topic_start:
                boundary_min = max(actual_overlap_start, int(prev_topic_end))
                boundary_max = min(actual_overlap_end, int(item_topic_start))
            else:
                overlap_start = max(prev_topic_start, item_topic_start)
                overlap_end = min(prev_topic_end, item_topic_end)
                boundary_min = max(actual_overlap_start, int(overlap_start))
                boundary_max = min(actual_overlap_end, int(overlap_end))
            boundary_min = max(boundary_min, int(prev["start"]) + 1)
            boundary_max = min(boundary_max, int(item["end"]) - 1)
            preferred_boundary = item.get("required_context_start")
            if preferred_boundary is None:
                boundary_candidate = int((boundary_min + boundary_max) / 2)
            else:
                boundary_candidate = max(
                    math.ceil(boundary_min),
                    min(int(preferred_boundary), math.floor(boundary_max)),
                )
            boundary = _nearest_safe_srt_boundary(
                boundary_candidate,
                boundary_min,
                boundary_max,
                srt_segments or [],
            )
            if boundary is None:
                blocking_segments = [
                    segment
                    for segment in (srt_segments or [])
                    if segment[0] < boundary_max and segment[1] > boundary_min
                ]
                reliable_continuous_sentence = (
                    blocking_segments
                    and not prev.get("merged_context")
                    and all(
                        segment[1] - segment[0] <= TOPIC_NATURAL_BOUNDARY_POST_MAX_SEC
                        for segment in blocking_segments
                    )
                )
                if not reliable_continuous_sentence:
                    boundary = max(
                        math.ceil(boundary_min),
                        min(boundary_candidate, math.floor(boundary_max)),
                    )
            if boundary is not None:
                prev["end"] = min(int(prev["end"]), boundary)
                item["start"] = max(int(item["start"]), boundary)
                merged[-1] = _cap_expanded_clip_mark(prev)
                merged.append(_cap_expanded_clip_mark(item))
                continue

        prev["end"] = max(prev["end"], item["end"])
        prev["topic_start"] = min(prev.get("topic_start", prev["start"]), item.get("topic_start", item["start"]))
        prev["topic_end"] = max(prev.get("topic_end", prev["end"]), item.get("topic_end", item["end"]))
        prev["context_expanded"] = bool(prev.get("context_expanded") or item.get("context_expanded"))
        prev["merged_context"] = True
        if "context_start_before_natural" in item:
            prev["context_start_before_natural"] = min(
                prev.get("context_start_before_natural", item["context_start_before_natural"]),
                item["context_start_before_natural"],
            )
        if "context_end_before_natural" in item:
            prev["context_end_before_natural"] = max(
                prev.get("context_end_before_natural", item["context_end_before_natural"]),
                item["context_end_before_natural"],
            )
        titles = titles_of(prev)
        for title in titles_of(item):
            if title not in titles:
                titles.append(title)
        if titles:
            prev["title"] = " / ".join(titles)[:60]
            prev["merged_titles"] = titles
        merged[-1] = _cap_expanded_clip_mark(prev)
    return [_refresh_natural_boundary_metadata(item) for item in merged]


def _refresh_natural_boundary_metadata(mark):
    """在限长、合并或去重后刷新实际保留下来的自然边界延伸量。"""
    item = dict(mark)
    context_start = item.get("context_start_before_natural")
    context_end = item.get("context_end_before_natural")
    if context_start is not None:
        item["natural_boundary_pre_sec"] = int(max(0, context_start - item["start"]))
    if context_end is not None:
        item["natural_boundary_post_sec"] = int(max(0, item["end"] - context_end))
    return item


def _cap_expanded_clip_mark(mark):
    """在字幕吸附和重叠处理后再次限长，优先保留话题核心或弹幕峰值。"""
    item = dict(mark)
    start_s = int(item["start"])
    end_s = int(item["end"])
    if end_s - start_s <= TOPIC_MAX_CLIP_SEC:
        return item

    topic_start = max(start_s, int(item.get("topic_start", start_s)))
    topic_end = min(end_s, int(item.get("topic_end", end_s)))
    if topic_end <= topic_start:
        topic_start, topic_end = start_s, end_s

    required_context_start = item.get("required_context_start")
    if required_context_start is not None:
        required_context_start = max(start_s, min(topic_start, int(required_context_start)))
        if topic_end - required_context_start < TOPIC_MAX_CLIP_SEC:
            # 触发语句和语义核心都能装入上限时，优先保留完整收尾，再从开头裁掉最少的静态前文。
            if end_s - required_context_start <= TOPIC_MAX_CLIP_SEC + TOPIC_REQUIRED_CONTEXT_OVERFLOW_SEC:
                item["start"] = int(required_context_start)
                item["end"] = int(max(required_context_start + 1, end_s))
                return item
            new_end = end_s
            new_start = max(start_s, new_end - TOPIC_MAX_CLIP_SEC)
            if new_start > topic_start:
                new_start = topic_start
                new_end = min(end_s, new_start + TOPIC_MAX_CLIP_SEC)
            item["start"] = int(new_start)
            item["end"] = int(max(new_start + 1, new_end))
            return item

    core_duration = topic_end - topic_start
    if core_duration >= TOPIC_MAX_CLIP_SEC:
        anchor = int(item.get("slice_anchor") or ((topic_start + topic_end) / 2))
        new_start = anchor - TOPIC_MAX_CLIP_SEC // 2
        new_start = max(start_s, min(new_start, end_s - TOPIC_MAX_CLIP_SEC))
        new_end = new_start + TOPIC_MAX_CLIP_SEC
    else:
        available_context = TOPIC_MAX_CLIP_SEC - core_duration
        pre_context = min(TOPIC_PRE_CONTEXT_SEC, available_context)
        new_start = max(start_s, topic_start - pre_context)
        new_end = min(end_s, new_start + TOPIC_MAX_CLIP_SEC)
        if new_end < topic_end:
            new_end = topic_end
            new_start = max(start_s, new_end - TOPIC_MAX_CLIP_SEC)

    item["start"] = int(new_start)
    item["end"] = int(max(new_start + 1, new_end))
    return item


# ============================================================
# 话题切片上下文扩展
# ============================================================

def _parse_srt_timestamp(value):
    """解析 SRT 时间戳，返回视频内秒数。"""
    h, m, rest = value.strip().split(":")
    s, ms = rest.replace(".", ",").split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def parse_srt_segments(srt_path):
    """解析 SRT，返回 [(start_s, end_s, text), ...]。时间均为视频内时间，并修复明显异常时间戳。"""
    return _load_repaired_srt_segments(srt_path)


def _srt_video_duration(srt_segments):
    """用最后一句字幕估算可用视频时长。"""
    if not srt_segments:
        return None
    return max(seg_end for _, seg_end, _ in srt_segments)


def _snap_clip_to_srt_segments(
    start_s,
    end_s,
    srt_segments,
    natural_pre_max_sec=TOPIC_NATURAL_BOUNDARY_PRE_MAX_SEC,
    natural_post_max_sec=TOPIC_NATURAL_BOUNDARY_POST_MAX_SEC,
):
    """吸附完整字幕句，并沿连续讲话延伸到最近的自然停顿。"""
    if not srt_segments:
        return start_s, end_s
    segments = sorted(srt_segments, key=lambda item: (item[0], item[1]))
    related_indexes = [
        index
        for index, segment in enumerate(segments)
        if segment[1] >= start_s and segment[0] <= end_s
    ]
    if not related_indexes:
        return start_s, end_s

    first_index = related_indexes[0]
    last_index = related_indexes[-1]
    first_segment = segments[first_index]
    last_segment = segments[last_index]
    snapped_start = min(start_s, first_segment[0]) if start_s - first_segment[0] <= 30 else start_s
    snapped_end = max(end_s, last_segment[1]) if last_segment[1] - end_s <= 90 else end_s

    cursor = first_index - 1
    current_start = first_segment[0]
    earliest_start = start_s - natural_pre_max_sec
    while cursor >= 0:
        previous = segments[cursor]
        gap = max(0.0, current_start - previous[1])
        if gap > TOPIC_CONTEXT_GAP or previous[0] < earliest_start:
            break
        snapped_start = min(snapped_start, previous[0])
        current_start = previous[0]
        cursor -= 1

    cursor = last_index + 1
    current_end = last_segment[1]
    latest_start = end_s + natural_post_max_sec
    while cursor < len(segments):
        following = segments[cursor]
        gap = max(0.0, following[0] - current_end)
        if gap > TOPIC_CONTEXT_GAP or following[0] > latest_start:
            break
        snapped_end = max(snapped_end, following[1])
        current_end = following[1]
        cursor += 1

    return snapped_start, snapped_end


def _integer_clip_bounds_outside_subtitles(start_s, end_s, srt_segments):
    """整数化时向外避开字幕句，防止 floor/ceil 反而落进相邻句内部。"""
    start_point = math.floor(max(0, start_s))
    end_point = math.ceil(max(end_s, start_s + 1))
    if not srt_segments:
        return start_point, end_point

    while True:
        blocking = [segment for segment in srt_segments if segment[0] < start_point < segment[1]]
        if not blocking:
            break
        earlier = math.floor(min(segment[0] for segment in blocking))
        if earlier >= start_point:
            earlier = start_point - 1
        start_point = max(0, earlier)

    while True:
        blocking = [segment for segment in srt_segments if segment[0] < end_point < segment[1]]
        if not blocking:
            break
        later = math.ceil(max(segment[1] for segment in blocking))
        if later <= end_point:
            later = end_point + 1
        end_point = later
    return start_point, max(end_point, start_point + 1)


def _looks_like_sc_or_gift_trigger(text):
    """判断字幕文本是否像 SC/礼物/付费留言触发点；兼容 ASR 把 SC 漏识别的情况。"""
    compact = re.sub(r'\s+', ' ', (text or "")).strip()
    if not compact:
        return False
    lower = compact.lower()
    if any(keyword in lower for keyword in SC_TRIGGER_KEYWORDS):
        return True
    return bool(THANKS_TRIGGER_RE.search(compact))


def _is_explicit_sc_trigger(text):
    """只有明确识别到 SC/醒目留言时，才允许跨较长时间回溯。"""
    lower = re.sub(r'\s+', ' ', (text or "")).strip().lower()
    return any(keyword in lower for keyword in (
        "sc", "s c", "super chat", "superchat", "醒目留言", "醒目", "付费留言",
    ))


def _gift_trigger_has_question_followup(index, topic_start, srt_segments, window_sec=45):
    """ASR 漏掉 SC 名词时，用紧随礼物感谢后的提问文本确认关联。"""
    if not 0 <= index < len(srt_segments):
        return False
    trigger_start = srt_segments[index][0]
    texts = []
    for seg_start, _, text in srt_segments[index:index + 12]:
        if seg_start > topic_start or seg_start > trigger_start + window_sec:
            break
        texts.append(text or "")
    compact = re.sub(r'\s+', '', "".join(texts))
    return bool(re.search(
        r'(?:他说|她说|音悦生说|观众说|问|留言).{0,50}'
        r'(?:吗|呢|怎么|为何|为什么|能不能|可不可以|怎么办|[？?])',
        compact,
    ))


def _find_sc_context_start(topic_start, srt_segments, lookback_sec=SC_CONTEXT_LOOKBACK_SEC):
    """在话题前回溯 SC/礼物触发字幕，返回应纳入切片的更早起点。"""
    if not srt_segments:
        return None
    window_start = max(0, topic_start - lookback_sec)
    candidates = [
        (idx, seg)
        for idx, seg in enumerate(srt_segments)
        if window_start <= seg[0] <= topic_start and _looks_like_sc_or_gift_trigger(seg[2])
    ]
    if not candidates:
        return None

    eligible = []
    for idx, seg in candidates:
        distance = topic_start - seg[0]
        if (
            distance <= SC_FALLBACK_GIFT_LOOKBACK_SEC
            or _is_explicit_sc_trigger(seg[2])
            or _gift_trigger_has_question_followup(idx, topic_start, srt_segments)
        ):
            eligible.append((idx, seg))
    if not eligible:
        return None

    idx, seg = eligible[-1]  # 用离话题最近的触发点，避免把更早无关礼物也切进来。
    start_s = seg[0]
    # SC 文本可能被 ASR 切成几句，向前吸附很近的连续字幕，保留完整提问/感谢。
    cursor = idx - 1
    while cursor >= 0:
        prev_start, prev_end, _ = srt_segments[cursor]
        if start_s - prev_end > TOPIC_CONTEXT_GAP or topic_start - prev_start > lookback_sec:
            break
        start_s = prev_start
        cursor -= 1
    return start_s


_TOPIC_LEAD_IN_TRIGGER_RE = re.compile(
    r'(?:对了|说到这个|说起来|你们猜|有个音悦生|有位音悦生|看到一条|念一条|刚才有|'
    r'昨天.{0,20}(?:发|送|问)|今天发的|接下来(?:看|玩)|下一个(?:视频|话题|游戏))'
)


def _find_topic_lead_in_start(reference_start, topic_start, srt_segments):
    """长话题的 AI 核心偏晚时，从参考范围内恢复明确的新话题触发语句。"""
    if not srt_segments or topic_start - reference_start < 30:
        return None
    search_start = max(reference_start, topic_start - TOPIC_LEAD_IN_LOOKBACK_SEC)
    for seg_start, _, text in srt_segments:
        if seg_start < search_start:
            continue
        if seg_start >= topic_start:
            break
        if _TOPIC_LEAD_IN_TRIGGER_RE.search(re.sub(r'\s+', '', text or "")):
            return seg_start
    return None


def _find_next_topic_hard_end(topic_end, reference_end, search_end, srt_segments):
    """核心已到参考范围末尾时，用明确转场阻止固定后文吞入下一话题。"""
    if not srt_segments or reference_end - topic_end > 5:
        return None
    for index, (seg_start, _, text) in enumerate(srt_segments):
        if seg_start < topic_end:
            continue
        if seg_start > search_end:
            break
        compact = re.sub(r'\s+', '', text or "")
        if not (
            _TOPIC_LEAD_IN_TRIGGER_RE.search(compact)
            or _is_explicit_sc_trigger(compact)
            or (
                _looks_like_sc_or_gift_trigger(compact)
                and _gift_trigger_has_question_followup(
                    index,
                    search_end,
                    srt_segments,
                )
            )
        ):
            continue
        latest_boundary = math.floor(seg_start)
        boundary = _nearest_safe_srt_boundary(
            latest_boundary,
            math.ceil(topic_end),
            latest_boundary,
            srt_segments,
        )
        return boundary if boundary is not None else latest_boundary
    return None


def _expand_clip_mark_with_context(mark, srt_segments=None, video_duration=None):
    """把 LLM 标记的话题范围扩展为真正用于 ffmpeg 的前后文切片范围。"""
    topic_start = int(float(mark.get("topic_start", mark["start"])))
    topic_end = int(float(mark.get("topic_end", mark["end"])))
    if topic_end <= topic_start:
        topic_end = topic_start + 1

    raw_duration = topic_end - topic_start
    semantic_focus = bool(mark.get("semantic_focus_validated"))
    if semantic_focus:
        reference_start = int(mark.get("reference_start", topic_start))
        reference_end = int(mark.get("reference_end", topic_end))
        pre_context_sec = (
            TOPIC_AI_FOCUS_EDGE_PRE_CONTEXT_SEC
            if topic_start - reference_start <= 5
            else TOPIC_AI_FOCUS_PRE_CONTEXT_SEC
        )
        post_context_sec = (
            TOPIC_AI_FOCUS_EDGE_POST_CONTEXT_SEC
            if reference_end - topic_end <= 5
            else TOPIC_AI_FOCUS_POST_CONTEXT_SEC
        )
        natural_pre_max_sec = TOPIC_AI_FOCUS_NATURAL_PRE_BOUNDARY_SEC
        natural_post_max_sec = TOPIC_AI_FOCUS_NATURAL_POST_BOUNDARY_SEC
    else:
        pre_context_sec = TOPIC_PRE_CONTEXT_SEC
        post_context_sec = TOPIC_POST_CONTEXT_SEC
        natural_pre_max_sec = TOPIC_NATURAL_BOUNDARY_PRE_MAX_SEC
        natural_post_max_sec = TOPIC_NATURAL_BOUNDARY_POST_MAX_SEC

    start_s = max(0, topic_start - pre_context_sec)
    end_s = topic_end + post_context_sec
    hard_context_end = None
    if semantic_focus:
        hard_context_end = _find_next_topic_hard_end(
            topic_end,
            int(mark.get("reference_end", topic_end)),
            end_s,
            srt_segments or [],
        )
        if hard_context_end is not None:
            end_s = min(end_s, hard_context_end)
    sc_context_start = _find_sc_context_start(topic_start, srt_segments or [])
    if sc_context_start is not None:
        start_s = min(start_s, sc_context_start)
    lead_in_start = None
    if semantic_focus and raw_duration >= TOPIC_LEAD_IN_RECOVERY_MIN_SEC:
        lead_in_start = _find_topic_lead_in_start(
            int(mark.get("reference_start", topic_start)),
            topic_start,
            srt_segments or [],
        )
        if lead_in_start is not None:
            start_s = min(start_s, lead_in_start)

    if end_s - start_s < TOPIC_MIN_CLIP_SEC:
        deficit = TOPIC_MIN_CLIP_SEC - (end_s - start_s)
        left = deficit if hard_context_end is not None else int(deficit * 0.4)
        right = 0 if hard_context_end is not None else deficit - left
        start_s = max(0, start_s - left)
        end_s += right

    if raw_duration < TOPIC_MAX_CLIP_SEC and end_s - start_s > TOPIC_MAX_CLIP_SEC:
        end_s = start_s + TOPIC_MAX_CLIP_SEC
        if end_s < topic_end:
            end_s = topic_end
            start_s = max(0, end_s - TOPIC_MAX_CLIP_SEC)

    context_start_s = start_s
    context_end_s = end_s
    start_s, end_s = _snap_clip_to_srt_segments(
        start_s,
        end_s,
        srt_segments or [],
        natural_pre_max_sec=natural_pre_max_sec,
        natural_post_max_sec=natural_post_max_sec,
    )
    if hard_context_end is not None:
        end_s = min(end_s, hard_context_end)

    if video_duration:
        end_s = min(end_s, video_duration)
        if end_s - start_s < TOPIC_MIN_CLIP_SEC and start_s > 0:
            start_s = max(0, end_s - TOPIC_MIN_CLIP_SEC)

    expanded = dict(mark)
    expanded["topic_start"] = topic_start
    expanded["topic_end"] = topic_end
    expanded["start"], expanded["end"] = _integer_clip_bounds_outside_subtitles(
        start_s,
        end_s,
        srt_segments or [],
    )
    if hard_context_end is not None:
        expanded["end"] = min(expanded["end"], int(hard_context_end))
        expanded["hard_context_end"] = int(hard_context_end)
    expanded["time_basis"] = "video_elapsed_seconds"
    expanded["context_expanded"] = True
    expanded["context_pre_sec"] = pre_context_sec
    expanded["context_post_sec"] = post_context_sec
    expanded["context_start_before_natural"] = int(context_start_s)
    expanded["context_end_before_natural"] = int(context_end_s)
    required_context_starts = [
        value for value in (sc_context_start, lead_in_start)
        if value is not None
    ]
    if required_context_starts:
        expanded["required_context_start"] = int(min(required_context_starts))
    expanded = _cap_expanded_clip_mark(expanded)
    return _refresh_natural_boundary_metadata(expanded)


def _expand_clip_marks_with_context(marks, srt_segments=None, video_duration=None):
    """批量扩展切片上下文；输入/输出时间均为视频内秒数。"""
    expanded = [
        _expand_clip_mark_with_context(mark, srt_segments=srt_segments, video_duration=video_duration)
        for mark in _dedupe_clip_marks(marks)
    ]
    return _merge_expanded_clip_marks(expanded, srt_segments=srt_segments)

# ============================================================
# 逐话题时间轴报告格式化
# ============================================================

_CIRCLED_NUMBERS = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳㉑㉒㉓㉔㉕㉖㉗㉘㉙㉚㉛㉜㉝㉞㉟㊱㊲㊳㊴㊵㊶㊷㊸㊹㊺㊻㊼㊽㊾㊿"


def _format_report_time(seconds):
    """报告展示用时间：1小时内用 MM:SS，超过 1 小时用 H:MM:SS。"""
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _topic_index_label(index):
    if 1 <= index <= len(_CIRCLED_NUMBERS):
        return _CIRCLED_NUMBERS[index - 1]
    return f"{index}."


def _replace_streamer_role(text, streamer_name):
    """报告展示时把“主播/正式名”替换为更自然的粉丝称呼。"""
    display_name = _streamer_report_name(streamer_name)
    if not display_name or display_name == "主播":
        return text
    result = text or ""
    for formal_name in STREAMER_NICKNAME_MAP:
        result = result.replace(formal_name, display_name)
    result = result.replace("主播", display_name)
    return _normalise_streamer_terms(result, streamer_name=display_name)


def _strip_emoji_for_title(title):
    """给 Part 标题做轻量清理，避免标题太花。"""
    return re.sub(r'^[^\w\u4e00-\u9fff]+', '', title).strip() or title


def _make_part_title(topics, streamer_name="主播"):
    """根据 Part 内话题生成阶段标题。"""
    titles = [_strip_emoji_for_title(_replace_streamer_role(t["title"], streamer_name)) for t in topics if t.get("title")]
    if not titles:
        return "阶段话题整理"
    if len(titles) == 1:
        return titles[0]
    first, second = titles[0], titles[1]
    if len(first) + len(second) <= 18:
        return f"{first}与{second}"
    return f"{first}等话题"


def _format_topic_block(topic, index, streamer_name="主播"):
    """格式化单个话题块，贴近用户给出的逐话题时间轴样式。"""
    label = _topic_index_label(index) if index else ""
    start = _format_report_time(topic["start"])
    end = _format_report_time(topic["end"])
    marker = " ✂️" if topic.get("can_slice") else ""
    title = _replace_streamer_role(topic["title"], streamer_name)
    lines = [f"{label}[{start}－{end}]{title}{marker}"]
    body = topic.get("body") or []
    lines.extend(_replace_streamer_role(line, streamer_name) for line in body)
    return "\n".join(lines)


def _group_topics_for_parts(topics, part_seconds=900):
    """按约 15 分钟一段聚合话题，生成 Part。"""
    sorted_topics = sorted(topics, key=lambda t: (t["start"], t["end"]))
    groups = []
    current = []
    group_start = None
    for topic in sorted_topics:
        if not current:
            current = [topic]
            group_start = topic["start"]
            continue
        if topic["start"] - group_start >= part_seconds:
            groups.append(current)
            current = [topic]
            group_start = topic["start"]
        else:
            current.append(topic)
    if current:
        groups.append(current)
    return groups


def _group_topics_by_hour(topics):
    """按视频内自然小时聚合话题，生成“每小时重点”。"""
    sorted_topics = sorted(topics, key=lambda t: (t["start"], t["end"]))
    buckets = []
    current_hour = None
    current = []
    for topic in sorted_topics:
        hour = int(topic["start"] // 3600)
        if current_hour is None:
            current_hour = hour
            current = [topic]
            continue
        if hour != current_hour:
            buckets.append((current_hour, current))
            current_hour = hour
            current = [topic]
        else:
            current.append(topic)
    if current:
        buckets.append((current_hour, current))
    return buckets


def _topic_peak_candidates(topic, peaks, window_sec=DANMAKU_WINDOW):
    """只保留中心落在语义核心内的峰值，避免擦边窗口误导切片锚点。"""
    if not peaks:
        return []
    start = int(topic["start"])
    end = int(topic["end"])
    return [
        (peak_start, density)
        for peak_start, density in peaks
        if start <= peak_start + window_sec / 2 <= end
    ]


def _topic_peak_density(topic, peaks, window_sec=DANMAKU_WINDOW):
    """计算语义核心内最高弹幕密度。"""
    densities = [density for _, density in _topic_peak_candidates(topic, peaks, window_sec)]
    return max(densities) if densities else 0


def _topic_peak_anchor(topic, peaks, window_sec=DANMAKU_WINDOW):
    """返回话题内最高弹幕峰值中心点；没有峰值则返回 None。"""
    candidates = _topic_peak_candidates(topic, peaks, window_sec)
    if not candidates:
        return None
    peak_start, _ = max(candidates, key=lambda item: item[1])
    return int(peak_start + window_sec / 2)


def _topic_peak_focus_window(topic, peaks, window_sec=DANMAKU_WINDOW):
    """返回话题内最高弹幕峰值窗口；实际切片优先围绕该窗口扩前后文。"""
    start = int(topic["start"])
    end = int(topic["end"])
    candidates = _topic_peak_candidates(topic, peaks, window_sec)
    if not candidates:
        return None
    peak_start, density = max(candidates, key=lambda item: item[1])
    focus_start = max(start, int(peak_start) - TOPIC_FOCUS_PRE_SEC)
    focus_end = min(end, int(peak_start) + TOPIC_FOCUS_POST_SEC)
    if focus_end <= focus_start:
        focus_end = min(end, focus_start + window_sec)
    return {
        "start": int(focus_start),
        "end": int(max(focus_end, focus_start + 1)),
        "anchor": int(peak_start + window_sec / 2),
        "density": density,
    }


def _topic_manual_star_anchor(topic):
    """返回话题内人工 ⭐ 的核心时间点；人工时间轴只作为参考，不直接决定完整切片长度。"""
    entries = topic.get("manual_timeline") or []
    starred = [
        item for item in entries
        if item.get("stars", 0) > 0 and topic["start"] <= item.get("start", -1) <= topic["end"]
    ]
    if not starred:
        return None
    best = max(starred, key=lambda item: (item.get("stars", 0), -abs(item["start"] - topic["start"])))
    return int(best["start"])


def _assign_topic_slice_window(topic, peaks):
    """为话题分配较短的实际切片核心范围；报告范围仍保留完整话题。"""
    topic_start = int(topic["start"])
    topic_end = int(topic["end"])
    if topic_end <= topic_start:
        return topic

    duration = topic_end - topic_start
    fixed = topic
    if duration <= TOPIC_DIRECT_SLICE_MAX_SEC:
        fixed["slice_start"] = topic_start
        fixed["slice_end"] = topic_end
        return fixed

    peak_focus = _topic_peak_focus_window(topic, peaks)
    if peak_focus:
        fixed["slice_start"] = peak_focus["start"]
        fixed["slice_end"] = peak_focus["end"]
        fixed["slice_anchor"] = peak_focus["anchor"]
        fixed["slice_anchor_source"] = "弹幕峰值"
        body = list(fixed.get("body") or [])
        note = (
            f"·切片核心：完整话题较长，实际切片围绕弹幕峰值"
            f"{fmt_time(peak_focus['anchor'])}截取，保留峰值前后完整反应"
        )
        if note not in body:
            body.append(note)
        fixed["body"] = body
        return fixed

    anchor = _topic_manual_star_anchor(topic)
    anchor_source = "人工⭐"
    if anchor is None:
        anchor = int((topic_start + topic_end) / 2)
        anchor_source = "话题中点"

    slice_start = max(topic_start, anchor - TOPIC_FOCUS_PRE_SEC)
    slice_end = min(topic_end, anchor + TOPIC_FOCUS_POST_SEC)
    if slice_end <= slice_start:
        slice_end = min(topic_end, slice_start + TOPIC_FOCUS_PRE_SEC + TOPIC_FOCUS_POST_SEC)
    fixed["slice_start"] = int(slice_start)
    fixed["slice_end"] = int(max(slice_end, slice_start + 1))
    fixed["slice_anchor"] = int(anchor)
    fixed["slice_anchor_source"] = anchor_source

    body = list(fixed.get("body") or [])
    note = (
        f"·切片核心：完整话题较长，实际切片围绕{anchor_source}"
        f"{fmt_time(anchor)}截取，报告仍保留完整上下文"
    )
    if note not in body:
        body.append(note)
    fixed["body"] = body
    return fixed


def _is_content_cuttable_topic(topic):
    """判断话题内容本身是否适合切片，避免只有背景语音/兜底说明被高弹幕误切。"""
    if topic.get("fallback"):
        return False
    if topic.get("reference_only"):
        return False
    if topic.get("source") in {"manual_timeline", "optimized_manual_timeline"} and not topic.get("ai_enriched"):
        return False
    if _is_bad_topic_title(topic.get("title", "")):
        return False
    text = " ".join([topic.get("title", "")] + list(topic.get("body") or []))
    compact = re.sub(r'\s+', '', text)
    if not compact:
        return False
    if any(keyword in compact for keyword in _UNCUTTABLE_CONTENT_KEYWORDS):
        return False
    return True


def _clean_topics_for_report(topics):
    """生成报告/切片前做最后一道清洗，防止坏标题或提示残留漏网。"""
    cleaned = []
    for topic in sorted(topics or [], key=lambda item: (item.get("start", 0), item.get("end", 0))):
        if topic.get("fallback"):
            cleaned.append(topic)
            continue
        body_lines = [_normalise_body_line(line) for line in topic.get("body") or []]
        body_lines = [line for line in body_lines if line]
        if not body_lines:
            continue
        title = _derive_topic_title(topic.get("title", ""), body_lines)
        if not title:
            continue
        fixed = dict(topic)
        fixed["title"] = title
        fixed["body"] = body_lines
        if _is_duplicate_topic(fixed, cleaned):
            continue
        cleaned.append(fixed)
    return cleaned


def _refresh_topic_danmaku_evidence(topic, peaks):
    """AI 缩小语义核心后重新计算弹幕依据，移除已落在核心外的旧峰值说明。"""
    candidates = _topic_peak_candidates(topic, peaks)
    best = max(candidates, key=lambda item: item[1]) if candidates else None
    evidence = None
    if best:
        peak_start, density = best
        evidence = f"·弹幕依据：{fmt_time(peak_start)} 附近峰值约 {density:.0f} 条/分钟"
    body = []
    inserted = False
    for line in topic.get("body") or []:
        if str(line).startswith("·弹幕依据："):
            if evidence and not inserted:
                body.append(evidence)
                inserted = True
            continue
        if evidence and not inserted and str(line).startswith("●人工时间轴"):
            body.append(evidence)
            inserted = True
        body.append(line)
    if evidence and not inserted:
        body.append(evidence)
    topic["body"] = body
    return best


def _apply_danmaku_slice_decisions(topics, peaks, avg_density):
    """从每小时重点中按弹幕密度筛选可切片段。"""
    if not topics:
        return []
    threshold = max(avg_density * CLIP_DENSITY_RATIO, avg_density + 10, 20)
    manual_threshold = max(avg_density * MANUAL_TIMELINE_STAR_DENSITY_RATIO, 20)
    for topic in topics:
        _refresh_topic_danmaku_evidence(topic, peaks)
        peak_density = _topic_peak_density(topic, peaks)
        topic["peak_density"] = peak_density
        topic["density_ratio"] = round(peak_density / avg_density, 2) if avg_density else 0
        normal_cut = peak_density >= threshold
        manual_stars = topic.get("manual_stars", 0)
        manual_cut = bool(peaks) and manual_stars > 0 and peak_density >= manual_threshold
        topic["can_slice"] = bool(
            _is_content_cuttable_topic(topic)
            and (normal_cut or manual_cut)
            and topic["end"] > topic["start"]
        )
        if topic["can_slice"]:
            _assign_topic_slice_window(topic, peaks)
    return topics


def _clip_marks_from_topics(topics):
    """根据已筛选的重点话题生成 clip_marks。"""
    return _dedupe_clip_marks([
        {
            "start": topic.get("slice_start", topic["start"]),
            "end": topic.get("slice_end", topic["end"]),
            "title": topic["title"],
            "publish_title": _normalise_publish_title(
                topic.get("publish_title"), topic["title"]
            ),
            "report_start": topic["start"],
            "report_end": topic["end"],
            "slice_anchor": topic.get("slice_anchor"),
            "slice_anchor_source": topic.get("slice_anchor_source"),
            "semantic_focus_validated": bool(topic.get("ai_focus_validated")),
            "reference_start": topic.get("reference_start"),
            "reference_end": topic.get("reference_end"),
        }
        for topic in topics
        if topic.get("can_slice")
    ])


def _topic_clip_filename(index, mark):
    """生成自动切片文件名；报告和实际 ffmpeg 输出必须共用此规则。"""
    title = str(mark.get("title", f"片段{index}")).strip() or f"片段{index}"
    safe_title = re.sub(r'[\\/:*?"<>|`]', '', title)
    safe_title = re.sub(r'\s+', ' ', safe_title).strip(' .')[:30]
    if not safe_title:
        safe_title = f"片段{index}"
    start_s = int(float(mark.get("start", 0)))
    return f"{index:02d}_{start_s}s_{safe_title}.flv"


def _clip_subtitle_filename(clip_filename):
    """片段字幕与视频同名，便于剪映成对导入。"""
    return os.path.splitext(clip_filename)[0] + ".srt"


def _write_clip_srt(srt_segments, clip_start, clip_end, output_path):
    """裁剪整场字幕并减去切片起点，生成从 0 秒开始的片段 SRT。"""
    clip_start = float(clip_start)
    clip_end = float(clip_end)
    entries = []
    for seg_start, seg_end, text in srt_segments or []:
        local_start = max(float(seg_start), clip_start) - clip_start
        local_end = min(float(seg_end), clip_end) - clip_start
        clean_text = str(text or "").strip()
        if local_end <= local_start or not clean_text:
            continue
        entries.append((local_start, local_end, clean_text))

    with open(output_path, "w", encoding="utf-8", newline="\n") as f:
        for index, (start_s, end_s, text) in enumerate(entries, 1):
            f.write(f"{index}\n{_srt_time(start_s)} --> {_srt_time(end_s)}\n{text}\n\n")
    return len(entries)


def _resolve_clip_subtitle_source(flv_path, data):
    """优先使用流水线校对字幕，兼容旧 JSON 回退到同名 SRT。"""
    candidates = [
        data.get("corrected_srt_path"),
        flv_path[:-4] + "_校对字幕.srt",
        data.get("srt_path"),
        flv_path[:-4] + ".srt",
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return os.path.abspath(path)
    return None


def _publish_title_report_lines(clip_marks):
    """生成 AutoCover 可直接解析的投稿标题区，只包含最终实际切片。"""
    marks = _dedupe_clip_marks(clip_marks or [])
    if not marks:
        return []
    lines = ["## 投稿标题建议", ""]
    for index, mark in enumerate(marks, 1):
        start = _format_report_time(mark["start"])
        end = _format_report_time(mark["end"])
        filename = _topic_clip_filename(index, mark)
        publish_title = _normalise_publish_title(
            mark.get("publish_title"), mark.get("title", "未命名片段")
        )
        lines.extend([
            f"### {index:02d}（{start}－{end}）",
            "",
            f"原文件：`{filename}`",
            "",
            f"**{publish_title}**",
            "",
        ])
    return lines


def _refinement_manifest_paths(base_path):
    return base_path + "_精调任务.json", base_path + "_精调任务.md"


def _build_refinement_manifest(video_path, source_srt_path, corrected_srt_path,
                               analysis_report_path, clip_marks_path, clip_marks,
                               manifest_json_path, manifest_md_path):
    """构造一场录播的统一精调任务数据。"""
    tasks = []
    for index, mark in enumerate(_dedupe_clip_marks(clip_marks or []), 1):
        filename = _topic_clip_filename(index, mark)
        tasks.append({
            "id": f"{index:02d}",
            "status": "等待自动切片",
            "clip_filename": filename,
            "slice_path": None,
            "subtitle_path": None,
            "start": int(mark["start"]),
            "end": int(mark["end"]),
            "duration": int(mark["end"] - mark["start"]),
            "topic_start": int(mark.get("topic_start", mark["start"])),
            "topic_end": int(mark.get("topic_end", mark["end"])),
            "topic_title": mark.get("title", "未命名片段"),
            "publish_title": _normalise_publish_title(
                mark.get("publish_title"), mark.get("title", "未命名片段")
            ),
            "natural_boundary_pre_sec": int(mark.get("natural_boundary_pre_sec", 0)),
            "natural_boundary_post_sec": int(mark.get("natural_boundary_post_sec", 0)),
            "steps": [
                {"key": key, "label": label, "status": "待处理"}
                for key, label in REFINEMENT_WORKFLOW_STEPS
            ],
        })
    now = datetime.now().isoformat(timespec="seconds")
    return {
        "schema_version": 1,
        "status": "等待自动切片" if tasks else "无可切片段",
        "generated_at": now,
        "updated_at": now,
        "video_name": os.path.basename(video_path),
        "source_video_path": os.path.abspath(video_path),
        "source_srt_path": os.path.abspath(source_srt_path) if source_srt_path else None,
        "corrected_srt_path": os.path.abspath(corrected_srt_path) if corrected_srt_path else None,
        "analysis_report_path": os.path.abspath(analysis_report_path),
        "clip_marks_path": os.path.abspath(clip_marks_path),
        "manifest_json_path": os.path.abspath(manifest_json_path),
        "manifest_md_path": os.path.abspath(manifest_md_path),
        "slice_output_dir": None,
        "tasks": tasks,
    }


def _render_refinement_manifest_markdown(manifest):
    """把精调任务数据渲染成可直接勾选的 Markdown。"""
    lines = [
        f"# {manifest.get('video_name', '录播')} 精调任务清单",
        f"> 自动生成 | 总状态: {manifest.get('status', '待处理')} | "
        f"更新时间: {manifest.get('updated_at', '')}",
        "",
        "## 文件",
        "",
        f"- 源录播: `{manifest.get('source_video_path') or '无'}`",
        f"- 校对字幕: `{manifest.get('corrected_srt_path') or '无'}`",
        f"- 话题报告: `{manifest.get('analysis_report_path') or '无'}`",
        f"- 切片标记: `{manifest.get('clip_marks_path') or '无'}`",
        f"- 切片目录: `{manifest.get('slice_output_dir') or '等待自动切片'}`",
        f"- 精调总清单: `{manifest.get('unified_queue_md_path') or '未启用'}`",
        "",
        "## 切片队列",
        "",
    ]
    tasks = manifest.get("tasks") or []
    if not tasks:
        lines.append("本次没有可切片段。")
        lines.append("")
        return "\n".join(lines)
    for task in tasks:
        lines.extend([
            f"### {task.get('id')} {task.get('topic_title', '未命名片段')}",
            "",
            f"- 状态: {task.get('status', '待处理')}",
            f"- 视频内时间: {_format_report_time(task.get('start', 0))}－"
            f"{_format_report_time(task.get('end', 0))}（{task.get('duration', 0)} 秒）",
            f"- 切片文件: `{task.get('slice_path') or task.get('clip_filename')}`",
            f"- 片段字幕: `{task.get('subtitle_path') or '等待自动切片'}`",
            f"- 投稿标题: {task.get('publish_title', '')}",
        ])
        for step in task.get("steps") or []:
            checked = "x" if step.get("status") == "已完成" else " "
            lines.append(f"- [{checked}] {step.get('label')}（{step.get('status', '待处理')}）")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _unified_refinement_queue_paths(queue_dir=None):
    root = os.path.abspath(queue_dir or DEFAULT_REFINEMENT_QUEUE_DIR)
    return (
        os.path.join(root, UNIFIED_REFINEMENT_QUEUE_JSON),
        os.path.join(root, UNIFIED_REFINEMENT_QUEUE_MD),
    )


def _refinement_task_is_completed(task):
    status = str(task.get("status", "")).strip()
    if status in {"已完成", "已发布", "已投稿"}:
        return True
    steps = task.get("steps") or []
    return bool(steps) and all(step.get("status") == "已完成" for step in steps)


def _unified_refinement_record(manifest):
    """从单场清单提取总队列需要的信息，保留剪映阶段的关键路径和首尾依据。"""
    tasks = []
    for task in manifest.get("tasks") or []:
        tasks.append({
            "id": task.get("id"),
            "status": task.get("status", "待处理"),
            "topic_title": task.get("topic_title", "未命名片段"),
            "publish_title": task.get("publish_title", ""),
            "start": int(task.get("start", 0)),
            "end": int(task.get("end", 0)),
            "duration": int(task.get("duration", 0)),
            "topic_start": int(task.get("topic_start", task.get("start", 0))),
            "topic_end": int(task.get("topic_end", task.get("end", 0))),
            "natural_boundary_pre_sec": int(task.get("natural_boundary_pre_sec", 0)),
            "natural_boundary_post_sec": int(task.get("natural_boundary_post_sec", 0)),
            "clip_filename": task.get("clip_filename"),
            "slice_path": task.get("slice_path"),
            "subtitle_path": task.get("subtitle_path"),
            "steps": [dict(step) for step in task.get("steps") or []],
        })
    completed_count = sum(_refinement_task_is_completed(task) for task in tasks)
    ready_count = sum(
        not _refinement_task_is_completed(task) and task.get("status") == "待精调"
        for task in tasks
    )
    waiting_slice_count = sum(task.get("status") == "等待自动切片" for task in tasks)
    source_video_path = os.path.abspath(manifest.get("source_video_path") or manifest.get("video_name") or "")
    return {
        "recording_key": os.path.normcase(source_video_path),
        "video_name": manifest.get("video_name", os.path.basename(source_video_path)),
        "status": manifest.get("status", "待处理"),
        "updated_at": manifest.get("updated_at", datetime.now().isoformat(timespec="seconds")),
        "source_video_path": source_video_path,
        "corrected_srt_path": manifest.get("corrected_srt_path"),
        "analysis_report_path": manifest.get("analysis_report_path"),
        "manifest_json_path": manifest.get("manifest_json_path"),
        "manifest_md_path": manifest.get("manifest_md_path"),
        "slice_output_dir": manifest.get("slice_output_dir"),
        "task_count": len(tasks),
        "pending_count": len(tasks) - completed_count,
        "ready_count": ready_count,
        "waiting_slice_count": waiting_slice_count,
        "completed_count": completed_count,
        "tasks": tasks,
    }


def _render_unified_refinement_queue_markdown(queue):
    """渲染跨录播总队列，优先展示真正需要进入剪映的任务。"""
    lines = [
        "# AutoSlice 精调任务总清单",
        f"> 自动生成 | {queue.get('recording_count', 0)} 场录播 | "
        f"待处理 {queue.get('pending_count', 0)} 个切片 | "
        f"可进剪映 {queue.get('ready_count', 0)} 个 | "
        f"更新时间: {queue.get('updated_at', '')}",
        "",
        "## 当前队列",
        "",
    ]
    recordings = queue.get("recordings") or []
    if not recordings:
        lines.extend(["目前没有精调任务。", ""])
        return "\n".join(lines)
    for recording in recordings:
        lines.extend([
            f"### {recording.get('video_name', '录播')}",
            "",
            f"- 状态: {recording.get('status', '待处理')}；"
            f"待处理 {recording.get('pending_count', 0)}/{recording.get('task_count', 0)}",
            f"- 校对字幕: `{recording.get('corrected_srt_path') or '无'}`",
            f"- 单场清单: `{recording.get('manifest_md_path') or '无'}`",
            f"- 切片目录: `{recording.get('slice_output_dir') or '等待自动切片'}`",
            "",
        ])
        tasks = recording.get("tasks") or []
        if not tasks:
            lines.extend(["本场没有可切片段。", ""])
            continue
        for task in tasks:
            completed = _refinement_task_is_completed(task)
            checked = "x" if completed else " "
            pre_context = max(0, int(task.get("topic_start", 0)) - int(task.get("start", 0)))
            post_context = max(0, int(task.get("end", 0)) - int(task.get("topic_end", 0)))
            lines.extend([
                f"- [{checked}] {task.get('id', '')} {task.get('topic_title', '未命名片段')}"
                f"（{task.get('status', '待处理')}，{task.get('duration', 0)} 秒）",
                f"  - 视频内时间: {_format_report_time(task.get('start', 0))}－"
                f"{_format_report_time(task.get('end', 0))}",
                f"  - 切片: `{task.get('slice_path') or task.get('clip_filename') or '等待自动切片'}`",
                f"  - 片段字幕: `{task.get('subtitle_path') or '等待自动切片'}`",
                f"  - 首尾: 已在话题核心前保留 {pre_context} 秒、后保留 {post_context} 秒；"
                f"自然停顿额外调整前 {task.get('natural_boundary_pre_sec', 0)} 秒、"
                f"后 {task.get('natural_boundary_post_sec', 0)} 秒",
                f"  - 投稿标题: {task.get('publish_title', '')}",
            ])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _upsert_unified_refinement_queue(manifest, queue_json_path=None, queue_md_path=None):
    """按源录播更新总队列；并发流水线通过进程内锁避免互相覆盖。"""
    default_json_path, default_md_path = _unified_refinement_queue_paths()
    json_path = os.path.abspath(queue_json_path or default_json_path)
    md_path = os.path.abspath(queue_md_path or default_md_path)
    os.makedirs(os.path.dirname(json_path), exist_ok=True)
    os.makedirs(os.path.dirname(md_path), exist_ok=True)
    record = _unified_refinement_record(manifest)
    with _UNIFIED_REFINEMENT_QUEUE_LOCK:
        queue = {"schema_version": 1, "recordings": []}
        if os.path.isfile(json_path):
            try:
                with open(json_path, encoding="utf-8") as f:
                    existing = json.load(f)
                if isinstance(existing, dict) and isinstance(existing.get("recordings"), list):
                    queue = existing
            except (OSError, ValueError, TypeError):
                pass
        recordings = [
            item for item in queue.get("recordings") or []
            if isinstance(item, dict) and item.get("recording_key") != record["recording_key"]
        ]
        recordings.append(record)
        recordings.sort(key=lambda item: str(item.get("updated_at", "")), reverse=True)
        queue.update({
            "schema_version": 1,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "recording_count": len(recordings),
            "task_count": sum(int(item.get("task_count", 0)) for item in recordings),
            "pending_count": sum(int(item.get("pending_count", 0)) for item in recordings),
            "ready_count": sum(int(item.get("ready_count", 0)) for item in recordings),
            "waiting_slice_count": sum(int(item.get("waiting_slice_count", 0)) for item in recordings),
            "completed_count": sum(int(item.get("completed_count", 0)) for item in recordings),
            "recordings": recordings,
        })
        json_temp_path = json_path + ".tmp"
        md_temp_path = md_path + ".tmp"
        with open(json_temp_path, "w", encoding="utf-8") as f:
            json.dump(queue, f, ensure_ascii=False, indent=2)
        with open(md_temp_path, "w", encoding="utf-8") as f:
            f.write(_render_unified_refinement_queue_markdown(queue))
        os.replace(json_temp_path, json_path)
        os.replace(md_temp_path, md_path)
    return json_path, md_path


def _write_refinement_manifest_files(manifest):
    """同步写入 JSON 和 Markdown 两种任务清单。"""
    json_path = manifest["manifest_json_path"]
    md_path = manifest["manifest_md_path"]
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(_render_refinement_manifest_markdown(manifest))
    return json_path, md_path


def _update_refinement_manifest_after_slice(manifest_json_path, report_dir, marks):
    """自动切片完成后回写实际文件路径，保留已有人工步骤状态。"""
    if not manifest_json_path or not os.path.isfile(manifest_json_path):
        return False
    with open(manifest_json_path, encoding="utf-8") as f:
        manifest = json.load(f)
    tasks_by_name = {
        task.get("clip_filename"): task
        for task in manifest.get("tasks") or []
        if task.get("clip_filename")
    }
    found_count = 0
    for index, mark in enumerate(_dedupe_clip_marks(marks or []), 1):
        filename = _topic_clip_filename(index, mark)
        task = tasks_by_name.get(filename)
        if not task:
            continue
        output_path = os.path.abspath(os.path.join(report_dir, filename))
        task["slice_path"] = output_path
        subtitle_path = os.path.abspath(
            os.path.join(report_dir, _clip_subtitle_filename(filename))
        )
        task["subtitle_path"] = subtitle_path if os.path.isfile(subtitle_path) else None
        for step in task.get("steps") or []:
            if step.get("key") == "correct_subtitles":
                step["label"] = "导入片段同名校对字幕并检查专名"
        if os.path.isfile(output_path):
            task["status"] = "待精调"
            found_count += 1
        else:
            task["status"] = "切片文件缺失"
    manifest["slice_output_dir"] = os.path.abspath(report_dir)
    manifest["status"] = "待精调" if found_count else "切片文件缺失"
    manifest["updated_at"] = datetime.now().isoformat(timespec="seconds")
    queue_json_path = manifest.get("unified_queue_json_path")
    queue_md_path = manifest.get("unified_queue_md_path")
    if queue_json_path or queue_md_path:
        try:
            _upsert_unified_refinement_queue(
                manifest,
                queue_json_path=queue_json_path,
                queue_md_path=queue_md_path,
            )
            manifest["unified_queue_warning"] = None
        except (OSError, ValueError, TypeError) as e:
            manifest["unified_queue_warning"] = f"精调总清单更新失败: {e}"
    _write_refinement_manifest_files(manifest)
    return True


def _build_timeline_report(video_name, peak_info, topics, failed_chunks=None, api_warning=None, streamer_name="主播", group_by_hour=False, manual_timeline=None, clip_marks=None, corrected_srt_path=None, unified_queue_md_path=None):
    """生成最终 Markdown：逐话题时间轴 + Part 分组。"""
    manual_timeline = manual_timeline or {}
    manual_entries = manual_timeline.get("entries") or []
    lines = [
        f"# {video_name} 话题分析报告",
        f"> 自动生成 | 模型: {LLM_MODEL} | {peak_info}",
        "> 时间基准：视频内时间/播放进度（不是现实钟点）；实际切片会自动向前后扩展保留上下文",
    ]
    if corrected_srt_path:
        lines.append(f"> 剪映校对字幕: {os.path.basename(corrected_srt_path)}")
    if unified_queue_md_path:
        lines.append(f"> 精调总清单: {unified_queue_md_path}")
    if manual_timeline.get("path"):
        star_count = sum(1 for item in manual_entries if item.get("stars", 0) > 0)
        source_count = manual_timeline.get("source_entry_count", len(manual_entries))
        raw_count = manual_timeline.get("raw_entry_count", source_count)
        optimized_count = manual_timeline.get("optimized_entry_count")
        if optimized_count is not None:
            count_label = f"当前分段原始 {raw_count} 条 → 字幕优化 {optimized_count} 个候选"
        else:
            count_label = (
                f"当前分段 {len(manual_entries)}/{source_count} 条记录"
                if source_count != len(manual_entries)
                else f"{len(manual_entries)} 条记录"
            )
        lines.append(
            f"> 人工时间轴辅助: {os.path.basename(manual_timeline['path'])} | "
            f"{count_label}, ⭐重点 {star_count} 条"
        )
        if manual_timeline.get("optimized_md_path"):
            lines.append(f"> 字幕优化时间轴: {manual_timeline['optimized_md_path']}")
    lines.extend(["---", "", "## 逐话题时间轴", ""])

    groups = _group_topics_for_parts(topics)
    if not groups:
        lines.append("本次没有解析到有效话题。")
        lines.append("")
    else:
        topic_index = 1
        if group_by_hour:
            iterable = _group_topics_by_hour(topics)
        else:
            iterable = [(idx - 1, group) for idx, group in enumerate(_group_topics_for_parts(topics), 1)]
        for display_part_index, (part_index, group) in enumerate(iterable, 1):
            part_start = min(t["start"] for t in group)
            part_end = max(t["end"] for t in group)
            if group_by_hour:
                part_title = f"第{part_index + 1}小时重点"
            else:
                part_title = _make_part_title(group, streamer_name=streamer_name)
            lines.append(
                f"Part {display_part_index}: {part_title} "
                f"({_format_report_time(part_start)}－{_format_report_time(part_end)})"
            )
            for topic in group:
                lines.append(_format_topic_block(topic, topic_index, streamer_name=streamer_name))
                topic_index += 1
            lines.append("")

    publish_title_lines = _publish_title_report_lines(clip_marks)
    if publish_title_lines:
        lines.extend(publish_title_lines)

    if api_warning:
        lines.append("## API 预检警告")
        lines.append("")
        lines.append(f"- 预检遇到临时错误，已继续尝试正式分块：{api_warning}")
        lines.append("")

    if failed_chunks:
        lines.append("## LLM 分块失败记录")
        lines.append("")
        for item in failed_chunks:
            lines.append(
                f"- 块 {item.get('index')} [{item.get('time')}] "
                f"连续失败，已跳过：{item.get('error')}"
            )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _analyze_topic_chunks(chunks, streamer_display_name, progress_callback=None):
    """逐块独立分析完整字幕和弹幕，不读取人工时间轴。"""
    if not chunks:
        return [], [], None

    api_warning = None
    if progress_callback:
        progress_callback("Step 4/5: 预检 API 连通性...", 22, 100)
    try:
        test_resp = _call_llm_with_retry(
            "只输出 OK 两个字母，不要解释，不要推理。",
            max_tokens=1000,
            attempts=3,
            progress_callback=progress_callback,
            progress_label="API预检",
            progress_step=22,
        )
        if not test_resp or len(test_resp.strip()) < 1:
            raise RuntimeError("API 返回空内容")
    except Exception as exc:
        message = _short_llm_error(exc)
        if not _is_retryable_llm_error(exc):
            if progress_callback:
                progress_callback(f"API 预检失败: {message}", 0, 100)
            raise RuntimeError(f"API 预检失败: {message}") from exc
        api_warning = f"API 预检遇到临时错误，正式分块已继续重试：{message}"
        if progress_callback:
            progress_callback(api_warning, 22, 100)

    accepted_topics = []
    failed_chunks = []
    consecutive_failed_chunks = 0
    successful_response_count = 0
    total = len(chunks)
    for index, chunk in enumerate(chunks):
        pct = 25 + int((index / total) * 68)
        chunk_time = fmt_time(chunk["start"])
        if progress_callback:
            progress_callback(
                f"Step 4/5: LLM分析 ({index + 1}/{total}, {chunk_time})...",
                pct,
                100,
            )

        prompt, chunk_start, chunk_end = _build_chunk_prompt(
            chunk,
            index,
            total,
            compact=False,
            streamer_name=streamer_display_name,
        )
        compact_prompt, _, _ = _build_chunk_prompt(
            chunk,
            index,
            total,
            compact=True,
            streamer_name=streamer_display_name,
        )
        try:
            response = _call_llm_with_retry(
                prompt,
                compact_prompt=compact_prompt,
                require_json=True,
                progress_callback=progress_callback,
                progress_label=f"块 {index + 1} API",
                progress_step=pct,
            )
        except Exception as exc:
            error = _short_llm_error(exc)
            failed_chunks.append({
                "index": index + 1,
                "start": int(chunk_start),
                "end": int(chunk_end),
                "time": fmt_time(chunk_start),
                "error": error,
            })
            if progress_callback:
                progress_callback(f"块 {index + 1} API 连续失败，已跳过: {error}", pct, 100)
            consecutive_failed_chunks = (
                consecutive_failed_chunks + 1
                if _is_retryable_llm_error(exc)
                else MAX_INITIAL_FAILED_CHUNKS
            )
            if (
                consecutive_failed_chunks >= MAX_INITIAL_FAILED_CHUNKS
                and successful_response_count == 0
            ):
                raise RuntimeError(
                    f"LLM API 连续 {consecutive_failed_chunks} 个分块失败，疑似上游服务不可用。"
                    f"最后错误: {error}"
                ) from exc
            fallback_topic = _make_fallback_topic_from_chunk(
                chunk,
                streamer_name=streamer_display_name,
            )
            if fallback_topic and not _is_duplicate_topic(fallback_topic, accepted_topics):
                accepted_topics.append(fallback_topic)
            continue

        successful_response_count += 1
        consecutive_failed_chunks = 0
        before_topic_count = len(accepted_topics)
        _parse_llm_response(
            response,
            chunk_start,
            chunk_end,
            accepted_topics,
            allow_markdown_fallback=False,
        )
        if len(accepted_topics) == before_topic_count:
            fallback_topic = _make_fallback_topic_from_chunk(
                chunk,
                streamer_name=streamer_display_name,
            )
            if fallback_topic and not _is_duplicate_topic(fallback_topic, accepted_topics):
                accepted_topics.append(fallback_topic)
        time.sleep(0.3)

    return accepted_topics, failed_chunks, api_warning


def _validate_unmatched_manual_topics(topics, streamer_name="音音", progress_callback=None):
    """后置复核首轮遗漏的时间轴候选；失败时只保留报告线索。"""
    manual_topics = [
        topic for topic in topics
        if topic.get("source") in {"manual_timeline", "optimized_manual_timeline"}
        and (not topic.get("ai_enriched") or topic.get("postcheck_pending"))
    ]
    if not manual_topics:
        return None

    original_ids = {id(topic) for topic in manual_topics}
    try:
        _enrich_manual_topics_with_llm(
            manual_topics,
            streamer_name=streamer_name,
            progress_callback=progress_callback,
        )
    except Exception as exc:
        for topic in topics:
            if id(topic) in original_ids:
                topic["reference_only"] = True
        return (
            "人工星标补充复核失败，未核验条目仅写入报告且不会自动切片："
            f"{_short_llm_error(exc)}"
        )

    topics[:] = [topic for topic in topics if id(topic) not in original_ids]
    topics.extend(manual_topics)
    topics.sort(key=lambda item: (item["start"], item["end"]))
    return None


def run_pipeline(
        flv_path, ass_path=None, progress_callback=None, manual_timeline_path=None,
        optimized_timeline_path=None):
    """
    完整流水线：SRT → 弹幕 → LLM分析 → 报告 + 切片标记

    返回: {
        "report": str (Markdown),
        "clip_marks": [{"start": s, "end": s, "title": str}, ...],
        "json_path": str,
        "md_path": str,
    }
    """
    video_name = os.path.basename(flv_path)
    base = flv_path[:-4]
    streamer_name = _infer_streamer_name(flv_path)
    streamer_display_name = _streamer_report_name(streamer_name)
    unified_queue_json_path, unified_queue_md_path = _unified_refinement_queue_paths()

    # Step 1: 确保 SRT 存在
    if progress_callback:
        progress_callback("Step 1/5: 检查/生成字幕...", 0, 100)
    source_srt_path = ensure_srt(flv_path, progress_callback)
    if not source_srt_path:
        raise RuntimeError("无法生成 SRT 字幕")
    corrected_srt_path = export_corrected_srt(source_srt_path)
    srt_path = corrected_srt_path or source_srt_path
    if corrected_srt_path and progress_callback:
        progress_callback(
            f"已生成剪映校对字幕: {os.path.basename(corrected_srt_path)}",
            12,
            100,
        )

    # Step 2: 弹幕分析
    if progress_callback:
        progress_callback("Step 2/5: 弹幕密度分析...", 15, 100)
    peaks = analyze_danmaku(ass_path) if ass_path else DanmakuDensitySeries()
    avg_den = _average_danmaku_density(peaks)
    if peaks:
        high_window_count = sum(
            density >= avg_den * CLIP_DENSITY_RATIO
            for _, density in peaks
        )
        peak_info = (
            f"弹幕密度 {len(peaks)} 个滑动窗口, "
            f"高能窗口 {high_window_count} 个, 全场平均密度 {avg_den:.0f}条/分钟"
        )
    else:
        peak_info = "无弹幕数据"

    # Step 3: SRT 分块
    if progress_callback:
        progress_callback("Step 3/5: SRT 分块中...", 20, 100)
    segs = parse_srt_text(srt_path)
    chunks = chunk_srt(segs, peaks)
    srt_duration = max((end for _, end, _ in segs), default=None)
    video_duration = _probe_video_duration(flv_path) or srt_duration
    if optimized_timeline_path:
        manual_timeline = _load_optimized_timeline_artifact(
            optimized_timeline_path,
            flv_path,
            manual_timeline_path=(
                manual_timeline_path
                if manual_timeline_path not in (None, "__none__")
                else None
            ),
        )
    else:
        manual_timeline = _prepare_optimized_manual_timeline(
            flv_path,
            base,
            segs,
            peaks,
            video_duration,
            manual_timeline_path,
            streamer_name=streamer_display_name,
            progress_callback=progress_callback,
        )
    raw_manual_entry_count = int(manual_timeline.get("raw_entry_count", 0))
    manual_entries = manual_timeline.get("entries") or []
    optimization_warning = manual_timeline.get("optimization_warning")
    if manual_entries:
        if progress_callback:
            count_label = (
                f"原始 {raw_manual_entry_count} 条 → 字幕优化 {len(manual_entries)} 个候选"
            )
            progress_callback(
                f"已加载人工时间轴: {os.path.basename(manual_timeline['path'])}，"
                f"{count_label}",
                21, 100,
            )
    # Step 4: 首轮只分析字幕和弹幕，避免人工措辞锚定标题与语义边界。
    accepted_topics, failed_chunks, api_precheck_warning = _analyze_topic_chunks(
        chunks,
        streamer_display_name,
        progress_callback=progress_callback,
    )
    if optimization_warning:
        api_precheck_warning = "；".join(
            item for item in (optimization_warning, api_precheck_warning) if item
        )

    # Step 5: 生成文件
    if progress_callback:
        progress_callback("Step 5/5: 生成报告...", 97, 100)

    _merge_manual_timeline_topics(accepted_topics, manual_entries)
    manual_validation_warning = _validate_unmatched_manual_topics(
        accepted_topics,
        streamer_name=streamer_display_name,
        progress_callback=progress_callback,
    )
    if manual_validation_warning:
        api_precheck_warning = "；".join(
            item for item in (api_precheck_warning, manual_validation_warning) if item
        )
    accepted_topics = _clean_topics_for_report(accepted_topics)
    _apply_danmaku_slice_decisions(accepted_topics, peaks, avg_den)
    raw_clip_marks = _clip_marks_from_topics(accepted_topics)
    srt_segments_for_context = parse_srt_segments(srt_path)
    clip_marks = _expand_clip_marks_with_context(
        raw_clip_marks,
        srt_segments=srt_segments_for_context,
        video_duration=_srt_video_duration(srt_segments_for_context),
    )
    report = _build_timeline_report(
        video_name, peak_info, accepted_topics,
        failed_chunks=failed_chunks, api_warning=api_precheck_warning,
        streamer_name=streamer_display_name,
        group_by_hour=True,
        manual_timeline=manual_timeline,
        clip_marks=clip_marks,
        corrected_srt_path=corrected_srt_path,
        unified_queue_md_path=unified_queue_md_path,
    )

    # 保存
    md_path = base + "_话题分析.md"
    json_path = base + "_clip_marks.json"
    task_manifest_json_path, task_manifest_md_path = _refinement_manifest_paths(base)

    with open(md_path, "w", encoding="utf-8") as f:
        f.write(report)

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({
            "video": video_name,
            "streamer_name": streamer_name,
            "streamer_display_name": streamer_display_name,
            "source_srt_path": source_srt_path,
            "corrected_srt_path": corrected_srt_path,
            "task_manifest_json_path": task_manifest_json_path,
            "task_manifest_md_path": task_manifest_md_path,
            "unified_queue_json_path": unified_queue_json_path,
            "unified_queue_md_path": unified_queue_md_path,
            "time_basis": "video_elapsed_seconds",
            "time_basis_note": "start/end 均为视频内秒数，不是真实钟点；topic_start/topic_end 为原话题范围，start/end 为含前后文的实际切片范围。",
            "expanded_with_context": True,
            "context_policy": {
                "pre_context_sec": TOPIC_PRE_CONTEXT_SEC,
                "post_context_sec": TOPIC_POST_CONTEXT_SEC,
                "min_clip_sec": TOPIC_MIN_CLIP_SEC,
                "max_clip_sec": TOPIC_MAX_CLIP_SEC,
                "required_context_overflow_sec": TOPIC_REQUIRED_CONTEXT_OVERFLOW_SEC,
            },
            "api_precheck_warning": api_precheck_warning,
            "failed_chunks": failed_chunks,
            "manual_timeline": _manual_timeline_summary(manual_timeline),
            "clip_marks": clip_marks,
        }, f, ensure_ascii=False, indent=2)

    refinement_manifest = _build_refinement_manifest(
        flv_path,
        source_srt_path,
        corrected_srt_path,
        md_path,
        json_path,
        clip_marks,
        task_manifest_json_path,
        task_manifest_md_path,
    )
    refinement_manifest["unified_queue_json_path"] = unified_queue_json_path
    refinement_manifest["unified_queue_md_path"] = unified_queue_md_path
    _write_refinement_manifest_files(refinement_manifest)
    unified_queue_warning = None
    try:
        _upsert_unified_refinement_queue(
            refinement_manifest,
            queue_json_path=unified_queue_json_path,
            queue_md_path=unified_queue_md_path,
        )
    except (OSError, ValueError, TypeError) as e:
        unified_queue_warning = f"精调总清单更新失败: {e}"
        if progress_callback:
            progress_callback(unified_queue_warning, 99, 100)

    if progress_callback:
        progress_callback(
            f"完成! {len(clip_marks)} 个可切片段 → {json_path}",
            100, 100
        )

    return {
        "report": report,
        "topic_count": len(accepted_topics),
        "clip_marks": clip_marks,
        "json_path": json_path,
        "md_path": md_path,
        "srt_path": srt_path,
        "source_srt_path": source_srt_path,
        "corrected_srt_path": corrected_srt_path,
        "task_manifest_json_path": task_manifest_json_path,
        "task_manifest_md_path": task_manifest_md_path,
        "unified_queue_json_path": unified_queue_json_path,
        "unified_queue_md_path": unified_queue_md_path,
        "unified_queue_warning": unified_queue_warning,
        "failed_chunks": failed_chunks,
        "api_precheck_warning": api_precheck_warning,
        "manual_timeline": _manual_timeline_summary(manual_timeline),
    }


_GENERATED_TOPIC_ARTIFACT_RE = re.compile(
    r'^\d{2,3}_\d+s_.+\.(?:flv|srt)$',
    re.IGNORECASE,
)


def _cleanup_stale_topic_clips(report_dir):
    """清理同目录旧自动视频/字幕，保留用户手工命名文件和其他副产物。"""
    if not os.path.isdir(report_dir):
        return 0
    removed = 0
    for name in os.listdir(report_dir):
        if not _GENERATED_TOPIC_ARTIFACT_RE.fullmatch(name):
            continue
        path = os.path.join(report_dir, name)
        if not os.path.isfile(path):
            continue
        os.remove(path)
        removed += 1
    return removed


def slice_from_marks(flv_path, json_path, output_dir, progress_callback=None):
    """
    【新功能】根据话题分析生成的 clip_marks.json 自动切片。
    完全独立于现有的弹幕切片和时间轴切片模式。

    返回: (切片数, 输出子目录)
    """
    if not os.path.exists(json_path):
        raise FileNotFoundError(f"JSON 文件不存在: {json_path}")

    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)

    marks = _dedupe_clip_marks(data.get("clip_marks", []))
    if not data.get("expanded_with_context"):
        srt_segments_for_context = parse_srt_segments(flv_path[:-4] + ".srt")
        marks = _expand_clip_marks_with_context(
            marks,
            srt_segments=srt_segments_for_context,
            video_duration=_srt_video_duration(srt_segments_for_context),
        )
    if not marks:
        if progress_callback:
            progress_callback("无切片标记", 0, 1)
        return 0, ""

    import subprocess as sp
    video_name = os.path.basename(flv_path)
    base_name = os.path.splitext(video_name)[0]
    report_dir = os.path.join(output_dir, base_name + "_话题切片")
    os.makedirs(report_dir, exist_ok=True)
    removed_count = _cleanup_stale_topic_clips(report_dir)
    subtitle_source_path = _resolve_clip_subtitle_source(flv_path, data)
    subtitle_segments = (
        parse_srt_segments(subtitle_source_path)
        if subtitle_source_path
        else []
    )

    if progress_callback:
        if removed_count:
            progress_callback(f"已清理 {removed_count} 个旧自动切片", 0, len(marks))
        progress_callback(f"开始切片 ({len(marks)} 段)...", 0, len(marks))

    count = 0
    for i, m in enumerate(marks):
        start_s = m["start"]
        end_s = m["end"]
        title = m.get("title", f"片段{i+1}")
        duration = end_s - start_s
        if duration <= 0:
            continue

        output_name = _topic_clip_filename(i + 1, m)
        output_path = os.path.join(report_dir, output_name)

        if progress_callback:
            progress_callback(f"切片 {i+1}/{len(marks)}: {title}", i+1, len(marks))

        sp.run([
            "ffmpeg", "-y", "-ss", str(start_s), "-i", flv_path,
            "-t", str(duration), "-c", "copy", output_path
        ], check=True, stdout=sp.PIPE, stderr=sp.DEVNULL,
           encoding="utf-8", errors="replace")
        if subtitle_segments:
            subtitle_path = os.path.join(
                report_dir,
                _clip_subtitle_filename(output_name),
            )
            _write_clip_srt(
                subtitle_segments,
                start_s,
                end_s,
                subtitle_path,
            )
        count += 1

    _update_refinement_manifest_after_slice(
        data.get("task_manifest_json_path"),
        report_dir,
        marks,
    )

    if progress_callback:
        progress_callback(f"完成! {count} 个片段 → {report_dir}", len(marks), len(marks))

    return count, report_dir


def _parse_hms(s):
    parts = s.split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    return int(parts[0]) * 60 + int(parts[1])


# ============================================================
# CLI 测试
# ============================================================
if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) > 1:
        flv = sys.argv[1]
        ass = flv[:-4] + ".ass" if not os.path.exists(flv[:-4] + ".ass") else flv[:-4] + ".ass"
        if not os.path.exists(ass):
            ass = None
        result = run_pipeline(flv, ass, progress_callback=lambda m, s, t: print(f"[{s}%] {m}"))
        print(f"\n报告: {result['md_path']}")
        print(f"切片标记: {len(result['clip_marks'])} 个")
        for cm in result['clip_marks'][:10]:
            print(f"  [{fmt_time(cm['start'])}-{fmt_time(cm['end'])}] {cm['title']}")
    else:
        print("用法: python topic_engine.py <视频.flv>")
