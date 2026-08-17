"""统一后台任务预约、生命周期与恢复策略。

``TaskStore`` 是任务数据的唯一持久化真相源。本模块只在内存中保留无法
持久化的协作取消 ``Event``，不维护第二份任务字典。预约时的活动任务扫描
和新任务插入始终位于同一个 SQLite 写事务中。
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import secrets
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from autoslice.streamer_profiles import StreamerProfile
from autoslice.task_store import (
    MAX_LIST_LIMIT,
    TaskNotFoundError,
    TaskRecord,
    TaskStore,
    TaskStoreTransaction,
    normalize_task_paths,
)


ACTIVE_TASK_STATUSES = frozenset({"queued", "running"})
DEFAULT_HISTORY_LIMIT = 200
DEFAULT_HISTORY_TTL_SECONDS = 24 * 60 * 60
DEFAULT_LIST_LIMIT = 100

_TASK_TYPE_RE = re.compile(r"^[a-z][a-z0-9_.-]*$")
_TASK_ID_PREFIX_RE = re.compile(r"[^a-zA-Z0-9_.-]+")
_PUBLIC_PROFILE_TEXT_FIELDS = (
    "id",
    "label",
    "canonical_name",
    "report_name",
    "title_prefix",
)
_PROFILE_FINGERPRINT_RE = re.compile(r"^[a-zA-Z0-9_.:-]{8,128}$")
_RECOVERY_MESSAGE = "上次运行已中断；请预约新任务并从持久化检查点重试。"


class TaskRegistryError(RuntimeError):
    """任务注册表基础异常。"""


class TaskLifecycleError(TaskRegistryError):
    """任务生命周期转换不合法。"""

    def __init__(
            self,
            task_id: str,
            current_status: str,
            requested_status: str,
    ) -> None:
        self.task_id = task_id
        self.current_status = current_status
        self.requested_status = requested_status
        super().__init__(
            f"任务 {task_id!r} 禁止从 {current_status!r} 转为 "
            f"{requested_status!r}；重试必须预约新 task_id"
        )


class TaskRegistryCapacityError(TaskRegistryError):
    """活动任务超过可安全扫描的边界，因而拒绝继续预约。"""


class TaskIdentityCollisionError(TaskRegistryError):
    """运行 nonce 重复，生成的 task_id 已存在。"""


def _validate_task_type(task_type: str) -> str:
    if (
            not isinstance(task_type, str)
            or len(task_type) > 64
            or not _TASK_TYPE_RE.fullmatch(task_type)):
        raise ValueError(
            "task_type 必须以小写字母开头，且只包含小写字母、数字、_、.、-"
        )
    return task_type


def _validate_non_negative_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} 必须是有限非负数")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{field} 必须是有限非负数")
    return result


def _normalise_conflict_types(
        task_type: str,
        conflict_types: Sequence[str] | set[str] | frozenset[str] | None,
) -> frozenset[str]:
    if conflict_types is None:
        return frozenset({task_type})
    if isinstance(conflict_types, str):
        values = (conflict_types,)
    else:
        try:
            values = tuple(conflict_types)
        except TypeError as exc:
            raise ValueError("conflict_types 必须是任务类型集合") from exc
    if not values:
        raise ValueError("conflict_types 不能为空")
    return frozenset(_validate_task_type(value) for value in values)


def _task_identity_digest(
        task_type: str,
        source_paths: Sequence[str],
        output_paths: Sequence[str],
) -> str:
    """摘要覆盖任务类型和全部规范化资源，不能退化为 basename。"""

    payload = {
        "output_paths": list(output_paths),
        "source_paths": list(source_paths),
        "task_type": task_type,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


def _task_id_prefix(prefix: str | None, task_type: str) -> str:
    candidate = str(prefix or task_type).strip()
    candidate = _TASK_ID_PREFIX_RE.sub("-", candidate).strip("-._")
    return (candidate or "task")[:40]


def _task_id_for_run(
        *,
        prefix: str,
        identity_digest: str,
        run_nonce: Any,
) -> str:
    nonce = str(run_nonce).strip()
    if not nonce:
        raise ValueError("token_factory/run_nonce 必须生成非空值")
    nonce_digest = hashlib.sha256(nonce.encode("utf-8")).hexdigest()[:16]
    # Web UI 长期按 ``pipeline_`` / ``subtitle_`` 等旧前缀筛选 SSE；
    # 身份摘要与 nonce 保持不变，仅保留原有下划线分隔契约。
    return f"{prefix}_{identity_digest}_{nonce_digest}"


def _profile_public_payload(
        profile: Mapping[str, Any] | StreamerProfile | None,
) -> dict[str, Any]:
    """生成安全且稳定的 profile 快照，主动丢弃密钥和本机路径等字段。"""

    if profile is None:
        return {}
    if isinstance(profile, StreamerProfile):
        source: Mapping[str, Any] = profile.to_public_dict()
        fingerprint = profile.subtitle_review_fingerprint()
    elif isinstance(profile, Mapping):
        source = profile
        fingerprint = source.get("fingerprint")
    else:
        raise TypeError("streamer_profile 必须是 Mapping、StreamerProfile 或 None")

    snapshot: dict[str, Any] = {}
    for field in _PUBLIC_PROFILE_TEXT_FIELDS:
        value = source.get(field)
        if value is None:
            continue
        if not isinstance(value, str):
            raise ValueError(f"streamer_profile.{field} 必须是字符串")
        snapshot[field] = value

    aliases = source.get("aliases")
    if aliases is not None:
        if (
                isinstance(aliases, (str, bytes))
                or not isinstance(aliases, Sequence)
                or any(not isinstance(value, str) for value in aliases)):
            raise ValueError("streamer_profile.aliases 必须是字符串序列")
        snapshot["aliases"] = list(dict.fromkeys(aliases))

    if fingerprint is None:
        encoded = json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        fingerprint = hashlib.sha256(encoded).hexdigest()
    if (
            not isinstance(fingerprint, str)
            or not _PROFILE_FINGERPRINT_RE.fullmatch(fingerprint)):
        raise ValueError("streamer_profile.fingerprint 格式无效")
    snapshot["fingerprint"] = fingerprint
    return copy.deepcopy(snapshot)


def _merge_metadata(
        current: Mapping[str, Any],
        patch: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """递归合并 metadata；``None`` 表示完全不写该字段。"""

    if patch is None:
        return None
    if not isinstance(patch, Mapping):
        raise ValueError("metadata 必须是 Mapping 或 None")
    merged = copy.deepcopy(dict(current))

    def merge_into(target: dict[str, Any], additions: Mapping[str, Any]) -> None:
        for key, value in additions.items():
            if not isinstance(key, str):
                raise ValueError("metadata 的键必须是字符串")
            existing = target.get(key)
            if isinstance(existing, Mapping) and isinstance(value, Mapping):
                nested = copy.deepcopy(dict(existing))
                merge_into(nested, value)
                target[key] = nested
            else:
                target[key] = copy.deepcopy(value)

    merge_into(merged, patch)
    return merged


def _record_snapshot(record: TaskRecord) -> dict[str, Any]:
    """返回独立字典，并明确提供旧 ``task dict`` 所需字段映射。"""

    # 旧 Web 层把预约 metadata 直接平铺在 task 字典顶层。先复制 metadata，
    # 再覆盖核心字段，既保持兼容，也禁止 metadata 冒充 status/task_id。
    payload = copy.deepcopy(record.metadata)
    payload.update(record.to_dict())
    legacy_result = record.result_summary
    if legacy_result is None and record.status in {
            "error", "cancelled", "interrupted"}:
        legacy_result = record.error_summary
    payload["result"] = copy.deepcopy(legacy_result)
    payload["error"] = record.error_summary
    payload["completed_at"] = record.finished_at
    payload["streamer_profile"] = copy.deepcopy(
        record.streamer_profile_snapshot
    )
    return payload


class TaskRegistry:
    """以 :class:`TaskStore` 为唯一真相源的后台任务注册表。"""

    def __init__(
            self,
            store: TaskStore,
            *,
            token_factory: Callable[[], Any] | None = None,
            clock: Callable[[], float] = time.time,
            history_ttl_seconds: float | None = DEFAULT_HISTORY_TTL_SECONDS,
            history_limit: int | None = DEFAULT_HISTORY_LIMIT,
            recover_on_startup: bool = True,
    ) -> None:
        if not isinstance(store, TaskStore):
            raise TypeError("store 必须是 TaskStore")
        if token_factory is not None and not callable(token_factory):
            raise TypeError("token_factory 必须可调用")
        if not callable(clock):
            raise TypeError("clock 必须可调用")
        if history_ttl_seconds is not None:
            _validate_non_negative_number(
                history_ttl_seconds,
                "history_ttl_seconds",
            )
        if (
                history_limit is not None
                and (
                    isinstance(history_limit, bool)
                    or not isinstance(history_limit, int)
                    or history_limit < 0
                )):
            raise ValueError("history_limit 必须是非负整数或 None")

        self.store = store
        self._token_factory = token_factory or (lambda: secrets.token_hex(12))
        self._clock = clock
        self.history_ttl_seconds = history_ttl_seconds
        self.history_limit = history_limit
        self._cancellation_events: dict[str, threading.Event] = {}
        self._event_lock = threading.Lock()
        self.recovered_task_ids: tuple[str, ...] = ()
        if recover_on_startup:
            recovered = self.startup_recovery()
            self.recovered_task_ids = tuple(record.task_id for record in recovered)

    def reserve(
            self,
            task_type: str,
            waiting_progress: str = "等待处理",
            *,
            prefix: str | None = None,
            source_path: str | Path | None = None,
            output_path: str | Path | None = None,
            source_paths: Sequence[str | Path] | str | Path | None = None,
            output_paths: Sequence[str | Path] | str | Path | None = None,
            conflict_types: Sequence[str] | set[str] | frozenset[str] | None = None,
            metadata: Mapping[str, Any] | None = None,
            streamer_profile: Mapping[str, Any] | StreamerProfile | None = None,
            total: int = 100,
            run_nonce: Any | None = None,
    ) -> tuple[str | None, str | None]:
        """原子预约任务，返回 ``(新 task_id, 冲突 task_id)``。

        冲突类型只约束相同源路径；任何活动任务的相同输出路径都会冲突。
        扫描与插入位于同一个 ``BEGIN IMMEDIATE`` 写事务内，因此不同线程或
        ``TaskRegistry`` 实例无法同时占用同一输出。
        """

        task_type = _validate_task_type(task_type)
        active_conflict_types = _normalise_conflict_types(
            task_type,
            conflict_types,
        )
        normalized_sources = normalize_task_paths(source_path, source_paths)
        normalized_outputs = normalize_task_paths(output_path, output_paths)
        source_set = frozenset(normalized_sources)
        output_set = frozenset(normalized_outputs)
        profile_snapshot = _profile_public_payload(streamer_profile)
        if metadata is not None and not isinstance(metadata, Mapping):
            raise ValueError("metadata 必须是 Mapping 或 None")
        identity_digest = _task_identity_digest(
            task_type,
            normalized_sources,
            normalized_outputs,
        )
        id_prefix = _task_id_prefix(prefix, task_type)

        with self.store.transaction() as transaction:
            for active in self._active_tasks(transaction):
                same_source = (
                    active.task_type in active_conflict_types
                    and bool(source_set.intersection(active.source_paths))
                )
                same_output = bool(output_set.intersection(active.output_paths))
                if same_source or same_output:
                    return None, active.task_id

            task_id = self._new_task_id(
                transaction,
                prefix=id_prefix,
                identity_digest=identity_digest,
                run_nonce=run_nonce,
            )
            transaction.create_task(
                task_id,
                task_type,
                source_paths=normalized_sources,
                output_paths=normalized_outputs,
                status="queued",
                progress=waiting_progress,
                step=0,
                total=total,
                metadata=copy.deepcopy(dict(metadata or {})),
                streamer_profile_snapshot=profile_snapshot,
            )
        return task_id, None

    @staticmethod
    def _active_tasks(
            transaction: TaskStoreTransaction,
    ) -> tuple[TaskRecord, ...]:
        active: list[TaskRecord] = []
        for status in sorted(ACTIVE_TASK_STATUSES):
            records = transaction.list_tasks(
                limit=MAX_LIST_LIMIT,
                order="created_asc",
                status=status,
            )
            if len(records) == MAX_LIST_LIMIT:
                raise TaskRegistryCapacityError(
                    f"{status} 活动任务达到安全扫描上限 {MAX_LIST_LIMIT}，拒绝预约"
                )
            active.extend(records)
        return tuple(active)

    def _new_task_id(
            self,
            transaction: TaskStoreTransaction,
            *,
            prefix: str,
            identity_digest: str,
            run_nonce: Any | None,
    ) -> str:
        if run_nonce is not None:
            task_id = _task_id_for_run(
                prefix=prefix,
                identity_digest=identity_digest,
                run_nonce=run_nonce,
            )
            if transaction.get_task(task_id) is not None:
                raise TaskIdentityCollisionError(
                    "显式 run_nonce 已被使用；真正重跑必须提供新 nonce"
                )
            return task_id

        for _ in range(16):
            task_id = _task_id_for_run(
                prefix=prefix,
                identity_digest=identity_digest,
                run_nonce=self._token_factory(),
            )
            if transaction.get_task(task_id) is None:
                return task_id
        raise TaskIdentityCollisionError(
            "token_factory 连续生成已使用的 nonce，无法创建新 task_id"
        )

    def get(self, task_id: str) -> TaskRecord | None:
        """读取一条独立的不可变任务快照。"""

        return self.store.get_task(task_id)

    def list(
            self,
            *,
            limit: int = DEFAULT_LIST_LIMIT,
            order: str = "updated_desc",
            status: str | None = None,
            task_type: str | None = None,
    ) -> list[TaskRecord]:
        """返回有限任务列表；上限由 ``TaskStore`` 统一验证。"""

        return self.store.list_tasks(
            limit=limit,
            order=order,
            status=status,
            task_type=task_type,
        )

    def snapshot(
            self,
            task_id: str | None = None,
            *,
            limit: int = DEFAULT_LIST_LIMIT,
            order: str = "updated_desc",
            status: str | None = None,
            task_type: str | None = None,
    ) -> dict[str, Any] | dict[str, dict[str, Any]] | None:
        """返回与外部对象隔离的字典快照。

        提供 ``task_id`` 时返回单项；省略时返回有限的 ``task_id -> task``
        映射，便于 Action 7.3 构造旧接口兼容层。
        """

        if task_id is not None:
            record = self.get(task_id)
            return _record_snapshot(record) if record is not None else None
        return {
            record.task_id: _record_snapshot(record)
            for record in self.list(
                limit=limit,
                order=order,
                status=status,
                task_type=task_type,
            )
        }

    def mark_running(
            self,
            task_id: str,
            *,
            progress: str | None = None,
            message: str | None = None,
            metadata: Mapping[str, Any] | None = None,
    ) -> TaskRecord:
        """只允许把 ``queued`` 显式转换为 ``running``。"""

        changes: dict[str, Any] = {"status": "running"}
        if progress is not None:
            changes["progress"] = progress
        if message is not None:
            changes["message"] = message
        return self._transition(
            task_id,
            target_status="running",
            allowed_statuses=frozenset({"queued"}),
            changes=changes,
            metadata=metadata,
        )

    def update_progress(
            self,
            task_id: str,
            *,
            progress: str | None = None,
            message: str | None = None,
            step: int | None = None,
            total: int | None = None,
            metadata: Mapping[str, Any] | None = None,
    ) -> TaskRecord:
        """更新活动任务进度；终态任务拒绝旧线程回写。"""

        changes: dict[str, Any] = {}
        if progress is not None:
            changes["progress"] = progress
        if message is not None:
            changes["message"] = message
        if step is not None:
            changes["step"] = step
        if total is not None:
            changes["total"] = total
        return self._transition(
            task_id,
            target_status=None,
            allowed_statuses=ACTIVE_TASK_STATUSES,
            changes=changes,
            metadata=metadata,
        )

    def complete(
            self,
            task_id: str,
            result: Any = None,
            *,
            progress: str = "完成",
            message: str | None = None,
            metadata: Mapping[str, Any] | None = None,
    ) -> TaskRecord:
        """把运行任务完成为 ``done``，``result`` 映射到 result_summary。"""

        changes: dict[str, Any] = {
            "status": "done",
            "progress": progress,
            "result_summary": copy.deepcopy(result),
            "error_summary": None,
        }
        if message is not None:
            changes["message"] = message
        return self._transition(
            task_id,
            target_status="done",
            allowed_statuses=frozenset({"running"}),
            changes=changes,
            metadata=metadata,
            finish_step=True,
        )

    def fail(
            self,
            task_id: str,
            error: str | BaseException,
            *,
            progress: str = "失败",
            message: str | None = None,
            metadata: Mapping[str, Any] | None = None,
    ) -> TaskRecord:
        """把活动任务失败为 ``error``，错误文本映射到 error_summary。"""

        error_summary = str(error)
        if not error_summary:
            error_summary = type(error).__name__ if isinstance(
                error,
                BaseException,
            ) else "任务失败"
        changes: dict[str, Any] = {
            "status": "error",
            "progress": progress,
            "result_summary": None,
            "error_summary": error_summary,
        }
        if message is not None:
            changes["message"] = message
        return self._transition(
            task_id,
            target_status="error",
            allowed_statuses=ACTIVE_TASK_STATUSES,
            changes=changes,
            metadata=metadata,
        )

    def cancel(
            self,
            task_id: str,
            *,
            reason: str = "用户已取消任务",
            metadata: Mapping[str, Any] | None = None,
    ) -> TaskRecord:
        """持久化 ``cancelled``，并通知已取得 Event 的后台工作线程。"""

        current = self.get(task_id)
        if current is None:
            raise TaskNotFoundError(f"任务不存在：{task_id}")
        if current.status == "cancelled":
            event = self.cancellation_event(task_id)
            event.set()
            return current
        record = self._transition(
            task_id,
            target_status="cancelled",
            allowed_statuses=ACTIVE_TASK_STATUSES,
            changes={
                "status": "cancelled",
                "progress": "已取消",
                "message": reason,
                "result_summary": None,
                "error_summary": reason,
            },
            metadata=metadata,
        )
        self.cancellation_event(task_id).set()
        return record

    def cancellation_event(self, task_id: str) -> threading.Event:
        """返回同一任务共享的协作取消 Event；任务数据仍只读取数据库。"""

        record = self.get(task_id)
        if record is None:
            raise TaskNotFoundError(f"任务不存在：{task_id}")
        with self._event_lock:
            event = self._cancellation_events.get(task_id)
            if event is None:
                event = threading.Event()
                self._cancellation_events[task_id] = event
            if record.status == "cancelled":
                event.set()
            return event

    def cancellation_requested(self, task_id: str) -> bool:
        """同时检查协作 Event 和持久状态，跨重启仍识别取消。"""

        with self._event_lock:
            event = self._cancellation_events.get(task_id)
            if event is not None and event.is_set():
                return True
        record = self.get(task_id)
        if record is None:
            raise TaskNotFoundError(f"任务不存在：{task_id}")
        return record.status == "cancelled"

    def forget_cancellation_events(
            self,
            task_ids: Sequence[str] | None = None,
    ) -> None:
        """丢弃已删除任务的运行期 Event，不触碰持久任务数据。"""

        with self._event_lock:
            if task_ids is None:
                self._cancellation_events.clear()
                return
            for task_id in task_ids:
                self._cancellation_events.pop(str(task_id), None)

    def _transition(
            self,
            task_id: str,
            *,
            target_status: str | None,
            allowed_statuses: frozenset[str],
            changes: Mapping[str, Any],
            metadata: Mapping[str, Any] | None,
            finish_step: bool = False,
    ) -> TaskRecord:
        with self.store.transaction() as transaction:
            current = transaction.get_task(task_id)
            if current is None:
                raise TaskNotFoundError(f"任务不存在：{task_id}")
            requested = target_status or current.status
            if current.status not in allowed_statuses:
                raise TaskLifecycleError(
                    current.task_id,
                    current.status,
                    requested,
                )
            prepared = dict(changes)
            if finish_step:
                prepared["step"] = current.total
            merged_metadata = _merge_metadata(current.metadata, metadata)
            if merged_metadata is not None:
                prepared["metadata"] = merged_metadata
            if not prepared:
                raise ValueError("进度更新至少需要一个字段")
            return transaction.update_task(task_id, **prepared)

    def cleanup_history(
            self,
            *,
            ttl_seconds: float | None = None,
            keep_latest: int | None = None,
            now: float | None = None,
    ) -> int:
        """通过 ``TaskStore.cleanup_tasks`` 清理终态；活动任务不会被删除。"""

        ttl = self.history_ttl_seconds if ttl_seconds is None else ttl_seconds
        limit = self.history_limit if keep_latest is None else keep_latest
        if ttl is None and limit is None:
            raise ValueError("cleanup_history 必须配置 ttl_seconds 或 keep_latest")
        finished_before = None
        if ttl is not None:
            ttl_value = _validate_non_negative_number(ttl, "ttl_seconds")
            current_time = self._clock() if now is None else now
            current_time = _validate_non_negative_number(current_time, "now")
            finished_before = max(0.0, current_time - ttl_value)
        deleted = self.store.cleanup_tasks(
            finished_before=finished_before,
            keep_latest=limit,
        )
        self._discard_deleted_events()
        return deleted

    def _discard_deleted_events(self) -> None:
        with self._event_lock:
            task_ids = tuple(self._cancellation_events)
        stale = {
            task_id
            for task_id in task_ids
            if self.store.get_task(task_id) is None
        }
        if not stale:
            return
        with self._event_lock:
            for task_id in stale:
                self._cancellation_events.pop(task_id, None)

    def startup_recovery(self) -> list[TaskRecord]:
        """幂等地把上次遗留的 queued/running 任务转为 interrupted。"""

        recovered: list[TaskRecord] = []
        with self.store.transaction() as transaction:
            for current in self._active_tasks(transaction):
                recovery = {
                    "action": "retry_from_checkpoint",
                    "checkpoint": {
                        "progress": current.progress,
                        "step": current.step,
                        "total": current.total,
                    },
                    "next_action": "使用原资源与 profile 预约新 task_id 后从检查点重试",
                    "previous_message": current.message,
                    "retryable": True,
                }
                if current.result_summary is not None:
                    recovery["previous_result_summary"] = copy.deepcopy(
                        current.result_summary
                    )
                metadata = _merge_metadata(
                    current.metadata,
                    {"startup_recovery": recovery},
                )
                updated = transaction.update_task(
                    current.task_id,
                    status="interrupted",
                    message=_RECOVERY_MESSAGE,
                    result_summary=recovery,
                    error_summary="后台进程退出时任务仍处于活动状态",
                    metadata=metadata,
                )
                recovered.append(updated)
        return recovered


__all__ = [
    "ACTIVE_TASK_STATUSES",
    "DEFAULT_HISTORY_LIMIT",
    "DEFAULT_HISTORY_TTL_SECONDS",
    "DEFAULT_LIST_LIMIT",
    "TaskIdentityCollisionError",
    "TaskLifecycleError",
    "TaskRegistry",
    "TaskRegistryCapacityError",
    "TaskRegistryError",
]
