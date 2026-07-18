"""切片目录扫描、标题匹配和候选帧任务管理。"""

from __future__ import annotations

import hashlib
import os
import secrets
import threading
from dataclasses import dataclass, field
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


def _is_ignored_video(path: Path, root: Path) -> bool:
    """判断视频是否位于默认不参与切片扫描的子目录。"""

    relative = path.relative_to(root)
    return any(
        part.casefold() in DEFAULT_IGNORED_DIRECTORY_NAMES
        for part in relative.parts[:-1]
    )


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
        self._media_tokens: dict[str, Path] = {}
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
            self._media_tokens[token] = source
            return token

    def _output_paths_for(self, relative_path: Path) -> dict[str, str]:
        output_relative = relative_path
        if self.output_dir == DEFAULT_OUTPUT_DIR.resolve():
            source_path = (self.root / relative_path).resolve()
            try:
                output_relative = source_path.relative_to(DEFAULT_OUTPUT_DIR.parent.resolve())
            except ValueError:
                output_relative = Path(self.root.name) / relative_path
        target_dir = self.output_dir / output_relative.parent
        stem = relative_path.stem
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

        tasks: dict[str, CoverTask] = {}
        media_tokens: dict[str, Path] = {}
        with self._lock:
            previous_tokens = self._media_tokens
            self._media_tokens = media_tokens
            try:
                for video_path in videos:
                    relative_path = video_path.relative_to(self.root)
                    task_id = self._task_id(relative_path)
                    title = match_title(video_path.name, self._title_map)
                    recommendation = recommend_visual_style(title)
                    previous = self._tasks.get(task_id)
                    task = CoverTask(
                        id=task_id,
                        video_path=str(video_path),
                        relative_path=relative_path.as_posix(),
                        filename=video_path.name,
                        title=title,
                        template_key=recommendation.template_key,
                        palette_key=recommendation.palette_key,
                        output_paths=self._output_paths_for(relative_path),
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
            if title is not None:
                cleaned_title = title.strip()
                if not cleaned_title:
                    raise ValueError("投稿标题不能为空")
                task.title = cleaned_title
            if template_key is not None:
                get_template(template_key)
                task.template_key = template_key
            if palette_key is not None:
                get_palette(palette_key)
                task.palette_key = palette_key
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
            try:
                path = self._media_tokens[token]
            except KeyError as exc:
                raise KeyError("媒体令牌无效或已过期") from exc
        if not path.is_file():
            raise FileNotFoundError(f"媒体文件已不存在：{path}")
        return path

    def media_token(self, path: str | Path) -> str:
        """为服务内部生成的媒体文件签发不透明令牌。"""

        with self._lock:
            return self._register_media(path)

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
