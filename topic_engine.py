"""
话题分析 + 智能切片引擎

流水线: FunASR转录 → 弹幕密度分析 → SRT分块 → DeepSeek Flash分析 → 报告 + 切片标记

用法:
  from topic_engine import run_pipeline
  result = run_pipeline(flv_path, ass_path, progress_callback=cb)
  # result: {"report": "...", "clip_marks": [...], "json_path": "..."}
"""

import os, re, json, time, requests
from collections import defaultdict
from datetime import timedelta


# ============================================================
# 配置
# ============================================================
CHUNK_SEC = 300          # 每块 5 分钟
LLM_MODEL = "deepseek-v4-flash"  # 用 Flash 省钱
LLM_MAX_TOKENS = 1500
DANMAKU_WINDOW = 60
DENSITY_RATIO = 0.30     # 弹幕密度阈值（稍低，用于标记而非切片）


def fmt_time(seconds):
    return str(timedelta(seconds=int(seconds)))


def load_api_config():
    """读取 API 配置，优先用 AutoSlice 自己的，否则用 Claude 的"""
    # 1. 先试 AutoSlice 独立配置
    auto_cfg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "api_config.json")
    if os.path.exists(auto_cfg):
        with open(auto_cfg) as f:
            cfg = json.load(f)
        return cfg["base_url"], cfg["token"], cfg.get("model", "deepseek-v4-flash")

    # 2. 回退到 Claude 配置
    cfg_path = os.path.expanduser(r"~\.claude\settings.json")
    with open(cfg_path) as f:
        cfg = json.load(f)
    return cfg["env"]["ANTHROPIC_BASE_URL"], cfg["env"]["ANTHROPIC_AUTH_TOKEN"], "deepseek-v4-flash"


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
    """解析 SRT，去空格，返回 [(start_s, text), ...]"""
    if not os.path.exists(srt_path):
        return []
    with open(srt_path, encoding="utf-8") as f:
        content = f.read()
    pattern = r'(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})\s*\n(.*?)(?=\n\n|\Z)'
    matches = re.findall(pattern, content, re.DOTALL)
    segs = []
    for start_str, end_str, text in matches:
        h, m, rest = start_str.split(":")
        s, ms = rest.split(",")
        start_s = int(h)*3600 + int(m)*60 + int(s) + int(ms)/1000
        text = text.strip().replace("\n", " ").replace(" ", "")
        if len(text) >= 2:
            segs.append((start_s, text))
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

    for start_s, text in segs:
        if start_s - chunk_start > chunk_sec:
            if current_texts:
                chunks.append(_make_chunk(chunk_start, current_texts, peaks, avg_density))
            chunk_start = start_s
            current_texts = []
        current_texts.append(f"[{fmt_time(start_s)}] {text}")

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

SYSTEM_PROMPT = """你是直播内容分析+切片决策助手。你只能分析【当前分块】里给出的字幕和弹幕密度，不要引用、复述或补写当前分块之外的内容。

## 目标风格

输出要像人工整理的“逐话题时间轴”：每个话题有时间范围，下面用 ·/● 写详细要点。不要写空洞总结，要写出具体发生了什么、主播怎么说、观众/弹幕有什么反应。

## 核心原则：相对密度判断

- 密度 > 全场平均 → 观众活跃 → 话题标题末尾加 ✂️
- 密度 ≈ 或 < 全场平均 → 常态/冷场 → 不加 ✂️
- 如果字幕内容平淡、只有游戏台词/沉默/机械复读，即使有短暂弹幕也谨慎不切

## 时间范围硬约束

- 输出的每个话题时间必须落在本次提示给出的“允许时间范围”内
- 不允许输出历史分块、示例分块、其它视频片段的时间戳
- 如果事件跨越分块，只写当前分块内能确认的部分
- 如果当前分块没有明显话题，输出“无明显话题”即可

## 输出格式（严格按此结构，不要输出 Part 行，程序会自动分组）

[开始－结束]话题标题 ✂️
·具体要点，写清楚事情经过
·主播观点/吐槽/补充细节
●礼物、弹幕爆点、观众金句或高能反应（如有）

[开始－结束]下一个话题标题
·具体要点
·补充细节

**关键要求：**
- 时间戳精确到秒，格式 `H:MM:SS`，例如 `0:04:00`
- 标题 5-15 字，概括核心内容，可加合适 emoji
- 每个话题 2-6 条要点，优先使用 `·`，礼物/弹幕/高能反应用 `●`
- 不要输出 Markdown 代码块
- 不要编造字幕里没有的信息
- 不要输出任何示例内容"""


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
        # 某些模型(如 deepseek)用 reasoning_content 代替 content
        if not content:
            rc = data.get("choices", [{}])[0].get("message", {}).get("reasoning_content", "")
            if rc:
                content = rc
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

# ============================================================
# LLM 响应解析与去重
# ============================================================

_HEADING_RE = re.compile(
    r'^\s*(?:#{1,6}\s*)?(?:[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳]|\d+[.)、])?\s*\['
    r'(\d{1,2}:\d{2}(?::\d{2})?)\s*[-–—－~～至]+\s*'
    r'(\d{1,2}:\d{2}(?::\d{2})?)\]\s*(.+?)\s*$'
)
_NO_SLICE_HINTS = ("不切", "不加标记", "不建议切", "不要切", "不适合切")
_PLACEHOLDER_TITLES = ("无明显话题", "话题标题", "下一个话题", "未命名片段")
_META_BODY_KEYWORDS = (
    "但注意", "注意：", "注意:", "我们需要", "我们应该", "我应该", "我倾向", "是否应该",
    "输出格式", "输出如下", "不要输出", "程序会自动", "允许时间范围", "当前分块",
    "时间范围", "时间戳必须", "格式：", "格式`", "根据原则", "指令说", "题目说", "不能假设",
    "只需输出", "只需要输出", "最后，检查", "Markdown代码块", "Markdown 代码块",
    "这里有一段字幕", "后面没有字幕", "所以我们", "因此，输出", "考虑一下",
)


def _strip_code_fence(response):
    """去掉 LLM 可能包裹的 Markdown 代码块。"""
    response = (response or "").strip()
    if response.startswith("```"):
        response = re.sub(r'^```\w*\n?', '', response)
        response = re.sub(r'\n?```$', '', response)
    return response.strip()


def _clean_topic_title(raw_title):
    """清理标题里的切片标记和原因说明，保留可读标题。"""
    title = raw_title.replace("✂️", "").replace("✂", "")
    title = re.sub(r'[（(]\s*(?:因为|不切|不加标记|不建议切|不要切|弹幕).*?[）)]', '', title)
    title = re.sub(r'\s+', ' ', title)
    return title.strip(' -—：:') or "未命名片段"


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
    """过滤模型思考过程、规则复述和占位要点。"""
    clean = _strip_body_prefix(line)
    if not clean:
        return True
    if clean in ("要点", "补充细节", "具体要点", "另一个事件", "等等。", "等等"):
        return True
    if any(keyword in clean for keyword in _META_BODY_KEYWORDS):
        return True
    if re.search(r'(应该|不应该|可以只输出|是否|格式|指令|原则|分块|代码块)', clean) and (
        clean.startswith(("但", "另外", "所以", "因此", "这里", "如果", "最后", "检查", "考虑"))
        or "我们" in clean
    ):
        return True
    return False


def _normalise_body_line(line):
    """规范正文要点前缀，让报告接近人工时间轴。"""
    line = line.strip()
    if not line or _is_meta_body_line(line):
        return ""
    if line.startswith(("·", "●")):
        return line
    if line.startswith(("- ", "* ", "• ")):
        return "·" + line[2:].strip()
    return "·" + line


def _parse_llm_response(response, chunk_start, chunk_end, accepted_topics=None):
    """
    解析单个分块的 LLM 输出。

    返回: (report_blocks, clip_marks)
    - report_blocks: 单话题时间轴块，主要用于测试和调试
    - clip_marks: 去重后的可切片段列表
    """
    accepted_topics = accepted_topics if accepted_topics is not None else []
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
        topic = {
            "start": start_s,
            "end": end_s,
            "start_str": current["start_str"],
            "end_str": current["end_str"],
            "title": current["title"],
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
def _dedupe_clip_marks(marks):
    """对 clip_marks 做最终去重，避免旧 JSON 或异常响应导致重复切片。"""
    deduped = []
    for mark in sorted(marks, key=lambda m: (int(m.get("start", 0)), int(m.get("end", 0)), m.get("title", ""))):
        try:
            topic = {
                "start": int(mark["start"]),
                "end": int(mark["end"]),
                "title": str(mark.get("title", "未命名片段")).strip() or "未命名片段",
            }
        except (KeyError, TypeError, ValueError):
            continue
        if topic["end"] <= topic["start"]:
            continue
        if _is_duplicate_topic(topic, deduped):
            continue
        deduped.append(topic)
    return deduped

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


def _strip_emoji_for_title(title):
    """给 Part 标题做轻量清理，避免标题太花。"""
    return re.sub(r'^[^\w\u4e00-\u9fff]+', '', title).strip() or title


def _make_part_title(topics):
    """根据 Part 内话题生成阶段标题。"""
    titles = [_strip_emoji_for_title(t["title"]) for t in topics if t.get("title")]
    if not titles:
        return "阶段话题整理"
    if len(titles) == 1:
        return titles[0]
    first, second = titles[0], titles[1]
    if len(first) + len(second) <= 18:
        return f"{first}与{second}"
    return f"{first}等话题"


def _format_topic_block(topic, index):
    """格式化单个话题块，贴近用户给出的逐话题时间轴样式。"""
    label = _topic_index_label(index) if index else ""
    start = _format_report_time(topic["start"])
    end = _format_report_time(topic["end"])
    marker = " ✂️" if topic.get("can_slice") else ""
    lines = [f"{label}[{start}－{end}]{topic['title']}{marker}"]
    body = topic.get("body") or []
    lines.extend(body)
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


def _build_timeline_report(video_name, peak_info, topics):
    """生成最终 Markdown：逐话题时间轴 + Part 分组。"""
    lines = [
        f"# {video_name} 话题分析报告",
        f"> 自动生成 | 模型: {LLM_MODEL} | {peak_info}",
        "---",
        "",
        "## 逐话题时间轴",
        "",
    ]

    groups = _group_topics_for_parts(topics)
    if not groups:
        lines.append("本次没有解析到有效话题。")
        return "\n".join(lines) + "\n"

    topic_index = 1
    for part_index, group in enumerate(groups, 1):
        part_start = min(t["start"] for t in group)
        part_end = max(t["end"] for t in group)
        part_title = _make_part_title(group)
        lines.append(
            f"Part {part_index}: {part_title} "
            f"({_format_report_time(part_start)}－{_format_report_time(part_end)})"
        )
        for topic in group:
            lines.append(_format_topic_block(topic, topic_index))
            topic_index += 1
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"

# ============================================================
# 主流程
# ============================================================

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
    total = len(chunks)

    # Step 4: 逐块 LLM 分析
    # Step 4: 逐块 LLM 分析（先预检 API）
    if progress_callback:
        progress_callback("Step 4/5: 预检 API 连通性...", 22, 100)
    try:
        test_resp = call_llm("回复OK即可", max_tokens=100)
        if not test_resp or len(test_resp.strip()) < 1:
            raise RuntimeError(f"API 返回空内容")
    except requests.HTTPError as e:
        msg = f"HTTP {e.response.status_code}: {e.response.text[:200]}"
        if progress_callback:
            progress_callback(f"API 预检失败: {msg}", 0, 100)
        return {
            "report": f"# API 连接失败\n\n{msg}\n\n请检查 api_config.json。",
            "clip_marks": [], "json_path": base + "_clip_marks.json",
            "md_path": base + "_话题分析.md", "srt_path": srt_path,
        }
    except Exception as e:
        if progress_callback:
            progress_callback(f"API 预检失败: {e}", 0, 100)
        return {
            "report": f"# API 连接失败\n\n错误: {e}\n\n请检查 api_config.json。",
            "clip_marks": [], "json_path": base + "_clip_marks.json",
            "md_path": base + "_话题分析.md", "srt_path": srt_path,
        }

    clip_marks = []
    accepted_topics = []

    for i, ch in enumerate(chunks):
        pct = 25 + int((i / total) * 70)
        t = fmt_time(ch["start"])
        if progress_callback:
            progress_callback(f"Step 4/5: LLM分析 ({i+1}/{total}, {t})...", pct, 100)

        # 构造 prompt：明确当前块允许输出的时间范围，避免模型复读历史示例
        chunk_start = ch["start"]
        chunk_end = ch.get("end", ch["start"] + CHUNK_SEC)
        prompt = (
            f"{SYSTEM_PROMPT}\n\n"
            f"## 当前分块\n"
            f"- 分块编号: 第{i+1}/{total}块\n"
            f"- 允许时间范围: {fmt_time(chunk_start)} - {fmt_time(chunk_end)}\n"
            f"- 弹幕信息: {ch['danmaku_info']}\n\n"
            f"## 字幕:\n{ch['text'][:4000]}"
        )

        # 重试
        response = None
        for retry in range(3):
            try:
                response = call_llm(prompt)
                break
            except Exception as e:
                if retry == 2 and progress_callback:
                    progress_callback(f"块 {i+1} API 失败: {e}", pct, 100)
                time.sleep(1.5)

        if not response:
            continue

        # 解析话题和切片标记：按当前块时间范围过滤，正文进入最终时间轴报告
        _, marks = _parse_llm_response(response, chunk_start, chunk_end, accepted_topics)
        clip_marks.extend(marks)
        time.sleep(0.3)  # 避免限流

    # Step 5: 生成文件
    if progress_callback:
        progress_callback("Step 5/5: 生成报告...", 97, 100)

    clip_marks = _dedupe_clip_marks(clip_marks)
    report = _build_timeline_report(video_name, peak_info, accepted_topics)

    # 保存
    md_path = base + "_话题分析.md"
    json_path = base + "_clip_marks.json"

    with open(md_path, "w", encoding="utf-8") as f:
        f.write(report)

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"clip_marks": clip_marks, "video": video_name}, f,
                  ensure_ascii=False, indent=2)

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
