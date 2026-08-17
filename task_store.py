"""线程安全的 AutoSlice SQLite 任务历史存储。"""

from __future__ import annotations

import copy
import json
import math
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence


TASK_STORE_SCHEMA_VERSION = 1
DEFAULT_TASK_STATE_DIR = Path(__file__).resolve().parent / ".autoslice-state"
DEFAULT_TASK_DATABASE_PATH = DEFAULT_TASK_STATE_DIR / "tasks.sqlite3"

VALID_TASK_STATUSES = frozenset({
    "queued",
    "running",
    "interrupted",
    "done",
    "error",
    "cancelled",
})
TERMINAL_TASK_STATUSES = frozenset({
    "interrupted",
    "done",
    "error",
    "cancelled",
})
MAX_LIST_LIMIT = 1000

_MAX_TASK_ID_LENGTH = 200
_MAX_TASK_TYPE_LENGTH = 64
_MAX_PROGRESS_LENGTH = 2000
_MAX_MESSAGE_LENGTH = 4000
_MAX_ERROR_SUMMARY_LENGTH = 4000
_MAX_RESULT_SUMMARY_BYTES = 64 * 1024
_MAX_TASK_METADATA_BYTES = 256 * 1024
_MAX_PROFILE_SNAPSHOT_BYTES = 256 * 1024
_TASK_TYPE_RE = re.compile(r"^[a-z][a-z0-9_.-]*$")
_TASK_ID_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f/\\]")
_SENSITIVE_TEXT_RE = re.compile(
    r"(?i)(?:api[ _-]?key|access[ _-]?token|refresh[ _-]?token|"
    r"token|authorization|cookie|password|private[ _-]?key|client[ _-]?secret)"
    r"\s*[:=]"
)
_PRIVATE_KEY_MARKER_RE = re.compile(
    r"-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----",
    re.IGNORECASE,
)
_SENSITIVE_FIELD_NAMES = frozenset({
    "apikey",
    "apitoken",
    "accesstoken",
    "refreshtoken",
    "token",
    "cookie",
    "cookies",
    "authorization",
    "password",
    "passwd",
    "privatekey",
    "secret",
    "clientsecret",
})
_SORT_ORDERS = {
    "updated_desc": "updated_at DESC, task_id ASC",
    "updated_asc": "updated_at ASC, task_id ASC",
    "created_desc": "created_at DESC, task_id ASC",
    "created_asc": "created_at ASC, task_id ASC",
    "finished_desc": "finished_at DESC, task_id ASC",
    "finished_asc": "finished_at ASC, task_id ASC",
}
_MIGRATION_1 = (
    """
    CREATE TABLE tasks (
        task_id TEXT PRIMARY KEY,
        task_type TEXT NOT NULL,
        source_path TEXT,
        output_path TEXT,
        source_paths TEXT NOT NULL DEFAULT '[]',
        output_paths TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL CHECK (
            status IN (
                'queued', 'running', 'interrupted',
                'done', 'error', 'cancelled'
            )
        ),
        progress TEXT NOT NULL DEFAULT '',
        message TEXT NOT NULL DEFAULT '',
        step INTEGER NOT NULL DEFAULT 0 CHECK (step >= 0),
        total INTEGER NOT NULL DEFAULT 100 CHECK (total >= 0),
        result_summary TEXT,
        error_summary TEXT,
        metadata TEXT NOT NULL DEFAULT '{}',
        streamer_profile_snapshot TEXT NOT NULL DEFAULT '{}',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        finished_at REAL,
        CHECK (step <= total),
        CHECK (finished_at IS NULL OR finished_at >= created_at),
        CHECK (
            (status IN (
                'interrupted', 'done', 'error', 'cancelled'
            ) AND finished_at IS NOT NULL)
            OR (status IN ('queued', 'running') AND finished_at IS NULL)
        )
    )
    """,
    "CREATE INDEX tasks_updated_idx ON tasks(updated_at DESC, task_id)",
    "CREATE INDEX tasks_status_updated_idx ON tasks(status, updated_at DESC)",
    "CREATE INDEX tasks_source_status_idx ON tasks(source_path, status)",
    "CREATE INDEX tasks_output_status_idx ON tasks(output_path, status)",
)
_MIGRATIONS = {1: _MIGRATION_1}
_EXPECTED_COLUMNS = frozenset({
    "task_id",
    "task_type",
    "source_path",
    "output_path",
    "source_paths",
    "output_paths",
    "status",
    "progress",
    "message",
    "step",
    "total",
    "result_summary",
    "error_summary",
    "metadata",
    "streamer_profile_snapshot",
    "created_at",
    "updated_at",
    "finished_at",
})


class TaskStoreError(RuntimeError):
    """任务存储基础异常。"""


class TaskStoreSchemaError(TaskStoreError):
    """数据库 schema 无法由当前代码安全读取。"""


class TaskStoreCorruptionError(TaskStoreError):
    """数据库损坏，且没有被静默覆盖。"""


class TaskNotFoundError(TaskStoreError, KeyError):
    """待更新的任务不存在。"""


class TaskConflictError(TaskStoreError):
    """同一 task_id 已绑定到另一项任务。"""


class SensitiveTaskDataError(ValueError):
    """待持久化内容疑似包含凭据。"""


class _DetectedCorruption(Exception):
    pass


@dataclass(frozen=True)
class TaskStoreRecovery:
    """一次显式隔离损坏数据库的审计信息。"""

    original_path: Path
    quarantine_path: Path
    audit_path: Path
    occurred_at: float
    reason: str
    quarantined_sidecars: tuple[Path, ...]


@dataclass(frozen=True)
class TaskRecord:
    """任务表的一条不可变读取快照。"""

    task_id: str
    task_type: str
    source_path: str | None
    output_path: str | None
    source_paths: tuple[str, ...]
    output_paths: tuple[str, ...]
    status: str
    progress: str
    message: str
    step: int
    total: int
    result_summary: Any
    error_summary: str | None
    metadata: dict[str, Any]
    streamer_profile_snapshot: dict[str, Any]
    created_at: float
    updated_at: float
    finished_at: float | None

    @property
    def input_path(self) -> str | None:
        """兼容把主 source path 称为 input path 的调用方。"""

        return self.source_path

    def to_dict(self) -> dict[str, Any]:
        """返回适合后续 TaskRegistry 序列化的独立字典。"""

        payload = {
            field: getattr(self, field)
            for field in self.__dataclass_fields__
        }
        return copy.deepcopy(payload)


def normalize_task_path(path: str | os.PathLike[str] | None) -> str | None:
    """把任务路径规范为绝对、去冗余并按平台折叠大小写的形式。"""

    if path is None:
        return None
    try:
        value = os.fspath(path)
    except TypeError as exc:
        raise ValueError("任务路径必须是字符串、PathLike 或 None") from exc
    if not isinstance(value, str) or not value.strip():
        raise ValueError("任务路径不能为空")
    if "\x00" in value:
        raise ValueError("任务路径不能包含 NUL 字符")
    expanded = os.path.expandvars(os.path.expanduser(value))
    return os.path.normcase(os.path.abspath(os.path.normpath(expanded)))


def normalize_task_paths(
        primary_path: str | os.PathLike[str] | None,
        paths: Sequence[str | os.PathLike[str]] | str | os.PathLike[str] | None,
) -> tuple[str, ...]:
    """规范并去重任务资源路径，同时保证主路径位于第一项。"""

    raw_paths: list[str | os.PathLike[str]] = []
    if primary_path is not None:
        raw_paths.append(primary_path)
    if paths is not None:
        if isinstance(paths, (str, os.PathLike)):
            raw_paths.append(paths)
        else:
            try:
                raw_paths.extend(paths)
            except TypeError as exc:
                raise ValueError("任务路径集合必须是可迭代路径序列") from exc
    normalized: list[str] = []
    seen: set[str] = set()
    for path in raw_paths:
        value = normalize_task_path(path)
        if value is None or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return tuple(normalized)


def _encode_path_tuple(paths: Sequence[str]) -> str:
    return json.dumps(
        list(paths),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _decode_path_tuple(value: str, field: str) -> tuple[str, ...]:
    decoded = _decode_json(value, field, default=[])
    if not isinstance(decoded, list) or any(
            not isinstance(item, str) or not item
            for item in decoded):
        raise TaskStoreSchemaError(f"数据库字段 {field} 必须是非空路径字符串数组")
    if len(decoded) != len(set(decoded)):
        raise TaskStoreSchemaError(f"数据库字段 {field} 不能包含重复路径")
    return tuple(decoded)


def _validate_task_id(task_id: str) -> str:
    if not isinstance(task_id, str) or not task_id.strip():
        raise ValueError("task_id 必须是非空字符串")
    if task_id != task_id.strip():
        raise ValueError("task_id 首尾不能包含空白")
    if len(task_id) > _MAX_TASK_ID_LENGTH:
        raise ValueError(f"task_id 不能超过 {_MAX_TASK_ID_LENGTH} 个字符")
    if _TASK_ID_CONTROL_RE.search(task_id):
        raise ValueError("task_id 不能包含路径分隔符或控制字符")
    return task_id


def _validate_task_type(task_type: str) -> str:
    if (
            not isinstance(task_type, str)
            or not task_type
            or len(task_type) > _MAX_TASK_TYPE_LENGTH
            or not _TASK_TYPE_RE.fullmatch(task_type)):
        raise ValueError(
            "task_type 必须以小写字母开头，且只包含小写字母、数字、_、.、-"
        )
    return task_type


def _validate_status(status: str) -> str:
    if not isinstance(status, str) or status not in VALID_TASK_STATUSES:
        allowed = ", ".join(sorted(VALID_TASK_STATUSES))
        raise ValueError(f"非法任务状态 {status!r}；允许值：{allowed}")
    return status


def _validate_counter(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} 必须是非负整数")
    return value


def _validate_timestamp(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} 必须是有限的非负时间戳")
    timestamp = float(value)
    if not math.isfinite(timestamp) or timestamp < 0:
        raise ValueError(f"{field} 必须是有限的非负时间戳")
    return timestamp


def _validate_safe_text(
        value: Any,
        field: str,
        *,
        maximum: int,
        optional: bool = False,
) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是字符串" + ("或 None" if optional else ""))
    if len(value) > maximum:
        raise ValueError(f"{field} 不能超过 {maximum} 个字符")
    if "\x00" in value:
        raise ValueError(f"{field} 不能包含 NUL 字符")
    if _SENSITIVE_TEXT_RE.search(value) or _PRIVATE_KEY_MARKER_RE.search(value):
        raise SensitiveTaskDataError(f"{field} 疑似包含凭据，拒绝写入任务历史")
    return value


def _normalised_sensitive_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def _check_json_secrets(value: Any, field: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{field} 的对象键必须是字符串")
            if _normalised_sensitive_key(key) in _SENSITIVE_FIELD_NAMES:
                raise SensitiveTaskDataError(
                    f"{field} 包含敏感字段 {key!r}，拒绝写入任务历史"
                )
            _check_json_secrets(item, field)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _check_json_secrets(item, field)
    elif isinstance(value, str):
        _validate_safe_text(value, field, maximum=max(len(value), 1))


def _encode_json(value: Any, field: str, *, maximum_bytes: int) -> str:
    _check_json_secrets(value, field)
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是可序列化的 JSON 数据") from exc
    if len(encoded.encode("utf-8")) > maximum_bytes:
        raise ValueError(f"{field} 序列化后不能超过 {maximum_bytes} 字节")
    return encoded


def _decode_json(value: str | None, field: str, *, default: Any) -> Any:
    if value is None:
        return copy.deepcopy(default)
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise TaskStoreSchemaError(f"数据库字段 {field} 不是有效 JSON") from exc


def _is_sqlite_corruption(error: sqlite3.DatabaseError) -> bool:
    error_code = getattr(error, "sqlite_errorcode", None)
    if isinstance(error_code, int):
        primary_code = error_code & 0xFF
        return primary_code in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}
    text = str(error).lower()
    return "not a database" in text or "database disk image is malformed" in text


class TaskStore:
    """使用每次操作独立连接和 SQLite WAL 的线程安全任务存储。

    ``TaskStore`` 实例不在线程间共享连接。每个公开操作都会开启显式事务；
    写事务使用 ``BEGIN IMMEDIATE``，并通过 ``busy_timeout`` 串行化并发写入。
    """

    def __init__(
            self,
            database_path: str | os.PathLike[str] | None = None,
            *,
            corruption_policy: str = "raise",
            busy_timeout: float = 10.0,
            clock: Callable[[], float] = time.time,
    ) -> None:
        if corruption_policy not in {"raise", "quarantine"}:
            raise ValueError("corruption_policy 只能是 'raise' 或 'quarantine'")
        if (
                isinstance(busy_timeout, bool)
                or not isinstance(busy_timeout, (int, float))
                or not math.isfinite(float(busy_timeout))
                or busy_timeout <= 0):
            raise ValueError("busy_timeout 必须是有限正数")
        candidate = Path(database_path or DEFAULT_TASK_DATABASE_PATH).expanduser()
        if str(candidate) == ":memory:":
            raise ValueError("TaskStore 不支持 :memory:；测试请使用 TemporaryDirectory")
        self.database_path = candidate.resolve()
        self.corruption_policy = corruption_policy
        self.busy_timeout = float(busy_timeout)
        self._clock = clock
        self.last_recovery: TaskStoreRecovery | None = None
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialise()

    @property
    def schema_version(self) -> int:
        """返回数据库当前的 ``PRAGMA user_version``。"""

        with self._read_transaction() as connection:
            return int(connection.execute("PRAGMA user_version").fetchone()[0])

    def migrate(self) -> int:
        """显式执行所有待应用 migration，并返回最终 schema version。"""

        try:
            return self._migrate_once()
        except _DetectedCorruption as exc:
            raise TaskStoreCorruptionError(
                f"任务数据库损坏，未覆盖原文件：{self.database_path}"
            ) from exc

    def create_task(
            self,
            task_id: str,
            task_type: str,
            *,
            source_path: str | os.PathLike[str] | None = None,
            output_path: str | os.PathLike[str] | None = None,
            source_paths: Sequence[str | os.PathLike[str]] | None = None,
            output_paths: Sequence[str | os.PathLike[str]] | None = None,
            status: str = "queued",
            progress: str = "",
            message: str = "",
            step: int = 0,
            total: int = 100,
            result_summary: Any = None,
            error_summary: str | None = None,
            metadata: Mapping[str, Any] | None = None,
            streamer_profile_snapshot: Mapping[str, Any] | None = None,
            created_at: float | None = None,
            finished_at: float | None = None,
    ) -> TaskRecord:
        """幂等创建任务；相同 ID 的身份字段不一致时明确报冲突。"""

        with self.transaction() as transaction:
            return transaction.create_task(
                task_id,
                task_type,
                source_path=source_path,
                output_path=output_path,
                source_paths=source_paths,
                output_paths=output_paths,
                status=status,
                progress=progress,
                message=message,
                step=step,
                total=total,
                result_summary=result_summary,
                error_summary=error_summary,
                metadata=metadata,
                streamer_profile_snapshot=streamer_profile_snapshot,
                created_at=created_at,
                finished_at=finished_at,
            )

    def update_task(self, task_id: str, **changes: Any) -> TaskRecord:
        """只更新显式提供的可变字段，不抹掉其他任务信息。"""

        with self.transaction() as transaction:
            return transaction.update_task(task_id, **changes)

    def get_task(self, task_id: str) -> TaskRecord | None:
        """按 ID 读取任务；合法但不存在的 ID 返回 ``None``。"""

        task_id = _validate_task_id(task_id)
        with self._read_transaction() as connection:
            return self._select_task(connection, task_id)

    def list_tasks(
            self,
            *,
            limit: int = 100,
            order: str = "updated_desc",
            status: str | None = None,
            task_type: str | None = None,
    ) -> list[TaskRecord]:
        """按白名单顺序返回有限任务列表。"""

        with self._read_transaction() as connection:
            return self._list_tasks(
                connection,
                limit=limit,
                order=order,
                status=status,
                task_type=task_type,
            )

    def delete_task(self, task_id: str) -> bool:
        """删除单个任务；任务不存在时返回 ``False``。"""

        with self.transaction() as transaction:
            return transaction.delete_task(task_id)

    def cleanup_tasks(
            self,
            *,
            finished_before: float | None = None,
            keep_latest: int | None = None,
            statuses: Sequence[str] = tuple(sorted(TERMINAL_TASK_STATUSES)),
    ) -> int:
        """按完成时间或保留数量清理终态历史，绝不删除活动任务。"""

        with self.transaction() as transaction:
            return transaction.cleanup_tasks(
                finished_before=finished_before,
                keep_latest=keep_latest,
                statuses=statuses,
            )

    @contextmanager
    def transaction(self) -> Iterator["TaskStoreTransaction"]:
        """开启可组合的原子写事务；异常退出会回滚全部操作。"""

        with self._connection_transaction(write=True) as connection:
            yield TaskStoreTransaction(self, connection)

    def _now(self) -> float:
        return _validate_timestamp(self._clock(), "当前时间")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.database_path),
            timeout=self.busy_timeout,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout * 1000)}")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _connection_transaction(
            self,
            *,
            write: bool,
    ) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except sqlite3.DatabaseError as exc:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            if _is_sqlite_corruption(exc):
                raise TaskStoreCorruptionError(
                    f"任务数据库损坏，未覆盖原文件：{self.database_path}"
                ) from exc
            raise TaskStoreError(f"任务数据库操作失败：{exc}") from exc
        except BaseException:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()

    @contextmanager
    def _read_transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection_transaction(write=False) as connection:
            yield connection

    def _initialise(self) -> None:
        try:
            self._migrate_once()
        except _DetectedCorruption as exc:
            if self.corruption_policy == "raise":
                raise TaskStoreCorruptionError(
                    f"任务数据库损坏，未覆盖原文件：{self.database_path}"
                ) from exc
            recovery = self._quarantine_corrupt_database(exc)
            try:
                self._migrate_once()
            except Exception as rebuild_error:
                raise TaskStoreCorruptionError(
                    f"损坏数据库已隔离到 {recovery.quarantine_path}，但重建失败"
                ) from rebuild_error

    def _migrate_once(self) -> int:
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            self._verify_integrity(connection)
            connection.execute("BEGIN IMMEDIATE")
            current = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current > TASK_STORE_SCHEMA_VERSION:
                raise TaskStoreSchemaError(
                    f"任务数据库 schema version {current} 高于当前支持的 "
                    f"{TASK_STORE_SCHEMA_VERSION}"
                )
            for version in range(current + 1, TASK_STORE_SCHEMA_VERSION + 1):
                statements = _MIGRATIONS.get(version)
                if statements is None:
                    raise TaskStoreSchemaError(f"缺少 schema migration {version}")
                for statement in statements:
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version = {version}")
            connection.commit()
            self._validate_schema(connection)
            self._verify_integrity(connection)
            connection.execute("PRAGMA journal_mode = WAL")
            return TASK_STORE_SCHEMA_VERSION
        except sqlite3.DatabaseError as exc:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            if _is_sqlite_corruption(exc):
                raise _DetectedCorruption(str(exc)) from exc
            raise TaskStoreError(f"任务数据库初始化失败：{exc}") from exc
        except BaseException:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _verify_integrity(connection: sqlite3.Connection) -> None:
        try:
            results = [str(row[0]) for row in connection.execute("PRAGMA quick_check")]
        except sqlite3.DatabaseError as exc:
            if _is_sqlite_corruption(exc):
                raise _DetectedCorruption(str(exc)) from exc
            raise
        if results != ["ok"]:
            raise _DetectedCorruption("; ".join(results[:5]) or "quick_check 未返回结果")

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> None:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(tasks)")
        }
        if columns != _EXPECTED_COLUMNS:
            missing = sorted(_EXPECTED_COLUMNS - columns)
            extra = sorted(columns - _EXPECTED_COLUMNS)
            raise TaskStoreSchemaError(
                f"任务表字段不匹配；缺少={missing}，多出={extra}"
            )

    def _quarantine_corrupt_database(
            self,
            cause: BaseException,
    ) -> TaskStoreRecovery:
        if not self.database_path.exists():
            raise TaskStoreCorruptionError("检测到数据库损坏，但原文件不存在") from cause
        suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        quarantine_path = self.database_path.with_name(
            f"{self.database_path.name}.corrupt-{suffix}-{uuid.uuid4().hex[:8]}"
        )
        os.replace(self.database_path, quarantine_path)
        sidecars: list[Path] = []
        for marker in ("-wal", "-shm"):
            source = Path(str(self.database_path) + marker)
            if source.exists():
                destination = Path(str(quarantine_path) + marker)
                os.replace(source, destination)
                sidecars.append(destination)
        occurred_at = self._now()
        audit_path = self.database_path.with_name(
            f"{self.database_path.name}.recovery.jsonl"
        )
        event = {
            "action": "quarantine_and_rebuild",
            "occurred_at": occurred_at,
            "original_path": str(self.database_path),
            "quarantine_path": str(quarantine_path),
            "quarantined_sidecars": [str(path) for path in sidecars],
            "reason": str(cause)[:500],
        }
        try:
            with audit_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise TaskStoreCorruptionError(
                f"损坏数据库已隔离到 {quarantine_path}，但恢复审计写入失败"
            ) from exc
        recovery = TaskStoreRecovery(
            original_path=self.database_path,
            quarantine_path=quarantine_path,
            audit_path=audit_path,
            occurred_at=occurred_at,
            reason=str(cause),
            quarantined_sidecars=tuple(sidecars),
        )
        self.last_recovery = recovery
        return recovery

    def _prepare_new_task(
            self,
            task_id: str,
            task_type: str,
            *,
            source_path: str | os.PathLike[str] | None,
            output_path: str | os.PathLike[str] | None,
            source_paths: Sequence[str | os.PathLike[str]] | None,
            output_paths: Sequence[str | os.PathLike[str]] | None,
            status: str,
            progress: str,
            message: str,
            step: int,
            total: int,
            result_summary: Any,
            error_summary: str | None,
            metadata: Mapping[str, Any] | None,
            streamer_profile_snapshot: Mapping[str, Any] | None,
            created_at: float | None,
            finished_at: float | None,
    ) -> dict[str, Any]:
        task_id = _validate_task_id(task_id)
        task_type = _validate_task_type(task_type)
        status = _validate_status(status)
        step = _validate_counter(step, "step")
        total = _validate_counter(total, "total")
        if step > total:
            raise ValueError("step 不能大于 total")
        created = self._now() if created_at is None else _validate_timestamp(
            created_at,
            "created_at",
        )
        if finished_at is None and status in TERMINAL_TASK_STATUSES:
            finished = created
        elif finished_at is None:
            finished = None
        else:
            finished = _validate_timestamp(finished_at, "finished_at")
        if status in TERMINAL_TASK_STATUSES and finished is None:
            raise ValueError("终态任务必须包含 finished_at")
        if status not in TERMINAL_TASK_STATUSES and finished is not None:
            raise ValueError("非终态任务不能包含 finished_at")
        if finished is not None and finished < created:
            raise ValueError("finished_at 不能早于 created_at")
        if (
                metadata is not None
                and not isinstance(metadata, Mapping)):
            raise ValueError("metadata 必须是对象或 None")
        if (
                streamer_profile_snapshot is not None
                and not isinstance(streamer_profile_snapshot, Mapping)):
            raise ValueError("streamer_profile_snapshot 必须是对象或 None")
        task_metadata = dict(metadata or {})
        profile = dict(streamer_profile_snapshot or {})
        normalized_sources = normalize_task_paths(source_path, source_paths)
        normalized_outputs = normalize_task_paths(output_path, output_paths)
        return {
            "task_id": task_id,
            "task_type": task_type,
            "source_path": normalized_sources[0] if normalized_sources else None,
            "output_path": normalized_outputs[0] if normalized_outputs else None,
            "source_paths": _encode_path_tuple(normalized_sources),
            "output_paths": _encode_path_tuple(normalized_outputs),
            "status": status,
            "progress": _validate_safe_text(
                progress,
                "progress",
                maximum=_MAX_PROGRESS_LENGTH,
            ),
            "message": _validate_safe_text(
                message,
                "message",
                maximum=_MAX_MESSAGE_LENGTH,
            ),
            "step": step,
            "total": total,
            "result_summary": _encode_json(
                result_summary,
                "result_summary",
                maximum_bytes=_MAX_RESULT_SUMMARY_BYTES,
            ) if result_summary is not None else None,
            "error_summary": _validate_safe_text(
                error_summary,
                "error_summary",
                maximum=_MAX_ERROR_SUMMARY_LENGTH,
                optional=True,
            ),
            "metadata": _encode_json(
                task_metadata,
                "metadata",
                maximum_bytes=_MAX_TASK_METADATA_BYTES,
            ),
            "streamer_profile_snapshot": _encode_json(
                profile,
                "streamer_profile_snapshot",
                maximum_bytes=_MAX_PROFILE_SNAPSHOT_BYTES,
            ),
            "created_at": created,
            "updated_at": created,
            "finished_at": finished,
        }

    @staticmethod
    def _insert_task(
            connection: sqlite3.Connection,
            values: Mapping[str, Any],
    ) -> bool:
        columns = tuple(values)
        placeholders = ", ".join("?" for _ in columns)
        cursor = connection.execute(
            f"INSERT INTO tasks ({', '.join(columns)}) VALUES ({placeholders}) "
            "ON CONFLICT(task_id) DO NOTHING",
            tuple(values[column] for column in columns),
        )
        return cursor.rowcount == 1

    def _select_task(
            self,
            connection: sqlite3.Connection,
            task_id: str,
    ) -> TaskRecord | None:
        row = connection.execute(
            "SELECT * FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        return self._row_to_record(row) if row is not None else None

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> TaskRecord:
        metadata = _decode_json(row["metadata"], "metadata", default={})
        if not isinstance(metadata, dict):
            raise TaskStoreSchemaError("metadata 必须是 JSON 对象")
        profile = _decode_json(
            row["streamer_profile_snapshot"],
            "streamer_profile_snapshot",
            default={},
        )
        if not isinstance(profile, dict):
            raise TaskStoreSchemaError("streamer_profile_snapshot 必须是 JSON 对象")
        source_paths = _decode_path_tuple(row["source_paths"], "source_paths")
        output_paths = _decode_path_tuple(row["output_paths"], "output_paths")
        source_path = row["source_path"]
        output_path = row["output_path"]
        if source_path != (source_paths[0] if source_paths else None):
            raise TaskStoreSchemaError("source_path 与 source_paths 主路径不一致")
        if output_path != (output_paths[0] if output_paths else None):
            raise TaskStoreSchemaError("output_path 与 output_paths 主路径不一致")
        return TaskRecord(
            task_id=str(row["task_id"]),
            task_type=str(row["task_type"]),
            source_path=source_path,
            output_path=output_path,
            source_paths=source_paths,
            output_paths=output_paths,
            status=str(row["status"]),
            progress=str(row["progress"]),
            message=str(row["message"]),
            step=int(row["step"]),
            total=int(row["total"]),
            result_summary=_decode_json(
                row["result_summary"],
                "result_summary",
                default=None,
            ),
            error_summary=row["error_summary"],
            metadata=metadata,
            streamer_profile_snapshot=profile,
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            finished_at=(
                float(row["finished_at"])
                if row["finished_at"] is not None
                else None
            ),
        )

    def _list_tasks(
            self,
            connection: sqlite3.Connection,
            *,
            limit: int,
            order: str,
            status: str | None,
            task_type: str | None,
    ) -> list[TaskRecord]:
        if (
                isinstance(limit, bool)
                or not isinstance(limit, int)
                or not 1 <= limit <= MAX_LIST_LIMIT):
            raise ValueError(f"limit 必须是 1 到 {MAX_LIST_LIMIT} 的整数")
        order_by = _SORT_ORDERS.get(order)
        if order_by is None:
            raise ValueError(f"非法列表顺序 {order!r}；允许值：{', '.join(_SORT_ORDERS)}")
        clauses: list[str] = []
        parameters: list[Any] = []
        if status is not None:
            clauses.append("status = ?")
            parameters.append(_validate_status(status))
        if task_type is not None:
            clauses.append("task_type = ?")
            parameters.append(_validate_task_type(task_type))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = connection.execute(
            f"SELECT * FROM tasks{where} ORDER BY {order_by} LIMIT ?",
            (*parameters, limit),
        ).fetchall()
        return [self._row_to_record(row) for row in rows]


class TaskStoreTransaction:
    """绑定单一写事务的任务操作集合，不应跨线程共享。"""

    _MUTABLE_FIELDS = frozenset({
        "status",
        "progress",
        "message",
        "step",
        "total",
        "result_summary",
        "error_summary",
        "metadata",
        "streamer_profile_snapshot",
        "finished_at",
    })

    def __init__(self, store: TaskStore, connection: sqlite3.Connection) -> None:
        self._store = store
        self._connection = connection

    def create_task(self, task_id: str, task_type: str, **values: Any) -> TaskRecord:
        prepared = self._store._prepare_new_task(
            task_id,
            task_type,
            source_path=values.pop("source_path", None),
            output_path=values.pop("output_path", None),
            source_paths=values.pop("source_paths", None),
            output_paths=values.pop("output_paths", None),
            status=values.pop("status", "queued"),
            progress=values.pop("progress", ""),
            message=values.pop("message", ""),
            step=values.pop("step", 0),
            total=values.pop("total", 100),
            result_summary=values.pop("result_summary", None),
            error_summary=values.pop("error_summary", None),
            metadata=values.pop("metadata", None),
            streamer_profile_snapshot=values.pop(
                "streamer_profile_snapshot",
                None,
            ),
            created_at=values.pop("created_at", None),
            finished_at=values.pop("finished_at", None),
        )
        if values:
            raise ValueError(f"非法创建字段：{', '.join(sorted(values))}")
        inserted = self._store._insert_task(self._connection, prepared)
        record = self._store._select_task(self._connection, prepared["task_id"])
        if record is None:
            raise TaskStoreError("任务写入后无法读取")
        identity = (
            record.task_type,
            record.source_paths,
            record.output_paths,
        )
        requested_identity = (
            prepared["task_type"],
            _decode_path_tuple(prepared["source_paths"], "source_paths"),
            _decode_path_tuple(prepared["output_paths"], "output_paths"),
        )
        if not inserted and identity != requested_identity:
            raise TaskConflictError(
                f"task_id {record.task_id!r} 已绑定到不同的类型或路径"
            )
        return record

    def update_task(self, task_id: str, **changes: Any) -> TaskRecord:
        task_id = _validate_task_id(task_id)
        if not changes:
            raise ValueError("局部更新至少需要一个字段")
        unknown = set(changes) - self._MUTABLE_FIELDS
        if unknown:
            raise ValueError(f"非法更新字段：{', '.join(sorted(unknown))}")
        current = self._store._select_task(self._connection, task_id)
        if current is None:
            raise TaskNotFoundError(f"任务不存在：{task_id}")
        prepared = self._prepare_changes(current, changes)
        assignments = ", ".join(f"{field} = ?" for field in prepared)
        self._connection.execute(
            f"UPDATE tasks SET {assignments} WHERE task_id = ?",
            (*prepared.values(), task_id),
        )
        updated = self._store._select_task(self._connection, task_id)
        if updated is None:
            raise TaskStoreError("任务更新后无法读取")
        return updated

    def get_task(self, task_id: str) -> TaskRecord | None:
        return self._store._select_task(
            self._connection,
            _validate_task_id(task_id),
        )

    def list_tasks(
            self,
            *,
            limit: int = 100,
            order: str = "updated_desc",
            status: str | None = None,
            task_type: str | None = None,
    ) -> list[TaskRecord]:
        return self._store._list_tasks(
            self._connection,
            limit=limit,
            order=order,
            status=status,
            task_type=task_type,
        )

    def delete_task(self, task_id: str) -> bool:
        task_id = _validate_task_id(task_id)
        cursor = self._connection.execute(
            "DELETE FROM tasks WHERE task_id = ?",
            (task_id,),
        )
        return cursor.rowcount == 1

    def cleanup_tasks(
            self,
            *,
            finished_before: float | None = None,
            keep_latest: int | None = None,
            statuses: Sequence[str] = tuple(sorted(TERMINAL_TASK_STATUSES)),
    ) -> int:
        if finished_before is None and keep_latest is None:
            raise ValueError("清理任务必须提供 finished_before 或 keep_latest")
        threshold = (
            _validate_timestamp(finished_before, "finished_before")
            if finished_before is not None
            else None
        )
        if (
                keep_latest is not None
                and (
                    isinstance(keep_latest, bool)
                    or not isinstance(keep_latest, int)
                    or keep_latest < 0
                )):
            raise ValueError("keep_latest 必须是非负整数或 None")
        selected = tuple(dict.fromkeys(statuses))
        if not selected:
            raise ValueError("statuses 不能为空")
        invalid = set(selected) - TERMINAL_TASK_STATUSES
        if invalid:
            raise ValueError(
                "cleanup_tasks 只能清理终态；非法状态："
                + ", ".join(sorted(invalid))
            )
        placeholders = ", ".join("?" for _ in selected)
        deleted = 0
        if threshold is not None:
            cursor = self._connection.execute(
                f"DELETE FROM tasks WHERE status IN ({placeholders}) "
                "AND finished_at < ?",
                (*selected, threshold),
            )
            deleted += cursor.rowcount
        if keep_latest is not None:
            victims = self._connection.execute(
                f"SELECT task_id FROM tasks WHERE status IN ({placeholders}) "
                "ORDER BY finished_at DESC, updated_at DESC, task_id ASC "
                "LIMIT -1 OFFSET ?",
                (*selected, keep_latest),
            ).fetchall()
            if victims:
                self._connection.executemany(
                    "DELETE FROM tasks WHERE task_id = ?",
                    ((row["task_id"],) for row in victims),
                )
                deleted += len(victims)
        return deleted

    def _prepare_changes(
            self,
            current: TaskRecord,
            changes: Mapping[str, Any],
    ) -> dict[str, Any]:
        prepared: dict[str, Any] = {}
        status = (
            _validate_status(changes["status"])
            if "status" in changes
            else current.status
        )
        if "status" in changes:
            prepared["status"] = status
        for field, maximum in (
                ("progress", _MAX_PROGRESS_LENGTH),
                ("message", _MAX_MESSAGE_LENGTH)):
            if field in changes:
                prepared[field] = _validate_safe_text(
                    changes[field],
                    field,
                    maximum=maximum,
                )
        step = (
            _validate_counter(changes["step"], "step")
            if "step" in changes
            else current.step
        )
        total = (
            _validate_counter(changes["total"], "total")
            if "total" in changes
            else current.total
        )
        if step > total:
            raise ValueError("step 不能大于 total")
        if "step" in changes:
            prepared["step"] = step
        if "total" in changes:
            prepared["total"] = total
        if "result_summary" in changes:
            value = changes["result_summary"]
            prepared["result_summary"] = (
                _encode_json(
                    value,
                    "result_summary",
                    maximum_bytes=_MAX_RESULT_SUMMARY_BYTES,
                )
                if value is not None
                else None
            )
        if "error_summary" in changes:
            prepared["error_summary"] = _validate_safe_text(
                changes["error_summary"],
                "error_summary",
                maximum=_MAX_ERROR_SUMMARY_LENGTH,
                optional=True,
            )
        if "metadata" in changes:
            metadata = changes["metadata"]
            if metadata is None:
                metadata = {}
            if not isinstance(metadata, Mapping):
                raise ValueError("metadata 必须是对象或 None")
            prepared["metadata"] = _encode_json(
                dict(metadata),
                "metadata",
                maximum_bytes=_MAX_TASK_METADATA_BYTES,
            )
        if "streamer_profile_snapshot" in changes:
            profile = changes["streamer_profile_snapshot"]
            if profile is None:
                profile = {}
            if not isinstance(profile, Mapping):
                raise ValueError("streamer_profile_snapshot 必须是对象或 None")
            prepared["streamer_profile_snapshot"] = _encode_json(
                dict(profile),
                "streamer_profile_snapshot",
                maximum_bytes=_MAX_PROFILE_SNAPSHOT_BYTES,
            )
        now = max(self._store._now(), current.updated_at)
        prepared["updated_at"] = now
        if "finished_at" in changes:
            value = changes["finished_at"]
            finished = (
                _validate_timestamp(value, "finished_at")
                if value is not None
                else None
            )
        elif status in TERMINAL_TASK_STATUSES:
            finished = current.finished_at if current.finished_at is not None else now
        elif "status" in changes and current.status in TERMINAL_TASK_STATUSES:
            finished = None
        else:
            finished = current.finished_at
        if status in TERMINAL_TASK_STATUSES and finished is None:
            raise ValueError("终态任务必须包含 finished_at")
        if status not in TERMINAL_TASK_STATUSES and finished is not None:
            raise ValueError("非终态任务不能包含 finished_at")
        if finished is not None and finished < current.created_at:
            raise ValueError("finished_at 不能早于 created_at")
        if finished != current.finished_at or "finished_at" in changes:
            prepared["finished_at"] = finished
        return prepared


__all__ = [
    "DEFAULT_TASK_DATABASE_PATH",
    "DEFAULT_TASK_STATE_DIR",
    "MAX_LIST_LIMIT",
    "SensitiveTaskDataError",
    "TASK_STORE_SCHEMA_VERSION",
    "TERMINAL_TASK_STATUSES",
    "TaskConflictError",
    "TaskNotFoundError",
    "TaskRecord",
    "TaskStore",
    "TaskStoreCorruptionError",
    "TaskStoreError",
    "TaskStoreRecovery",
    "TaskStoreSchemaError",
    "VALID_TASK_STATUSES",
    "normalize_task_path",
    "normalize_task_paths",
]
