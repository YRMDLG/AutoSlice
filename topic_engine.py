"""
话题分析 + 智能切片引擎

流水线: FunASR转录 → 弹幕密度分析 → SRT分块 → DeepSeek Pro分析 → 报告 + 切片标记

用法:
  from topic_engine import run_pipeline
  result = run_pipeline(flv_path, ass_path, progress_callback=cb)
  # result: {"report": "...", "clip_marks": [...], "json_path": "..."}
"""

import html
import os, re, json, time, zipfile, requests
from collections import defaultdict
from datetime import datetime, timedelta


# ============================================================
# 配置
# ============================================================
CHUNK_SEC = 600          # 每块 10 分钟：减少 API 调用，降低话题被硬切碎的概率
LLM_MODEL = "deepseek-v4-pro"
LLM_MAX_TOKENS = 6000
LLM_COMPACT_MAX_TOKENS = 3000
LLM_FULL_TEXT_CHARS = 8000
LLM_COMPACT_TEXT_CHARS = 2200
LLM_RETRY_DELAYS = (3, 8, 20, 45)
MAX_INITIAL_FAILED_CHUNKS = 3
DANMAKU_WINDOW = 60
DENSITY_RATIO = 0.30     # 弹幕密度阈值（稍低，用于标记而非切片）
CLIP_DENSITY_RATIO = 1.20  # 话题切片至少需要达到全场平均的 1.2 倍
TOPIC_PRE_CONTEXT_SEC = 90      # 话题切片向前保留前因
TOPIC_POST_CONTEXT_SEC = 120    # 话题切片向后保留后果/反应
TOPIC_MIN_CLIP_SEC = 180        # 太短的高能点至少扩成 3 分钟上下文
TOPIC_MAX_CLIP_SEC = 900        # 防止单个切片过长（原话题超过该值时不强行截断）
TOPIC_CONTEXT_GAP = 4.0         # SRT 语句间隔边界
SC_CONTEXT_LOOKBACK_SEC = 240   # 话题前 4 分钟内的 SC/礼物触发点会纳入切片
SRT_ABNORMAL_CHARS_PER_SEC = 18 # 超过该语速视为 ASR 时间戳异常
SRT_ESTIMATED_CHARS_PER_SEC = 7 # 异常长字幕按该语速估算结束时间
SRT_MAX_ESTIMATED_SEG_SEC = 300 # 单条异常字幕最多估算 5 分钟
TOPIC_MIN_REPORT_SEC = 60       # 正文较多但模型给出几秒时，报告至少扩到 1 分钟
TOPIC_MAX_REPAIRED_REPORT_SEC = 180
MANUAL_TIMELINE_DIR = r"F:\切片时间轴"
MANUAL_TIMELINE_CHUNK_MARGIN_SEC = 180
MANUAL_TIMELINE_TOPIC_PRE_SEC = 30
MANUAL_TIMELINE_TOPIC_POST_SEC = 150
MANUAL_TIMELINE_STAR_DENSITY_RATIO = 0.80

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


def fmt_time(seconds):
    return str(timedelta(seconds=int(seconds)))


def _infer_streamer_name(video_path):
    """从录播路径推断主播名；例如 1947277414-泽音Melody -> 泽音Melody。"""
    parts = re.split(r'[\\/]+', video_path or "")
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
        r'(?P<y>\d{4})年(?P<m>\d{1,2})月(?P<d>\d{1,2})号[-_\s]*'
        r'(?P<h>\d{1,2})点(?P<mi>\d{1,2})分(?P<s>\d{1,2})秒',
        r'(?P<y>\d{4})[-.](?P<m>\d{1,2})[-.](?P<d>\d{1,2})[-_\s]+'
        r'(?P<h>\d{1,2})[-点:](?P<mi>\d{1,2})[-分:](?P<s>\d{1,2})',
    )
    for text in candidates:
        for pattern in patterns:
            match = re.search(pattern, text)
            if not match:
                continue
            parts = {key: int(value) for key, value in match.groupdict().items()}
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
    current_date = video_start.date()
    last_event_dt = None
    header_re = re.compile(r'(\d{4})-(\d{1,2})-(\d{1,2})\s+\d{1,2}:\d{2}:\d{2}')
    event_re = re.compile(r'^\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s+(.+?)\s*$')
    for raw in lines or []:
        line = re.sub(r'\s+', ' ', str(raw or "")).strip()
        if not line:
            continue
        header = header_re.search(line)
        if header and "记录如下" in line:
            y, m, d = map(int, header.groups())
            try:
                current_date = datetime(y, m, d).date()
            except ValueError:
                pass
            continue
        match = event_re.match(line)
        if not match:
            continue
        h, minute, sec, text = match.groups()
        second = int(sec or 0)
        try:
            event_dt = datetime(
                current_date.year, current_date.month, current_date.day,
                int(h), int(minute), second,
            )
        except ValueError:
            continue
        if event_dt < video_start - timedelta(minutes=10):
            event_dt += timedelta(days=1)
        if last_event_dt and event_dt + timedelta(hours=12) < last_event_dt:
            event_dt += timedelta(days=1)
            current_date = event_dt.date()
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
        last_event_dt = event_dt
    return entries


def load_manual_timeline(video_path, timeline_dir=MANUAL_TIMELINE_DIR):
    """加载与录播日期匹配的人工时间轴 docx。"""
    video_start = _extract_video_start_datetime(video_path)
    doc_path = _find_manual_timeline_doc(video_path, timeline_dir)
    if not video_start or not doc_path:
        return {"path": None, "entries": [], "video_start": video_start}
    entries = _parse_manual_timeline_lines(_read_docx_lines(doc_path), video_start)
    return {"path": doc_path, "entries": entries, "video_start": video_start}


def _format_manual_entry_for_prompt(entry):
    stars = "⭐" * min(int(entry.get("stars", 0)), 5)
    prefix = f"{stars} " if stars else ""
    return f"- [{fmt_time(entry['start'])} / {entry.get('clock', '')}] {prefix}{entry.get('text', '')}"


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
    return int(topic["start"]) - margin <= int(entry["start"]) <= int(topic["end"]) + margin


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
    """把 ⭐ 人工重点附加到话题；LLM 漏掉时补一个人工重点话题。"""
    if not entries:
        return topics
    for topic in topics:
        if not _is_manual_merge_target(topic):
            continue
        matched = [entry for entry in entries if _manual_entry_matches_topic(entry, topic)]
        if not matched:
            continue
        topic["manual_stars"] = max([topic.get("manual_stars", 0)] + [entry.get("stars", 0) for entry in matched])
        topic["manual_timeline"] = matched
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
        if entry.get("stars", 0) <= 0:
            continue
        if any(_is_manual_merge_target(topic) and _manual_entry_matches_topic(entry, topic) for topic in topics):
            continue
        topic = {
            "start": max(0, int(entry["start"]) - MANUAL_TIMELINE_TOPIC_PRE_SEC),
            "end": int(entry["start"]) + MANUAL_TIMELINE_TOPIC_POST_SEC,
            "start_str": fmt_time(max(0, int(entry["start"]) - MANUAL_TIMELINE_TOPIC_PRE_SEC)),
            "end_str": fmt_time(int(entry["start"]) + MANUAL_TIMELINE_TOPIC_POST_SEC),
            "title": _manual_title_from_text(entry["text"]),
            "can_slice": False,
            "body": [f"●人工时间轴{'⭐' * min(entry.get('stars', 0), 5)}：{fmt_time(entry['start'])} {entry['text']}"],
            "manual_stars": entry.get("stars", 0),
            "manual_timeline": [entry],
            "source": "manual_timeline",
        }
        if not _is_duplicate_topic(topic, [old for old in topics if _is_manual_merge_target(old)]):
            topics.append(topic)
    topics.sort(key=lambda item: (item["start"], item["end"]))
    return topics


def _topics_from_manual_timeline(entries, max_gap_sec=240):
    """人工时间轴快路径：按时间相近记录聚合成干净话题。"""
    sorted_entries = sorted(entries or [], key=lambda item: item["start"])
    groups = []
    current = []
    for entry in sorted_entries:
        if not current:
            current = [entry]
            continue
        same_hour = int(entry["start"] // 3600) == int(current[-1]["start"] // 3600)
        if same_hour and entry["start"] - current[-1]["start"] <= max_gap_sec:
            current.append(entry)
        else:
            groups.append(current)
            current = [entry]
    if current:
        groups.append(current)

    topics = []
    for group in groups:
        title_entry = next((item for item in group if item.get("stars", 0) > 0), group[0])
        body = []
        for item in group:
            time_label = fmt_time(item["start"])
            if item.get("stars", 0) > 0:
                stars = "⭐" * min(item.get("stars", 0), 5)
                body.append(f"●人工时间轴{stars}：{time_label} {item['text']}")
            else:
                body.append(f"·时间轴：{time_label} {item['text']}")
        topic = {
            "start": max(0, int(group[0]["start"]) - (MANUAL_TIMELINE_TOPIC_PRE_SEC if title_entry.get("stars", 0) else 0)),
            "end": int(group[-1]["start"]) + (MANUAL_TIMELINE_TOPIC_POST_SEC if title_entry.get("stars", 0) else 120),
            "start_str": fmt_time(max(0, int(group[0]["start"]) - (MANUAL_TIMELINE_TOPIC_PRE_SEC if title_entry.get("stars", 0) else 0))),
            "end_str": fmt_time(int(group[-1]["start"]) + (MANUAL_TIMELINE_TOPIC_POST_SEC if title_entry.get("stars", 0) else 120)),
            "title": _manual_title_from_text(title_entry["text"]),
            "can_slice": False,
            "body": body,
            "manual_stars": max(item.get("stars", 0) for item in group),
            "manual_timeline": group,
            "source": "manual_timeline",
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

    if progress_callback:
        progress_callback("加载 FunASR 模型(CPU)...", 10, 100)

    model = AutoModel(
        model="iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
        device="cuda:0", disable_update=True
    )

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
    n_chunks = max(1, int(dur / chunk_dur))

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
                        for t in ts:
                            if len(t) == 2:
                                all_segs.append((start_t + t[0]/1000.0, start_t + t[1]/1000.0, text))
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

def analyze_danmaku(ass_path):
    """提取弹幕密度峰值，返回 [(start_s, density), ...]"""
    if not ass_path or not os.path.exists(ass_path):
        return []

    timestamps = []
    with open(ass_path, encoding="utf-8") as f:
        for line in f:
            if line.startswith("Dialogue:"):
                parts = line.split(",")
                h, m, s = parts[1].strip().split(":")
                timestamps.append(int(h) * 3600 + int(m) * 60 + float(s))

    time_counts = defaultdict(int)
    for t in timestamps:
        time_counts[t] += 1

    sorted_times = sorted(time_counts.keys())
    peaks = []
    for i in range(0, len(sorted_times), max(1, len(sorted_times) // 200)):
        start = sorted_times[i]
        end = start + DANMAKU_WINDOW
        density = sum(c for t, c in time_counts.items() if start <= t < end)
        peaks.append((int(start), density))

    peaks.sort(key=lambda x: x[1], reverse=True)
    if peaks:
        threshold = max(peaks[0][1] * DENSITY_RATIO, 2)
        peaks = [(s, d) for s, d in peaks if d >= threshold]

    return peaks


# ============================================================
# Step 3: SRT 解析 + 分块
# ============================================================

def parse_srt_text(srt_path):
    """解析 SRT，去空格，返回 [(start_s, end_s, text), ...]，并修复明显异常时间戳。"""
    if not os.path.exists(srt_path):
        return []
    with open(srt_path, encoding="utf-8") as f:
        content = f.read()
    pattern = r'(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})\s*\n(.*?)(?=\n\n|\Z)'
    matches = re.findall(pattern, content, re.DOTALL)
    segs = []
    for start_str, end_str, text in matches:
        start_s = _parse_srt_timestamp(start_str)
        end_s = _parse_srt_timestamp(end_str)
        text = text.strip().replace("\n", " ").replace(" ", "")
        if len(text) < 2:
            continue
        if segs and text == segs[-1][2]:
            # FunASR 偶发把同一大段按 0.x 秒重复刷几十次，保留一条即可。
            segs[-1] = (segs[-1][0], max(segs[-1][1], _repair_srt_end_time(start_s, end_s, text)), text)
            continue
        segs.append((start_s, _repair_srt_end_time(start_s, end_s, text), text))
    return sorted(segs, key=lambda x: x[0])


def chunk_srt(segs, peaks, chunk_sec=CHUNK_SEC):
    """将 SRT 按时间分块，每块附带弹幕密度信息"""
    if not segs:
        return []
    # 计算全场平均密度
    all_densities = [d for _, d in peaks] if peaks else [0]
    avg_density = sum(all_densities) / len(all_densities) if all_densities else 0

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

## 人工时间轴参考

- 如果提示中提供“人工时间轴参考”，它是人工整理的墙钟时间，程序已换算成视频内时间；可作为字幕识别错误时的辅助证据
- 带 ⭐ 的人工时间轴片段更值得留意：如果它与当前字幕/弹幕能对上，优先整理成具体话题，can_slice 可更积极
- 人工时间轴只是辅助，不要照抄成解释说明；最终 points 仍要写成自然的内容要点

## 输出格式：只输出 JSON，不要输出 Markdown

**关键要求：**
- 只输出一个 JSON 对象，不要输出解释、草稿、分析过程、代码块或 Markdown
- JSON 格式严格如下：
{"topics":[{"start":"0:04:00","end":"0:08:00","title":"话题标题","can_slice":false,"points":["具体要点，写清楚事情经过","补充细节"]}]}
- 时间戳精确到秒，格式 `H:MM:SS`，例如 `0:04:00`
- 标题 5-15 字，概括核心内容，可加合适 emoji
- 每个话题 2-6 条 points；礼物、弹幕爆点、观众金句可直接写进 points
- 遇到 SC/醒目留言/观众长留言时，尽量保留观众开头对主播的称呼，例如“音姐……”“音音……”“麻麻……”
- 不要编造字幕里没有的信息
- 不要输出任何示例内容
- 不要解释为什么切或不切，不要在 points 里写弹幕密度判断、格式说明、推理过程；切片只用 can_slice 表示
- 不要写“我决定/现在写/标题可以/只能基于字幕/注意起始时间”等模型思考过程"""


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
            timeout=120,
            proxies={"http": None, "https": None},
        )
        resp.raise_for_status()
        data = resp.json()
        # 兼容不同 OpenAI 响应格式
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        if not content:
            reasoning_content = data.get("choices", [{}])[0].get("message", {}).get("reasoning_content", "")
            # DeepSeek Pro 可能把主要 token 用在 reasoning_content。
            # 只允许 reasoning_content 中的完整 JSON 进入后续结构化解析，禁止把普通推理文本当报告。
            if reasoning_content and "{" in reasoning_content and "}" in reasoning_content:
                content = reasoning_content
        if not content:
            content = data["choices"][0].get("text", "")
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
            timeout=120,
            proxies={"http": None, "https": None},
        )
        resp.raise_for_status()
        data = resp.json()
        for block in data.get("content", []):
            if block["type"] == "text":
                return block["text"]
        return ""


def _short_llm_error(error):
    """把 LLM/API 异常压缩成适合进度显示的一行。"""
    if isinstance(error, requests.HTTPError) and error.response is not None:
        text = (error.response.text or "").replace("\n", " ").strip()
        return f"HTTP {error.response.status_code}: {text[:160]}"
    return str(error)[:200]


def _is_retryable_llm_error(error):
    """判断是否适合重试：服务端 5xx、限流 429、连接/超时。"""
    if isinstance(error, (requests.Timeout, requests.ConnectionError)):
        return True
    if isinstance(error, requests.HTTPError) and error.response is not None:
        status = error.response.status_code
        return status == 429 or 500 <= status < 600
    return False


def _call_llm_with_retry(prompt, compact_prompt=None, max_tokens=LLM_MAX_TOKENS,
                         compact_max_tokens=LLM_COMPACT_MAX_TOKENS, attempts=None,
                         sleep_func=time.sleep, progress_callback=None,
                         progress_label="API", progress_step=0):
    """对临时性 LLM/API 错误做退避重试；连续失败后再抛出。"""
    total_attempts = attempts or (len(LLM_RETRY_DELAYS) + 1)
    last_error = None
    for attempt in range(total_attempts):
        use_compact = compact_prompt is not None and attempt >= 2
        active_prompt = compact_prompt if use_compact else prompt
        active_tokens = compact_max_tokens if use_compact else max_tokens
        try:
            return call_llm(active_prompt, max_tokens=active_tokens)
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
    """构造分块 prompt；compact=True 用于 API 5xx 后降级。"""
    chunk_start = ch["start"]
    chunk_end = ch.get("end", ch["start"] + CHUNK_SEC)
    text_limit = LLM_COMPACT_TEXT_CHARS if compact else LLM_FULL_TEXT_CHARS
    if compact:
        prompt_head = (
            "你是直播逐话题时间轴整理助手。只分析当前分块，只输出最终话题条目；"
            "当前分块有连续讲话时只整理成1-2个核心话题，内容特别密集最多3个；普通闲聊/游戏过程也要写；"
            "只有几乎无有效讲话才输出“无明显话题”。"
            "人工时间轴参考是辅助证据，带⭐的片段更值得留意，但不要输出解释说明。"
            "can_slice只给值得自动切片的段，不值得切也要写进报告。"
            "不要解释规则、不要写弹幕密度判断、不要写推理过程、不要写候选列表。"
            "只输出JSON对象：{\"topics\":[{\"start\":\"0:00:00\",\"end\":\"0:05:00\",\"title\":\"话题标题\",\"can_slice\":false,\"points\":[\"具体要点\"]}]}。\n\n"
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
        f"## 人工时间轴参考（已换算为视频内时间）\n{ch.get('manual_timeline_info') or '无'}\n\n"
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
    if not srt_path or not os.path.exists(srt_path):
        return []
    with open(srt_path, encoding="utf-8") as f:
        content = f.read()
    pattern = r'(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})\s*\n(.*?)(?=\n\n|\Z)'
    segments = []
    for start_str, end_str, text in re.findall(pattern, content, re.DOTALL):
        clean_text = text.strip().replace("\n", " ").strip()
        if not clean_text:
            continue
        start_s = _parse_srt_timestamp(start_str)
        end_s = _repair_srt_end_time(start_s, _parse_srt_timestamp(end_str), clean_text)
        if segments and clean_text == segments[-1][2]:
            segments[-1] = (segments[-1][0], max(segments[-1][1], end_s), clean_text)
            continue
        segments.append((start_s, end_s, clean_text))
    return sorted(segments, key=lambda x: x[0])


def _srt_video_duration(srt_segments):
    """用最后一句字幕估算可用视频时长。"""
    if not srt_segments:
        return None
    return max(seg_end for _, seg_end, _ in srt_segments)


def _snap_clip_to_srt_segments(start_s, end_s, srt_segments):
    """把切片边界吸附到完整字幕句，避免从半句话开始/结束。"""
    if not srt_segments:
        return start_s, end_s
    related = [seg for seg in srt_segments if seg[1] >= start_s and seg[0] <= end_s]
    if not related:
        return start_s, end_s
    return min(start_s, related[0][0]), max(end_s, related[-1][1])


def _looks_like_sc_or_gift_trigger(text):
    """判断字幕文本是否像 SC/礼物/付费留言触发点；兼容 ASR 把 SC 漏识别的情况。"""
    compact = re.sub(r'\s+', ' ', (text or "")).strip()
    if not compact:
        return False
    lower = compact.lower()
    if any(keyword in lower for keyword in SC_TRIGGER_KEYWORDS):
        return True
    return bool(THANKS_TRIGGER_RE.search(compact))


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

    idx, seg = candidates[-1]  # 用离话题最近的触发点，避免把更早无关礼物也切进来。
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


def _expand_clip_mark_with_context(mark, srt_segments=None, video_duration=None):
    """把 LLM 标记的话题范围扩展为真正用于 ffmpeg 的前后文切片范围。"""
    topic_start = int(float(mark.get("topic_start", mark["start"])))
    topic_end = int(float(mark.get("topic_end", mark["end"])))
    if topic_end <= topic_start:
        topic_end = topic_start + 1

    raw_duration = topic_end - topic_start
    start_s = max(0, topic_start - TOPIC_PRE_CONTEXT_SEC)
    end_s = topic_end + TOPIC_POST_CONTEXT_SEC
    sc_context_start = _find_sc_context_start(topic_start, srt_segments or [])
    if sc_context_start is not None:
        start_s = min(start_s, sc_context_start)

    if end_s - start_s < TOPIC_MIN_CLIP_SEC:
        deficit = TOPIC_MIN_CLIP_SEC - (end_s - start_s)
        left = int(deficit * 0.4)
        right = deficit - left
        start_s = max(0, start_s - left)
        end_s += right

    if raw_duration < TOPIC_MAX_CLIP_SEC and end_s - start_s > TOPIC_MAX_CLIP_SEC:
        end_s = start_s + TOPIC_MAX_CLIP_SEC
        if end_s < topic_end:
            end_s = topic_end
            start_s = max(0, end_s - TOPIC_MAX_CLIP_SEC)

    start_s, end_s = _snap_clip_to_srt_segments(start_s, end_s, srt_segments or [])

    if video_duration:
        end_s = min(end_s, video_duration)
        if end_s - start_s < TOPIC_MIN_CLIP_SEC and start_s > 0:
            start_s = max(0, end_s - TOPIC_MIN_CLIP_SEC)

    expanded = dict(mark)
    expanded["topic_start"] = topic_start
    expanded["topic_end"] = topic_end
    expanded["start"] = int(max(0, start_s))
    expanded["end"] = int(max(end_s, start_s + 1))
    expanded["time_basis"] = "video_elapsed_seconds"
    expanded["context_expanded"] = True
    expanded["context_pre_sec"] = TOPIC_PRE_CONTEXT_SEC
    expanded["context_post_sec"] = TOPIC_POST_CONTEXT_SEC
    return expanded


def _expand_clip_marks_with_context(marks, srt_segments=None, video_duration=None):
    """批量扩展切片上下文；输入/输出时间均为视频内秒数。"""
    return [
        _expand_clip_mark_with_context(mark, srt_segments=srt_segments, video_duration=video_duration)
        for mark in _dedupe_clip_marks(marks)
    ]

# ============================================================
# 逐话题时间轴报告格式化
# ============================================================

_CIRCLED_NUMBERS = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"


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
    return result.replace("主播", display_name)


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


def _topic_peak_density(topic, peaks, window_sec=DANMAKU_WINDOW):
    """计算话题时间范围内/附近最高弹幕密度。"""
    if not peaks:
        return 0
    start = int(topic["start"])
    end = int(topic["end"])
    densities = [
        density for peak_start, density in peaks
        if peak_start + window_sec >= start and peak_start <= end
    ]
    return max(densities) if densities else 0


def _is_content_cuttable_topic(topic):
    """判断话题内容本身是否适合切片，避免只有背景语音/兜底说明被高弹幕误切。"""
    if topic.get("fallback"):
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


def _apply_danmaku_slice_decisions(topics, peaks, avg_density):
    """从每小时重点中按弹幕密度筛选可切片段。"""
    if not topics:
        return []
    threshold = max(avg_density * CLIP_DENSITY_RATIO, avg_density + 10, 20)
    manual_threshold = max(avg_density * MANUAL_TIMELINE_STAR_DENSITY_RATIO, 20)
    for topic in topics:
        peak_density = _topic_peak_density(topic, peaks)
        topic["peak_density"] = peak_density
        topic["density_ratio"] = round(peak_density / avg_density, 2) if avg_density else 0
        normal_cut = peak_density >= threshold
        manual_stars = topic.get("manual_stars", 0)
        manual_cut = manual_stars > 0 and (
            manual_stars >= 2
            or not peaks
            or peak_density >= manual_threshold
        )
        topic["can_slice"] = bool(
            _is_content_cuttable_topic(topic)
            and (normal_cut or manual_cut)
            and topic["end"] > topic["start"]
        )
    return topics


def _clip_marks_from_topics(topics):
    """根据已筛选的重点话题生成 clip_marks。"""
    return _dedupe_clip_marks([
        {"start": topic["start"], "end": topic["end"], "title": topic["title"]}
        for topic in topics
        if topic.get("can_slice")
    ])


def _build_timeline_report(video_name, peak_info, topics, failed_chunks=None, api_warning=None, streamer_name="主播", group_by_hour=False, manual_timeline=None):
    """生成最终 Markdown：逐话题时间轴 + Part 分组。"""
    manual_timeline = manual_timeline or {}
    manual_entries = manual_timeline.get("entries") or []
    lines = [
        f"# {video_name} 话题分析报告",
        f"> 自动生成 | 模型: {LLM_MODEL} | {peak_info}",
        "> 时间基准：视频内时间/播放进度（不是现实钟点）；实际切片会自动向前后扩展保留上下文",
    ]
    if manual_timeline.get("path"):
        star_count = sum(1 for item in manual_entries if item.get("stars", 0) > 0)
        lines.append(
            f"> 人工时间轴辅助: {os.path.basename(manual_timeline['path'])} | "
            f"{len(manual_entries)} 条记录, ⭐重点 {star_count} 条"
        )
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


def run_pipeline(flv_path, ass_path=None, progress_callback=None):
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

    # Step 1: 确保 SRT 存在
    if progress_callback:
        progress_callback("Step 1/5: 检查/生成字幕...", 0, 100)
    srt_path = ensure_srt(flv_path, progress_callback)
    if not srt_path:
        raise RuntimeError("无法生成 SRT 字幕")

    # Step 2: 弹幕分析
    if progress_callback:
        progress_callback("Step 2/5: 弹幕密度分析...", 15, 100)
    peaks = analyze_danmaku(ass_path) if ass_path else []
    avg_den = sum(d for _, d in peaks) / len(peaks) if peaks else 0
    peak_info = f"弹幕峰值 {len(peaks)} 个窗口, 全场平均密度 {avg_den:.0f}条/分钟" if peaks else "无弹幕数据"

    # Step 3: SRT 分块
    if progress_callback:
        progress_callback("Step 3/5: SRT 分块中...", 20, 100)
    segs = parse_srt_text(srt_path)
    chunks = chunk_srt(segs, peaks)
    manual_timeline = load_manual_timeline(flv_path)
    manual_entries = manual_timeline.get("entries") or []
    if manual_entries:
        chunks = _attach_manual_timeline_to_chunks(chunks, manual_entries)
        if progress_callback:
            progress_callback(
                f"已加载人工时间轴: {os.path.basename(manual_timeline['path'])}，"
                f"{len(manual_entries)} 条记录",
                21, 100,
            )
    total = len(chunks)

    # Step 4: 有人工时间轴时走快路径，避免 Pro 推理过慢；无人工时间轴再调用 LLM。
    api_precheck_warning = None
    clip_marks = []
    accepted_topics = []
    failed_chunks = []
    consecutive_failed_chunks = 0

    if manual_entries:
        accepted_topics = _topics_from_manual_timeline(manual_entries)
        if progress_callback:
            progress_callback(
                f"Step 4/5: 使用人工时间轴快路径 ({len(accepted_topics)} 个话题)，跳过 LLM 分块...",
                93, 100,
            )
    else:
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
        except Exception as e:
            msg = _short_llm_error(e)
            if _is_retryable_llm_error(e):
                api_precheck_warning = msg
                if progress_callback:
                    progress_callback(
                        f"API 预检遇到上游临时错误，将继续尝试正式分块: {msg}",
                        22, 100,
                    )
            else:
                if progress_callback:
                    progress_callback(f"API 预检失败: {msg}", 0, 100)
                raise RuntimeError(f"API 预检失败: {msg}") from e



        for i, ch in enumerate(chunks):
            pct = 25 + int((i / total) * 70)
            t = fmt_time(ch["start"])
            if progress_callback:
                progress_callback(f"Step 4/5: LLM分析 ({i+1}/{total}, {t})...", pct, 100)

            # 构造 prompt：失败重试时会自动降级为紧凑提示，降低 5xx 概率
            prompt, chunk_start, chunk_end = _build_chunk_prompt(ch, i, total, compact=False, streamer_name=streamer_display_name)
            compact_prompt, _, _ = _build_chunk_prompt(ch, i, total, compact=True, streamer_name=streamer_display_name)

            try:
                response = _call_llm_with_retry(
                    prompt,
                    compact_prompt=compact_prompt,
                    progress_callback=progress_callback,
                    progress_label=f"块 {i+1} API",
                    progress_step=pct,
                )
            except Exception as e:
                err = _short_llm_error(e)
                failed_chunks.append({
                    "index": i + 1,
                    "start": int(chunk_start),
                    "end": int(chunk_end),
                    "time": fmt_time(chunk_start),
                    "error": err,
                })
                if progress_callback:
                    progress_callback(f"块 {i+1} API 连续失败，已跳过: {err}", pct, 100)
                if _is_retryable_llm_error(e):
                    consecutive_failed_chunks += 1
                else:
                    consecutive_failed_chunks = MAX_INITIAL_FAILED_CHUNKS
                if consecutive_failed_chunks >= MAX_INITIAL_FAILED_CHUNKS and not accepted_topics and not clip_marks:
                    raise RuntimeError(
                        f"LLM API 连续 {consecutive_failed_chunks} 个分块失败，疑似上游服务不可用。"
                        f"最后错误: {err}"
                    ) from e
                continue

            consecutive_failed_chunks = 0
            # 解析话题和切片标记：按当前块时间范围过滤，正文进入最终时间轴报告
            before_topic_count = len(accepted_topics)
            _, marks = _parse_llm_response(
                response,
                chunk_start,
                chunk_end,
                accepted_topics,
                allow_markdown_fallback=False,
            )
            clip_marks.extend(marks)
            if len(accepted_topics) == before_topic_count:
                fallback_topic = _make_fallback_topic_from_chunk(ch, streamer_name=streamer_display_name)
                if fallback_topic and not _is_duplicate_topic(fallback_topic, accepted_topics):
                    accepted_topics.append(fallback_topic)
            time.sleep(0.3)  # 避免限流

    # Step 5: 生成文件
    if progress_callback:
        progress_callback("Step 5/5: 生成报告...", 97, 100)

    _merge_manual_timeline_topics(accepted_topics, manual_entries)
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
    )

    # 保存
    md_path = base + "_话题分析.md"
    json_path = base + "_clip_marks.json"

    with open(md_path, "w", encoding="utf-8") as f:
        f.write(report)

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({
            "video": video_name,
            "streamer_name": streamer_name,
            "streamer_display_name": streamer_display_name,
            "time_basis": "video_elapsed_seconds",
            "time_basis_note": "start/end 均为视频内秒数，不是真实钟点；topic_start/topic_end 为原话题范围，start/end 为含前后文的实际切片范围。",
            "expanded_with_context": True,
            "context_policy": {
                "pre_context_sec": TOPIC_PRE_CONTEXT_SEC,
                "post_context_sec": TOPIC_POST_CONTEXT_SEC,
                "min_clip_sec": TOPIC_MIN_CLIP_SEC,
                "max_clip_sec": TOPIC_MAX_CLIP_SEC,
            },
            "api_precheck_warning": api_precheck_warning,
            "failed_chunks": failed_chunks,
            "manual_timeline": {
                "path": manual_timeline.get("path"),
                "entry_count": len(manual_entries),
                "star_count": sum(1 for item in manual_entries if item.get("stars", 0) > 0),
                "time_basis": "wall_clock_converted_to_video_elapsed_seconds" if manual_entries else None,
            },
            "clip_marks": clip_marks,
        }, f, ensure_ascii=False, indent=2)

    if progress_callback:
        progress_callback(
            f"完成! {len(clip_marks)} 个可切片段 → {json_path}",
            100, 100
        )

    return {
        "report": report,
        "clip_marks": clip_marks,
        "json_path": json_path,
        "md_path": md_path,
        "srt_path": srt_path,
        "failed_chunks": failed_chunks,
        "api_precheck_warning": api_precheck_warning,
        "manual_timeline": manual_timeline,
    }


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

    if progress_callback:
        progress_callback(f"开始切片 ({len(marks)} 段)...", 0, len(marks))

    count = 0
    for i, m in enumerate(marks):
        start_s = m["start"]
        end_s = m["end"]
        title = m.get("title", f"片段{i+1}")
        duration = end_s - start_s
        if duration <= 0:
            continue

        # 安全文件名
        safe_title = re.sub(r'[\\/:*?"<>|]', '', title)[:30]
        output_name = f"{i+1:02d}_{int(start_s)}s_{safe_title}.flv"
        output_path = os.path.join(report_dir, output_name)

        if progress_callback:
            progress_callback(f"切片 {i+1}/{len(marks)}: {title}", i+1, len(marks))

        sp.run([
            "ffmpeg", "-y", "-ss", str(start_s), "-i", flv_path,
            "-t", str(duration), "-c", "copy", output_path
        ], check=True, stdout=sp.PIPE, stderr=sp.DEVNULL,
           encoding="utf-8", errors="replace")
        count += 1

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
