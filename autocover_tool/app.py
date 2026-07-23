"""AutoCover 本地 Web API。"""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from flask import Flask, after_this_request, jsonify, render_template, request, send_file
from werkzeug.exceptions import HTTPException

if __package__ == "autocover_tool":
    from .autocover import API_VERSION, SERVICE_ID
    from .autocover.fonts import get_default_font_status
    from .autocover.renderer import (
        RenderResult,
        StickerOverlay,
        TextTransform,
        commit_output_transaction,
        render_cover,
    )
    from .autocover.stickers import DEFAULT_STICKER_ROOT, StickerLibrary
    from .autocover.style import CANVAS_SPECS, PALETTES, TEMPLATES
    from .autocover.titles import recommend_layout_variants
    from .autocover.workspace import (
        DEFAULT_INPUT_DIR,
        DEFAULT_OUTPUT_DIR,
        CoverTask,
        CoverWorkspace,
    )
else:
    from autocover import API_VERSION, SERVICE_ID
    from autocover.fonts import get_default_font_status
    from autocover.renderer import (
        RenderResult,
        StickerOverlay,
        TextTransform,
        commit_output_transaction,
        render_cover,
    )
    from autocover.stickers import DEFAULT_STICKER_ROOT, StickerLibrary
    from autocover.style import CANVAS_SPECS, PALETTES, TEMPLATES
    from autocover.titles import recommend_layout_variants
    from autocover.workspace import (
        DEFAULT_INPUT_DIR,
        DEFAULT_OUTPUT_DIR,
        CoverTask,
        CoverWorkspace,
    )


MAX_TITLE_LENGTH = 500
MAX_COPY_LINES = 8
MAX_COPY_LINE_LENGTH = 120
MAX_EXPORT_TASKS = 100
MAX_TASK_ID_LENGTH = 128
HEX_COLOR_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}$")
DEPRECATION_WARNING = '299 AutoCover "Deprecated compatibility endpoint"'
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class ApiError(Exception):
    """可安全返回给前端的 API 错误。"""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _mark_deprecated_endpoint() -> None:
    """为保留兼容性的旧接口添加弃用响应头。"""

    @after_this_request
    def add_deprecation_headers(response):
        response.headers["Deprecation"] = "true"
        response.headers["Warning"] = DEPRECATION_WARNING
        return response


def _json_body() -> dict[str, Any]:
    payload = request.get_json(silent=True)
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise ApiError("请求内容必须是 JSON 对象")
    return payload


def _request_hostname() -> str:
    try:
        return (urlsplit(f"//{request.host}").hostname or "").casefold()
    except ValueError:
        return ""


def _origin_is_local() -> bool:
    origin = request.headers.get("Origin")
    if not origin:
        referer = request.headers.get("Referer")
        if not referer:
            return True
        origin = referer
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and (parsed.hostname or "").casefold() in LOCAL_HOSTS
    )


def _workspace(app: Flask) -> CoverWorkspace:
    workspace = app.extensions.get("cover_workspace")
    if not isinstance(workspace, CoverWorkspace):
        raise ApiError("请先扫描切片目录", 409)
    return workspace


def _sticker_library(app: Flask) -> StickerLibrary:
    library = app.extensions.get("sticker_library")
    if not isinstance(library, StickerLibrary):
        raise ApiError("贴图库尚未初始化", 500)
    return library


def _optional_list(
    payload: dict[str, Any],
    key: str,
    *,
    max_items: int,
    max_item_length: int,
) -> list[str] | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ApiError(f"{key} 必须是字符串数组")
    if len(value) > max_items:
        raise ApiError(f"{key} 最多包含 {max_items} 项")
    if any(not item.strip() for item in value):
        raise ApiError(f"{key} 不能包含空字符串")
    if any(len(item) > max_item_length for item in value):
        raise ApiError(f"{key} 单项最多 {max_item_length} 个字符")
    return value


def _optional_string(
    payload: dict[str, Any],
    key: str,
    *,
    max_length: int,
) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ApiError(f"{key} 必须是字符串")
    cleaned = value.strip()
    if not cleaned:
        raise ApiError(f"{key} 不能为空")
    if len(cleaned) > max_length:
        raise ApiError(f"{key} 最多 {max_length} 个字符")
    return cleaned


def _focus_value(payload: dict[str, Any], key: str, default: float) -> float:
    value = payload.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ApiError(f"{key} 必须是 0 到 1 之间的数字")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise ApiError(f"{key} 必须是 0 到 1 之间的数字")
    return number


def _number_value(
    payload: dict[str, Any],
    key: str,
    *,
    minimum: float,
    maximum: float,
    default: float | None = None,
) -> float:
    value = payload.get(key, default)
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ApiError(f"{key} 必须是 {minimum} 到 {maximum} 之间的数字")
    number = float(value)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise ApiError(f"{key} 必须是 {minimum} 到 {maximum} 之间的数字")
    return number


def _canvas_layout(payload: dict[str, Any], canvas_key: str) -> dict[str, Any]:
    layouts = payload.get("layouts")
    if layouts is None:
        return {}
    if not isinstance(layouts, dict):
        raise ApiError("layouts 必须是按封面比例组织的对象")
    layout = layouts.get(canvas_key, {})
    if not isinstance(layout, dict):
        raise ApiError(f"layouts.{canvas_key} 必须是对象")
    return layout


def _text_transforms(layout: dict[str, Any]) -> list[TextTransform] | None:
    value = layout.get("text")
    if value is None:
        return None
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ApiError("文字布局必须是对象数组")
    if len(value) > MAX_COPY_LINES:
        raise ApiError(f"文字布局最多包含 {MAX_COPY_LINES} 项")
    return [
        TextTransform(
            x=_number_value(item, "x", minimum=0.0, maximum=1.0),
            y=_number_value(item, "y", minimum=0.0, maximum=1.0),
            scale=_number_value(item, "scale", minimum=0.45, maximum=2.0, default=1.0),
            font_size=(
                None
                if item.get("font_size") is None
                else round(
                    _number_value(item, "font_size", minimum=24.0, maximum=320.0)
                )
            ),
        )
        for item in value
    ]


def _sticker_overlays(
    layout: dict[str, Any],
    library: StickerLibrary,
) -> list[StickerOverlay]:
    value = layout.get("stickers", [])
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ApiError("贴图布局必须是对象数组")
    if len(value) > 20:
        raise ApiError("单张封面最多添加 20 个贴图")
    overlays: list[StickerOverlay] = []
    for item in value:
        asset_id = item.get("asset_id")
        if not isinstance(asset_id, str) or not asset_id:
            raise ApiError("贴图 asset_id 不能为空")
        if len(asset_id) > MAX_TASK_ID_LENGTH:
            raise ApiError(f"贴图 asset_id 最多 {MAX_TASK_ID_LENGTH} 个字符")
        overlays.append(
            StickerOverlay(
                asset_id=asset_id,
                image_path=str(library.resolve(asset_id)),
                x=_number_value(item, "x", minimum=0.0, maximum=1.0),
                y=_number_value(item, "y", minimum=0.0, maximum=1.0),
                width=_number_value(item, "width", minimum=0.03, maximum=0.80),
                rotation=_number_value(
                    item,
                    "rotation",
                    minimum=-180.0,
                    maximum=180.0,
                    default=0.0,
                ),
            )
        )
    return overlays


def _render_options(
    payload: dict[str, Any],
    canvas_key: str,
    library: StickerLibrary,
) -> dict[str, Any]:
    layout = _canvas_layout(payload, canvas_key)
    fallback_focus_x = _focus_value(payload, "focus_x", 0.5)
    fallback_focus_y = _focus_value(payload, "focus_y", 0.5)
    copy_lines = _optional_list(
        payload,
        "copy_lines",
        max_items=MAX_COPY_LINES,
        max_item_length=MAX_COPY_LINE_LENGTH,
    )
    line_colors = _optional_list(
        payload,
        "line_colors",
        max_items=MAX_COPY_LINES,
        max_item_length=7,
    )
    line_stroke_colors = _optional_list(
        payload,
        "line_stroke_colors",
        max_items=MAX_COPY_LINES,
        max_item_length=7,
    )
    for key, colors in (
        ("line_colors", line_colors),
        ("line_stroke_colors", line_stroke_colors),
    ):
        if colors is None:
            continue
        if any(HEX_COLOR_PATTERN.fullmatch(color) is None for color in colors):
            raise ApiError(f"{key} 必须使用 #RRGGBB 十六进制颜色")
        if copy_lines is None or len(colors) != len(copy_lines):
            raise ApiError(f"{key} 数量必须与 copy_lines 一致")
    return {
        "copy_lines": copy_lines,
        "line_colors": line_colors,
        "line_stroke_colors": line_stroke_colors,
        "focus_x": _focus_value(layout, "focus_x", fallback_focus_x),
        "focus_y": _focus_value(layout, "focus_y", fallback_focus_y),
        "text_transforms": _text_transforms(layout),
        "stickers": _sticker_overlays(layout, library),
    }


def _apply_task_edits(workspace: CoverWorkspace, task_id: str, payload: dict[str, Any]) -> CoverTask:
    title = _optional_string(payload, "title", max_length=MAX_TITLE_LENGTH)
    template_key = _optional_string(payload, "template_key", max_length=64)
    palette_key = _optional_string(payload, "palette_key", max_length=64)
    return workspace.update_task(
        task_id,
        title=title,
        template_key=template_key,
        palette_key=palette_key,
    )


def _render_result_payload(
    workspace: CoverWorkspace,
    result: RenderResult,
) -> dict[str, Any]:
    payload = result.to_dict()
    payload.pop("output_path", None)
    background_path = payload.pop("background_path", None)
    payload["filename"] = Path(result.output_path).name
    payload["media_token"] = workspace.media_token(result.output_path)
    if isinstance(background_path, str):
        payload["background_media_token"] = workspace.media_token(background_path)
    return payload


def _render_task_result(
    app: Flask,
    workspace: CoverWorkspace,
    task: CoverTask,
    canvas_key: str,
    output_path: Path,
    payload: dict[str, Any],
    *,
    include_background: bool = False,
    options: dict[str, Any] | None = None,
) -> RenderResult:
    if canvas_key not in CANVAS_SPECS:
        raise ApiError(f"不支持的封面比例：{canvas_key}")
    if not task.candidates:
        raise ValueError("该任务尚未生成候选帧")
    candidate = task.candidates[task.selected_index]
    render_options = (
        options
        if options is not None
        else _render_options(payload, canvas_key, _sticker_library(app))
    )
    background_output = (
        output_path.with_name(f"{output_path.stem}-background.jpg")
        if include_background
        else None
    )
    return render_cover(
        candidate.path,
        task.title,
        output_path,
        canvas_key=canvas_key,
        template_key=task.template_key,
        palette_key=task.palette_key,
        background_output_path=background_output,
        **render_options,
    )


def _render_task(
    app: Flask,
    workspace: CoverWorkspace,
    task: CoverTask,
    canvas_key: str,
    output_path: Path,
    payload: dict[str, Any],
    *,
    include_background: bool = False,
) -> dict[str, Any]:
    result = _render_task_result(
        app,
        workspace,
        task,
        canvas_key,
        output_path,
        payload,
        include_background=include_background,
    )
    return _render_result_payload(workspace, result)


def _save_task(
    app: Flask,
    workspace: CoverWorkspace,
    task: CoverTask,
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    task = workspace.task_snapshot(task.id)
    canvases = payload.get("canvases", ["4x3", "16x9"])
    if not isinstance(canvases, list) or not canvases:
        raise ApiError("canvases 必须是非空数组")
    if any(not isinstance(canvas, str) or canvas not in CANVAS_SPECS for canvas in canvases):
        raise ApiError("canvases 包含不支持的封面比例")
    unique_canvases = list(dict.fromkeys(canvases))
    library = _sticker_library(app)
    jobs = [
        (
            canvas_key,
            Path(task.output_paths[canvas_key]),
            _render_options(payload, canvas_key, library),
        )
        for canvas_key in unique_canvases
    ]

    pending: list[tuple[Path, Path]] = []
    staged_paths: list[Path] = []
    rendered: list[tuple[RenderResult, Path]] = []
    try:
        for canvas_key, output, options in jobs:
            staging = output.with_name(
                f".{output.name}.{secrets.token_hex(8)}.stage.jpg"
            )
            staged_paths.append(staging)
            result = _render_task_result(
                app,
                workspace,
                task,
                canvas_key,
                staging,
                payload,
                options=options,
            )
            rendered_path = Path(result.output_path)
            pending.append((rendered_path, output))
            rendered.append((result, output))

        commit_output_transaction(pending)
        return [
            _render_result_payload(
                workspace,
                replace(result, output_path=str(output.resolve())),
            )
            for result, output in rendered
        ]
    finally:
        for staging in staged_paths:
            try:
                staging.unlink(missing_ok=True)
            except OSError:
                pass


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    """创建可测试的本地 Flask 应用。"""

    app = Flask(__name__)
    app.config.from_mapping(
        JSON_AS_ASCII=False,
        MAX_CONTENT_LENGTH=1_000_000,
        STICKER_DIR=str(DEFAULT_STICKER_ROOT),
    )
    if test_config:
        app.config.update(test_config)
    app.extensions["cover_workspace"] = None
    sticker_library = StickerLibrary(app.config["STICKER_DIR"])
    sticker_library.scan()
    app.extensions["sticker_library"] = sticker_library

    @app.before_request
    def enforce_local_request_boundary():
        if not app.config.get("ENFORCE_LOCAL_REQUESTS", True):
            return None
        if _request_hostname() not in LOCAL_HOSTS:
            return jsonify({"ok": False, "error": "拒绝不受信任的 Host"}), 403
        if request.method in WRITE_METHODS and not _origin_is_local():
            return jsonify({"ok": False, "error": "拒绝跨站写请求"}), 403
        return None

    @app.errorhandler(ApiError)
    def handle_api_error(error: ApiError):
        return jsonify({"ok": False, "error": error.message}), error.status_code

    @app.errorhandler(KeyError)
    def handle_key_error(error: KeyError):
        message = str(error.args[0]) if error.args else "请求的资源不存在"
        return jsonify({"ok": False, "error": message}), 404

    @app.errorhandler(FileNotFoundError)
    @app.errorhandler(NotADirectoryError)
    @app.errorhandler(ValueError)
    def handle_bad_request(error: Exception):
        return jsonify({"ok": False, "error": str(error)}), 400

    @app.errorhandler(HTTPException)
    def handle_http_error(error: HTTPException):
        return jsonify({"ok": False, "error": error.description}), error.code

    @app.errorhandler(Exception)
    def handle_unexpected_error(error: Exception):
        app.logger.exception("API 请求处理失败")
        return jsonify({"ok": False, "error": "处理失败，请查看服务日志"}), 500

    @app.get("/api/options")
    def options():
        font_status = get_default_font_status()
        return jsonify(
            {
                "ok": True,
                "service": SERVICE_ID,
                "api_version": API_VERSION,
                "canvases": [item.to_dict() for item in CANVAS_SPECS.values()],
                "templates": [item.to_dict() for item in TEMPLATES.values()],
                "palettes": [item.to_dict() for item in PALETTES.values()],
                "default_input_dir": str(DEFAULT_INPUT_DIR),
                "default_output_dir": str(DEFAULT_OUTPUT_DIR.resolve()),
                "default_font": font_status.to_public_dict(),
            }
        )

    @app.get("/api/fonts/default")
    def default_font():
        font_status = get_default_font_status()
        if not font_status.available or font_status.font_path is None:
            raise ApiError("本机尚未配置濑户体", 404)
        mimetype = {
            ".otf": "font/otf",
            ".ttc": "font/collection",
            ".ttf": "font/ttf",
        }.get(font_status.font_path.suffix.casefold(), "application/octet-stream")
        return send_file(
            font_status.font_path,
            mimetype=mimetype,
            conditional=True,
            max_age=86_400,
        )

    @app.post("/api/layout-variants")
    def layout_variants():
        title = _optional_string(_json_body(), "title", max_length=MAX_TITLE_LENGTH)
        if title is None:
            raise ApiError("title 不能为空")
        variants = recommend_layout_variants(title)
        return jsonify({"ok": True, "variants": [variant.to_dict() for variant in variants]})

    @app.get("/api/stickers")
    def stickers():
        library = _sticker_library(app)
        if request.args.get("refresh") == "1":
            library.scan()
        return jsonify(
            {
                "ok": True,
                "assets": [asset.to_dict() for asset in library.list_assets()],
                "summary": library.summary(),
            }
        )

    @app.get("/api/stickers/<asset_id>/image")
    def sticker_image(asset_id: str):
        return send_file(_sticker_library(app).resolve(asset_id), conditional=True, max_age=3600)

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.post("/api/workspace/scan")
    def scan_workspace():
        payload = _json_body()
        root = payload.get("root")
        if not isinstance(root, str) or not root.strip():
            raise ApiError("root 必须是有效的切片目录")
        recursive = payload.get("recursive", True)
        if not isinstance(recursive, bool):
            raise ApiError("recursive 必须是布尔值")
        optional_paths = {}
        for key in ("title_file", "cache_dir", "output_dir"):
            value = payload.get(key)
            if value is not None and not isinstance(value, str):
                raise ApiError(f"{key} 必须是路径字符串")
            optional_paths[key] = value or None

        workspace = CoverWorkspace(
            root,
            title_file=optional_paths["title_file"],
            cache_dir=optional_paths["cache_dir"],
            output_dir=optional_paths["output_dir"],
            recursive=recursive,
        )
        workspace.scan()
        app.extensions["cover_workspace"] = workspace
        return jsonify({"ok": True, "tasks": workspace.all_payloads()})

    @app.get("/api/tasks")
    def list_tasks():
        _mark_deprecated_endpoint()
        workspace = _workspace(app)
        return jsonify({"ok": True, "tasks": workspace.all_payloads()})

    @app.patch("/api/tasks/<task_id>")
    def update_task(task_id: str):
        _mark_deprecated_endpoint()
        workspace = _workspace(app)
        _apply_task_edits(workspace, task_id, _json_body())
        return jsonify({"ok": True, "task": workspace.task_payload(task_id)})

    @app.delete("/api/tasks/<task_id>")
    def remove_task(task_id: str):
        workspace = _workspace(app)
        workspace.remove_task(task_id)
        return jsonify({"ok": True, "tasks": workspace.all_payloads()})

    @app.post("/api/tasks/<task_id>/candidates")
    def generate_candidates(task_id: str):
        workspace = _workspace(app)
        payload = _json_body()
        count = payload.get("count", 12)
        force = payload.get("force", False)
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 30:
            raise ApiError("count 必须是 1 到 30 之间的整数")
        if not isinstance(force, bool):
            raise ApiError("force 必须是布尔值")
        workspace.generate_candidates(task_id, count=count, force=force)
        return jsonify({"ok": True, "task": workspace.task_payload(task_id)})

    @app.post("/api/tasks/<task_id>/select-frame")
    def select_frame(task_id: str):
        workspace = _workspace(app)
        token = _json_body().get("media_token")
        if not isinstance(token, str) or not token:
            raise ApiError("media_token 不能为空")
        workspace.select_candidate(task_id, token)
        return jsonify({"ok": True, "task": workspace.task_payload(task_id)})

    @app.post("/api/tasks/<task_id>/preview")
    def preview(task_id: str):
        workspace = _workspace(app)
        payload = _json_body()
        _apply_task_edits(workspace, task_id, payload)
        task = workspace.task_snapshot(task_id)
        canvas_key = payload.get("canvas_key", "4x3")
        if not isinstance(canvas_key, str):
            raise ApiError("canvas_key 必须是字符串")
        preview_key = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:12]
        output = workspace.cache_dir / "previews" / task.id / f"{canvas_key}-{preview_key}.jpg"
        result = _render_task(
            app,
            workspace,
            task,
            canvas_key,
            output,
            payload,
            include_background=True,
        )
        workspace.cleanup_preview_cache(
            task.id,
            preserve_paths=(
                output,
                output.with_name(f"{output.stem}-background.jpg"),
            ),
        )
        return jsonify({"ok": True, "preview": result, "task": workspace.task_payload(task_id)})

    @app.post("/api/tasks/<task_id>/save")
    def save(task_id: str):
        workspace = _workspace(app)
        payload = _json_body()
        _apply_task_edits(workspace, task_id, payload)
        task = workspace.task_snapshot(task_id)
        outputs = _save_task(app, workspace, task, payload)
        return jsonify({"ok": True, "outputs": outputs})

    @app.post("/api/export")
    def export_all():
        _mark_deprecated_endpoint()
        workspace = _workspace(app)
        payload = _json_body()
        task_ids = payload.get("task_ids")
        if task_ids is None:
            tasks = workspace.list_tasks()
        else:
            if not isinstance(task_ids, list) or any(not isinstance(item, str) for item in task_ids):
                raise ApiError("task_ids 必须是字符串数组")
            if len(task_ids) > MAX_EXPORT_TASKS:
                raise ApiError(f"一次最多导出 {MAX_EXPORT_TASKS} 个任务")
            if any(not item or len(item) > MAX_TASK_ID_LENGTH for item in task_ids):
                raise ApiError(f"task_ids 单项必须为 1 到 {MAX_TASK_ID_LENGTH} 个字符")
            tasks = [workspace.get_task(task_id) for task_id in dict.fromkeys(task_ids)]
        exported = [
            {"task_id": task.id, "outputs": _save_task(app, workspace, task, payload)}
            for task in tasks
        ]
        return jsonify({"ok": True, "count": len(exported), "tasks": exported})

    @app.get("/api/media/<token>")
    def media(token: str):
        workspace = _workspace(app)
        return send_file(workspace.resolve_media(token), conditional=True, max_age=3600)

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5010, debug=False)
