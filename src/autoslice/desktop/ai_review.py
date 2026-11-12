"""桌面字幕 AI 检查边界：复用旧检查器，私有缓存只保存建议状态。"""

from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
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
    SUBTITLE_REVIEW_BATCH_SIZE,
    SUBTITLE_REVIEW_VERSION,
    subtitle_review_profile_rules,
    suggest_subtitle_corrections,
)

SCHEMA_VERSION = 1


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def document_hash(entries: list[SubtitleEntry] | tuple[SubtitleEntry, ...]) -> str:
    return _digest([asdict(entry) for entry in entries])


def text_digest(entry: SubtitleEntry) -> str:
    """AI 查的是文字：只改时间轴不算“改过”，不用重查。"""

    return _digest(entry.text)[:16]


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
                 local_only: bool = False, reviewed_entries: dict | None = None,
                 checked: dict | None = None, settings_key: str = ""):
        self.suggestions = suggestions
        self.cache_key = cache_key
        self.content_hash = content_hash
        # 只有本地同音检查的结果（没调用过 AI）：重新做 AI 检查时不用提醒“会消耗额度”
        self.local_only = local_only
        self.reviewed_entries = dict(reviewed_entries or {})
        # AI 查过的每句字幕当时的文字指纹：取消、关窗后接着查时，文字没变的不再发
        self.checked = dict(checked or {})
        # 查的时候用的模型和规则；换了就不能拿旧的“查过”跳过
        self.settings_key = settings_key
        # 恢复时因字幕已改而作废的建议条数
        self.dropped = 0

    def changed_indices(self, entries) -> set[int]:
        return {entry.index for entry in entries
                if self.reviewed_entries.get(str(entry.index)) != _digest(asdict(entry))}

    def unchecked(self, entries) -> list[int]:
        """还没被 AI 查过（或查过后文字又改了）的字幕编号，按字幕顺序。"""

        return [entry.index for entry in entries if self.checked.get(str(entry.index)) != text_digest(entry)]

    def absorb(self, other: "AIReviewSession", entries) -> None:
        """把新查完的一批（或全部）并进来：只替换重新查过的那几句，其它建议和已处理状态都留着。

        同一句再查出同样的建议不重复，原来采纳 / 跳过的状态照旧；原文和当前字幕对不上的待确认建议作废。
        """

        texts = {entry.index: entry.text for entry in entries}
        status = {item.suggestion_id: item.status for item in self.suggestions}
        covered = {int(key) for key in other.checked}
        merged: dict[str, Suggestion] = {}
        for item in self.suggestions:
            if item.cue_id in covered and item.status == "pending":
                continue
            merged[item.suggestion_id] = item
        for item in other.suggestions:
            if item.suggestion_id in merged:
                continue
            merged[item.suggestion_id] = replace(item, status=status.get(item.suggestion_id, item.status))
        order = {entry.index: position for position, entry in enumerate(entries)}
        self.suggestions = sorted(
            (item for item in merged.values()
             if item.cue_id in texts and (item.status != "pending" or texts[item.cue_id] == item.original_text)),
            key=lambda item: order[item.cue_id],
        )
        self.checked.update(other.checked)
        self.reviewed_entries.update(other.reviewed_entries)
        self.settings_key = other.settings_key or self.settings_key
        self.cache_key = other.cache_key
        self.local_only = self.local_only and other.local_only
        self.content_hash = document_hash(entries)

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
                    # 采纳 AI 改法后的文字也算查过，“继续检查”不再把它发出去
                    if str(entry.index) in self.checked:
                        self.checked[str(entry.index)] = text_digest(entry)


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
        # 同时在跑的批数；测试里可调成 1，让“第几批后取消”可预期
        self.concurrency = DESKTOP_REVIEW_CONCURRENCY

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
        checked = envelope.get("checked", {})
        if not isinstance(reviewed, dict) or any(not isinstance(value, str) for value in reviewed.values()):
            return None, "invalid"
        if not isinstance(checked, dict) or any(not isinstance(value, str) for value in checked.values()):
            return None, "invalid"
        current = document_hash(document.entries)
        settings_key = str(envelope.get("settings_hash") or "")
        if envelope.get("content_hash") == current:
            return AIReviewSession(suggestions, envelope["cache_key"], current, local_only, reviewed,
                                   checked, settings_key), "ready"
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
        session = AIReviewSession(kept, envelope["cache_key"], current, local_only, reviewed, checked, settings_key)
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
            "checked": session.checked,
            "suggestions": [asdict(item) for item in session.suggestions],
        })

    def check(self, document: SubtitleDocument, project_title: str, *, force: bool = False,
              progress_callback=None, cue_ids: set[int] | None = None,
              previous: AIReviewSession | None = None, partial_callback=None) -> AIReviewSession:
        """分批检查：每批查完就交给 partial_callback（界面边查边显示、当场落盘），取消后已查完的不丢。

        previous 是当前会话：同一套模型和规则下查过、文字没变的句子不再发（force 时照样重查）；
        cue_ids 只查这些句子，其它句子的建议原样保留。progress_callback 抛异常就是取消，
        已经在跑的批次让它跑完（结果丢掉，检查器自己的缓存还在），不再发新的批次。
        """

        if hashlib.sha256(document.source_path.read_bytes()).hexdigest() != document.source_fingerprint:
            raise ValueError("源字幕已变化，请重新加载后再检查")
        config, profile, settings = self._identity(document, project_title)
        settings_key = _digest(settings)
        key = self._key(document, project_title, settings)
        by_index = {entry.index: entry for entry in document.entries}
        if cue_ids is not None and (not cue_ids or not cue_ids <= set(by_index)):
            raise ValueError("请选择有效的复查字幕")
        targets = [entry for entry in document.entries if cue_ids is None or entry.index in cue_ids]
        same_rules = previous is not None and not previous.local_only and previous.settings_key == settings_key
        done = {} if force or not same_rules else {
            str(entry.index): previous.checked[str(entry.index)] for entry in targets
            if previous.checked.get(str(entry.index)) == text_digest(entry)
        }
        pending = [entry for entry in targets if str(entry.index) not in done]
        batches = [pending[start:start + SUBTITLE_REVIEW_BATCH_SIZE]
                   for start in range(0, len(pending), SUBTITLE_REVIEW_BATCH_SIZE)]
        # 旧检查器只读这份快照（整份，前后文要用）；缓存在用户应用数据目录，不写投稿文件夹。
        snapshot = "\n\n".join(f"{entry.index}\n{entry.start} --> {entry.end}\n{entry.text}"
                               for entry in document.entries) + "\n"
        # 学到的术语表只挑和这份字幕有关的几十个，不整份塞给模型；没学过就一个额外参数都不加。
        learned = Knowledge(self.storage).relevant_glossary(
            profile.id, "\n".join(entry.text for entry in document.entries),
        )
        extra = {"glossary": learned} if learned else {}

        def review(batch):
            ids = [entry.index for entry in batch]
            # 每批单独一个目录：检查器的缓存按批记，取消后同一批再查直接命中
            input_path = self._result_path(_digest((key, ids)))
            input_path.parent.mkdir(parents=True, exist_ok=True)
            if not input_path.exists() or input_path.read_text(encoding="utf-8") != snapshot:
                input_path.write_text(snapshot, encoding="utf-8", newline="\n")
            result = self.checker(input_path, context_title=project_title, streamer_profile=profile,
                                  use_cache=not force, progress_callback=None, concurrency=1,
                                  request_timeout=DESKTOP_REVIEW_TIMEOUT, review_indices=ids, **extra)
            return batch, result

        found: list[Suggestion] = []
        checked = dict(done)

        def assemble(*, final: bool) -> AIReviewSession:
            own = [item for item in found]
            if final:
                # 本地同音等建议顺带并进来；AI 已经给过建议的句子以 AI 为准
                taken = {(item.cue_id, item.suggested_text) for item in own}
                covered = {item.cue_id for item in own}
                own += [item for item in self.local_suggestions(document, project_title)
                        if item.cue_id not in covered and (item.cue_id, item.suggested_text) not in taken
                        and str(item.cue_id) in checked and str(item.cue_id) not in done]
            session = AIReviewSession(
                own, key, document_hash(document.entries),
                reviewed_entries={str(entry.index): _digest(asdict(entry)) for entry in document.entries
                                  if str(entry.index) in checked and str(entry.index) not in done},
                checked={index: digest for index, digest in checked.items() if index not in done},
                settings_key=settings_key,
            )
            base = AIReviewSession([], key, session.content_hash, settings_key=settings_key)
            if previous is not None and (same_rules or cue_ids is not None):
                base = AIReviewSession(list(previous.suggestions), key, previous.content_hash,
                                       reviewed_entries=previous.reviewed_entries,
                                       checked=previous.checked if same_rules else {}, settings_key=settings_key)
            base.absorb(session, document.entries)
            return base

        total = len(batches)
        if progress_callback:
            progress_callback("正在检查字幕…", 0, total)
        lock = threading.Lock()
        executor = ThreadPoolExecutor(max_workers=max(1, min(self.concurrency, total or 1)),
                                      thread_name_prefix="autoslice-desktop-review")
        try:
            queue, running, completed = list(batches), set(), 0
            while queue or running:
                # 跑完一批才补发下一批：取消时最多白跑正在路上的几批，不会把剩下的全发出去
                while queue and len(running) < self.concurrency:
                    running.add(executor.submit(review, queue.pop(0)))
                finished, running = wait(running, return_when=FIRST_COMPLETED)
                for future in finished:
                    batch, result = future.result()
                    if hashlib.sha256(document.source_path.read_bytes()).hexdigest() != document.source_fingerprint:
                        raise ValueError("源字幕在检查期间已变化，请重新加载")
                    with lock:
                        found.extend(self._batch_suggestions(result, batch, settings_key, config.model))
                        checked.update({str(entry.index): text_digest(entry) for entry in batch})
                    completed += 1
                    # 先交结果再报进度：取消是在报进度时抛出的，这一批不会丢
                    if partial_callback:
                        partial_callback(assemble(final=False))
                    if progress_callback:
                        progress_callback(f"已检查 {completed}/{total} 批", completed, total)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        return assemble(final=True)

    @staticmethod
    def _batch_suggestions(result, batch, settings_key: str, model: str) -> list[Suggestion]:
        """一批的结果：只收这批里、原文和快照一致的句子；编号只看规则 + 句子 + 改法，重复返回不重复。"""

        texts = {entry.index: entry.text for entry in batch}
        suggestions, seen = [], set()
        for item in (result or {}).get("suggestions", []):
            try:
                cue_id = int(item["index"])
            except (KeyError, TypeError, ValueError):
                continue
            if texts.get(cue_id) != item.get("original") or not item.get("corrected"):
                continue
            suggestion_id = _digest((settings_key, cue_id, item["original"], item["corrected"]))[:20]
            if suggestion_id in seen:
                continue
            seen.add(suggestion_id)
            suggestions.append(Suggestion(
                suggestion_id, cue_id, item["original"], item["corrected"],
                item.get("reason", ""), item.get("confidence"),
                source=item.get("source", "ai"), model=model,
            ))
        return suggestions
