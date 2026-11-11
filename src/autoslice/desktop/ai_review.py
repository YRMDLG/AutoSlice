"""桌面字幕 AI 检查边界：复用旧检查器，私有缓存只保存建议状态。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from autoslice.desktop.archive.knowledge import Knowledge
from autoslice.desktop.asr.hotwords import build_hotwords
from autoslice.desktop.correction_memory import CorrectionMemory
from autoslice.desktop.foundation import DesktopStorage
from autoslice.desktop.homophone import HomophoneIndex, apply_hits, describe
from autoslice.desktop.subtitles import SubtitleDocument, SubtitleEntry
from autoslice.llm.transport import load_api_config
from autoslice.streamer_profiles import resolve_streamer_profile
from autoslice.subtitle_workflow import (
    SUBTITLE_REVIEW_VERSION,
    subtitle_review_profile_rules,
    suggest_subtitle_corrections,
)

SCHEMA_VERSION = 1


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def document_hash(entries: list[SubtitleEntry] | tuple[SubtitleEntry, ...]) -> str:
    return _digest([asdict(entry) for entry in entries])


@dataclass(frozen=True)
class Suggestion:
    suggestion_id: str
    cue_id: int
    original_text: str
    suggested_text: str
    reason: str
    confidence: float | None
    status: str = "pending"
    source: str = "ai"
    model: str = ""
    prompt_version: int = SUBTITLE_REVIEW_VERSION


class AIReviewSession:
    def __init__(self, suggestions: list[Suggestion], cache_key: str, content_hash: str,
                 local_only: bool = False, reviewed_entries: dict | None = None):
        self.suggestions = suggestions
        self.cache_key = cache_key
        self.content_hash = content_hash
        # 只有本地同音检查的结果（没调用过 AI）：重新做 AI 检查时不用提醒“会消耗额度”
        self.local_only = local_only
        self.reviewed_entries = dict(reviewed_entries or {})
        # 恢复时因字幕已改而作废的建议条数
        self.dropped = 0

    def changed_indices(self, entries) -> set[int]:
        return {entry.index for entry in entries
                if self.reviewed_entries.get(str(entry.index)) != _digest(asdict(entry))}

    @property
    def pending(self) -> list[Suggestion]:
        return [item for item in self.suggestions if item.status == "pending"]

    def find(self, suggestion_id: str) -> Suggestion:
        return next(item for item in self.suggestions if item.suggestion_id == suggestion_id)

    def mark(self, suggestion_id: str, status: str, entries) -> None:
        if status not in ("pending", "accepted", "skipped"):
            raise ValueError("未知建议状态")
        self.suggestions = [replace(item, status=status) if item.suggestion_id == suggestion_id else item
                            for item in self.suggestions]
        self.content_hash = document_hash(entries)
        item = next((item for item in self.suggestions if item.suggestion_id == suggestion_id), None)
        if status == "accepted" and item is not None:
            for entry in entries:
                if entry.index == item.cue_id:
                    self.reviewed_entries[str(entry.index)] = _digest(asdict(entry))


DESKTOP_REVIEW_CONCURRENCY = 4
DESKTOP_REVIEW_TIMEOUT = (15, 120)


class AIReviewService:
    def __init__(self, storage: DesktopStorage, checker=suggest_subtitle_corrections,
                 config_loader=load_api_config, terms_provider=None):
        self.storage = storage
        self.checker = checker
        self.config_loader = config_loader
        self.terms_provider = terms_provider or self._review_terms
        self.memory = CorrectionMemory(storage)

    def _review_terms(self, document: SubtitleDocument, project_title: str) -> list[str]:
        """同音检查用的术语：固定词、主播配置、你确认过的学到术语（比识别热词多放，不受 60 个限制）。"""

        return build_hotwords(document.video.path, storage=self.storage, title=project_title, limit=2000)

    def local_suggestions(self, document: SubtitleDocument, project_title: str,
                          *, homophones: bool = True) -> list[Suggestion]:
        """本地检查：不调用 AI、不花额度、秒出结果；都只是待确认建议，不会自动改字。

        依次用：主播固定纠错（原来只在 AI 检查时生效）、已记住的校对改法、观察中且在别的字幕里
        从没被保留过原写法的改法（同一个错词改过就提示，可靠性低一档，理由里写明），最后是同音字。
        没装 pypinyin 时只是少了同音字这一项。
        """

        index = HomophoneIndex(self.terms_provider(document, project_title), enabled=homophones)
        profile = resolve_streamer_profile("auto", document.video.path, context_hint=project_title)
        try:
            fixed = subtitle_review_profile_rules(profile)[1]
        except (TypeError, ValueError):
            fixed = ()
        remembered = self.memory.replacements(profile.id)
        observed = tuple(
            (item.wrong, item.right) for _profile, item in self.memory.entries(profile.id)
            if not item.promoted and item.kept == 0 and len(item.wrong) >= 2 and item.wrong != item.right
            and (item.wrong, item.right) not in remembered
        )
        layers = (("主播固定纠错", fixed), ("校对记忆", remembered), ("校对记忆（观察中，未记住）", observed))
        content = document_hash(document.entries)
        suggestions = []
        for entry in document.entries:
            corrected = entry.text
            reasons, sources = [], []
            for label, pairs in layers:
                used = []
                for wrong, right in sorted(pairs, key=lambda pair: -len(pair[0])):
                    if wrong in corrected and right not in corrected:
                        corrected = corrected.replace(wrong, right)
                        used.append(f"“{wrong}”→“{right}”")
                if used:
                    reasons.append(f"{label}：" + "、".join(used))
                    sources.append("fixed_replacement" if label == "主播固定纠错" else "memory")
            hits = index.find(corrected)
            if not hits and not reasons:
                continue
            corrected = apply_hits(corrected, hits)
            if corrected == entry.text:
                continue
            suggestions.append(Suggestion(
                _digest((content, entry.index, entry.text, corrected))[:20], entry.index, entry.text, corrected,
                "；".join(filter(None, [*reasons, describe(hits)])),
                None, source=sources[0] if sources else "homophone",
            ))
        return suggestions

    def _local_identity(self, document, project_title):
        profile = resolve_streamer_profile("auto", document.video.path, context_hint=project_title)
        # 本地检查也看“观察中”的改法：记忆有变化时，旧的本地结果按过期处理
        observed = tuple((item.wrong, item.right, item.kept) for _p, item in self.memory.entries(profile.id)
                         if not item.promoted)
        return (profile.id, profile.subtitle_review_fingerprint(),
                self.terms_provider(document, project_title), self.memory.replacements(profile.id), observed)

    def _identity(self, document: SubtitleDocument, project_title: str):
        config = self.config_loader()
        profile = resolve_streamer_profile("auto", document.video.path, context_hint=project_title)
        # 凭据绝不进入缓存键或文件；模型及规则变化会使结果失效。
        settings = (config.base_url, config.api_type, config.model,
                    config.review_reasoning_effort, getattr(config, "proxy_mode", None),
                    getattr(config, "allow_insecure_http", False), profile.id,
                    profile.subtitle_review_fingerprint(), SUBTITLE_REVIEW_VERSION,
                    self._local_identity(document, project_title))
        return config, profile, settings

    def _key(self, document: SubtitleDocument, project_title: str, settings) -> str:
        return _digest((str(document.source_path.resolve()), project_title,
                        document_hash(document.entries), settings))

    def _session_path(self, document: SubtitleDocument) -> Path:
        return self.storage.ai_cache / f"session-{_digest(str(document.source_path.resolve()))}.json"

    def _result_path(self, key: str) -> Path:
        return self.storage.ai_cache / key / "input.srt"

    def load(self, document: SubtitleDocument, project_title: str) -> tuple[AIReviewSession | None, str]:
        try:
            envelope = json.loads(self._session_path(document).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None, "missing"
        except (OSError, ValueError, UnicodeError):
            return None, "invalid"
        if not isinstance(envelope, dict) or envelope.get("schema_version") != SCHEMA_VERSION:
            return None, "invalid"
        try:
            local_only = envelope.get("local_only", False)
            if type(local_only) is not bool:
                return None, "invalid"
            settings = self._local_identity(document, project_title) if local_only else self._identity(document, project_title)[2]
            suggestions = [Suggestion(**item) for item in envelope["suggestions"]]
        except (OSError, ValueError) as exc:
            return None, "unconfigured" if "未配置" in str(exc) else "invalid"
        except (KeyError, TypeError):
            return None, "invalid"
        if envelope.get("settings_hash") != _digest(settings) or envelope.get("project_title") != project_title:
            return None, "stale"
        if not isinstance(envelope.get("cache_key"), str) or any(
            item.status not in ("pending", "accepted", "skipped") for item in suggestions
        ):
            return None, "invalid"
        if envelope.get("source_fingerprint") != document.source_fingerprint:
            return None, "stale"
        reviewed = envelope.get("reviewed_entries", {})
        if not isinstance(reviewed, dict) or any(not isinstance(value, str) for value in reviewed.values()):
            return None, "invalid"
        current = document_hash(document.entries)
        if envelope.get("content_hash") == current:
            return AIReviewSession(suggestions, envelope["cache_key"], current, local_only, reviewed), "ready"
        # 字幕和检查时不一样（例如启动时没恢复草稿）：逐条核对，原文还对得上的建议照样能用，
        # 不用整批作废、重花额度。草稿里采纳过、现在又变回原文的，重新待确认。
        texts = {entry.index: entry.text for entry in document.entries}
        kept = []
        for item in suggestions:
            matches = texts.get(item.cue_id) == item.original_text
            if item.status == "pending" and not matches:
                continue
            kept.append(replace(item, status="pending") if item.status == "accepted" and matches else item)
        if not any(item.status == "pending" for item in kept):
            return None, "stale"
        session = AIReviewSession(kept, envelope["cache_key"], current, local_only, reviewed)
        session.dropped = len(suggestions) - len(kept)
        return session, "partial"

    def save(self, document: SubtitleDocument, project_title: str, session: AIReviewSession) -> None:
        settings = self._local_identity(document, project_title) if session.local_only else self._identity(document, project_title)[2]
        self.storage._write_json(self._session_path(document), {
            "schema_version": SCHEMA_VERSION,
            "project_title": project_title,
            "settings_hash": _digest(settings),
            "cache_key": session.cache_key,
            "content_hash": session.content_hash,
            "source_fingerprint": document.source_fingerprint,
            "local_only": session.local_only,
            "reviewed_entries": session.reviewed_entries,
            "suggestions": [asdict(item) for item in session.suggestions],
        })

    def check(self, document: SubtitleDocument, project_title: str, *, force: bool = False,
              progress_callback=None, cue_ids: set[int] | None = None,
              previous: AIReviewSession | None = None) -> AIReviewSession:
        if hashlib.sha256(document.source_path.read_bytes()).hexdigest() != document.source_fingerprint:
            raise ValueError("源字幕已变化，请重新加载后再检查")
        config, profile, settings = self._identity(document, project_title)
        key = self._key(document, project_title, settings)
        if cue_ids is not None:
            if not cue_ids or not cue_ids <= {entry.index for entry in document.entries}:
                raise ValueError("请选择有效的复查字幕")
            key = _digest((key, sorted(cue_ids)))
        input_path = self._result_path(key)
        input_path.parent.mkdir(parents=True, exist_ok=True)
        # 旧检查器只读这份快照；旧缓存也留在用户应用数据目录，不写投稿文件夹。
        blocks = [f"{entry.index}\n{entry.start} --> {entry.end}\n{entry.text}"
                  for entry in document.entries]
        snapshot = "\n\n".join(blocks) + "\n"
        if not input_path.exists() or input_path.read_text(encoding="utf-8") != snapshot:
            input_path.write_text(snapshot, encoding="utf-8", newline="\n")
        # 学到的术语表只挑和这份字幕有关的几十个，不整份塞给模型；没学过就一个额外参数都不加。
        learned = Knowledge(self.storage).relevant_glossary(
            profile.id, "\n".join(entry.text for entry in document.entries),
        )
        extra = {"glossary": learned} if learned else {}
        if cue_ids is not None:
            extra["review_indices"] = sorted(cue_ids)
        # 桌面端是边等边用：4 批并行（147 句约 5 批，实测 27 秒降到一轮多），单次最多等 120 秒
        result = self.checker(input_path, context_title=project_title,
                              streamer_profile=profile, use_cache=not force,
                              progress_callback=progress_callback,
                              concurrency=DESKTOP_REVIEW_CONCURRENCY,
                              request_timeout=DESKTOP_REVIEW_TIMEOUT, **extra)
        if hashlib.sha256(document.source_path.read_bytes()).hexdigest() != document.source_fingerprint:
            raise ValueError("源字幕在检查期间已变化，请重新加载")
        by_index = {entry.index: entry for entry in document.entries}
        suggestions = []
        for item in result.get("suggestions", []):
            cue_id = int(item["index"])
            entry = by_index.get(cue_id)
            if entry is None or entry.text != item["original"] or (cue_ids is not None and cue_id not in cue_ids):
                continue
            suggestion_id = _digest((key, cue_id, item["original"], item["corrected"]))[:20]
            suggestions.append(Suggestion(
                suggestion_id, cue_id, item["original"], item["corrected"],
                item.get("reason", ""), item.get("confidence"),
                source=item.get("source", "ai"), model=config.model,
            ))
        # 本地同音建议顺带并进来；AI 已经给过建议的那几条以 AI 为准
        covered = {item.cue_id for item in suggestions}
        suggestions += [item for item in self.local_suggestions(document, project_title)
                        if item.cue_id not in covered and (cue_ids is None or item.cue_id in cue_ids)]
        reviewed = dict(previous.reviewed_entries) if previous and cue_ids is not None else {}
        if previous is not None and cue_ids is not None:
            suggestions = [item for item in previous.suggestions if item.cue_id not in cue_ids
                           and item.cue_id in by_index] + suggestions
        reviewed.update({str(entry.index): _digest(asdict(entry)) for entry in document.entries
                         if cue_ids is None or entry.index in cue_ids})
        order = {entry.index: index for index, entry in enumerate(document.entries)}
        suggestions.sort(key=lambda item: order[item.cue_id])
        return AIReviewSession(suggestions, key, document_hash(document.entries), reviewed_entries=reviewed)
