import json
import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


_TEST_TASK_DATABASE_DIR = TemporaryDirectory(prefix="autoslice-media-test-tasks-")
_PREVIOUS_TASK_DATABASE = os.environ.get("AUTOSLICE_TASK_DB")
os.environ["AUTOSLICE_TASK_DB"] = str(
    Path(_TEST_TASK_DATABASE_DIR.name) / "tasks.sqlite3"
)

import autoslice.web.app as app_module


class MediaPreviewApiTests(unittest.TestCase):

    def setUp(self):
        app_module.app.config.update(TESTING=True)
        app_module.tasks.clear()
        self.directory = TemporaryDirectory(prefix="autoslice-media-api-")
        self.root = Path(self.directory.name) / "中文 空格 工作区"
        self.root.mkdir(parents=True)
        self.client = app_module.app.test_client()
        bootstrap = self.client.get("/api/security/session")
        self.assertEqual(bootstrap.status_code, 200)

    def tearDown(self):
        app_module.tasks.clear()
        self.directory.cleanup()

    def _create_task(
            self,
            task_id="media-task",
            *,
            clip_id="clip-1",
            clip_bytes=b"0123456789abcdef",
            task_status="done",
            task_type="topic_pipeline",
            artifact_root=None,
            clip_path=None,
            clip_extension=".mp4",
            clip_status="待精调",
            missing_clip=False,
            symlink_clip=False,
    ):
        output_root = self.root / f"{task_id} output"
        output_root.mkdir(parents=True, exist_ok=True)
        artifact_dir = (
            Path(artifact_root)
            if artifact_root is not None
            else output_root / f"{task_id}_自动切片"
        )
        slice_dir = output_root / f"{task_id}_话题切片"
        data_dir = artifact_dir / "数据"
        data_dir.mkdir(parents=True, exist_ok=True)
        slice_dir.mkdir(parents=True, exist_ok=True)
        default_clip = slice_dir / f"{clip_id}{clip_extension}"
        actual_clip = Path(clip_path) if clip_path is not None else default_clip
        if not missing_clip:
            if symlink_clip:
                outside = self.root / f"{task_id} outside.mp4"
                outside.write_bytes(clip_bytes)
                try:
                    actual_clip.parent.mkdir(parents=True, exist_ok=True)
                    actual_clip.symlink_to(outside)
                except (OSError, NotImplementedError) as exc:
                    self.skipTest(f"当前环境不支持创建符号链接：{exc}")
            else:
                actual_clip.parent.mkdir(parents=True, exist_ok=True)
                actual_clip.write_bytes(clip_bytes)
        manifest = {
            "schema_version": 1,
            "artifact_dir": str(artifact_dir),
            "slice_output_dir": str(slice_dir),
            "tasks": [{
                "id": clip_id,
                "status": clip_status,
                "clip_filename": actual_clip.name,
                "slice_path": str(actual_clip),
            }],
        }
        (data_dir / "精调任务.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        app_module.task_registry.store.create_task(
            task_id,
            task_type,
            source_path=self.root / f"{task_id} source.flv",
            output_paths=(artifact_dir, slice_dir),
            status=task_status,
            result_summary={"artifact_dir": str(artifact_dir)},
            metadata={"output_dir": str(output_root)},
        )
        return output_root, artifact_dir, slice_dir, actual_clip

    def _issue(self, task_id="media-task", clip_id="clip-1"):
        response = self.client.post(
            f"/api/tasks/{task_id}/clips/{clip_id}/media-token"
        )
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def test_token_and_short_clip_get_are_task_scoped(self):
        _, _, _, clip = self._create_task()
        issued = self._issue()
        self.assertGreaterEqual(len(issued["token"]), 32)
        self.assertEqual(issued["expires_in"], 300)
        self.assertNotIn(str(clip), issued["media_url"])

        response = self.client.get(issued["media_url"])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"0123456789abcdef")
        self.assertEqual(response.headers["Accept-Ranges"], "bytes")
        self.assertEqual(response.headers["Content-Length"], "16")
        self.assertNotIn(str(self.root), response.get_data(as_text=True))
        self.assertNotIn("Content-Disposition", response.headers)

    def test_missing_forged_expired_and_cross_binding_tokens_are_rejected(self):
        self._create_task("media-one")
        self._create_task("media-two")
        now = [time.time()]
        owner = app_module.MediaPreviewOwner(
            lambda: app_module.task_registry,
            clock=lambda: now[0],
        )
        with patch.object(app_module, "media_preview_owner", owner):
            issued = self._issue("media-one")

            for url in (
                    "/api/tasks/media-one/clips/clip-1/media",
                    "/api/tasks/media-one/clips/clip-1/media?token=forged-token",
                    "/api/tasks/media-two/clips/clip-1/media?token=" + issued["token"],
            ):
                with self.subTest(url=url):
                    response = self.client.get(url)
                    self.assertEqual(response.status_code, 404)
                    self.assertEqual(
                        response.get_json(),
                        {"error": "媒体不存在或访问令牌无效"},
                    )

            now[0] += 301
            expired = self.client.get(issued["media_url"])
        self.assertEqual(expired.status_code, 404)
        self.assertNotIn(issued["token"], expired.get_data(as_text=True))

    def test_client_paths_filenames_and_clip_id_traversal_are_rejected(self):
        self._create_task()
        absolute = str(self.root / "秘密.mp4")
        for response in (
            self.client.post(
                "/api/tasks/media-task/clips/clip-1/media-token",
                json={"path": absolute},
            ),
            self.client.post(
                "/api/tasks/media-task/clips/clip-1/media-token",
                json={"filename": "secret.mp4"},
            ),
            self.client.get(
                "/api/tasks/media-task/clips/clip-1/media",
                query_string={"path": "..\\secret.mp4", "token": "forged"},
            ),
        ):
            self.assertEqual(response.status_code, 400)
            self.assertNotIn(absolute, response.get_data(as_text=True))
        traversal = self.client.get(
            "/api/tasks/media-task/clips/..%2Fsource/media?token=forged"
        )
        self.assertIn(traversal.status_code, (404, 400))
        self.assertNotIn(str(self.root), traversal.get_data(as_text=True))

    def test_lan_rechecks_registered_media_against_current_allowed_root(self):
        self._create_task("lan-media")
        allowed_root = self.root / "different-allowed-root"
        allowed_root.mkdir()
        headers = {
            "Host": "192.168.1.20:5002",
            "X-AutoSlice-Token": "A7b9C2d4E6f8G1h3J5k7L9m2N4p6Q8r0",
        }
        with patch.dict(
                os.environ,
                {
                    "AUTOSLICE_LAN_MODE": "1",
                    "AUTOSLICE_LAN_TOKEN": headers["X-AutoSlice-Token"],
                    "AUTOSLICE_LAN_HOSTS": "192.168.1.20",
                    "AUTOSLICE_LAN_ORIGINS": "http://192.168.1.20:5002",
                    "AUTOSLICE_ALLOWED_ROOTS": str(allowed_root),
                },
                clear=False,
        ):
            response = self.client.post(
                "/api/tasks/lan-media/clips/clip-1/media-token",
                headers=headers,
            )
        self.assertEqual(response.status_code, 403)
        self.assertNotIn(str(self.root), response.get_data(as_text=True))

    def test_media_token_rejects_delete_and_recreate_same_path(self):
        _, _, _, clip = self._create_task("replaced-media")
        issued = self._issue("replaced-media")
        original = clip.read_bytes()
        clip.unlink()
        clip.write_bytes(original)
        response = self.client.get(issued["media_url"])
        self.assertEqual(response.status_code, 404)

    def test_unfinished_and_disallowed_tasks_are_rejected(self):
        self._create_task("queued-media", task_status="queued")
        self._create_task("subtitle-media", task_type="subtitle_review")

        queued = self.client.post(
            "/api/tasks/queued-media/clips/clip-1/media-token"
        )
        disallowed = self.client.post(
            "/api/tasks/subtitle-media/clips/clip-1/media-token"
        )
        self.assertEqual(queued.status_code, 409)
        self.assertEqual(disallowed.status_code, 403)
        self.assertNotIn(str(self.root), queued.get_data(as_text=True))
        self.assertNotIn(str(self.root), disallowed.get_data(as_text=True))

    def test_cross_task_artifact_and_outside_root_are_rejected(self):
        _, foreign_artifact, _, _ = self._create_task("foreign-media")
        self._create_task("owned-media", artifact_root=foreign_artifact)
        foreign = self.client.post(
            "/api/tasks/owned-media/clips/clip-1/media-token"
        )
        self.assertEqual(foreign.status_code, 403)

        outside = self.root / "outside-registered.mp4"
        outside.write_bytes(b"outside")
        _, artifact, slice_dir, _ = self._create_task("outside-media", missing_clip=True)
        manifest_path = artifact / "数据" / "精调任务.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["tasks"][0]["slice_path"] = str(outside)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        response = self.client.post(
            "/api/tasks/outside-media/clips/clip-1/media-token"
        )
        self.assertEqual(response.status_code, 403)
        self.assertNotIn(str(slice_dir), response.get_data(as_text=True))

    def test_symlink_non_media_and_missing_files_are_rejected(self):
        self._create_task("symlink-media", symlink_clip=True)
        symlink = self.client.post(
            "/api/tasks/symlink-media/clips/clip-1/media-token"
        )
        self.assertEqual(symlink.status_code, 403)

        self._create_task("text-media", clip_extension=".txt")
        text_file = self.client.post(
            "/api/tasks/text-media/clips/clip-1/media-token"
        )
        self.assertEqual(text_file.status_code, 403)

        self._create_task("missing-media", missing_clip=True)
        missing = self.client.post(
            "/api/tasks/missing-media/clips/clip-1/media-token"
        )
        self.assertEqual(missing.status_code, 404)

    def test_single_range_prefix_suffix_and_invalid_ranges(self):
        self._create_task(clip_bytes=b"0123456789")
        issued = self._issue()
        url = issued["media_url"]
        cases = (
            ("bytes=0-3", 206, b"0123", "bytes 0-3/10"),
            ("bytes=4-", 206, b"456789", "bytes 4-9/10"),
            ("bytes=-3", 206, b"789", "bytes 7-9/10"),
        )
        for range_header, status, body, content_range in cases:
            with self.subTest(range_header=range_header):
                response = self.client.get(url, headers={"Range": range_header})
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.data, body)
                self.assertEqual(response.headers["Content-Range"], content_range)
                self.assertEqual(response.headers["Content-Length"], str(len(body)))

        for range_header in ("bytes=10-", "bytes=8-2", "bytes=0-1,3-4", "items=0-1"):
            with self.subTest(range_header=range_header):
                response = self.client.get(url, headers={"Range": range_header})
                self.assertEqual(response.status_code, 416)
                self.assertEqual(response.headers["Accept-Ranges"], "bytes")
                self.assertEqual(response.headers["Content-Range"], "bytes */10")
                self.assertEqual(response.headers["Content-Length"], "0")
                self.assertEqual(response.data, b"")

    def test_head_has_same_headers_without_content_and_errors_are_redacted(self):
        self._create_task()
        issued = self._issue()
        head = self.client.head(issued["media_url"])
        get = self.client.get(issued["media_url"])
        self.assertEqual(head.status_code, 200)
        self.assertEqual(head.data, b"")
        self.assertEqual(head.headers["Content-Length"], get.headers["Content-Length"])
        self.assertEqual(head.headers["Accept-Ranges"], "bytes")

        bad = self.client.get(
            "/api/tasks/media-task/clips/clip-1/media",
            query_string={
                "token": "not-a-real-token",
                "path": str(self.root / "private.mp4"),
            },
        )
        self.assertEqual(bad.status_code, 400)
        body = bad.get_data(as_text=True)
        self.assertNotIn(str(self.root), body)
        self.assertNotIn("not-a-real-token", body)

    def test_preview_does_not_call_ffmpeg_or_other_media_processing(self):
        self._create_task()
        with patch.object(app_module.subprocess, "run") as run:
            issued = self._issue()
            response = self.client.get(issued["media_url"])
        self.assertEqual(response.status_code, 200)
        run.assert_not_called()


def tearDownModule():
    app_module.tasks.clear()
    if _PREVIOUS_TASK_DATABASE is None:
        os.environ.pop("AUTOSLICE_TASK_DB", None)
    else:
        os.environ["AUTOSLICE_TASK_DB"] = _PREVIOUS_TASK_DATABASE
    _TEST_TASK_DATABASE_DIR.cleanup()
