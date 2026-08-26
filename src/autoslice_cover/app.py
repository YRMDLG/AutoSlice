"""AutoCover 本地 Web API。"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import time
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from flask import Flask, after_this_request, jsonify, render_template, request, send_file
from werkzeug.exceptions import HTTPException

from autoslice.security_policy import LOOPBACK_HOSTS, SecurityPolicy

from . import API_VERSION, SERVICE_ID
from .copy_recommendations import ALLOWED_ROLES, generate_copy_recommendations
from .drafts import CoverDraftStore
from .fonts import get_default_font_status
from .paths import STATIC_DIR, TEMPLATE_DIR
from .renderer import (
    RenderResult,
    StickerOverlay,
    TextTransform,
    commit_output_transaction,
    render_cover,
)
from .stickers import DEFAULT_STICKER_ROOT, MAX_STICKER_BYTES, StickerLibrary
from .style import CANVAS_SPECS, PALETTES, TEMPLATES
from .titles import recommend_layout_variants
from .workspace import (
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
MAX_STICKER_UPLOAD_BYTES = MAX_STICKER_BYTES
HEX_COLOR_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}$")
DEPRECATION_WARNING = '299 AutoCover "Deprecated compatibility endpoint"'
SESSION_BOOTSTRAP_PATHS = frozenset({"/", "/api/security/session"})


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


def _draft_store(app: Flask) -> CoverDraftStore:
    store = app.extensions.get("cover_draft_store")
    if not isinstance(store, CoverDraftStore):
        raise ApiError("请先扫描切片目录", 409)
    return store


def _draft_settings(
    task: CoverTask,
    payload: dict[str, Any],
    preview: dict[str, Any],
    canvas_key: str,
) -> dict[str, Any]:
    """把已成功渲染的请求整理成可由前端再次读取的磁盘草稿。"""

    placements = preview.get("placements")
    if not isinstance(placements, list):
        placements = []
    copy_lines = payload.get("copy_lines")
    if not isinstance(copy_lines, list):
        copy_lines = [item.get("text", "") for item in placements if isinstance(item, dict)]
    line_colors = payload.get("line_colors")
    if not isinstance(line_colors, list):
        line_colors = [item.get("color", "#ffffff") for item in placements if isinstance(item, dict)]
    stroke_colors = payload.get("line_stroke_colors")
    if not isinstance(stroke_colors, list):
        stroke_colors = [
            item.get("stroke_color", "#111111")
            for item in placements
            if isinstance(item, dict)
        ]
    raw_layouts = payload.get("layouts")
    layouts = json.loads(json.dumps(raw_layouts if isinstance(raw_layouts, dict) else {}))
    for ratio in ("4x3", "16x9"):
        if not isinstance(layouts.get(ratio), dict):
            layouts[ratio] = {
                "text": None,
                "stickers": [],
                "focus_x": 0.5,
                "focus_y": 0.5,
                "background_scale": 1.0,
            }
    current = layouts[canvas_key]
    width = max(1.0, float(preview.get("width", 1)))
    height = max(1.0, float(preview.get("height", 1)))
    if placements:
        current["text"] = [
            {
                "x": float(item["box"][0]) / width,
                "y": float(item["box"][1]) / height,
                "scale": 1.0,
                "font_size": float(item.get("font_size", 96)),
                "center_x": False,
                "center_y": False,
            }
            for item in placements
            if isinstance(item, dict)
            and isinstance(item.get("box"), (list, tuple))
            and len(item["box"]) == 4
        ]
    line_roles = payload.get("line_roles")
    if (
        not isinstance(line_roles, list)
        or len(line_roles) != len(copy_lines)
        or any(role not in ALLOWED_ROLES for role in line_roles)
    ):
        line_roles = []
    copy_candidates = payload.get("copy_candidates")
    if not isinstance(copy_candidates, list):
        copy_candidates = []
    safe_candidates: list[dict[str, Any]] = []
    for candidate in copy_candidates[:3]:
        if not isinstance(candidate, dict):
            continue
        key = candidate.get("key")
        label = candidate.get("label")
        reason = candidate.get("reason")
        template_key = candidate.get("template_key")
        palette_key = candidate.get("palette_key")
        lines = candidate.get("lines")
        if (
            not all(isinstance(value, str) and value.strip() for value in (
                key, label, reason, template_key, palette_key
            ))
            or template_key not in TEMPLATES
            or palette_key not in PALETTES
            or not isinstance(lines, list)
        ):
            continue
        safe_lines = [
            {"text": str(line.get("text", ""))[:80], "role": line.get("role")}
            for line in lines[:MAX_COPY_LINES]
            if isinstance(line, dict)
            and isinstance(line.get("text"), str)
            and line.get("text", "").strip()
            and line.get("role") in ALLOWED_ROLES
        ]
        if safe_lines:
            safe_candidates.append({
                "key": key[:64],
                "label": label[:32],
                "reason": reason[:240],
                "template_key": template_key,
                "palette_key": palette_key,
                "lines": safe_lines,
            })
    return {
        "title": task.title,
        "template_key": task.template_key,
        "palette_key": task.palette_key,
        "copy_lines": [str(item) for item in copy_lines],
        "line_colors": [str(item) for item in line_colors],
        "line_stroke_colors": [str(item) for item in stroke_colors],
        "line_roles": [str(item) for item in line_roles],
        "copy_candidates": safe_candidates,
        "selected_copy_candidate_key": str(
            payload.get("selected_copy_candidate_key") or ""
        )[:64],
        "copy_warning": str(payload.get("copy_warning") or "")[:300],
        "auto_style": False,
        "layouts": layouts,
    }


def _load_disk_drafts(
    workspace: CoverWorkspace,
    store: CoverDraftStore,
) -> list[dict[str, Any]]:
    """恢复选中帧并把磁盘路径转换为当前进程可用的媒体令牌。"""

    tasks = {task.relative_path: task for task in workspace.list_tasks()}
    public: list[dict[str, Any]] = []
    for record in store.load_records(tasks.values()):
        task = tasks.get(record["relative_path"])
        if task is None:
            continue
        try:
            workspace.restore_saved_candidate(
                task.id,
                record["selected_frame_path"],
                timestamp=record["selected_timestamp"],
                score=record["selected_score"],
                metrics=record["selected_metrics"],
            )
        except (OSError, TypeError, ValueError):
            continue
        previews: dict[str, dict[str, Any]] = {}
        for canvas_key, saved in record["previews"].items():
            metadata = dict(saved["metadata"])
            image_path = saved["image_path"]
            background_path = saved["background_path"]
            metadata["filename"] = image_path.name
            metadata["media_token"] = workspace.media_token(image_path)
            metadata["selected_timestamp"] = saved.get("selected_timestamp")
            if background_path is not None:
                metadata["background_media_token"] = workspace.media_token(background_path)
            previews[canvas_key] = metadata
        public.append(
            {
                "relative_path": record["relative_path"],
                "updated_at": record["updated_at"],
                "selected_timestamp": record["selected_timestamp"],
                "settings": record["settings"],
                "previews": previews,
                "active": record["active"],
            }
        )
    return public


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


def _boolean_value(
    payload: dict[str, Any],
    key: str,
    *,
    default: bool = False,
) -> bool:
    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise ApiError(f"{key} 必须是布尔值")
    return value


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
            center_x=_boolean_value(item, "center_x"),
            center_y=_boolean_value(item, "center_y"),
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
                center_x=_boolean_value(item, "center_x"),
                center_y=_boolean_value(item, "center_y"),
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
        "background_scale": _number_value(
            layout,
            "background_scale",
            minimum=1.0,
            maximum=2.5,
            default=1.0,
        ),
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
    if not task.candidates and task.custom_candidate is None:
        raise ValueError("该任务尚未生成候选帧")
    candidate = task.custom_candidate or task.candidates[task.selected_index]
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
        video_path=task.video_path,
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

    app = Flask(
        __name__,
        static_folder=str(STATIC_DIR),
        template_folder=str(TEMPLATE_DIR),
    )
    app.config.from_mapping(
        JSON_AS_ASCII=False,
        MAX_CONTENT_LENGTH=MAX_STICKER_UPLOAD_BYTES,
        STICKER_DIR=str(DEFAULT_STICKER_ROOT),
        IMPORTED_STICKER_DIR=os.environ.get("AUTOCOVER_USER_ASSET_DIR"),
        AUTOSLICE_BASE_URL=os.environ.get(
            "AUTOSLICE_URL",
            "http://127.0.0.1:5002",
        ),
        COPY_RECOMMENDATION_RUNNER=None,
    )
    if test_config:
        app.config.update(test_config)
    app.extensions["cover_workspace"] = None
    app.extensions["cover_draft_store"] = None
    security_policy = SecurityPolicy(
        env_prefix="AUTOCOVER",
        cookie_name="autocover_local_session",
        access_header="X-AutoCover-Token",
    )
    app.extensions["security_policy"] = security_policy
    def effective_paths_allowed(*paths: object) -> bool:
        return security_policy.validate_effective_paths(*paths).allowed

    def public_local_path(path: object) -> str:
        return "" if security_policy.settings().lan_mode else str(path)

    sticker_library = StickerLibrary(
        app.config["STICKER_DIR"],
        import_root=app.config.get("IMPORTED_STICKER_DIR"),
        path_authorizer=security_policy.path_is_allowed,
    )
    try:
        sticker_library.scan()
    except PermissionError:
        app.logger.warning("贴图库根目录不在当前允许范围内，已 fail-closed")
    app.extensions["sticker_library"] = sticker_library

    @app.before_request
    def enforce_local_request_boundary():
        decision = security_policy.authorize_flask_request(request)
        if not decision.allowed:
            return jsonify({"ok": False, "error": decision.message}), decision.status_code
        path_decision = security_policy.validate_flask_request_paths(request)
        if not path_decision.allowed:
            return (
                jsonify({"ok": False, "error": path_decision.message}),
                path_decision.status_code,
            )
        return None

    @app.after_request
    def issue_local_browser_session(response):
        if (
            request.method == "GET"
            and request.path in SESSION_BOOTSTRAP_PATHS
            and response.status_code < 400
        ):
            security_policy.attach_session_cookie(
                response,
                scheme=request.scheme,
                host_header=request.host,
                secure=request.is_secure,
            )
        if security_policy.settings().lan_mode:
            if response.is_json:
                payload = response.get_json(silent=True)
                if payload is not None:
                    response.set_data(app.json.dumps(
                        security_policy.redact_lan_payload(payload)
                    ))
            elif response.mimetype in {"text/html", "text/plain", "text/markdown"}:
                response.set_data(security_policy.redact_lan_text(
                    response.get_data(as_text=True)
                ))
        return response

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

    @app.errorhandler(PermissionError)
    def handle_permission_error(error: PermissionError):
        return jsonify({"ok": False, "error": "资源路径不在当前允许范围内"}), 403

    @app.errorhandler(HTTPException)
    def handle_http_error(error: HTTPException):
        return jsonify({"ok": False, "error": error.description}), error.code

    @app.errorhandler(Exception)
    def handle_unexpected_error(error: Exception):
        app.logger.exception("API 请求处理失败")
        return jsonify({"ok": False, "error": "处理失败，请查看服务日志"}), 500

    @app.get("/api/security/session")
    def bootstrap_security_session():
        """建立 HttpOnly 会话，响应正文不包含令牌或 Cookie 值。"""

        mode = "lan" if security_policy.settings().lan_mode else "local"
        return jsonify({"ok": True, "mode": mode})

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
                "default_input_dir": public_local_path(DEFAULT_INPUT_DIR),
                "default_output_dir": public_local_path(DEFAULT_OUTPUT_DIR.resolve()),
                "default_font": font_status.to_public_dict(),
            }
        )

    @app.get("/api/fonts/default")
    def default_font():
        font_status = get_default_font_status()
        # 浏览器预览应尽量使用和 Pillow 导出相同的字体。濑户体未配置时，
        # 直接返回已验证可用的系统回退字体，避免 CSS 请求 404 后与导出端字体不一致。
        render_path = font_status.render_path
        if render_path is None:
            raise ApiError("本机没有可用的中文字体，请配置 AUTOCOVER_FONT_PATH", 404)
        mimetype = {
            ".otf": "font/otf",
            ".ttc": "font/collection",
            ".ttf": "font/ttf",
        }.get(render_path.suffix.casefold(), "application/octet-stream")
        return send_file(
            render_path,
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

    @app.post("/api/tasks/<task_id>/copy-variants")
    def copy_variants(task_id: str):
        workspace = _workspace(app)
        payload = _json_body()
        unknown = set(payload) - {"title"}
        if unknown:
            raise ApiError("请求体只允许包含当前任务 title，不接受 SRT 或其他本机路径")
        task = workspace.task_snapshot(task_id)
        current_title = (
            _optional_string(payload, "title", max_length=MAX_TITLE_LENGTH)
            if "title" in payload
            else task.title
        )
        contract = task.cover_contract
        result = generate_copy_recommendations(
            video_path=task.video_path,
            current_title=current_title,
            publish_title=(contract.publish_title if contract is not None else task.title),
            editorial_interest_reason=(
                contract.editorial_interest_reason if contract is not None else None
            ),
            corrected_srt_path=(
                contract.corrected_srt_path if contract is not None else None
            ),
            runner=app.config.get("COPY_RECOMMENDATION_RUNNER"),
        )
        return jsonify({"ok": True, **result.to_dict()})

    @app.get("/api/stickers")
    def stickers():
        library = _sticker_library(app)
        if not effective_paths_allowed(library.root, library.import_root):
            raise PermissionError("贴图路径不在当前允许范围内")
        if request.args.get("refresh") == "1":
            library.scan()
        return jsonify(
            {
                "ok": True,
                "assets": [asset.to_dict() for asset in library.list_assets()],
                "summary": library.summary(),
            }
        )

    @app.post("/api/stickers/import")
    def import_sticker():
        library = _sticker_library(app)
        if not effective_paths_allowed(library.import_root):
            raise PermissionError("贴图路径不在当前允许范围内")
        uploaded = request.files.get("file")
        if uploaded is None or not uploaded.filename:
            raise ApiError("请选择要导入的图片")
        raw = uploaded.stream.read(MAX_STICKER_UPLOAD_BYTES + 1)
        if len(raw) > MAX_STICKER_UPLOAD_BYTES:
            raise ApiError("导入图片不能超过 16 MB", 413)
        asset = library.import_image(uploaded.filename, raw)
        return jsonify({
            "ok": True,
            "asset": asset.to_dict(),
            "assets": [item.to_dict() for item in library.list_assets()],
            "summary": library.summary(),
        })

    @app.get("/api/stickers/<asset_id>/image")
    def sticker_image(asset_id: str):
        return send_file(_sticker_library(app).resolve(asset_id), conditional=True, max_age=3600)

    @app.get("/")
    def index():
        base_url = str(app.config.get("AUTOSLICE_BASE_URL") or "").rstrip("/")
        try:
            parsed = urlsplit(base_url)
            if (
                parsed.scheme not in {"http", "https"}
                or (parsed.hostname or "").casefold() not in LOOPBACK_HOSTS
            ):
                raise ValueError
        except ValueError:
            base_url = "http://127.0.0.1:5002"
        return render_template("index.html", autoslice_base_url=base_url)

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
        for key in (
            "title_file",
            "cache_dir",
            "output_dir",
            "manifest_json_path",
        ):
            value = payload.get(key)
            if value is not None and not isinstance(value, str):
                raise ApiError(f"{key} 必须是路径字符串")
            optional_paths[key] = value or None

        workspace = CoverWorkspace(
            root,
            title_file=optional_paths["title_file"],
            cache_dir=optional_paths["cache_dir"],
            output_dir=optional_paths["output_dir"],
            manifest_json_path=optional_paths["manifest_json_path"],
            recursive=recursive,
            path_authorizer=security_policy.path_is_allowed,
        )
        workspace.scan()
        draft_store = CoverDraftStore(workspace.root, workspace.output_dir)
        if not effective_paths_allowed(
                workspace.root,
                workspace.cache_dir,
                workspace.output_dir,
                workspace.manifest_json_path,
                draft_store.root,
                draft_store.index_path):
            raise PermissionError("工作区路径不在当前允许范围内")
        drafts = _load_disk_drafts(workspace, draft_store)
        app.extensions["cover_workspace"] = workspace
        app.extensions["cover_draft_store"] = draft_store
        return jsonify(
            {
                "ok": True,
                "tasks": workspace.all_payloads(),
                "drafts": drafts,
                "draft_path": public_local_path(draft_store.index_path),
            }
        )

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

    @app.post("/api/tasks/<task_id>/draft-active")
    def mark_draft_active(task_id: str):
        workspace = _workspace(app)
        task = workspace.get_task(task_id)
        saved = _draft_store(app).set_active(task.relative_path)
        return jsonify(
            {
                "ok": True,
                "saved": saved,
                "draft_path": public_local_path(_draft_store(app).index_path),
            }
        )

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

    @app.get("/api/tasks/<task_id>/video-metadata")
    def video_metadata(task_id: str):
        workspace = _workspace(app)
        metadata = workspace.video_metadata(task_id)
        return jsonify({
            "ok": True,
            "metadata": metadata.to_dict(),
            "task": workspace.task_payload(task_id),
        })

    @app.post("/api/tasks/<task_id>/select-timestamp")
    def select_timestamp(task_id: str):
        workspace = _workspace(app)
        payload = _json_body()
        timestamp = _number_value(
            payload,
            "timestamp",
            minimum=0.0,
            maximum=7 * 24 * 60 * 60,
        )
        workspace.select_timestamp(task_id, timestamp)
        return jsonify({"ok": True, "task": workspace.task_payload(task_id)})

    @app.post("/api/tasks/<task_id>/preview")
    def preview(task_id: str):
        workspace = _workspace(app)
        payload = _json_body()
        draft_revision = _number_value(
            payload,
            "draft_updated_at",
            minimum=0.0,
            maximum=10_000_000_000_000.0,
            default=time.time() * 1000.0,
        )
        current_task = workspace.get_task(task_id)
        if not _draft_store(app).reserve_revision(
            current_task.relative_path,
            draft_revision,
        ):
            raise ApiError("该预览请求已被更新的编辑替代", 409)
        _apply_task_edits(workspace, task_id, payload)
        task = workspace.task_snapshot(task_id)
        canvas_key = payload.get("canvas_key", "4x3")
        if not isinstance(canvas_key, str):
            raise ApiError("canvas_key 必须是字符串")
        preview_key = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:12]
        output = workspace.cache_dir / "previews" / task.id / f"{canvas_key}-{preview_key}.jpg"
        render_result = _render_task_result(
            app,
            workspace,
            task,
            canvas_key,
            output,
            payload,
            include_background=True,
        )
        result = _render_result_payload(workspace, render_result)
        draft_saved = False
        draft_warning = None
        try:
            candidate = workspace.selected_candidate(task_id)
            draft_saved = _draft_store(app).save_preview(
                workspace.task_snapshot(task_id),
                settings=_draft_settings(task, payload, result, canvas_key),
                canvas_key=canvas_key,
                preview_payload=result,
                result=render_result,
                candidate=candidate,
                revision=draft_revision,
            )
        except (OSError, TypeError, ValueError) as error:
            draft_warning = f"磁盘草稿保存失败：{error}"
            app.logger.warning(draft_warning)
        workspace.cleanup_preview_cache(
            task.id,
            preserve_paths=(
                output,
                output.with_name(f"{output.stem}-background.jpg"),
            ),
        )
        return jsonify(
            {
                "ok": True,
                "preview": result,
                "task": workspace.task_payload(task_id),
                "draft_saved": draft_saved,
                "draft_path": public_local_path(_draft_store(app).index_path),
                "draft_warning": draft_warning,
            }
        )

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
