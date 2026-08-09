"""
AutoSlice 核心引擎
支持两种模式：
  1. 弹幕密度模式：分析 ASS 弹幕 + SRT 字幕 → 自动找爆点 → 切片
  2. 时间轴模式：导入朋友标记的 docx 时间轴 → 切片
"""

import os
import re
import subprocess
import json
from collections import defaultdict


# ============================================================
# 配置参数
# ============================================================
DANMAKU_WINDOW = 60
DENSITY_RATIO = 0.45
MAX_OVERLAP = 40
SLICE_STEP = 1
CONTEXT_GAP = 4.0
MAX_EXPAND = 60.0
VIDEO_GAP_THRESHOLD = 60
VIDEO_START_KEYS = ["一起看吧", "看这个视频", "看个视频", "给大家看个",
                     "点开这个", "先暂停一下", "暂停一下啊", "我先暂停",
                     "看一下这个视频", "一起看看", "点视频"]


# ============================================================
# 工具函数
# ============================================================

def seconds_to_srt_time(seconds):
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds - int(seconds)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


# ============================================================
# SRT 字幕生成
# ============================================================

def generate_srt(video_path, progress_callback=None):
    """兼容旧入口，统一使用可续跑且原子写入的新 FunASR 流程。"""
    try:
        from topic_engine import ensure_srt

        return ensure_srt(video_path, progress_callback=progress_callback)
    except Exception as exc:
        if progress_callback:
            progress_callback(f"识别失败: {exc}", 0, 1)
        return None


# ============================================================
# SRT / ASS 解析
# ============================================================

def parse_srt(srt_path):
    """解析 SRT 文件，返回 [(start_s, end_s, text), ...]"""
    if not srt_path or not os.path.exists(srt_path):
        return []
    with open(srt_path, "r", encoding="utf-8") as f:
        content = f.read()
    pattern = r'(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})\s*\n(.*?)(?=\n\n|\Z)'
    matches = re.findall(pattern, content, re.DOTALL)
    segments = []
    for start_str, end_str, text in matches:
        h1, m1, rest1 = start_str.split(":")
        s1, ms1 = rest1.split(",")
        start_s = int(h1) * 3600 + int(m1) * 60 + int(s1) + int(ms1) / 1000
        h2, m2, rest2 = end_str.split(":")
        s2, ms2 = rest2.split(",")
        end_s = int(h2) * 3600 + int(m2) * 60 + int(s2) + int(ms2) / 1000
        segments.append((start_s, end_s, text.strip().replace("\n", " ")))
    return sorted(segments, key=lambda x: x[0])


def extract_danmaku_timestamps(ass_path):
    """从 ASS 弹幕文件提取 Dialogue 起始时间戳"""
    if not ass_path or not os.path.exists(ass_path):
        return []
    timestamps = []
    with open(ass_path, "r", encoding="utf-8") as f:
        for line in f:
            if line.startswith("Dialogue:"):
                parts = line.split(",")
                time_str = parts[1].strip()
                h, m, s = time_str.split(":")
                timestamps.append(int(h) * 3600 + int(m) * 60 + float(s))
    return timestamps


# ============================================================
# 弹幕密度分析
# ============================================================

def find_dense_periods(timestamps, window_size, max_overlap, step):
    """滑动窗口找弹幕密度最高的时间段，返回 [(start, density), ...] 按密度降序"""
    time_counts = defaultdict(int)
    for t in timestamps:
        time_counts[t] += 1
    density_periods = []
    sorted_times = sorted(time_counts.keys())
    for i in range(0, len(sorted_times), step):
        start = sorted_times[i]
        end = start + window_size
        density = sum(count for time, count in time_counts.items() if start <= time < end)
        density_periods.append((start, density))
    density_periods.sort(key=lambda x: x[1], reverse=True)
    filtered = []
    for start_time, density in density_periods:
        valid = True
        for selected_start, _ in filtered:
            overlap = min(selected_start + window_size, start_time + window_size) - max(selected_start, start_time)
            if overlap > max_overlap:
                valid = False
                break
        if valid:
            filtered.append((int(start_time), density))
    return filtered


# ============================================================
# 上下文扩展
# ============================================================

def find_context_boundaries(segments, dense_start, dense_end, gap_threshold=4.0, max_expand=60.0):
    """从弹幕爆点向前/向后扩展，用 SRT 字幕找上下文边界"""
    if not segments:
        return dense_start, dense_end
    ctx_start = dense_start
    for seg_start, seg_end, text in reversed(segments):
        if seg_end <= ctx_start:
            if ctx_start - seg_end <= gap_threshold:
                ctx_start = seg_start
            else:
                break
        elif seg_start < ctx_start and seg_end > ctx_start:
            ctx_start = min(ctx_start, seg_start)
    if dense_start - ctx_start > max_expand:
        ctx_start = dense_start - max_expand
    ctx_end = dense_end
    for seg_start, seg_end, text in segments:
        if seg_start >= ctx_end:
            if seg_start - ctx_end <= gap_threshold:
                ctx_end = seg_end
            else:
                break
        elif seg_start < ctx_end and seg_end > ctx_end:
            ctx_end = max(ctx_end, seg_end)
    if ctx_end - dense_end > max_expand:
        ctx_end = dense_end + max_expand
    return ctx_start, ctx_end


def merge_overlapping_periods(periods, min_gap=10.0):
    """合并重叠或相近的片段，去重边界"""
    if not periods:
        return []
    periods = sorted(periods, key=lambda x: x[0])
    merged = []
    cur_start, cur_end = periods[0]
    for start, end in periods[1:]:
        if start - cur_end <= min_gap:
            cur_end = max(cur_end, end)
        else:
            merged.append((cur_start, cur_end, cur_end - cur_start))
            cur_start, cur_end = start, end
    merged.append((cur_start, cur_end, cur_end - cur_start))
    # 去重
    deduped = [merged[0]]
    for start, end, dur in merged[1:]:
        prev_start, prev_end, prev_dur = deduped[-1]
        if start < prev_end:
            new_start = prev_end
            new_dur = end - new_start
            if new_dur > 5:
                deduped.append((new_start, end, new_dur))
        else:
            deduped.append((start, end, dur))
    return deduped


# ============================================================
# 看视频检测
# ============================================================

def detect_video_watching_segments(srt_segments):
    """从 SRT 字幕识别看视频时间段（语音空白 >60s）"""
    if not srt_segments:
        return []
    video_segments = []
    last_speech_end = 0
    for seg_start, seg_end, text in srt_segments:
        gap = seg_start - last_speech_end if last_speech_end > 0 else 0
        text_norm = text.replace(" ", "")
        if gap > VIDEO_GAP_THRESHOLD:
            preamble_start = last_speech_end
            search_end = last_speech_end
            for s, e, t in reversed(srt_segments):
                if e <= search_end:
                    if search_end - e <= 8:
                        preamble_start = s
                        search_end = s
                    else:
                        break
            preamble_start = max(preamble_start, last_speech_end - 120)
            video_segments.append((preamble_start, seg_start))
        for kw in VIDEO_START_KEYS:
            if kw in text_norm and gap > 15:
                if not video_segments or video_segments[-1][0] != last_speech_end:
                    video_segments.append((last_speech_end, seg_start))
                break
        last_speech_end = max(last_speech_end, seg_end)
    video_segments = [(s, e) for s, e in video_segments if e - s >= 40]
    if video_segments:
        merged = [video_segments[0]]
        for s, e in video_segments[1:]:
            if s - merged[-1][1] < 30:
                merged[-1] = (merged[-1][0], e)
            else:
                merged.append((s, e))
        video_segments = merged
    return video_segments


# ============================================================
# 时间轴解析
# ============================================================

def parse_timeline_docx(docx_path):
    """解析朋友标记的时间轴 Word 文档，返回 [(seconds, desc, stars), ...]"""
    try:
        from docx import Document
    except ImportError:
        return []
    if not os.path.exists(docx_path):
        return []
    if os.path.getsize(docx_path) == 0:
        return []  # 空文件，朋友还没填
    try:
        doc = Document(docx_path)
    except Exception:
        return []  # 文件损坏或格式不对
    timestamps = []
    prev_seconds = 0
    for para in doc.paragraphs:
        text = para.text.strip()
        if not text:
            continue
        match = re.match(r'(\d{2}):(\d{2}):(\d{2})\s+(.*)', text)
        if not match:
            continue
        h, m, s = int(match.group(1)), int(match.group(2)), int(match.group(3))
        seconds = h * 3600 + m * 60 + s
        prev_seconds = seconds
        desc = match.group(4).strip()
        stars = desc.count('⭐')
        timestamps.append((seconds, desc, stars))
    if not timestamps:
        return []
    fixed = []
    day_offset = 0
    prev_s = timestamps[0][0]
    for s, desc, stars in timestamps:
        if s < prev_s - 21600:
            day_offset += 86400
        fixed.append((s + day_offset, desc, stars))
        prev_s = s
    return fixed


def parse_timeline_json(json_path):
    """
    解析话题分析生成的 JSON 时间轴文件。
    支持 clip_marks (topic_engine) 和 topics (topic_analyzer) 两种格式。
    返回保留显式起止范围的标准标记字典。
    """
    if not json_path or not os.path.exists(json_path):
        return []
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)
    marks = []
    items = data.get("clip_marks") or data.get("topics") or []
    default_time_basis = data.get("time_basis", "video_elapsed_seconds")
    for item in items:
        try:
            start = float(item.get("start", item.get("topic_start")))
            end = float(item.get("end", item.get("topic_end")))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        topic_start = item.get("topic_start", start)
        topic_end = item.get("topic_end", end)
        try:
            topic_start = float(topic_start)
            topic_end = float(topic_end)
        except (TypeError, ValueError):
            topic_start, topic_end = start, end
        marks.append({
            "start": start,
            "end": end,
            "topic_start": topic_start,
            "topic_end": topic_end,
            "title": str(item.get("title") or "未命名片段").strip(),
            "time_basis": item.get("time_basis", default_time_basis),
        })
    return marks


# ============================================================
# 视频切片
# ============================================================

def slice_video(video_path, output_path, start_time, duration):
    """用 ffmpeg 切出视频片段"""
    subprocess.run([
        "ffmpeg", "-y", "-ss", str(start_time), "-i", video_path,
        "-t", str(duration), "-c", "copy", output_path
    ], check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
       encoding="utf-8", errors="replace")


def _extract_stream_start(video_name):
    """
    从视频文件名提取开播时间（秒）。
    支持格式: ...2026年06月12日20点01分... 或 ...2026-06-12 20_01...
    返回: 秒数（如 20:01 = 72060），失败返回 None
    """
    # 格式1: 2026年06月12日20点01分
    m = re.search(r'(\d{2})点(\d{2})分', video_name)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        return h * 3600 + mi * 60

    # 格式2: 2026-06-12 20_01
    m = re.search(r'(\d{2})_(\d{2})', video_name)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        # 只有时间部分的两位数，20_01 = 20:01
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return h * 3600 + mi * 60

    return None


def get_video_duration(video_path):
    """获取视频时长（秒）"""
    try:
        probe = subprocess.run([
            "ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", video_path
        ], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        info = json.loads(probe.stdout.decode("utf-8", errors="replace"))
        return float(info.get("format", {}).get("duration", 0))
    except:
        return 0


# ============================================================
# 主处理流程
# ============================================================

def is_file_locked(filepath):
    """检查文件是否正在被写入（被其他进程占用）"""
    try:
        import time
        size1 = os.path.getsize(filepath)
        time.sleep(0.5)
        size2 = os.path.getsize(filepath)
        return size1 != size2  # 大小在变化 = 正在录制
    except:
        return True


def process_video(flv_path, ass_path, output_dir, mode="danmaku",
                  timeline_path=None, timeline_json=None, progress_callback=None):
    """
    处理单个视频。
    """
    video_name = os.path.basename(flv_path)

    # 检查是否正在录制
    if video_name.startswith("[正在录制]") or video_name.startswith("[录制中]"):
        if progress_callback:
            progress_callback("跳过：正在录制中", 0, 1)
        return 0, ""
    if is_file_locked(flv_path):
        if progress_callback:
            progress_callback("跳过：文件正在被写入", 0, 1)
        return 0, ""

    base_name = os.path.splitext(video_name)[0]
    video_output_dir = os.path.join(output_dir, base_name)
    os.makedirs(video_output_dir, exist_ok=True)

    total_steps = 100
    step = [0]

    def progress(msg, pct=None, total=None):
        if pct is not None and total is not None:
            step[0] = pct
        else:
            step[0] = min(step[0] + 20, 95)
        if progress_callback:
            progress_callback(msg, step[0], 100)
        print(f"  [{step[0]}%] {msg}")

    # Step 1: SRT
    progress("生成/检查 SRT 字幕...")
    srt_path = generate_srt(flv_path)
    if not srt_path and mode != "timeline":
        progress("字幕生成失败，无法继续")
        return 0, video_output_dir

    # Step 2: 确定切片时间段
    progress("分析切片时间段...")
    video_duration = get_video_duration(flv_path)
    srt_segments = parse_srt(srt_path) if srt_path else []
    expanded = []

    # === 混合模式：时间轴候选 + 弹幕密度筛选 ===
    if mode == "hybrid" and timeline_path and os.path.exists(timeline_path):
        if not ass_path or not os.path.exists(ass_path):
            progress("混合模式需要弹幕文件")
            return 0, video_output_dir
        tl_raw = parse_timeline_docx(timeline_path)
        if not tl_raw:
            progress("时间轴解析失败")
            return 0, video_output_dir
        # 时间换算
        stream_start_s = _extract_stream_start(video_name)
        if stream_start_s is not None:
            tl_converted = []
            for s, desc, stars in tl_raw:
                video_s = s - stream_start_s
                if video_s < 0: video_s += 86400
                if video_duration > 0 and (video_s < 0 or video_s > video_duration + 300):
                    continue
                tl_converted.append((video_s, desc, stars))
            tl_raw = tl_converted
        # 弹幕密度分析
        timestamps = extract_danmaku_timestamps(ass_path)
        if not timestamps:
            progress("无弹幕数据")
            return 0, video_output_dir
        all_periods = find_dense_periods(timestamps, DANMAKU_WINDOW, MAX_OVERLAP, SLICE_STEP)
        peak = all_periods[0][1] if all_periods else 0
        threshold_hybrid = max(peak * 0.15, 3)
        # 混合模式核心逻辑：
        #   ⭐ 条目 → 强制保留
        #   非⭐   → 弹幕密度达标才保留
        #   重叠    → 合并去重
        expanded = []
        starred_count = density_pass = 0
        for ts, desc, stars in tl_raw:
            density = sum(1 for t in timestamps if ts <= t < ts + DANMAKU_WINDOW)
            keep = False
            if stars >= 1:
                keep = True
                starred_count += 1
            elif density >= threshold_hybrid:
                keep = True
                density_pass += 1
            if keep:
                start = max(0, ts - 150)
                end = min(video_duration or 99999, ts + 150)
                if srt_segments:
                    ctx_start, ctx_end = find_context_boundaries(
                        srt_segments, start, end,
                        gap_threshold=CONTEXT_GAP, max_expand=MAX_EXPAND
                    )
                    start = min(start, ctx_start)
                    end = max(end, ctx_end)
                expanded.append((start, end, desc))
        # 重叠去重
        if expanded:
            expanded_sorted = sorted(expanded, key=lambda x: x[0])
            deduped = [expanded_sorted[0]]
            for item in expanded_sorted[1:]:
                prev = deduped[-1]
                if item[0] < prev[1]:  # 重叠 → 合并
                    new_end = max(prev[1], item[1])
                    new_desc = prev[2] if len(prev[2]) >= len(item[2]) else item[2]
                    deduped[-1] = (prev[0], new_end, new_desc)
                else:
                    deduped.append(item)
            expanded = deduped
        skipped = len(tl_raw) - starred_count - density_pass
        progress(f"时间轴 {len(tl_raw)} 条, ⭐强制 {starred_count} 条, 密度达标 {density_pass} 条, 跳过 {skipped} 条 (阈值={threshold_hybrid:.0f}, 峰值={peak})")
        if not expanded:
            progress("无达标爆点")
            return 0, video_output_dir

    elif mode == "timeline":
        tl_raw = None
        is_json = False
        if timeline_json and os.path.exists(timeline_json):
            tl_raw = parse_timeline_json(timeline_json)
            is_json = True
        elif timeline_path and os.path.exists(timeline_path):
            tl_raw = parse_timeline_docx(timeline_path)
        if tl_raw:
            # JSON 时间轴：时间是视频内秒数，直接使用
            # docx 时间轴：时间是绝对时钟，需换算
            if not is_json:
                stream_start_s = _extract_stream_start(video_name)
                if stream_start_s is not None:
                    tl_converted = []
                    skipped_before = skipped_after = 0
                    for s, desc, stars in tl_raw:
                        video_s = s - stream_start_s
                        if video_s < 0:
                            video_s += 86400
                        if video_duration > 0:
                            if video_s < 0:
                                skipped_before += 1
                                continue
                            if video_s > video_duration + 300:
                                skipped_after += 1
                                continue
                        tl_converted.append((video_s, desc, stars))
                    tl_raw = tl_converted
                    msg = f"时间轴 {len(tl_raw)} 条（已换算开播时间）"
                    if skipped_before: msg += f"，跳过 {skipped_before} 条在录播开始前"
                    if skipped_after: msg += f"，跳过 {skipped_after} 条在录播结束后"
                    if progress_callback:
                        progress_callback(msg, 15, 100)
            else:
                if progress_callback:
                    progress_callback(f"JSON 时间轴 {len(tl_raw)} 条（视频内时间，直接使用）", 15, 100)
            expanded = []
            if is_json:
                for mark in sorted(tl_raw, key=lambda item: item["start"]):
                    start = max(0, float(mark["start"]))
                    end = min(video_duration or 99999, float(mark["end"]))
                    if end > start:
                        expanded.append((start, end, mark["title"]))
            else:
                # DOCX 只提供一个钟点，保留旧版前后文扩展行为。
                tl_sorted = sorted(tl_raw, key=lambda x: x[0])
                for ts, desc, stars in tl_sorted:
                    start = max(0, ts - 150)
                    end = min(video_duration or 99999, ts + 150)
                    if srt_segments:
                        ctx_start, ctx_end = find_context_boundaries(
                            srt_segments, start, end,
                            gap_threshold=CONTEXT_GAP, max_expand=MAX_EXPAND
                        )
                        # 只扩不缩：标记时刻绝对不能丢
                        start = min(start, ctx_start)
                        end = max(end, ctx_end)
                    expanded.append((start, end, desc))
        else:
            progress("时间轴解析失败")
            return 0, video_output_dir
    else:
        if not ass_path or not os.path.exists(ass_path):
            progress("缺少弹幕文件，无法切片")
            return 0, video_output_dir
        timestamps = extract_danmaku_timestamps(ass_path)
        if not timestamps:
            progress("弹幕文件无数据")
            return 0, video_output_dir
        all_periods = find_dense_periods(timestamps, DANMAKU_WINDOW, MAX_OVERLAP, SLICE_STEP)
        if not all_periods:
            progress("未找到弹幕密集区域")
            return 0, video_output_dir
        peak_density = all_periods[0][1]
        threshold = max(peak_density * DENSITY_RATIO, 3)
        dense_periods = [(s, d) for s, d in all_periods if d >= threshold]
        if not dense_periods:
            progress("无符合条件的爆点")
            return 0, video_output_dir
        expanded = []
        for start_time, density in dense_periods:
            dense_start = start_time
            dense_end = start_time + DANMAKU_WINDOW
            if srt_segments:
                ctx_start, ctx_end = find_context_boundaries(
                    srt_segments, dense_start, dense_end,
                    gap_threshold=CONTEXT_GAP, max_expand=MAX_EXPAND
                )
            else:
                ctx_start, ctx_end = dense_start, dense_end
            expanded.append((ctx_start, ctx_end))

    if not expanded:
        progress("无切片候选")
        return 0, video_output_dir

    # Step 3: 看视频检测（仅弹幕模式）
    if mode == "danmaku" and srt_segments:
        progress("检测看视频段...")
        video_segments = detect_video_watching_segments(srt_segments)
        if video_segments:
            expanded_v = []
            for ctx_start, ctx_end in expanded:
                ns, ne = ctx_start, ctx_end
                for vs, ve in video_segments:
                    if ctx_start < ve and ctx_end > vs:
                        expand_left = min(ctx_start - vs, MAX_EXPAND)
                        expand_right = min(ve - ctx_end, MAX_EXPAND)
                        ns = ctx_start - expand_left
                        ne = ctx_end + expand_right
                expanded_v.append((ns, ne))
            expanded = expanded_v

    # Step 4: 合并重叠（时间轴/混合模式跳过，保持描述信息）
    if mode in ("timeline", "hybrid"):
        merged = [(s, e, e - s, d) for s, e, d in expanded]
    else:
        progress("合并重叠片段...")
        merged = merge_overlapping_periods(expanded)

    # Step 5: 切片
    progress(f"切片输出 ({len(merged)} 个)...")
    # 获取真实时间用于文件名（时间轴/混合模式）
    stream_start_s = _extract_stream_start(video_name)
    for idx, item in enumerate(merged):
        seg_start = item[0]
        seg_end = item[1]
        seg_duration = seg_end - seg_start
        seg_start = max(0, seg_start)
        seg_duration = seg_end - seg_start

        # 文件名：时间轴/混合模式加真实时间和描述
        if len(item) >= 4 and item[3]:
            desc = item[3]
            # 真实时间
            if stream_start_s is not None:
                real_s = seg_start + stream_start_s
                if real_s >= 86400: real_s -= 86400
                h, m = int(real_s // 3600), int((real_s % 3600) // 60)
                time_str = f"{h:02d}{m:02d}"
            else:
                time_str = f"{int(seg_start):04d}s"
            # 描述截短、去非法字符
            short_desc = desc[:30].replace('/', '_').replace('\\', '_').replace(':', '_').replace('*', '_').replace('?', '_').replace('"', '_').replace('<', '_').replace('>', '_').replace('|', '_').strip()
            output_name = f"{time_str}_{short_desc}.flv"
        else:
            output_name = f"{int(seg_start):04d}s_{base_name}.flv"

        output_path = os.path.join(video_output_dir, output_name)
        slice_video(flv_path, output_path, seg_start, seg_duration)

    progress(f"完成！{len(merged)} 个片段 → {video_output_dir}")
    return len(merged), video_output_dir
