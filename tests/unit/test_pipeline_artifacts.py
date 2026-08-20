import unittest

from autoslice.pipeline_artifacts import persist_pipeline_artifacts


class PipelineArtifactsTests(unittest.TestCase):
    def _invoke(self, calls, *, queue=None, retry=False, progress=None):
        layout = {
            "artifact_dir": "bundle/artifacts",
            "overview_path": "bundle/overview.md",
            "output_root": "bundle",
            "slice_dir": "bundle/slices",
        }

        def write_text(path, value):
            calls.append(("report", path, value))

        def write_json(path, value):
            calls.append(("json", path, value))

        def build_manifest(*args):
            calls.append(("build_manifest", args))
            return {"manifest_json_path": "bundle/task.json", "tasks": []}

        def write_manifest(manifest):
            calls.append(("write_manifest", dict(manifest)))

        def upsert(manifest, **kwargs):
            calls.append(("queue", dict(manifest), kwargs))
            if queue is not None:
                raise queue

        def write_checkpoint(*args, **kwargs):
            calls.append(("checkpoint", args, kwargs))

        def organize(*args, **kwargs):
            calls.append(("organize", args, kwargs))
            return {"overview_path": "bundle/organized-overview.md"}

        result = persist_pipeline_artifacts(
            video_path="recording.flv",
            report_path="bundle/report.md",
            report="# report",
            json_path="bundle/marks.json",
            payload={"clip_marks": []},
            source_srt_path="source.srt",
            corrected_srt_path="corrected.srt",
            clip_marks=[],
            task_manifest_json_path="bundle/task.json",
            task_manifest_md_path="bundle/task.md",
            unified_queue_json_path="bundle/queue.json",
            unified_queue_md_path="bundle/queue.md",
            artifact_layout_version=1,
            artifact_layout=layout,
            clip_review_checkpoint_path="bundle/checkpoint.json",
            accepted_topics=[{"title": "topic"}],
            clip_review_warning="review warning",
            checkpoint_source="artifact_retry" if retry else "pipeline",
            clip_review_completed_at="2026-08-20T12:00:00",
            write_manifest_on_queue_warning=retry,
            queue_warning_callback=progress,
            write_artifact_text=write_text,
            write_artifact_json=write_json,
            build_refinement_manifest=build_manifest,
            write_refinement_manifest_files=write_manifest,
            upsert_unified_refinement_queue=upsert,
            write_completed_clip_review_checkpoint=write_checkpoint,
            organize_existing_artifacts=organize,
        )
        return result

    def test_main_queue_success_persists_in_order_and_returns_organized(self):
        calls = []
        result = self._invoke(calls)

        self.assertEqual(
            [call[0] for call in calls],
            ["report", "json", "build_manifest", "write_manifest", "queue",
             "checkpoint", "organize"],
        )
        manifest = calls[3][1]
        self.assertEqual(manifest["unified_queue_json_path"], "bundle/queue.json")
        self.assertEqual(manifest["unified_queue_md_path"], "bundle/queue.md")
        self.assertEqual(manifest["artifact_dir"], "bundle/artifacts")
        self.assertEqual(manifest["overview_path"], "bundle/overview.md")
        self.assertEqual(result["organized"]["overview_path"], "bundle/organized-overview.md")
        self.assertIsNone(result["unified_queue_warning"])
        self.assertEqual(result["refinement_manifest"], manifest)

    def test_retry_queue_success_writes_completed_checkpoint_source(self):
        calls = []
        result = self._invoke(calls, retry=True)

        self.assertEqual([call[0] for call in calls], [
            "report", "json", "build_manifest", "write_manifest", "queue",
            "checkpoint", "organize",
        ])
        self.assertEqual(calls[5][2]["source"], "artifact_retry")
        self.assertEqual(result["organized"]["overview_path"], "bundle/organized-overview.md")

    def test_main_queue_failure_warns_progress_but_keeps_manifest_write_once(self):
        calls = []
        progress = []
        result = self._invoke(
            calls,
            queue=OSError("queue unavailable"),
            progress=lambda *args: progress.append(args),
        )

        self.assertEqual(
            [call[0] for call in calls],
            ["report", "json", "build_manifest", "write_manifest", "queue",
             "checkpoint", "organize"],
        )
        self.assertEqual(result["unified_queue_warning"], "精调总清单更新失败: queue unavailable")
        self.assertEqual(progress, [(result["unified_queue_warning"], 99, 100)])

    def test_retry_queue_failure_rewrites_manifest_with_warning(self):
        calls = []
        result = self._invoke(calls, queue=ValueError("bad queue"), retry=True)

        self.assertEqual([call[0] for call in calls], [
            "report", "json", "build_manifest", "write_manifest", "queue",
            "write_manifest", "checkpoint", "organize",
        ])
        self.assertEqual(
            calls[5][1]["unified_queue_warning"],
            "精调总清单更新失败: bad queue",
        )
        self.assertEqual(result["refinement_manifest"]["unified_queue_warning"],
                         "精调总清单更新失败: bad queue")

    def test_non_queue_failures_and_unexpected_queue_failures_are_transparent(self):
        calls = []

        def fail_report(*_args):
            raise RuntimeError("report failed")

        with self.assertRaisesRegex(RuntimeError, "report failed"):
            persist_pipeline_artifacts(
                **self._minimal_args(calls, write_artifact_text=fail_report)
            )

        calls = []
        with self.assertRaisesRegex(RuntimeError, "unexpected queue failure"):
            self._invoke(calls, queue=RuntimeError("unexpected queue failure"))
        self.assertEqual([call[0] for call in calls], [
            "report", "json", "build_manifest", "write_manifest", "queue",
        ])

    def _minimal_args(self, calls, **overrides):
        layout = {
            "artifact_dir": "bundle/artifacts",
            "overview_path": "bundle/overview.md",
            "output_root": "bundle",
            "slice_dir": "bundle/slices",
        }
        args = {
            "video_path": "recording.flv", "report_path": "report.md",
            "report": "report", "json_path": "marks.json", "payload": {},
            "source_srt_path": "source.srt", "corrected_srt_path": "corrected.srt",
            "clip_marks": [], "task_manifest_json_path": "task.json",
            "task_manifest_md_path": "task.md", "unified_queue_json_path": "queue.json",
            "unified_queue_md_path": "queue.md", "artifact_layout_version": 1,
            "artifact_layout": layout, "clip_review_checkpoint_path": "checkpoint.json",
            "accepted_topics": [], "clip_review_warning": None,
            "checkpoint_source": "pipeline", "clip_review_completed_at": "now",
            "write_artifact_text": lambda *_args: calls.append(("report",)),
            "write_artifact_json": lambda *_args: calls.append(("json",)),
            "build_refinement_manifest": lambda *_args: {"manifest_json_path": "task.json"},
            "write_refinement_manifest_files": lambda *_args: calls.append(("write_manifest",)),
            "upsert_unified_refinement_queue": lambda *_args, **_kwargs: calls.append(("queue",)),
            "write_completed_clip_review_checkpoint": lambda *_args, **_kwargs: calls.append(("checkpoint",)),
            "organize_existing_artifacts": lambda *_args, **_kwargs: {"overview_path": "overview"},
        }
        args.update(overrides)
        return args


if __name__ == "__main__":
    unittest.main()
