"""把证据库汇总成报告：给人看的 report.md，机器可读的 report.json，以及可以整份贴给 GPT 的 digest.json。

digest 刻意做成“几十 KB 的精华”：档案本身留在本机，用户跑完扫描后只需要把 digest.json交给 GPT，就能让它基于你的真实习惯继续优化。体积有硬上限，超了会逐段裁短。
"""

from __future__ import annotations

import json
import random
import re
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable, Mapping

from .extractors.subtitles import quantile
from .extractors.terms import grams
from .layout import StreamerMatcher
from .stopwords import is_generic
from .store import ArchiveStore

REPORT_VERSION = 1
MAX_DIGEST_BYTES = 150_000
_EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿⭐⭕⌚-⏿❤]")
_BRACKET = re.compile(r"^\s*[【\[]([^】\]]{1,20})[】\]]")
_DIGITS = re.compile(r"\d+")
_PUNCTUATION = {"！": "感叹号", "？": "问号", "…": "省略号", "，": "逗号", "。": "句号", "~": "波浪号", "、": "顿号"}


def _median(values: Iterable[float]) -> float:
    ordered = sorted(values)
    return round(ordered[len(ordered) // 2], 3) if ordered else 0.0


def _known_by_streamer() -> tuple[dict[str, set[str]], dict[str, set[tuple[str, str]]]]:
    """各主播档案里已有的术语和固定纠错（报告里标出“已经有了”）。"""

    glossary: dict[str, set[str]] = {}
    replacements: dict[str, set[tuple[str, str]]] = {}
    try:
        from autoslice.streamer_profiles import load_streamer_profiles

        profiles, _default = load_streamer_profiles()
    except (ImportError, OSError, ValueError):
        return glossary, replacements
    for profile_id, profile in profiles.items():
        glossary[profile_id] = set(profile.subtitle_glossary) | set(profile.aliases)
        replacements[profile_id] = set(profile.asr_replacements)
    return glossary, replacements


def _overview(store: ArchiveStore, matcher: StreamerMatcher) -> dict:
    by_streamer = [
        {"id": row["streamer"], "label": matcher.label(row["streamer"]), "entries": row["n"], "with_corrected": row["c"]}
        for row in store.query(
            "SELECT streamer, COUNT(*) AS n, SUM(corrected_count > 0) AS c FROM entries GROUP BY streamer ORDER BY n DESC LIMIT 30"
        )
    ]
    last = store.last_run()
    return {
        "entries": store.scalar("SELECT COUNT(*) FROM entries"),
        "with_srt": store.scalar("SELECT COUNT(*) FROM entries WHERE srt_count > 0"),
        "with_corrected": store.scalar("SELECT COUNT(*) FROM entries WHERE corrected_count > 0"),
        "with_both": store.scalar(
            "SELECT COUNT(*) FROM entries WHERE EXISTS(SELECT 1 FROM edits d WHERE d.entry_id = entries.id)"
        ),
        "with_burned": store.scalar("SELECT COUNT(*) FROM entries WHERE burned_count > 0"),
        "with_cover": store.scalar("SELECT COUNT(*) FROM entries WHERE cover_count > 0"),
        "video_gb": round(store.scalar("SELECT SUM(video_bytes) FROM entries") / 1e9, 1),
        "date_min": store.scalar("SELECT MIN(date) FROM entries WHERE date != ''", default=""),
        "date_max": store.scalar("SELECT MAX(date) FROM entries WHERE date != ''", default=""),
        "by_year": {str(row["year"]): row["n"] for row in store.query(
            "SELECT year, COUNT(*) AS n FROM entries WHERE year > 0 GROUP BY year ORDER BY year")},
        "by_streamer": by_streamer,
        "roots": store.roots(),
        "last_run": dict(last) if last else {},
    }


def _corrections(store: ArchiveStore, matcher: StreamerMatcher, known: Mapping[str, set], limit: int = 300) -> list[dict]:
    rows = store.query(
        "SELECT e.streamer AS streamer, c.wrong AS wrong, c.right AS right, COUNT(DISTINCT c.entry_id) AS videos,"
        " SUM(c.n) AS n, SUM(c.kept) AS kept, MIN(c.context) AS context, MAX(e.date) AS last"
        " FROM corrections c JOIN entries e ON e.id = c.entry_id GROUP BY e.streamer, c.wrong, c.right"
        " ORDER BY videos DESC, n DESC LIMIT 30000"
    )
    totals: Counter = Counter()
    for row in rows:
        totals[(row["streamer"], row["wrong"])] += row["videos"]
    items = []
    for row in rows:
        if row["videos"] < 2:
            continue
        items.append({
            "streamer": row["streamer"], "label": matcher.label(row["streamer"]),
            "wrong": row["wrong"], "right": row["right"], "videos": row["videos"], "n": row["n"], "kept": row["kept"],
            # 精度：改过的次数 /（改过的次数 + 在别处被原样保留的次数）；低了说明这个词多数时候是对的
            "precision": round(row["n"] / max(1, row["n"] + row["kept"]), 2),
            "dominance": round(row["videos"] / totals[(row["streamer"], row["wrong"])], 2),
            "context": row["context"], "last": row["last"],
            "known": (row["wrong"], row["right"]) in known.get(row["streamer"], ()),
        })
    return items[:limit]


def candidate_terms(store: ArchiveStore, known: Mapping[str, set], min_df: int = 3, per_streamer: int = 400) -> dict[str, list[dict]]:
    """每个主播的候选术语：跨文件夹出现够多、不是更长词的碎片、不是普通词、不在术语表里。"""

    rows = store.query(
        "SELECT e.streamer AS streamer, t.term AS term, COUNT(DISTINCT t.entry_id) AS df, SUM(t.n) AS total"
        " FROM terms t JOIN entries e ON e.id = t.entry_id GROUP BY e.streamer, t.term HAVING df >= ?"
        " ORDER BY df DESC LIMIT 300000", (min_df,),
    )
    spread: Counter = Counter(row["term"] for row in rows)
    by_streamer: dict[str, dict[str, tuple[int, int]]] = defaultdict(dict)
    for row in rows:
        by_streamer[row["streamer"]][row["term"]] = (row["df"], row["total"])
    result: dict[str, list[dict]] = {}
    for streamer, table in by_streamer.items():
        fragments: set[str] = set()
        for term, (df, _total) in table.items():
            for part in (term[:-1], term[1:]):
                if len(part) >= 2 and part in table and df >= 0.8 * table[part][0]:
                    fragments.add(part)
        items = [
            {"term": term, "df": df, "total": total, "spread": spread[term], "known": term in known.get(streamer, ())}
            for term, (df, total) in table.items()
            if term not in fragments and not is_generic(term) and not term.isdigit()
        ]
        items.sort(key=lambda item: (-item["df"], -item["total"], item["term"]))
        result[streamer] = items[:per_streamer]
    return result


def _titles(store: ArchiveStore, matcher: StreamerMatcher) -> dict:
    rows = store.query("SELECT title, streamer, date FROM entries ORDER BY date, title")
    titles = [row["title"] for row in rows]
    if not titles:
        return {"count": 0}
    lengths = [len(title) for title in titles]
    prefixes = Counter(match.group(1) for title in titles if (match := _BRACKET.match(title)))
    punctuation = {name: round(sum(1 for title in titles if mark in title) / len(titles), 3) for mark, name in _PUNCTUATION.items()}
    skeletons: dict[str, list[str]] = defaultdict(list)
    for title in titles:
        skeletons[_DIGITS.sub("N", _BRACKET.sub("", title)).strip()].append(title)
    series = [
        {"pattern": pattern, "count": len(group), "examples": group[:3]}
        for pattern, group in sorted(skeletons.items(), key=lambda item: -len(item[1]))
        if len(group) >= 3 and "N" in pattern
    ][:20]
    words: Counter = Counter()
    for title in titles:
        words.update(set(grams(_BRACKET.sub("", title))))
    fragments = {part for word, n in words.items() for part in (word[:-1], word[1:]) if part in words and n >= 0.8 * words[part]}
    top_words = [
        {"term": word, "titles": n} for word, n in words.most_common(400)
        if n >= 3 and word not in fragments and not is_generic(word)
    ][:60]
    spread_rng = random.Random(7)
    pool = list(rows)
    return {
        "count": len(titles),
        "length": {"p10": quantile(lengths, 0.1), "p50": quantile(lengths, 0.5), "p90": quantile(lengths, 0.9), "max": max(lengths)},
        "bracket_prefix_rate": round(sum(prefixes.values()) / len(titles), 3),
        "bracket_prefixes": [{"label": label, "titles": n} for label, n in prefixes.most_common(12)],
        "emoji_rate": round(sum(1 for title in titles if _EMOJI.search(title)) / len(titles), 3),
        "digit_rate": round(sum(1 for title in titles if any(ch.isdigit() for ch in title)) / len(titles), 3),
        "punctuation_rate": punctuation,
        "series": series,
        "top_words": top_words,
        "samples_recent": [{"title": row["title"], "date": row["date"], "streamer": row["streamer"]} for row in pool[-30:]],
        "samples_spread": [
            {"title": row["title"], "date": row["date"], "streamer": row["streamer"]}
            for row in sorted(spread_rng.sample(pool, min(40, len(pool))), key=lambda item: item["date"])
        ],
    }


def _habit_summary(rows) -> dict:
    cues = sum(row["cues"] for row in rows)
    return {
        "videos": len(rows), "cues": cues,
        "cps_p50": _median(row["cps_p50"] for row in rows), "cps_p90": _median(row["cps_p90"] for row in rows),
        "cps_p90_q90": round(quantile([row["cps_p90"] for row in rows], 0.9), 2),
        "chars_p50": _median(row["chars_p50"] for row in rows), "chars_p90": _median(row["chars_p90"] for row in rows),
        "chars_p90_q90": round(quantile([row["chars_p90"] for row in rows], 0.9), 1),
        "line_p90": _median(row["line_p90"] for row in rows),
        "line_p90_q90": round(quantile([row["line_p90"] for row in rows], 0.9), 1),
        "multiline_rate": _median(row["multiline"] for row in rows),
        "trailing_punct_rate": _median(row["trailing_punct"] for row in rows),
        "short_rate": round(sum(row["short"] for row in rows) / max(1, cues), 4),
        "long_rate": round(sum(row["long"] for row in rows) / max(1, cues), 4),
        "overlap_rate": round(sum(row["overlap"] for row in rows) / max(1, cues), 4),
    }


def _habits(store: ArchiveStore, matcher: StreamerMatcher) -> dict:
    rows = store.query(
        "SELECT s.*, e.streamer AS streamer, e.year AS year FROM subtitle_stats s JOIN entries e ON e.id = s.entry_id"
    )
    if not rows:
        return {"videos": 0}
    by_streamer: dict[str, list] = defaultdict(list)
    by_year: dict[int, list] = defaultdict(list)
    sources = Counter(row["source"] for row in rows)
    for row in rows:
        by_streamer[row["streamer"]].append(row)
        if row["year"]:
            by_year[row["year"]].append(row)
    return {
        **_habit_summary(rows), "sources": dict(sources),
        "by_streamer": {
            streamer: {"label": matcher.label(streamer), **_habit_summary(group)}
            for streamer, group in sorted(by_streamer.items(), key=lambda item: -len(item[1]))[:8]
        },
        "by_year": {str(year): _habit_summary(group) for year, group in sorted(by_year.items())},
    }


def _edits(store: ArchiveStore) -> dict:
    rows = store.query("SELECT kind, SUM(n) AS n FROM edits GROUP BY kind ORDER BY n DESC")
    aligned = sum(row["n"] for row in rows if row["kind"] not in ("split", "merge"))
    return {
        "aligned_cues": aligned,
        "kinds": {row["kind"]: row["n"] for row in rows},
        "changed_rate": round(1 - (next((row["n"] for row in rows if row["kind"] == "same"), 0) / aligned), 4) if aligned else 0,
    }


def _hue_group(color: str) -> str:
    import colorsys

    red, green, blue = (int(color[index:index + 2], 16) / 255 for index in (1, 3, 5))
    hue, saturation, value = colorsys.rgb_to_hsv(red, green, blue)
    if value < 0.2:
        return "黑"
    if saturation < 0.12:
        return "白" if value > 0.75 else "灰"
    names = ("红", "橙", "黄", "黄绿", "绿", "青绿", "青", "蓝", "靛蓝", "紫", "品红", "粉红")
    return names[int(((hue * 360) + 15) % 360 // 30)]


def _covers(store: ArchiveStore) -> dict:
    rows = store.query("SELECT c.*, e.year AS year FROM covers c JOIN entries e ON e.id = c.entry_id")
    if not rows:
        return {"count": 0}
    groups: Counter = Counter()
    for row in rows:
        palette = json.loads(row["palette"] or "[]")
        if palette:
            groups[_hue_group(palette[0][0])] += 1
    sizes = Counter(f"{row['width']}x{row['height']}" for row in rows)
    return {
        "count": len(rows),
        "named_rate": round(sum(row["named"] for row in rows) / len(rows), 3),
        "brightness_p50": _median(row["brightness"] for row in rows),
        "saturation_p50": _median(row["saturation"] for row in rows),
        "text_edge_mean": {
            "top": round(sum(row["edge_top"] for row in rows) / len(rows), 3),
            "middle": round(sum(row["edge_mid"] for row in rows) / len(rows), 3),
            "bottom": round(sum(row["edge_bottom"] for row in rows) / len(rows), 3),
        },
        "dominant_hues": [{"hue": hue, "share": round(n / len(rows), 3)} for hue, n in groups.most_common(8)],
        "sizes": [{"size": size, "covers": n} for size, n in sizes.most_common(6)],
        "by_year": {str(row["year"]): row["n"] for row in store.query(
            "SELECT e.year AS year, COUNT(*) AS n FROM covers c JOIN entries e ON e.id = c.entry_id WHERE e.year > 0"
            " GROUP BY e.year ORDER BY e.year")},
    }


def _quality(store: ArchiveStore) -> dict:
    return {
        "entries_without_srt": store.scalar("SELECT COUNT(*) FROM entries WHERE srt_count = 0"),
        "entries_with_errors": store.scalar("SELECT COUNT(*) FROM entries WHERE error != ''"),
        "entries_with_several_videos": store.scalar("SELECT COUNT(*) FROM entries WHERE video_count > 1"),
        "entries_without_date": store.scalar("SELECT COUNT(*) FROM entries WHERE date = ''"),
        "entries_unrecognized_streamer": store.scalar("SELECT COUNT(*) FROM entries WHERE streamer = ''"),
        "sample_errors": [row["error"] for row in store.query("SELECT error FROM entries WHERE error != '' LIMIT 5")],
    }


def build_report(store: ArchiveStore, matcher: StreamerMatcher | None = None) -> dict:
    matcher = matcher or StreamerMatcher.from_profiles()
    glossary, replacements = _known_by_streamer()
    return {
        "version": REPORT_VERSION,
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "overview": _overview(store, matcher),
        "quality": _quality(store),
        "edits": _edits(store),
        "corrections": _corrections(store, matcher, replacements),
        "terms": {matcher.label(streamer): items for streamer, items in candidate_terms(store, glossary).items()},
        "titles": _titles(store, matcher),
        "subtitle_habits": _habits(store, matcher),
        "covers": _covers(store),
    }


def render_markdown(report: dict) -> str:
    """给人看的报告。"""

    overview, quality = report["overview"], report["quality"]
    lines = [
        "# 旧切片学习报告", "",
        f"生成于 {report['generated']}。数据只在本机，没有上传任何东西。", "",
        "## 概况", "",
        f"- 投稿文件夹 {overview['entries']} 个（{overview['date_min'] or '?'} ~ {overview['date_max'] or '?'}），成片约 {overview['video_gb']} GB",
        f"- 有字幕 {overview['with_srt']}，有校对稿 {overview['with_corrected']}，初稿+校对稿成对 {overview['with_both']}，"
        f"有字幕版成片 {overview['with_burned']}，有封面 {overview['with_cover']}",
        "- 按年份：" + "、".join(f"{year}年 {n}" for year, n in overview["by_year"].items()) if overview["by_year"] else "- 按年份：（没有日期信息）",
        "- 按主播：" + "、".join(f"{item['label']} {item['entries']}" for item in overview["by_streamer"][:8]),
        "", "## 数据质量", "",
        f"- 没有字幕 {quality['entries_without_srt']}，读取出错 {quality['entries_with_errors']}，一个文件夹多个成片 {quality['entries_with_several_videos']}，"
        f"认不出日期 {quality['entries_without_date']}，认不出主播 {quality['entries_unrecognized_streamer']}",
    ]
    edits = report["edits"]
    if edits["aligned_cues"]:
        kinds = "、".join(f"{kind} {n}" for kind, n in edits["kinds"].items() if kind != "same")
        lines += ["", "## 你校对时改什么", "", f"- 对得上的字幕 {edits['aligned_cues']} 条，其中改过的约 {edits['changed_rate']:.0%}", f"- 改动类型：{kinds}"]
    if report["corrections"]:
        lines += ["", "## 反复出现的错词（至少两个视频）", ""]
        for item in report["corrections"][:40]:
            warn = f" · 别处还有 {item['kept']} 处原样保留（精度 {item['precision']:.0%}，不适合当固定纠错）" if item["precision"] < 0.8 else ""
            lines.append(f"- 「{item['wrong']}」→「{item['right']}」 {item['videos']} 个视频 · {item['label']}{' · 已在词库' if item['known'] else ''}{warn}")
    for label, items in report["terms"].items():
        shown = [item for item in items if not item["known"]][:40]
        if shown:
            lines += ["", f"## 候选术语 · {label}", "", "、".join(f"{item['term']}({item['df']})" for item in shown)]
    titles = report["titles"]
    if titles.get("count"):
        length = titles["length"]
        lines += [
            "", "## 标题习惯", "",
            f"- {titles['count']} 个标题，长度中位数 {length['p50']:.0f} 字（90% 在 {length['p90']:.0f} 字以内）",
            f"- 开头带【】的占 {titles['bracket_prefix_rate']:.0%}，带 emoji 的占 {titles['emoji_rate']:.0%}，带数字的占 {titles['digit_rate']:.0%}",
            "- 常用标点占比：" + "、".join(f"{name} {rate:.0%}" for name, rate in titles["punctuation_rate"].items() if rate),
        ]
        if titles["series"]:
            lines.append("- 疑似系列：" + "；".join(f"{item['pattern']}（{item['count']} 期）" for item in titles["series"][:6]))
    habits = report["subtitle_habits"]
    if habits.get("videos"):
        lines += [
            "", "## 字幕习惯", "",
            f"- {habits['videos']} 个视频、{habits['cues']} 条字幕；一条中位数 {habits['chars_p50']:.0f} 字（90 分位 {habits['chars_p90']:.0f}），"
            f"读速中位数 {habits['cps_p50']:.1f} 字/秒（90 分位 {habits['cps_p90']:.1f}）",
            f"- 多行字幕占 {habits['multiline_rate']:.0%}，末尾带标点占 {habits['trailing_punct_rate']:.0%}，"
            f"过短（<0.65s）{habits['short_rate']:.1%}，过长（>8s）{habits['long_rate']:.1%}，重叠 {habits['overlap_rate']:.1%}",
        ]
    covers = report["covers"]
    if covers.get("count"):
        lines += [
            "", "## 封面", "",
            f"- 分析了 {covers['count']} 张；主色：" + "、".join(f"{item['hue']} {item['share']:.0%}" for item in covers["dominant_hues"][:6]),
            f"- 明暗中位数 {covers['brightness_p50']:.2f}，饱和度中位数 {covers['saturation_p50']:.2f}；"
            f"文字边缘多落在：上 {covers['text_edge_mean']['top']:.2f} / 中 {covers['text_edge_mean']['middle']:.2f} / 下 {covers['text_edge_mean']['bottom']:.2f}",
        ]
    return "\n".join(lines) + "\n"


def build_digest(report: dict, max_bytes: int = MAX_DIGEST_BYTES) -> dict:
    """可以整份贴给 GPT 的精华：只有统计和少量样本，没有视频、字幕全文和完整路径；超过体积上限就逐段裁短。"""

    digest = {
        "kind": "autoslice-archive-digest",
        "version": REPORT_VERSION,
        "generated": report["generated"],
        "for_assistant": (
            "这是一位直播切片员本机旧投稿档案的统计摘要（隐私：不含视频、字幕全文和完整路径，只有少量字幕片段）。请基于它帮助优化 AutoSlice："
            "字幕校对（错词库、术语表、字幕质量阈值）、标题和简介口吻、封面风格与系列模板。"
            "先指出数据里最显著的规律和可疑之处，再给出可以落到 docs/desktop-vnext/ARCHIVE_LEARNING.md 里“待办”的具体改动。"
        ),
        "overview": {key: value for key, value in report["overview"].items() if key not in ("roots", "last_run")},
        "quality": report["quality"],
        "edits": report["edits"],
        "corrections": [
            {key: item[key] for key in ("label", "wrong", "right", "videos", "n", "kept", "precision", "dominance", "context", "known")}
            for item in report["corrections"][:120]
        ],
        "terms": {label: [{"term": i["term"], "df": i["df"], "spread": i["spread"], "known": i["known"]} for i in items[:200]]
                  for label, items in report["terms"].items()},
        "titles": report["titles"],
        "subtitle_habits": report["subtitle_habits"],
        "covers": report["covers"],
    }
    while len(json.dumps(digest, ensure_ascii=False).encode("utf-8")) > max_bytes:
        if digest["corrections"]:
            digest["corrections"] = digest["corrections"][: max(0, len(digest["corrections"]) // 2)]
            continue
        trimmed = False
        for label, items in digest["terms"].items():
            if len(items) > 10:
                digest["terms"][label] = items[: len(items) // 2]
                trimmed = True
        if trimmed:
            continue
        titles = digest["titles"]
        if titles.get("samples_spread"):
            titles["samples_spread"] = titles["samples_spread"][: len(titles["samples_spread"]) // 2]
            titles["samples_recent"] = titles["samples_recent"][: len(titles["samples_recent"]) // 2]
            continue
        break
    return digest


def write_report(store: ArchiveStore, out_dir: str | Path, matcher: StreamerMatcher | None = None) -> dict[str, Path]:
    """写出 report.json / report.md / digest.json；返回各文件路径。"""

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    report = build_report(store, matcher)
    paths = {"json": out / "report.json", "markdown": out / "report.md", "digest": out / "digest.json"}
    paths["json"].write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    paths["markdown"].write_text(render_markdown(report), encoding="utf-8")
    paths["digest"].write_text(json.dumps(build_digest(report), ensure_ascii=False, indent=1), encoding="utf-8")
    return paths


# 给 proposals 等同包模块用的公开名字。
corrections_table = _corrections
habit_table = _habits
known_by_streamer = _known_by_streamer
title_stats = _titles
