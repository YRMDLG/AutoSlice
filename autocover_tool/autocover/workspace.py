"""切片目录扫描、标题匹配和候选帧任务管理。"""

from __future__ import annotations

import hashlib
import os
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .style import get_palette, get_template
from .titles import load_title_map, match_title, recommend_visual_style
from .video import FrameCandidate, extract_candidate_frames


VIDEO_EXTENSIONS = frozenset({".flv", ".mp4", ".mkv", ".mov", ".avi"})
DEFAULT_INPUT_DIR = Path(
    os.environ.get("AUTOCOVER_INPUT_DIR", Path.cwd() / "input")
).expanduser().resolve()
DEFAULT_OUTPUT_DIR = Path(
    os.environ.get("AUTOCOVER_OUTPUT_DIR", Path.cwd() / "covers")
).expanduser().resolve()
DEFAULT_IGNORED_DIRECTORY_NAMES = frozenset(
    name.casefold() for name in ("视频素材", "封面", "封面输出")
)
MEDIA_TOKEN_TTL_SEC = 12 * 60 * 60
MEDIA_TOKEN_LIMIT = 2048
PREVIEW_HISTORY_PER_TASK = 6


def _is_ignored_video(path: Path, root: Path) -> bool:
    """判断视频是否位于默认不参与切片扫描的子目录。"""

    relative = path.relative_to(root)
    return any(
        part.casefold() in DEFAULT_IGNORED_DIRECTORY_NAMES
        for part in relative.parts[:-1]
    )


def _path_timestamps(path: Path) -> tuple[float, float]:
    try:
        stat = path.stat()
    except OSError:
        return 0.0, 0.0
    created_at = getattr(stat, "st_birthtime", stat.st_ctime)
    return float(created_at), float(stat.st_mtime)


@dataclass(slots=True)
class CoverTask:
    """一个待制作双比例封面的切片任务。"""

    id: str
    video_path: str
    relative_path: str
    filename: str
    title: str
    template_key: str
    palette_key: str
    folder_created_at: float
    folder_modified_at: float
    source_created_at: float
    source_modified_at: float
    status: str = "pending"
    candidates: tuple[FrameCandidate, ...] = ()
    selected_index: int = 0
    error: str | None = None
    output_paths: dict[str, str] = field(default_factory=dict)


class CoverWorkspace:
    """管理一个切片目录中的封面任务和允许访问的媒体文件。"""

    def __init__(
        self,
        root: str | Path,
        *,
        title_file: str | Path | None = None,
        cache_dir: str | Path | None = None,
        output_dir: str | Path | None = None,
        recursive: bool = True,
    ) -> None:
        workspace_root = Path(root).expanduser().resolve()
        if not workspace_root.is_dir():
            raise NotADirectoryError(f"切片目录不存在：{workspace_root}")

        self.root = workspace_root
        self.title_file = Path(title_file).expanduser().resolve() if title_file else None
        self.cache_dir = Path(cache_dir or Path.cwd() / ".cache" / "frames").expanduser().resolve()
        self.output_dir = Path(output_dir or DEFAULT_OUTPUT_DIR).expanduser().resolve()
        self.recursive = recursive
        self._title_map = load_title_map(self.title_file)
        self._tasks: dict[str, CoverTask] = {}
        self._media_tokens: OrderedDict[str, tuple[Path, float]] = OrderedDict()
        self._token_secret = secrets.token_bytes(32)
        self._lock = threading.RLock()

    @staticmethod
    def _task_id(relative_path: Path) -> str:
        normalized = relative_path.as_posix().casefold().encode("utf-8")
        return hashlib.sha256(normalized).hexdigest()[:16]

    def _register_media(self, path: str | Path) -> str:
        with self._lock:
            source = Path(path).expanduser().resolve()
            if not source.is_file():
                raise FileNotFoundError(f"媒体文件不存在：{source}")
            digest = hashlib.sha256(
                self._token_secret + str(source).casefold().encode("utf-8")
            ).hexdigest()
            token = digest[:32]
            now = time.time()
            self._prune_media_tokens_locked(now)
            self._media_tokens[token] = (source, now)
            self._media_tokens.move_to_end(token)
            self._prune_media_tokens_locked(now)
            return token

    def _prune_media_tokens_locked(self, now: float | None = None) -> None:
        now = time.time() if now is None else float(now)
        expired = [
            token
            for token, (_, accessed_at) in self._media_tokens.items()
            if now - accessed_at > MEDIA_TOKEN_TTL_SEC
        ]
        for token in expired:
            self._media_tokens.pop(token, None)
        while len(self._media_tokens) > MEDIA_TOKEN_LIMIT:
            self._media_tokens.popitem(last=False)

    def _output_paths_for(
        self,
        relative_path: Path,
        *,
        include_source_suffix: bool = False,
    ) -> dict[str, str]:
        output_relative = relative_path
        if self.output_dir == DEFAULT_OUTPUT_DIR.resolve():
            source_path = (self.root / relative_path).resolve()
            try:
                output_relative = source_path.relative_to(DEFAULT_OUTPUT_DIR.parent.resolve())
            except ValueError:
                output_relative = Path(self.root.name) / relative_path
        target_dir = self.output_dir / output_relative.parent
        stem = relative_path.stem
        if include_source_suffix:
            suffix = relative_path.suffix.lstrip(".").casefold() or "video"
            stem = f"{stem}-{suffix}"
        return {
            "4x3": str((target_dir / f"{stem}-4x3.jpg").resolve()),
            "16x9": str((target_dir / f"{stem}-16x9.jpg").resolve()),
        }

    def scan(self) -> list[CoverTask]:
        """扫描支持的视频并重建任务列表。"""

        iterator = self.root.rglob("*") if self.recursive else self.root.glob("*")
        videos = sorted(
            (
                path.resolve()
                for path in iterator
                if (
                    path.is_file()
                    and path.suffix.casefold() in VIDEO_EXTENSIONS
                    and not _is_ignored_video(path, self.root)
                )
            ),
            key=lambda path: path.relative_to(self.root).as_posix().casefold(),
        )
        relative_paths = {
            video_path: video_path.relative_to(self.root)
            for video_path in videos
        }
        provisional_outputs = {
            relative_path: self._output_paths_for(relative_path)
            for relative_path in relative_paths.values()
        }
        output_counts: dict[str, int] = {}
        for outputs in provisional_outputs.values():
            collision_key = str(Path(outputs["4x3"])).casefold()
            output_counts[collision_key] = output_counts.get(collision_key, 0) + 1

        tasks: dict[str, CoverTask] = {}
        media_tokens: OrderedDict[str, tuple[Path, float]] = OrderedDict()
        with self._lock:
            previous_tokens = self._media_tokens
            self._media_tokens = media_tokens
            try:
                for video_path in videos:
                    relative_path = relative_paths[video_path]
                    task_id = self._task_id(relative_path)
                    title = match_title(video_path.name, self._title_map)
                    recommendation = recommend_visual_style(title)
                    folder_created_at, folder_modified_at = _path_timestamps(
                        video_path.parent
                    )
                    source_created_at, source_modified_at = _path_timestamps(video_path)
                    previous = self._tasks.get(task_id)
                    task = CoverTask(
                        id=task_id,
                        video_path=str(video_path),
                        relative_path=relative_path.as_posix(),
                        filename=video_path.name,
                        title=title,
                        template_key=recommendation.template_key,
                        palette_key=recommendation.palette_key,
                        folder_created_at=folder_created_at,
                        folder_modified_at=folder_modified_at,
                        source_created_at=source_created_at,
                        source_modified_at=source_modified_at,
                        output_paths=(
                            self._output_paths_for(
                                relative_path,
                                include_source_suffix=True,
                            )
                            if output_counts[
                                str(Path(
                                    provisional_outputs[relative_path]["4x3"]
                                )).casefold()
                            ] > 1
                            else provisional_outputs[relative_path]
                        ),
                    )
                    if previous is not None and previous.video_path == task.video_path:
                        task.status = previous.status
                        task.candidates = previous.candidates
                        task.selected_index = min(
                            previous.selected_index,
                            max(0, len(previous.candidates) - 1),
                        )
                        task.error = previous.error
                        task.title = previous.title
                        task.template_key = previous.template_key
                        task.palette_key = previous.palette_key
                    self._register_media(video_path)
                    for candidate in task.candidates:
                        self._register_media(candidate.path)
                    tasks[task_id] = task
            except Exception:
                self._media_tokens = previous_tokens
                raise
            self._tasks = tasks
        return list(tasks.values())

    def list_tasks(self) -> list[CoverTask]:
        """返回按相对路径排序的任务快照。"""

        with self._lock:
            return sorted(self._tasks.values(), key=lambda task: task.relative_path.casefold())

    def get_task(self, task_id: str) -> CoverTask:
        """按 ID 获取任务。"""

        with self._lock:
            try:
                return self._tasks[task_id]
            except KeyError as exc:
                raise KeyError(f"封面任务不存在：{task_id}") from exc

    def task_snapshot(self, task_id: str) -> CoverTask:
        """返回与后续编辑隔离的任务快照，供一次预览或导出完整使用。"""

        with self._lock:
            task = self.get_task(task_id)
            return replace(
                task,
                candidates=tuple(task.candidates),
                output_paths=dict(task.output_paths),
            )

    def remove_task(self, task_id: str) -> CoverTask:
        """从当前工作区队列移除任务，不修改对应源视频。"""

        with self._lock:
            try:
                return self._tasks.pop(task_id)
            except KeyError as exc:
                raise KeyError(f"封面任务不存在：{task_id}") from exc

    def update_task(
        self,
        task_id: str,
        *,
        title: str | None = None,
        template_key: str | None = None,
        palette_key: str | None = None,
    ) -> CoverTask:
        """更新可编辑标题、模板和调色板。"""

        with self._lock:
            task = self.get_task(task_id)
            cleaned_title = task.title
            if title is not None:
                cleaned_title = title.strip()
                if not cleaned_title:
                    raise ValueError("投稿标题不能为空")
            cleaned_template_key = task.template_key
            if template_key is not None:
                get_template(template_key)
                cleaned_template_key = template_key
            cleaned_palette_key = task.palette_key
            if palette_key is not None:
                get_palette(palette_key)
                cleaned_palette_key = palette_key
            task.title = cleaned_title
            task.template_key = cleaned_template_key
            task.palette_key = cleaned_palette_key
            return task

    def generate_candidates(
        self,
        task_id: str,
        *,
        count: int = 12,
        force: bool = False,
    ) -> CoverTask:
        """为任务提取候选帧，并记录执行状态。"""

        with self._lock:
            task = self.get_task(task_id)
            if task.status == "extracting":
                raise RuntimeError("该任务正在提取候选帧，请等待完成")
            task.status = "extracting"
            task.error = None
            source_path = task.video_path
        try:
            candidates = tuple(
                extract_candidate_frames(
                    source_path,
                    cache_dir=self.cache_dir,
                    count=count,
                    force=force,
                )
            )
            if not candidates:
                raise RuntimeError("没有生成任何候选帧")
        except Exception as exc:
            with self._lock:
                current = self._tasks.get(task_id)
                if current is not None and current.video_path == source_path:
                    current.status = "error"
                    current.error = " ".join(str(exc).split())[:500]
            raise

        with self._lock:
            current = self._tasks.get(task_id)
            if current is None:
                raise KeyError(f"封面任务在候选帧提取期间被移除：{task_id}")
            if current.video_path != source_path:
                raise RuntimeError("封面任务源视频在候选帧提取期间发生变化")
            for candidate in candidates:
                self._register_media(candidate.path)
            current.candidates = candidates
            current.selected_index = 0
            current.status = "ready"
            current.error = None
            return current

    def select_candidate(self, task_id: str, media_token: str) -> CoverTask:
        """把属于当前任务的候选帧设为选中帧。"""

        selected_path = self.resolve_media(media_token)
        with self._lock:
            task = self.get_task(task_id)
            for index, candidate in enumerate(task.candidates):
                if Path(candidate.path).resolve() == selected_path:
                    task.selected_index = index
                    return task
        raise ValueError("所选媒体不是该任务的候选帧")

    def selected_candidate(self, task_id: str) -> FrameCandidate:
        """返回任务当前选中的候选帧。"""

        with self._lock:
            task = self.get_task(task_id)
            if not task.candidates:
                raise ValueError("该任务尚未生成候选帧")
            return task.candidates[task.selected_index]

    def resolve_media(self, token: str) -> Path:
        """将不透明媒体令牌解析为已登记文件，拒绝任意路径。"""

        with self._lock:
            now = time.time()
            self._prune_media_tokens_locked(now)
            try:
                path, _ = self._media_tokens[token]
            except KeyError as exc:
                raise KeyError("媒体令牌无效或已过期") from exc
            self._media_tokens[token] = (path, now)
            self._media_tokens.move_to_end(token)
        if not path.is_file():
            with self._lock:
                self._media_tokens.pop(token, None)
            raise FileNotFoundError(f"媒体文件已不存在：{path}")
        return path

    def media_token(self, path: str | Path) -> str:
        """为服务内部生成的媒体文件签发不透明令牌。"""

        with self._lock:
            return self._register_media(path)

    def cleanup_preview_cache(
        self,
        task_id: str,
        *,
        keep: int = PREVIEW_HISTORY_PER_TASK,
        preserve_paths: tuple[str | Path, ...] = (),
    ) -> list[Path]:
        """只保留任务最近若干份预览，同时撤销已删除文件的媒体令牌。"""

        if keep < 1:
            raise ValueError("预览保留数量必须至少为 1")
        preview_dir = (self.cache_dir / "previews" / task_id).resolve()
        if not preview_dir.is_dir():
            return []
        preserve = {
            Path(path).expanduser().resolve()
            for path in preserve_paths
            if path
        }
        covers = sorted(
            (
                path.resolve()
                for path in preview_dir.glob("*.jpg")
                if not path.name.endswith("-background.jpg")
            ),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        retained = set(covers[:keep]) | preserve
        removed = []
        for cover in covers:
            if cover in retained:
                continue
            related = (
                cover,
                cover.with_name(f"{cover.stem}-background.jpg"),
            )
            for path in related:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    continue
                removed.append(path)
        if removed:
            removed_set = {path.resolve() for path in removed}
            with self._lock:
                for token, (path, _) in tuple(self._media_tokens.items()):
                    if path in removed_set:
                        self._media_tokens.pop(token, None)
        return removed

    def task_payload(self, task_id: str) -> dict[str, Any]:
        """生成不包含本地绝对媒体路径的 API 数据。"""

        with self._lock:
            task = self.get_task(task_id)
            video_token = self._register_media(task.video_path)
            candidates = []
            for index, candidate in enumerate(task.candidates):
                candidates.append(
                    {
                        "token": self._register_media(candidate.path),
                        "timestamp": candidate.timestamp,
                        "score": candidate.score,
                        "metrics": candidate.metrics.to_dict(),
                        "cached": candidate.cached,
                        "selected": index == task.selected_index,
                    }
                )
            return {
                "id": task.id,
                "filename": task.filename,
                "relative_path": task.relative_path,
                "title": task.title,
                "folder_created_at": task.folder_created_at,
                "folder_modified_at": task.folder_modified_at,
                "source_created_at": task.source_created_at,
                "source_modified_at": task.source_modified_at,
                "template_key": task.template_key,
                "palette_key": task.palette_key,
                "status": task.status,
                "error": task.error,
                "video_token": video_token,
                "candidates": candidates,
                "output_files": {
                    key: Path(path).name for key, path in task.output_paths.items()
                },
            }

    def all_payloads(self) -> list[dict[str, Any]]:
        """返回全部任务的 API 数据。"""

        with self._lock:
            return [self.task_payload(task.id) for task in self.list_tasks()]
