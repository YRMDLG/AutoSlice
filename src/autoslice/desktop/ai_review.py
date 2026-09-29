"""桌面字幕 AI 检查边界：复用旧检查器，私有缓存只保存建议状态。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from autoslice.desktop.foundation import DesktopStorage
from autoslice.desktop.subtitles import SubtitleDocument, SubtitleEntry
from autoslice.llm.transport import load_api_config
from autoslice.streamer_profiles import resolve_streamer_profile
from autoslice.subtitle_workflow import SUBTITLE_REVIEW_VERSION, suggest_subtitle_corrections

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
    def __init__(self, suggestions: list[Suggestion], cache_key: str, content_hash: str):
        self.suggestions = suggestions
        self.cache_key = cache_key
        self.content_hash = content_hash

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


class AIReviewService:
    def __init__(self, storage: DesktopStorage, checker=suggest_subtitle_corrections,
                 config_loader=load_api_config):
        self.storage = storage
        self.checker = checker
        self.config_loader = config_loader

    def _identity(self, document: SubtitleDocument, project_title: str):
        config = self.config_loader()
        profile = resolve_streamer_profile("auto", document.video.path, context_hint=project_title)
        # 凭据绝不进入缓存键或文件；模型及规则变化会使结果失效。
        settings = (config.base_url, config.api_type, config.model,
                    config.review_reasoning_effort, getattr(config, "proxy_mode", None),
                    getattr(config, "allow_insecure_http", False), profile.id,
                    profile.subtitle_review_fingerprint(), SUBTITLE_REVIEW_VERSION)
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
            _, _, settings = self._identity(document, project_title)
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
        if envelope.get("content_hash") != document_hash(document.entries):
            return None, "stale"
        return AIReviewSession(suggestions, envelope["cache_key"], envelope["content_hash"]), "ready"

    def save(self, document: SubtitleDocument, project_title: str, session: AIReviewSession) -> None:
        _, _, settings = self._identity(document, project_title)
        self.storage._write_json(self._session_path(document), {
            "schema_version": SCHEMA_VERSION,
            "project_title": project_title,
            "settings_hash": _digest(settings),
            "cache_key": session.cache_key,
            "content_hash": session.content_hash,
            "source_fingerprint": document.source_fingerprint,
            "suggestions": [asdict(item) for item in session.suggestions],
        })

    def check(self, document: SubtitleDocument, project_title: str, *, force: bool = False,
              progress_callback=None) -> AIReviewSession:
        if hashlib.sha256(document.source_path.read_bytes()).hexdigest() != document.source_fingerprint:
            raise ValueError("源字幕已变化，请重新加载后再检查")
        config, profile, settings = self._identity(document, project_title)
        key = self._key(document, project_title, settings)
        input_path = self._result_path(key)
        input_path.parent.mkdir(parents=True, exist_ok=True)
        # 旧检查器只读这份快照；旧缓存也留在用户应用数据目录，不写投稿文件夹。
        blocks = [f"{entry.index}\n{entry.start} --> {entry.end}\n{entry.text}"
                  for entry in document.entries]
        snapshot = "\n\n".join(blocks) + "\n"
        if not input_path.exists() or input_path.read_text(encoding="utf-8") != snapshot:
            input_path.write_text(snapshot, encoding="utf-8", newline="\n")
        result = self.checker(input_path, context_title=project_title,
                              streamer_profile=profile, use_cache=not force,
                              progress_callback=progress_callback)
        if hashlib.sha256(document.source_path.read_bytes()).hexdigest() != document.source_fingerprint:
            raise ValueError("源字幕在检查期间已变化，请重新加载")
        by_index = {entry.index: entry for entry in document.entries}
        suggestions = []
        for item in result.get("suggestions", []):
            cue_id = int(item["index"])
            entry = by_index.get(cue_id)
            if entry is None or entry.text != item["original"]:
                continue
            suggestion_id = _digest((key, cue_id, item["original"], item["corrected"]))[:20]
            suggestions.append(Suggestion(
                suggestion_id, cue_id, item["original"], item["corrected"],
                item.get("reason", ""), item.get("confidence"),
                source=item.get("source", "ai"), model=config.model,
            ))
        return AIReviewSession(suggestions, key, document_hash(document.entries))
