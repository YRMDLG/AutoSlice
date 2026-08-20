import tempfile
import threading
import unittest
from pathlib import Path

from autoslice.streamer_profiles import StreamerProfile
from autoslice.task_registry import TaskLifecycleError, TaskRegistry
from autoslice.task_store import TaskStore


class ManualClock:
    def __init__(self, value=1_000.0):
        self.value = float(value)
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            return self.value

    def advance(self, seconds=1.0):
        with self._lock:
            self.value += float(seconds)


class TokenFactory:
    def __init__(self):
        self.value = 0
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            self.value += 1
            return f"run-{self.value}"


class TaskRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.database_path = self.root / "state" / "tasks.sqlite3"
        self.clock = ManualClock()
        self.tokens = TokenFactory()
        self.store = TaskStore(
            self.database_path,
            busy_timeout=5.0,
            clock=self.clock,
        )
        self.registry = TaskRegistry(
            self.store,
            token_factory=self.tokens,
            clock=self.clock,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def reserve(self, task_type="subtitle_review", **kwargs):
        task_id, conflict_task_id = self.registry.reserve(task_type, **kwargs)
        self.assertIsNotNone(task_id)
        self.assertIsNone(conflict_task_id)
        return task_id

    def test_same_basename_in_different_directories_does_not_conflict(self):
        first_source = self.root / "streamer-a" / "recording.flv"
        second_source = self.root / "streamer-b" / "recording.flv"

        first_id = self.reserve(source_paths=(first_source,))
        second_id = self.reserve(source_paths=(second_source,))

        self.assertNotEqual(first_id, second_id)
        self.assertNotEqual(
            self.registry.get(first_id).source_paths,
            self.registry.get(second_id).source_paths,
        )

    def test_same_source_and_type_returns_existing_task_id(self):
        source = self.root / "input" / "same.flv"
        first_id = self.reserve(source_paths=(source,))

        task_id, conflict_task_id = self.registry.reserve(
            "subtitle_review",
            source_paths=(source,),
        )

        self.assertIsNone(task_id)
        self.assertEqual(conflict_task_id, first_id)

    def test_conflict_types_detect_cross_type_source_conflict(self):
        source = self.root / "input" / "cross.flv"
        first_id = self.reserve("topic_pipeline", source_paths=(source,))

        task_id, conflict_task_id = self.registry.reserve(
            "timeline_optimization",
            source_paths=(source,),
            conflict_types=("topic_pipeline", "timeline_optimization"),
        )

        self.assertIsNone(task_id)
        self.assertEqual(conflict_task_id, first_id)

    def test_different_sources_with_same_output_conflict_for_any_type(self):
        output = self.root / "output" / "shared.srt"
        first_id = self.reserve(
            "subtitle_review",
            source_paths=(self.root / "one.flv",),
            output_paths=(output,),
        )

        task_id, conflict_task_id = self.registry.reserve(
            "topic_pipeline",
            source_paths=(self.root / "two.flv",),
            output_paths=(output,),
            conflict_types=("topic_pipeline",),
        )

        self.assertIsNone(task_id)
        self.assertEqual(conflict_task_id, first_id)

    def test_concurrent_same_output_has_exactly_one_reservation(self):
        worker_count = 8
        barrier = threading.Barrier(worker_count)
        results = []
        errors = []
        result_lock = threading.Lock()
        output = self.root / "output" / "concurrent.srt"

        def worker(index):
            try:
                barrier.wait(timeout=5)
                result = self.registry.reserve(
                    "subtitle_review",
                    source_paths=(self.root / f"source-{index}.flv",),
                    output_paths=(output,),
                )
                with result_lock:
                    results.append(result)
            except BaseException as exc:
                with result_lock:
                    errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(index,))
            for index in range(worker_count)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        successful = [task_id for task_id, conflict in results if task_id]
        conflicts = [conflict for task_id, conflict in results if conflict]
        self.assertEqual(len(successful), 1)
        self.assertEqual(conflicts, [successful[0]] * (worker_count - 1))
        self.assertEqual(len(self.registry.list(limit=20)), 1)

    def test_completed_resource_can_run_again_with_new_task_id(self):
        source = self.root / "input" / "rerun.flv"
        output = self.root / "output" / "rerun.srt"
        first_id = self.reserve(
            source_paths=(source,),
            output_paths=(output,),
        )
        self.registry.mark_running(first_id)
        self.registry.complete(first_id, {"path": str(output)})

        second_id = self.reserve(
            source_paths=(source,),
            output_paths=(output,),
        )

        self.assertNotEqual(first_id, second_id)
        self.assertEqual(self.registry.get(first_id).status, "done")
        self.assertEqual(self.registry.get(second_id).status, "queued")

    def test_snapshot_keeps_json_object_in_summary_and_result(self):
        task_id = self.reserve(
            "topic_pipeline",
            source_paths=(self.root / "recording.flv",),
        )
        self.registry.mark_running(task_id)
        self.registry.complete(task_id, {"topic_count": 3, "slice_count": 2})

        snapshot = self.registry.snapshot(task_id)

        self.assertEqual(
            snapshot["result_summary"],
            {"topic_count": 3, "slice_count": 2},
        )
        self.assertEqual(
            snapshot["result"],
            {"topic_count": 3, "slice_count": 2},
        )

    def test_identity_digest_uses_every_full_resource_path(self):
        source = self.root / "input" / "identity.flv"
        first_output = self.root / "one" / "same.srt"
        second_output = self.root / "two" / "same.srt"
        first_id, conflict = self.registry.reserve(
            "subtitle_review",
            source_paths=(source,),
            output_paths=(first_output,),
            run_nonce="same-test-nonce",
        )
        self.assertIsNone(conflict)
        self.registry.mark_running(first_id)
        self.registry.complete(first_id)

        second_id, conflict = self.registry.reserve(
            "subtitle_review",
            source_paths=(source,),
            output_paths=(second_output,),
            run_nonce="same-test-nonce",
        )

        self.assertIsNone(conflict)
        self.assertNotEqual(first_id, second_id)

    def test_terminal_task_rejects_old_worker_writes(self):
        task_id = self.reserve(source_paths=(self.root / "terminal.flv",))
        self.registry.mark_running(task_id)
        completed = self.registry.complete(task_id, {"count": 2})

        with self.assertRaises(TaskLifecycleError):
            self.registry.mark_running(task_id)
        with self.assertRaises(TaskLifecycleError):
            self.registry.update_progress(task_id, step=50)
        with self.assertRaises(TaskLifecycleError):
            self.registry.complete(task_id, {"count": 99})

        unchanged = self.registry.get(task_id)
        self.assertEqual(unchanged.status, "done")
        self.assertEqual(unchanged.result_summary, completed.result_summary)

    def test_cancel_sets_event_and_persists_cancelled_state(self):
        task_id = self.reserve(source_paths=(self.root / "cancel.flv",))
        self.registry.mark_running(task_id)
        cancellation = self.registry.cancellation_event(task_id)
        self.assertFalse(cancellation.is_set())

        cancelled = self.registry.cancel(task_id, reason="停止当前字幕任务")

        self.assertTrue(cancellation.wait(timeout=0.1))
        self.assertTrue(self.registry.cancellation_requested(task_id))
        self.assertEqual(cancelled.status, "cancelled")
        self.assertEqual(self.registry.get(task_id).error_summary, "停止当前字幕任务")
        with self.assertRaises(TaskLifecycleError):
            self.registry.complete(task_id)

    def test_startup_recovery_interrupts_only_active_tasks_and_is_idempotent(self):
        profile = {
            "id": "streamer-a",
            "label": "主播 A",
            "aliases": ["A"],
        }
        queued_id = self.reserve(
            "subtitle_review",
            source_paths=(self.root / "queued.flv",),
            output_paths=(self.root / "queued.srt",),
            metadata={"checkpoint_path": "relative/checkpoint.json", "keep": 1},
            streamer_profile=profile,
        )
        self.registry.update_progress(
            queued_id,
            progress="已转录一部分",
            step=23,
        )
        running_id = self.reserve(
            "topic_pipeline",
            source_paths=(self.root / "running.flv",),
            metadata={"phase": "analysis"},
            streamer_profile=profile,
        )
        self.registry.mark_running(running_id, progress="正在分析")
        self.registry.update_progress(running_id, step=41)

        done_id = self.reserve(source_paths=(self.root / "done.flv",))
        self.registry.mark_running(done_id)
        self.registry.complete(done_id, {"ok": True})
        error_id = self.reserve(source_paths=(self.root / "error.flv",))
        self.registry.fail(error_id, "预期失败")
        cancelled_id = self.reserve(source_paths=(self.root / "cancelled.flv",))
        self.registry.cancel(cancelled_id)
        interrupted_id = "manual-interrupted-task"
        self.store.create_task(
            interrupted_id,
            "subtitle_review",
            source_paths=(self.root / "interrupted.flv",),
            status="interrupted",
            progress="旧中断",
        )
        terminal_before = {
            task_id: self.store.get_task(task_id).to_dict()
            for task_id in (done_id, error_id, cancelled_id, interrupted_id)
        }

        reopened_store = TaskStore(
            self.database_path,
            busy_timeout=5.0,
            clock=self.clock,
        )
        reopened = TaskRegistry(
            reopened_store,
            token_factory=TokenFactory(),
            clock=self.clock,
        )

        self.assertCountEqual(
            reopened.recovered_task_ids,
            (queued_id, running_id),
        )
        recovered_queued = reopened.get(queued_id)
        recovered_running = reopened.get(running_id)
        self.assertEqual(recovered_queued.status, "interrupted")
        self.assertEqual(recovered_queued.progress, "已转录一部分")
        self.assertEqual(recovered_queued.step, 23)
        self.assertEqual(
            recovered_queued.source_paths,
            self.registry.get(queued_id).source_paths,
        )
        self.assertEqual(
            recovered_queued.output_paths,
            self.registry.get(queued_id).output_paths,
        )
        self.assertEqual(
            recovered_queued.streamer_profile_snapshot["id"],
            "streamer-a",
        )
        self.assertEqual(recovered_queued.metadata["keep"], 1)
        self.assertEqual(
            recovered_queued.result_summary["action"],
            "retry_from_checkpoint",
        )
        self.assertEqual(
            recovered_queued.metadata["startup_recovery"]["next_action"],
            "使用原资源与 profile 预约新 task_id 后从检查点重试",
        )
        self.assertEqual(recovered_running.status, "interrupted")
        for task_id, before in terminal_before.items():
            self.assertEqual(reopened.get(task_id).to_dict(), before)

        recovered_before = {
            task_id: reopened.get(task_id).to_dict()
            for task_id in (queued_id, running_id)
        }
        self.assertEqual(reopened.startup_recovery(), [])
        for task_id, before in recovered_before.items():
            self.assertEqual(reopened.get(task_id).to_dict(), before)

    def test_profile_snapshot_is_safe_and_isolated(self):
        mapping_profile = {
            "id": "mapping",
            "label": "映射主播",
            "canonical_name": "Mapping",
            "report_name": "Mapping",
            "title_prefix": "【Mapping】",
            "aliases": ["M"],
            "api_key": "must-not-persist",
            "title_style_profile": str(self.root / "private-style.json"),
        }
        mapping_id = self.reserve(
            source_paths=(self.root / "mapping.flv",),
            streamer_profile=mapping_profile,
        )
        mapping_profile["label"] = "已被外部修改"
        mapping_profile["aliases"].append("changed")

        stored = self.registry.get(mapping_id)
        self.assertEqual(stored.streamer_profile_snapshot["label"], "映射主播")
        self.assertEqual(stored.streamer_profile_snapshot["aliases"], ["M"])
        self.assertNotIn("api_key", stored.streamer_profile_snapshot)
        self.assertNotIn("title_style_profile", stored.streamer_profile_snapshot)
        self.assertRegex(
            stored.streamer_profile_snapshot["fingerprint"],
            r"^[0-9a-f]{64}$",
        )

        stored.streamer_profile_snapshot["aliases"].append("tampered")
        dictionary_snapshot = self.registry.snapshot(mapping_id)
        dictionary_snapshot["streamer_profile"]["label"] = "tampered"
        self.assertEqual(
            self.registry.get(mapping_id).streamer_profile_snapshot["aliases"],
            ["M"],
        )
        self.assertEqual(
            self.registry.get(mapping_id).streamer_profile_snapshot["label"],
            "映射主播",
        )

        dataclass_profile = StreamerProfile(
            id="dataclass",
            label="配置主播",
            canonical_name="Dataclass",
            report_name="Dataclass",
            title_prefix="【Dataclass】",
            aliases=("D",),
            path_keywords=("private-path-keyword",),
            subtitle_glossary=("专名",),
            asr_replacements=(("错词", "正词"),),
            title_style_profile=self.root / "style.json",
            outro_clip=None,
        )
        dataclass_id = self.reserve(
            source_paths=(self.root / "dataclass.flv",),
            streamer_profile=dataclass_profile,
        )
        dataclass_snapshot = self.registry.get(
            dataclass_id
        ).streamer_profile_snapshot
        self.assertEqual(
            dataclass_snapshot["fingerprint"],
            dataclass_profile.subtitle_review_fingerprint(),
        )
        self.assertNotIn("path_keywords", dataclass_snapshot)
        self.assertNotIn("subtitle_glossary", dataclass_snapshot)

    def test_metadata_updates_merge_without_clearing_existing_values(self):
        task_id = self.reserve(
            source_paths=(self.root / "metadata.flv",),
            metadata={
                "checkpoint": {"segment": 1, "keep": True},
                "owner": "subtitle",
            },
        )
        self.registry.mark_running(
            task_id,
            metadata={"checkpoint": {"segment": 2}},
        )
        self.registry.update_progress(
            task_id,
            step=50,
            metadata={"new_field": [1, 2]},
        )
        completed = self.registry.complete(
            task_id,
            {"output": "relative/result.srt"},
            metadata={"checkpoint": {"finished": True}},
        )

        self.assertEqual(completed.metadata["owner"], "subtitle")
        self.assertEqual(
            completed.metadata["checkpoint"],
            {"segment": 2, "keep": True, "finished": True},
        )
        self.assertEqual(completed.metadata["new_field"], [1, 2])
        snapshot = self.registry.snapshot(task_id)
        self.assertEqual(snapshot["result"], {"output": "relative/result.srt"})
        self.assertIsNone(snapshot["error"])
        self.assertEqual(snapshot["completed_at"], completed.finished_at)
        self.assertEqual(snapshot["owner"], "subtitle")
        self.assertEqual(snapshot["checkpoint"]["finished"], True)

    def test_legacy_snapshot_exposes_error_as_result_without_losing_error(self):
        task_id, _ = self.registry.reserve(
            "subtitle_render",
            source_paths=(self.root / "成片.mp4",),
            run_nonce="legacy-error",
        )
        self.registry.mark_running(task_id)
        self.registry.fail(task_id, "编码失败")

        snapshot = self.registry.snapshot(task_id)

        self.assertEqual(snapshot["status"], "error")
        self.assertEqual(snapshot["result"], "编码失败")
        self.assertEqual(snapshot["error"], "编码失败")

    def test_ttl_and_limit_cleanup_never_delete_active_tasks(self):
        queued_id = self.reserve(source_paths=(self.root / "active-queued.flv",))
        running_id = self.reserve(source_paths=(self.root / "active-running.flv",))
        self.registry.mark_running(running_id)
        terminal_ids = []
        for index in range(3):
            task_id = self.reserve(
                source_paths=(self.root / f"terminal-{index}.flv",),
            )
            self.registry.mark_running(task_id)
            self.registry.complete(task_id)
            terminal_ids.append(task_id)
            self.clock.advance()
        self.clock.advance()

        deleted = self.registry.cleanup_history(
            ttl_seconds=0,
            keep_latest=0,
        )

        self.assertEqual(deleted, len(terminal_ids))
        self.assertEqual(self.registry.get(queued_id).status, "queued")
        self.assertEqual(self.registry.get(running_id).status, "running")
        for task_id in terminal_ids:
            self.assertIsNone(self.registry.get(task_id))
        self.assertLessEqual(len(self.registry.snapshot(limit=1)), 1)

    def test_history_is_readable_after_task_store_reopens(self):
        source = self.root / "persistent.flv"
        output = self.root / "persistent.srt"
        task_id = self.reserve(
            source_paths=(source,),
            output_paths=(output,),
            metadata={"persisted": True},
        )
        self.registry.mark_running(task_id)
        self.registry.complete(task_id, {"output_path": str(output)})

        reopened_store = TaskStore(
            self.database_path,
            busy_timeout=5.0,
            clock=self.clock,
        )
        reopened_registry = TaskRegistry(
            reopened_store,
            token_factory=TokenFactory(),
            clock=self.clock,
        )
        reopened = reopened_registry.get(task_id)

        self.assertEqual(reopened.status, "done")
        self.assertEqual(reopened.source_paths, self.registry.get(task_id).source_paths)
        self.assertEqual(reopened.output_paths, self.registry.get(task_id).output_paths)
        self.assertEqual(reopened.metadata, {"persisted": True})
        self.assertEqual(
            reopened_registry.snapshot(task_id)["result"],
            {"output_path": str(output)},
        )


if __name__ == "__main__":
    unittest.main()
