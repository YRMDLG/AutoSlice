"""从证据库生成“待确认的建议”：学到了什么、证据是什么，由用户确认后才写进各处，不静默改任何配置。

四类建议：
- replacement：固定纠错（错词 → 正确词），确认后写进本机覆盖词库（受“每个主播最多 100 条”的上限约束）；
- glossary：候选术语，确认后进“学到的术语表”（不受 200 条上限约束，运行时按当前字幕检索，不整份塞给大模型）；
- titles：把你的旧标题当作投稿文案的口吻范例（运行时挑和当前视频最像的几个）；
- subtitle_limits：按你自己字幕的 90 分位定的字数/读速阈值，给字幕质量提醒用。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from autoslice.desktop.foundation import DesktopStorage

from .knowledge import Knowledge, archive_dir
from .layout import StreamerMatcher
from .report import candidate_terms, corrections_table, habit_table, known_by_streamer, title_stats
from .store import ArchiveStore

PENDING, ACCEPTED, REJECTED = "pending", "accepted", "rejected"
MIN_VIDEOS = 3
# 改过的次数占“改过 + 别处原样保留”的比例至少这么高，才当固定纠错（防止把“意思→ins”这种多数时候是对的词改坏）。
MIN_PRECISION = 0.8
MIN_TERM_DF = 5
MAX_SPREAD = 2
HABIT_MIN_VIDEOS = 20


@dataclass(frozen=True)
class Proposal:
    id: str
    kind: str
    streamer: str
    label: str
    title: str
    payload: dict
    evidence: str = ""
    score: float = 0.0
    status: str = PENDING


def _id(*parts: str) -> str:
    return hashlib.sha1("\t".join(parts).encode("utf-8")).hexdigest()[:12]


def _is_profile(streamer: str) -> bool:
    return bool(streamer) and not streamer.startswith("ext:")


def build_proposals(store: ArchiveStore, matcher: StreamerMatcher | None = None) -> list[Proposal]:
    matcher = matcher or StreamerMatcher.from_profiles()
    glossary, replacements = known_by_streamer()
    proposals: list[Proposal] = []
    for item in corrections_table(store, matcher, replacements, limit=100000):
        if (
            item["known"] or not item["evidence_complete"] or item["videos"] < MIN_VIDEOS
            or item["precision"] < MIN_PRECISION or not _is_profile(item["streamer"])
        ):
            continue
        proposals.append(Proposal(
            id=_id("replacement", item["streamer"], item["wrong"], item["right"]), kind="replacement",
            streamer=item["streamer"], label=item["label"],
            title=f"固定纠错：「{item['wrong']}」→「{item['right']}」",
            payload={"wrong": item["wrong"], "right": item["right"]},
            evidence=f"{item['videos']} 个视频里都改过（共 {item['n']} 处，别处原样保留 {item['kept']} 处），例：{item['context']}",
            score=item["videos"] + item["n"] * 0.01,
        ))
    for streamer, items in candidate_terms(store, glossary, min_df=MIN_TERM_DF).items():
        if not _is_profile(streamer):
            continue
        for item in items[:150]:
            if item["known"] or item["spread"] > MAX_SPREAD:
                continue
            proposals.append(Proposal(
                id=_id("glossary", streamer, item["term"]), kind="glossary", streamer=streamer,
                label=matcher.label(streamer), title=f"术语：{item['term']}", payload={"term": item["term"]},
                evidence=f"出现在 {item['df']} 个视频里", score=float(item["df"]),
            ))
    titles = title_stats(store, matcher)
    if titles.get("count", 0) >= 10:
        for streamer in {row["streamer"] for row in store.query("SELECT DISTINCT streamer FROM entries")}:
            if not _is_profile(streamer):
                continue
            rows = store.query("SELECT title FROM entries WHERE streamer = ? ORDER BY date, title", (streamer,))
            if len(rows) < 10:
                continue
            chosen = [row["title"] for row in rows]
            proposals.append(Proposal(
                id=_id("titles", streamer, str(len(chosen))), kind="titles", streamer=streamer,
                label=matcher.label(streamer), title=f"把 {len(chosen)} 个旧标题当作投稿文案的口吻范例",
                payload={"titles": chosen[-400:]}, evidence="运行时只挑和当前视频最像的几个给 AI", score=1000.0,
            ))
    habit_summary = habit_table(store, matcher)
    for streamer, summary in habit_summary.get("by_streamer", {}).items():
        if not _is_profile(streamer) or summary["videos"] < HABIT_MIN_VIDEOS:
            continue
        limits = {
            "cps_limit": round(summary["cps_p90_q90"] * 1.1, 1),
            "chars_limit": round(summary["chars_p90_q90"] * 1.1),
            "line_limit": round(summary["line_p90_q90"] * 1.1),
        }
        proposals.append(Proposal(
            id=_id("subtitle_limits", streamer), kind="subtitle_limits", streamer=streamer, label=summary["label"],
            title=f"字幕提醒按你的习惯：读速超过 {limits['cps_limit']} 字/秒、一条超过 {limits['chars_limit']} 字就提醒",
            payload=limits, evidence=f"来自 {summary['videos']} 个视频、{summary['cues']} 条字幕", score=900.0,
        ))
    proposals.sort(key=lambda item: (-item.score, item.id))
    return proposals


# ── 处理状态 ──
def _status_path(storage: DesktopStorage) -> Path:
    return archive_dir(storage) / "proposals.json"


def load_status(storage: DesktopStorage) -> dict[str, str]:
    try:
        payload = json.loads(_status_path(storage).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    value = payload.get("status") if isinstance(payload, dict) else None
    return {str(key): str(item) for key, item in value.items()} if isinstance(value, dict) else {}


def set_status(storage: DesktopStorage, ids: list[str], status: str) -> None:
    current = load_status(storage)
    for proposal_id in ids:
        current[proposal_id] = status
    DesktopStorage._write_json(_status_path(storage), {"schema_version": 1, "status": current})


def with_status(storage: DesktopStorage, proposals: list[Proposal]) -> list[Proposal]:
    status = load_status(storage)
    return [replace(item, status=status.get(item.id, PENDING)) for item in proposals]


def apply_proposals(
    storage: DesktopStorage, proposals: list[Proposal],
    *, replace_pair: Callable[[str, str, str], object] | None = None,
) -> tuple[list[str], list[str]]:
    """应用一批建议；返回（已应用的 id, 失败说明）。固定纠错写不进去（冲突、满额）的只记失败，不影响其他。"""

    if replace_pair is None:
        from autoslice.streamer_profiles import add_streamer_profile_replacement as replace_pair
    knowledge = Knowledge(storage)
    applied: list[str] = []
    failures: list[str] = []
    terms: dict[str, list[str]] = {}
    term_ids: dict[str, list[str]] = {}
    for item in proposals:
        try:
            if item.kind == "replacement":
                replace_pair(item.streamer, item.payload["wrong"], item.payload["right"])
                applied.append(item.id)
            elif item.kind == "glossary":
                terms.setdefault(item.streamer, []).append(item.payload["term"])
                term_ids.setdefault(item.streamer, []).append(item.id)
            elif item.kind == "titles":
                knowledge.set_titles(item.streamer, list(item.payload["titles"]))
                applied.append(item.id)
            elif item.kind == "subtitle_limits":
                knowledge.set_habits(item.streamer, dict(item.payload))
                applied.append(item.id)
        except (ValueError, OSError, KeyError) as exc:
            failures.append(f"{item.title}：{exc}")
    for streamer, items in terms.items():
        knowledge.add_glossary(streamer, items)
        applied.extend(term_ids[streamer])
    if applied:
        set_status(storage, applied, ACCEPTED)
    return applied, failures
