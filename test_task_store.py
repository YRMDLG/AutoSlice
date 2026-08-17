import json
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from task_store import (
    DEFAULT_TASK_DATABASE_PATH,
    MAX_LIST_LIMIT,
    SensitiveTaskDataError,
    TASK_STORE_SCHEMA_VERSION,
    TaskConflictError,
    TaskStore,
    TaskStoreCorruptionError,
    normalize_task_path,
)


class TaskStoreTests(unittest.TestCase):
    def test_reopen_preserves_complete_task_record(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "state" / "tasks.sqlite3"
            source = root / "录播" / "主播-2026-08-17.flv"
            output = root / "输出" / "主播-2026-08-17_自动切片"
            store = TaskStore(database)
            created = store.create_task(
                "pipeline_001",
                "topic_pipeline",
                source_path=source / ".." / source.name,
                output_path=output,
                progress="准备分析",
                message="任务已预约",
                result_summary={"topic_count": 0},
                streamer_profile_snapshot={
                    "id": "generic",
                    "aliases": ["测试主播"],
                },
                created_at=100.0,
            )
            store.update_task(
                created.task_id,
                status="done",
                progress="完成",
                message="分析完成",
                step=100,
                result_summary={"topic_count": 3, "slice_count": 2},
            )

            reopened = TaskStore(database)
            restored = reopened.get_task(created.task_id)

        self.assertIsNotNone(restored)
        self.assertEqual(restored.source_path, normalize_task_path(source))
        self.assertEqual(restored.output_path, normalize_task_path(output))
        self.assertEqual(restored.status, "done")
        self.assertEqual(restored.progress, "完成")
        self.assertEqual(restored.message, "分析完成")
        self.assertEqual((restored.step, restored.total), (100, 100))
        self.assertEqual(
            restored.result_summary,
            {"topic_count": 3, "slice_count": 2},
        )
        self.assertEqual(restored.error_summary, None)
        self.assertEqual(restored.streamer_profile_snapshot["id"], "generic")
        self.assertEqual(restored.created_at, 100.0)
        self.assertGreaterEqual(restored.updated_at, restored.created_at)
        self.assertIsNotNone(restored.finished_at)

    def test_same_filename_in_different_normalized_directories_stays_independent(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = TaskStore(root / "tasks.sqlite3")
            first_source = root / "第一场" / "直播.flv"
            second_source = root / "第二场" / "直播.flv"
            first = store.create_task(
                "task_first",
                "topic_pipeline",
                source_path=first_source.parent / "." / first_source.name,
                output_path=root / "输出一",
            )
            second = store.create_task(
                "task_second",
                "topic_pipeline",
                source_path=second_source.parent / "子目录" / ".." / second_source.name,
                output_path=root / "输出二",
            )

            tasks = store.list_tasks(order="created_asc")

        self.assertEqual(len(tasks), 2)
        self.assertEqual(first.source_path, normalize_task_path(first_source))
        self.assertEqual(second.source_path, normalize_task_path(second_source))
        self.assertNotEqual(first.source_path, second.source_path)

    def test_idempotent_create_preserves_existing_state_and_rejects_new_identity(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = TaskStore(root / "tasks.sqlite3")
            store.create_task(
                "stable_task",
                "subtitle_review",
                source_path=root / "字幕.srt",
                progress="等待",
            )
            store.update_task(
                "stable_task",
                status="running",
                progress="处理中",
                step=25,
            )

            repeated = store.create_task(
                "stable_task",
                "subtitle_review",
                source_path=root / "." / "字幕.srt",
                progress="不应覆盖",
            )

            self.assertEqual(repeated.status, "running")
            self.assertEqual(repeated.progress, "处理中")
            self.assertEqual(repeated.step, 25)
            with self.assertRaisesRegex(TaskConflictError, "不同的类型或路径"):
                store.create_task(
                    "stable_task",
                    "subtitle_review",
                    source_path=root / "另一份字幕.srt",
                )

    def test_partial_update_does_not_erase_old_fields(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = TaskStore(root / "tasks.sqlite3")
            initial = store.create_task(
                "partial_update",
                "subtitle_render",
                source_path=root / "成片.mp4",
                output_path=root / "成片_字幕版.mp4",
                progress="等待压制",
                message="保留此消息",
                result_summary={"existing": True},
                error_summary="旧错误摘要",
                streamer_profile_snapshot={"id": "zeyin", "label": "泽音"},
            )

            updated = store.update_task(initial.task_id, step=30)

        self.assertEqual(updated.task_type, initial.task_type)
        self.assertEqual(updated.source_path, initial.source_path)
        self.assertEqual(updated.output_path, initial.output_path)
        self.assertEqual(updated.progress, "等待压制")
        self.assertEqual(updated.message, "保留此消息")
        self.assertEqual(updated.result_summary, {"existing": True})
        self.assertEqual(updated.error_summary, "旧错误摘要")
        self.assertEqual(updated.streamer_profile_snapshot["id"], "zeyin")
        self.assertEqual(updated.step, 30)

    def test_explicit_transaction_rolls_back_all_changes(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = TaskStore(root / "tasks.sqlite3")
            store.create_task("kept", "topic_pipeline", source_path=root / "kept.flv")

            with self.assertRaisesRegex(RuntimeError, "触发回滚"):
                with store.transaction() as transaction:
                    transaction.create_task(
                        "rolled_back",
                        "topic_pipeline",
                        source_path=root / "rollback.flv",
                    )
                    transaction.update_task("kept", progress="不应保留")
                    raise RuntimeError("触发回滚")

            self.assertIsNone(store.get_task("rolled_back"))
            self.assertEqual(store.get_task("kept").progress, "")

    def test_concurrent_threads_can_write_through_one_store(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = TaskStore(root / "tasks.sqlite3")

            def create(index):
                return store.create_task(
                    f"thread_{index:02d}",
                    "topic_pipeline",
                    source_path=root / f"录播-{index:02d}.flv",
                    progress=f"线程 {index}",
                ).task_id

            with ThreadPoolExecutor(max_workers=8) as executor:
                task_ids = list(executor.map(create, range(32)))

            saved = store.list_tasks(limit=64, order="created_asc")

        self.assertEqual(len(task_ids), 32)
        self.assertEqual({task.task_id for task in saved}, set(task_ids))

    def test_schema_version_and_public_migration_entrypoint(self):
        with TemporaryDirectory() as temporary:
            database = Path(temporary) / "tasks.sqlite3"
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 0)

            store = TaskStore(database)

            self.assertEqual(store.migrate(), TASK_STORE_SCHEMA_VERSION)
            self.assertEqual(store.schema_version, TASK_STORE_SCHEMA_VERSION)
            with closing(sqlite3.connect(database)) as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(tasks)")
                }

        self.assertEqual(version, TASK_STORE_SCHEMA_VERSION)
        self.assertIn("streamer_profile_snapshot", columns)
        self.assertIn("finished_at", columns)

    def test_corrupt_database_raises_without_overwriting_original(self):
        with TemporaryDirectory() as temporary:
            database = Path(temporary) / "tasks.sqlite3"
            original = b"not-a-sqlite-database\x00task-history"
            database.write_bytes(original)

            with self.assertRaisesRegex(TaskStoreCorruptionError, "未覆盖原文件"):
                TaskStore(database)

            self.assertEqual(database.read_bytes(), original)
            self.assertEqual(list(database.parent.glob("*.corrupt-*")), [])

    def test_corrupt_database_can_be_quarantined_with_audit_then_rebuilt(self):
        with TemporaryDirectory() as temporary:
            database = Path(temporary) / "tasks.sqlite3"
            original = b"broken sqlite contents"
            database.write_bytes(original)

            store = TaskStore(database, corruption_policy="quarantine")
            recovery = store.last_recovery

            self.assertIsNotNone(recovery)
            self.assertEqual(recovery.quarantine_path.read_bytes(), original)
            self.assertTrue(database.is_file())
            self.assertEqual(store.schema_version, TASK_STORE_SCHEMA_VERSION)
            self.assertEqual(store.list_tasks(), [])
            events = [
                json.loads(line)
                for line in recovery.audit_path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["action"], "quarantine_and_rebuild")
        self.assertEqual(events[0]["quarantine_path"], str(recovery.quarantine_path))

    def test_list_limit_sort_and_filters_are_bounded(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = TaskStore(root / "tasks.sqlite3")
            store.create_task("old", "topic_pipeline", created_at=10.0)
            store.create_task("middle", "subtitle_review", created_at=20.0)
            store.create_task("new", "topic_pipeline", created_at=30.0)

            newest = store.list_tasks(limit=2, order="created_desc")
            oldest = store.list_tasks(limit=2, order="created_asc")
            filtered = store.list_tasks(task_type="topic_pipeline", order="created_asc")

            self.assertEqual([task.task_id for task in newest], ["new", "middle"])
            self.assertEqual([task.task_id for task in oldest], ["old", "middle"])
            self.assertEqual([task.task_id for task in filtered], ["old", "new"])
            with self.assertRaisesRegex(ValueError, "limit"):
                store.list_tasks(limit=MAX_LIST_LIMIT + 1)
            with self.assertRaisesRegex(ValueError, "非法列表顺序"):
                store.list_tasks(order="task_id; DROP TABLE tasks")

    def test_cleanup_only_removes_selected_terminal_history(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = TaskStore(root / "tasks.sqlite3")
            store.create_task("old_done", "topic_pipeline", status="done", created_at=10.0)
            store.create_task("old_error", "topic_pipeline", status="error", created_at=20.0)
            store.create_task("active", "topic_pipeline", status="running", created_at=5.0)
            store.create_task("new_done", "topic_pipeline", status="done", created_at=30.0)
            store.create_task("newest_done", "topic_pipeline", status="done", created_at=40.0)

            deleted_by_age = store.cleanup_tasks(finished_before=25.0)
            deleted_by_limit = store.cleanup_tasks(keep_latest=1)
            remaining = store.list_tasks(limit=10, order="created_asc")

            self.assertEqual(deleted_by_age, 2)
            self.assertEqual(deleted_by_limit, 1)
            self.assertEqual(
                [task.task_id for task in remaining],
                ["active", "newest_done"],
            )
            with self.assertRaisesRegex(ValueError, "只能清理终态"):
                store.cleanup_tasks(keep_latest=0, statuses=("running",))

    def test_delete_validation_and_secret_rejection_are_explicit(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = TaskStore(root / "tasks.sqlite3")
            store.create_task("delete_me", "topic_pipeline")

            self.assertTrue(store.delete_task("delete_me"))
            self.assertFalse(store.delete_task("delete_me"))
            with self.assertRaisesRegex(ValueError, "task_id"):
                store.get_task("../invalid")
            with self.assertRaisesRegex(ValueError, "非法任务状态"):
                store.create_task("bad_status", "topic_pipeline", status="complete")
            with self.assertRaisesRegex(ValueError, "非终态任务"):
                store.create_task(
                    "bad_finished_at",
                    "topic_pipeline",
                    status="running",
                    created_at=10.0,
                    finished_at=11.0,
                )
            with self.assertRaisesRegex(ValueError, "非法更新字段"):
                store.update_task("missing", source_path=root / "x.flv")
            with self.assertRaisesRegex(SensitiveTaskDataError, "敏感字段"):
                store.create_task(
                    "secret_profile",
                    "topic_pipeline",
                    streamer_profile_snapshot={"id": "generic", "api_token": "secret"},
                )
            with self.assertRaisesRegex(SensitiveTaskDataError, "疑似包含凭据"):
                store.create_task(
                    "secret_error",
                    "topic_pipeline",
                    error_summary="Authorization: Bearer secret",
                )

    def test_default_database_lives_in_ignored_local_state_directory(self):
        project_root = Path(__file__).resolve().parent
        ignore_lines = {
            line.strip()
            for line in (project_root / ".gitignore").read_text(encoding="utf-8").splitlines()
        }

        self.assertEqual(DEFAULT_TASK_DATABASE_PATH.parent.name, ".autoslice-state")
        self.assertEqual(DEFAULT_TASK_DATABASE_PATH.parent.parent, project_root)
        self.assertIn(".autoslice-state/", ignore_lines)


if __name__ == "__main__":
    unittest.main()
