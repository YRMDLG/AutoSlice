"use strict";

const EXPECTED_SERVICE_ID = "autocover";
const EXPECTED_API_VERSION = 8;
const CENTER_SNAP_THRESHOLD_PX = 8;
const LEGACY_DEFAULT_INPUT_DIR = "output";
const COMMON_LINE_COLORS = Object.freeze([
  "#ffe34d", "#ffffff", "#f04444", "#d06e95",
  "#6850c7", "#32ddf2", "#ff8ebc", "#171717",
]);
const COMMON_STROKE_COLORS = Object.freeze(["#111111", "#ffffff"]);
const MAX_MANUAL_COPY_LINES = 8;
const DRAFT_STORAGE_KEY = "autocover.task-drafts.v1";
const ACTIVE_TASK_SESSION_KEY = "autocover.active-task.v1";
const DRAFT_STORAGE_VERSION = 1;
const MAX_STORED_DRAFTS = 120;
// 已经有可用预览时不再用整块遮罩打断编辑；首次生成时延迟显示，避免一闪而过。
const PREVIEW_LOADER_DELAY_MS = 220;
const QUEUE_SORT_KEYS = new Set([
  "folder_created_desc", "folder_created_asc", "folder_modified_desc",
  "source_modified_desc", "name_asc", "name_desc",
]);
const queueNameCollator = new Intl.Collator("zh-CN", { numeric: true, sensitivity: "base" });

const state = {
  options: null,
  tasks: [],
  activeTaskId: null,
  ratio: "4x3",
  settings: new Map(),
  workspaceConfig: null,
  previewTimer: null,
  previewLoaderTimer: null,
  recommendTimer: null,
  busyCount: 0,
  stickerAssets: [],
  stickerSummary: null,
  selectedElement: null,
  preview: null,
  interactivePreview: null,
  previewRequestId: 0,
  timelineRequestId: 0,
  elementInteractionActive: false,
  overlayObserver: null,
  activeColorLine: 0,
  syncRatios: true,
  queueSort: "folder_created_desc",
  drafts: new Map(),
  draftTimer: null,
  fileDraftPath: "",
};

const elements = {};

function byId(id) {
  return document.getElementById(id);
}

function cacheElements() {
  [
    "workbench", "workspace-summary", "open-workspace", "batch-export", "rescan",
    "task-count", "task-sort", "task-list", "preview-state", "cover-frame", "cover-preview", "cover-background-preview",
    "preview-empty", "preview-loader", "candidate-summary", "candidate-strip",
    "refresh-candidates", "active-filename", "reset-copy", "editor-controls",
    "title-input", "layout-variants", "template-select", "palette-select", "palette-preview", "copy-lines", "add-copy-line",
    "common-colors", "common-stroke-colors", "stroke-color-input", "sync-ratios",
    "cover-overlay", "refresh-stickers", "upload-sticker", "upload-sticker-file", "sticker-group", "sticker-search", "sticker-grid",
    "sticker-library-summary", "sticker-result-count",
    "focus-x", "focus-y", "focus-x-value", "focus-y-value", "background-scale", "background-scale-value",
    "frame-timeline", "timeline-range", "timeline-current", "timeline-duration", "timeline-status", "refresh-preview",
    "reset-layout", "save-current", "save-cover", "status-dot", "status-text", "status-detail", "workspace-dialog",
    "workspace-form", "root-path", "title-file", "output-path", "recursive-scan",
    "font-status", "workspace-error", "scan-submit",
  ].forEach((id) => {
    elements[id] = byId(id);
  });
}

async function api(path, options = {}) {
  const isFormData = options.body instanceof FormData;
  const response = await fetch(path, {
    headers: {
      ...(isFormData ? {} : { "Content-Type": "application/json" }),
      ...(options.headers || {}),
    },
    ...options,
  });
  const payload = await response.json().catch(() => ({ error: "服务返回了无效数据" }));
  if (!response.ok || payload.ok === false) {
    throw new Error(payload.error || `请求失败 (${response.status})`);
  }
  return payload;
}

function validateApiCompatibility(options) {
  if (
    options?.service !== EXPECTED_SERVICE_ID
    || options?.api_version !== EXPECTED_API_VERSION
  ) {
    throw new Error("服务版本过旧，请关闭旧服务后重新启动 AutoCover");
  }
}

function normalizedWindowsPath(value) {
  return String(value || "")
    .trim()
    .replaceAll("/", "\\")
    .replace(/\\+$/, "")
    .toLocaleLowerCase("zh-CN");
}

function migrateWorkspaceConfig(config) {
  const migrated = { ...(config || {}) };
  const defaultInput = state.options?.default_input_dir || "input";
  if (
    !migrated.root
    || normalizedWindowsPath(migrated.root) === normalizedWindowsPath(LEGACY_DEFAULT_INPUT_DIR)
  ) {
    migrated.root = defaultInput;
  }
  return migrated;
}

function setStatus(message, detail = "", kind = "ready") {
  elements["status-text"].textContent = message;
  elements["status-detail"].textContent = detail;
  elements["status-detail"].title = detail;
  elements["status-dot"].className = `status-dot ${kind === "ready" ? "" : kind}`.trim();
}

function setWorkspaceError(message = "") {
  const error = elements["workspace-error"];
  error.textContent = message;
  error.hidden = !message;
  elements["root-path"].setAttribute("aria-invalid", String(Boolean(message)));
}

function isBusy() {
  return state.busyCount > 0;
}

function refreshInteractionState() {
  const busy = isBusy();
  const hasTask = Boolean(activeTask());
  const editable = hasTask && !busy;
  elements.workbench.setAttribute("aria-busy", String(busy));
  elements.workbench.classList.toggle("is-busy", busy);
  elements["editor-controls"].disabled = !editable;
  elements["reset-copy"].disabled = !editable;
  elements["refresh-preview"].disabled = !editable;
  elements["save-current"].disabled = !editable;
  elements["save-cover"].disabled = !editable;
  elements["add-copy-line"].disabled = !editable
    || (taskSettings(activeTask()).copy_lines?.length || 0) >= MAX_MANUAL_COPY_LINES;
  elements["refresh-candidates"].disabled = !editable;
  elements["timeline-range"].disabled = !editable || !Number(activeTask()?.video_duration);
  elements["background-scale"].disabled = !editable;
  elements["sync-ratios"].disabled = !editable;
  elements["batch-export"].disabled = busy || state.tasks.length === 0;
  elements.rescan.disabled = busy || !state.workspaceConfig;
  elements["task-sort"].disabled = busy || state.tasks.length === 0;
  elements["open-workspace"].disabled = busy;
  elements["scan-submit"].disabled = busy;
  elements["refresh-stickers"].disabled = busy;
  elements["upload-sticker"].disabled = !editable;
  document.querySelectorAll("[data-ratio]").forEach((button) => {
    button.disabled = !editable;
  });
  [
    elements["task-list"],
    elements["candidate-strip"],
    elements["sticker-grid"],
  ].forEach((container) => {
    container?.querySelectorAll("button").forEach((button) => {
      button.disabled = busy;
    });
  });
  if (elements["cover-overlay"]) {
    elements["cover-overlay"].inert = busy;
    elements["cover-overlay"].setAttribute("aria-disabled", String(busy));
  }
  updateBackgroundPanState();
}

function finiteNumber(value, minimum, maximum, fallback) {
  const number = Number(value);
  return Number.isFinite(number) ? clamp(number, minimum, maximum) : fallback;
}

function taskDraftKey(task) {
  if (!task || !state.workspaceConfig?.root) return "";
  return `${normalizedWindowsPath(state.workspaceConfig.root)}\n${normalizedWindowsPath(task.relative_path)}`;
}

function reloadTaskDraftKey() {
  try {
    // sessionStorage 天然只属于当前标签页：刷新时保留，全新打开的标签页为空。
    // 不再依赖 PerformanceNavigationTiming；部分浏览器会把 Ctrl+F5 报告成 navigate，
    // 导致明明保存了当前任务却无法恢复预览。
    return sessionStorage.getItem(ACTIVE_TASK_SESSION_KEY) || "";
  } catch (error) {
    return "";
  }
}

function normalizedDraftTimestamp(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number < 0) return 0;
  return number < 1_000_000_000_000 ? number * 1000 : number;
}

function draftPreviewMatches(localDraft, diskDraft) {
  if (!localDraft?.settings || !diskDraft?.settings) return false;
  const localTimestamp = Number(localDraft.selected_timestamp);
  const diskTimestamp = Number(diskDraft.selected_timestamp);
  const sameTimestamp = (
    !Number.isFinite(localTimestamp) && !Number.isFinite(diskTimestamp)
  ) || (
    Number.isFinite(localTimestamp)
    && Number.isFinite(diskTimestamp)
    && Math.abs(localTimestamp - diskTimestamp) <= 0.05
  );
  if (!sameTimestamp) return false;
  return JSON.stringify(serializeTaskSettings(localDraft.settings))
    === JSON.stringify(serializeTaskSettings(diskDraft.settings));
}

function loadStoredDrafts() {
  state.drafts.clear();
  try {
    const payload = JSON.parse(localStorage.getItem(DRAFT_STORAGE_KEY) || "null");
    if (payload?.version !== DRAFT_STORAGE_VERSION || !Array.isArray(payload.items)) return;
    payload.items.slice(0, MAX_STORED_DRAFTS).forEach((item) => {
      if (Array.isArray(item) && typeof item[0] === "string" && item[1]?.settings) {
        state.drafts.set(item[0], {
          ...item[1],
          updated_at: normalizedDraftTimestamp(item[1].updated_at),
        });
      }
    });
  } catch (error) {
    console.warn("读取封面草稿失败，将使用默认设置", error);
  }
}

function mergeDiskDrafts(items, workspaceRoot) {
  if (!Array.isArray(items)) return;
  const workspacePrefix = `${normalizedWindowsPath(workspaceRoot)}\n`;
  const diskKeys = new Set();
  items.forEach((item) => {
    if (!item || typeof item.relative_path !== "string" || !item.settings) return;
    const key = `${normalizedWindowsPath(workspaceRoot)}\n${normalizedWindowsPath(item.relative_path)}`;
    diskKeys.add(key);
    const diskDraft = {
      updated_at: normalizedDraftTimestamp(item.updated_at),
      selected_timestamp: Number.isFinite(Number(item.selected_timestamp))
        ? Number(item.selected_timestamp)
        : null,
      settings: item.settings,
      previews: item.previews && typeof item.previews === "object" ? item.previews : {},
      active: Boolean(item.active),
      disk_saved: true,
    };
    const local = state.drafts.get(key);
    if (local && normalizedDraftTimestamp(local.updated_at) > diskDraft.updated_at) {
      const reusableDiskPreviews = draftPreviewMatches(local, diskDraft)
        ? diskDraft.previews
        : {};
      state.drafts.set(key, {
        ...local,
        // 浏览器在收到成功预览响应后会比服务端磁盘写入晚几毫秒更新
        // localStorage。若设置和选中帧完全相同，磁盘预览仍然有效，不能仅因
        // 时间戳较新就清空，否则每次刷新都会丢失预览并重新渲染。
        previews: reusableDiskPreviews,
        active: diskDraft.active,
        disk_saved: true,
      });
    } else {
      state.drafts.set(key, diskDraft);
    }
  });
  // 磁盘草稿不存在或因源视频身份变化而失效时，浏览器里残留的 media_token
  // 可能属于已经重启的服务进程。保留用户设置和选帧，只丢弃无法验证来源的
  // 预览，避免恢复流程提前 return 后一直停留在旧画面。
  for (const [key, draft] of state.drafts.entries()) {
    if (!key.startsWith(workspacePrefix) || diskKeys.has(key) || !draft?.previews) continue;
    state.drafts.set(key, {
      ...draft,
      previews: {},
      disk_saved: false,
    });
  }
  flushStoredDrafts();
}

function serializeTextTransforms(value) {
  if (!Array.isArray(value)) return null;
  return value.map((item) => ({
    x: finiteNumber(item?.x, 0, 1, 0),
    y: finiteNumber(item?.y, 0, 1, 0),
    scale: finiteNumber(item?.scale, 0.45, 2, 1),
    font_size: finiteNumber(item?.font_size, 24, 320, 96),
    center_x: Boolean(item?.center_x),
    center_y: Boolean(item?.center_y),
  }));
}

function serializeStickers(value) {
  if (!Array.isArray(value)) return [];
  return value.slice(0, 20).filter((item) => typeof item?.asset_id === "string").map((item) => ({
    uid: typeof item.uid === "string" ? item.uid : stickerUid(),
    asset_id: item.asset_id,
    x: finiteNumber(item.x, 0, 1, 0.5),
    y: finiteNumber(item.y, 0, 1, 0.5),
    width: finiteNumber(item.width, 0.03, 0.8, 0.18),
    rotation: finiteNumber(item.rotation, -180, 180, 0),
    center_x: Boolean(item.center_x),
    center_y: Boolean(item.center_y),
  }));
}

function serializeLayout(layout) {
  return {
    text: serializeTextTransforms(layout?.text),
    stickers: serializeStickers(layout?.stickers),
    focus_x: finiteNumber(layout?.focus_x, 0, 1, 0.5),
    focus_y: finiteNumber(layout?.focus_y, 0, 1, 0.5),
    background_scale: finiteNumber(layout?.background_scale, 1, 2.5, 1),
  };
}

function serializeTaskSettings(settings) {
  const copyLines = Array.isArray(settings.copy_lines)
    ? settings.copy_lines.slice(0, MAX_MANUAL_COPY_LINES).map((line) => String(line).slice(0, 40))
    : null;
  const serializeColors = (value) => {
    if (!Array.isArray(value) || !copyLines || value.length !== copyLines.length) return null;
    const colors = value.map(normalizeHexColor);
    return colors.every(Boolean) ? colors : null;
  };
  return {
    title: String(settings.title || "").slice(0, 180),
    template_key: String(settings.template_key || ""),
    palette_key: String(settings.palette_key || ""),
    copy_lines: copyLines,
    line_colors: serializeColors(settings.line_colors),
    line_stroke_colors: serializeColors(settings.line_stroke_colors),
    auto_style: Boolean(settings.auto_style),
    layouts: {
      "4x3": serializeLayout(settings.layouts?.["4x3"]),
      "16x9": serializeLayout(settings.layouts?.["16x9"]),
    },
  };
}

function restoreTaskSettings(task, draft) {
  const defaults = defaultSettings(task);
  const saved = draft?.settings;
  if (!saved || typeof saved !== "object") return defaults;
  const templateKeys = new Set((state.options?.templates || []).map((item) => item.key));
  const paletteKeys = new Set((state.options?.palettes || []).map((item) => item.key));
  const copyLines = Array.isArray(saved.copy_lines)
    ? saved.copy_lines.slice(0, MAX_MANUAL_COPY_LINES).map((line) => String(line).slice(0, 40))
    : null;
  const restoreColors = (value) => {
    if (!Array.isArray(value) || !copyLines || value.length !== copyLines.length) return null;
    const colors = value.map(normalizeHexColor);
    return colors.every(Boolean) ? colors : null;
  };
  const visibleCount = (copyLines || []).filter((line) => line.trim()).length;
  const restoreLayout = (value) => {
    const layout = serializeLayout(value);
    if (layout.text?.length !== visibleCount) layout.text = null;
    return layout;
  };
  return {
    ...defaults,
    title: typeof saved.title === "string" ? saved.title.slice(0, 180) : defaults.title,
    template_key: templateKeys.has(saved.template_key) ? saved.template_key : defaults.template_key,
    palette_key: paletteKeys.has(saved.palette_key) ? saved.palette_key : defaults.palette_key,
    copy_lines: copyLines,
    line_colors: restoreColors(saved.line_colors),
    line_stroke_colors: restoreColors(saved.line_stroke_colors),
    auto_style: Boolean(saved.auto_style),
    layouts: {
      "4x3": restoreLayout(saved.layouts?.["4x3"]),
      "16x9": restoreLayout(saved.layouts?.["16x9"]),
    },
  };
}

function flushStoredDrafts() {
  window.clearTimeout(state.draftTimer);
  state.draftTimer = null;
  const items = [...state.drafts.entries()]
    .sort((left, right) => Number(right[1]?.updated_at || 0) - Number(left[1]?.updated_at || 0))
    .slice(0, MAX_STORED_DRAFTS);
  state.drafts = new Map(items);
  try {
    localStorage.setItem(DRAFT_STORAGE_KEY, JSON.stringify({
      version: DRAFT_STORAGE_VERSION,
      items,
    }));
  } catch (error) {
    console.warn("保存封面草稿失败", error);
  }
}

function taskDraft(task) {
  const key = taskDraftKey(task);
  return key ? state.drafts.get(key) || null : null;
}

function persistTaskDraft(task = activeTask(), { immediate = false } = {}) {
  if (!task || !state.settings.has(task.id)) return;
  const key = taskDraftKey(task);
  if (!key) return;
  const previous = state.drafts.get(key);
  const selectedTimestamp = task.selected_timestamp === null
    || task.selected_timestamp === undefined
    ? Number.NaN
    : Number(task.selected_timestamp);
  const settings = serializeTaskSettings(state.settings.get(task.id));
  const timestamp = Number.isFinite(selectedTimestamp)
    ? selectedTimestamp
    : previous?.selected_timestamp ?? null;
  const unchanged = previous
    && JSON.stringify(previous.settings) === JSON.stringify(settings)
    && Number(previous.selected_timestamp) === Number(timestamp);
  const updatedAt = Math.max(
    Date.now(),
    normalizedDraftTimestamp(previous?.updated_at) + 1,
  );
  state.drafts.set(key, {
    ...previous,
    updated_at: updatedAt,
    selected_timestamp: timestamp,
    settings,
    previews: unchanged ? previous?.previews || {} : {},
  });
  window.clearTimeout(state.draftTimer);
  if (immediate) flushStoredDrafts();
  else state.draftTimer = window.setTimeout(flushStoredDrafts, 120);
}

function setBusy(active, message = "处理中...") {
  state.busyCount = Math.max(0, state.busyCount + (active ? 1 : -1));
  const busy = isBusy();
  refreshInteractionState();
  if (active) {
    setStatus(message, "", "busy");
  } else if (!busy && elements["status-dot"].classList.contains("busy")) {
    setStatus("就绪");
  }
}

function activeTask() {
  return state.tasks.find((task) => task.id === state.activeTaskId) || null;
}

function compareQueueTime(left, right, key, descending) {
  const leftValue = Number(left[key]) || 0;
  const rightValue = Number(right[key]) || 0;
  if (leftValue === rightValue) return 0;
  if (!leftValue) return 1;
  if (!rightValue) return -1;
  return descending ? rightValue - leftValue : leftValue - rightValue;
}

function compareTasks(left, right) {
  let result = 0;
  if (state.queueSort === "folder_created_desc" || state.queueSort === "folder_created_asc") {
    const descending = state.queueSort.endsWith("_desc");
    result = compareQueueTime(left, right, "folder_created_at", descending);
    if (!result) result = compareQueueTime(left, right, "source_created_at", descending);
  } else if (state.queueSort === "folder_modified_desc") {
    result = compareQueueTime(left, right, "folder_modified_at", true);
  } else if (state.queueSort === "source_modified_desc") {
    result = compareQueueTime(left, right, "source_modified_at", true);
  } else {
    result = queueNameCollator.compare(left.relative_path, right.relative_path);
    if (state.queueSort === "name_desc") result = -result;
  }
  return result || queueNameCollator.compare(left.relative_path, right.relative_path);
}

function sortTasks() {
  state.tasks.sort(compareTasks);
}

function replaceTask(task) {
  const index = state.tasks.findIndex((item) => item.id === task.id);
  if (index >= 0) {
    state.tasks[index] = task;
  }
}

function defaultSettings(task) {
  return {
    title: task.title,
    template_key: task.template_key,
    palette_key: task.palette_key,
    copy_lines: null,
    line_colors: null,
    line_stroke_colors: null,
    auto_style: true,
    variants: [],
    layouts: {
      "4x3": { text: null, stickers: [], focus_x: 0.5, focus_y: 0.5, background_scale: 1 },
      "16x9": { text: null, stickers: [], focus_x: 0.5, focus_y: 0.5, background_scale: 1 },
    },
  };
}

function taskSettings(task) {
  if (!state.settings.has(task.id)) {
    state.settings.set(task.id, restoreTaskSettings(task, taskDraft(task)));
  }
  return state.settings.get(task.id);
}

function ratioLayout(settings, ratio = state.ratio) {
  if (!settings.layouts[ratio]) {
    settings.layouts[ratio] = {
      text: null, stickers: [], focus_x: 0.5, focus_y: 0.5, background_scale: 1,
    };
  }
  const layout = settings.layouts[ratio];
  if (!Number.isFinite(layout.focus_x)) layout.focus_x = 0.5;
  if (!Number.isFinite(layout.focus_y)) layout.focus_y = 0.5;
  if (!Number.isFinite(layout.background_scale)) layout.background_scale = 1;
  return layout;
}

function otherRatio(ratio = state.ratio) {
  return ratio === "4x3" ? "16x9" : "4x3";
}

function synchronizeCurrentLayout(settings) {
  if (!state.syncRatios) return;
  const source = ratioLayout(settings);
  const target = ratioLayout(settings, otherRatio());
  target.text = source.text?.map((transform) => ({ ...transform })) || null;
  target.stickers = source.stickers.map((sticker) => ({ ...sticker }));
  target.focus_x = source.focus_x;
  target.focus_y = source.focus_y;
  target.background_scale = source.background_scale;
}

function syncElementTransform(settings, type, index, model) {
  if (!state.syncRatios) return;
  const target = ratioLayout(settings, otherRatio());
  if (type === "text") {
    const sourceText = ratioLayout(settings).text || [];
    target.text = sourceText.map((transform) => ({ ...transform }));
    return;
  }
  let counterpart = target.stickers.find((sticker) => sticker.uid === model.uid);
  if (!counterpart) {
    counterpart = { ...model };
    target.stickers.splice(Math.min(index, target.stickers.length), 0, counterpart);
  } else {
    Object.assign(counterpart, model);
  }
}

function clearTextLayouts(settings) {
  Object.values(settings.layouts).forEach((layout) => {
    layout.text = null;
  });
  state.selectedElement = null;
}

function visibleCopyLineIndices(settings) {
  return (settings.copy_lines || [])
    .map((line, index) => (line.trim() ? index : -1))
    .filter((index) => index >= 0);
}

function insertedTextTransform(transforms, index) {
  const before = transforms[index - 1] || null;
  const after = transforms[index] || null;
  const reference = before || after;
  const fontSize = Number.isFinite(reference?.font_size) ? reference.font_size : 96;
  const estimatedHeight = (transform) => clamp(
    (((Number.isFinite(transform?.font_size) ? transform.font_size : fontSize) * 1.14) + 16) / 1080,
    0.06,
    0.38,
  );
  const newHeight = estimatedHeight(reference);
  const gap = 0.025;
  const maximumY = Math.max(0, 1 - newHeight - 0.01);
  let y = 0.42;
  if (before && after) {
    const between = before.y + estimatedHeight(before) + gap;
    y = between + newHeight <= after.y - gap
      ? between
      : after.y - newHeight - gap;
  } else if (before) {
    const below = before.y + estimatedHeight(before) + gap;
    y = below <= maximumY ? below : before.y - newHeight - gap;
  } else if (after) {
    const above = after.y - newHeight - gap;
    y = above >= 0 ? above : after.y + estimatedHeight(after) + gap;
  }
  return {
    x: clamp(reference?.x ?? 0.12, 0, 1),
    y: clamp(y, 0, maximumY),
    scale: reference?.scale || 1,
    font_size: fontSize,
  };
}

function updateManualCopyLine(settings, index, value) {
  if (!Array.isArray(settings.copy_lines) || index < 0 || index >= settings.copy_lines.length) {
    return false;
  }
  const visibleBefore = visibleCopyLineIndices(settings);
  const wasVisible = visibleBefore.includes(index);
  settings.copy_lines[index] = value;
  const isVisible = Boolean(value.trim());
  if (wasVisible === isVisible) return true;

  const position = wasVisible
    ? visibleBefore.indexOf(index)
    : visibleBefore.filter((lineIndex) => lineIndex < index).length;
  Object.values(settings.layouts).forEach((layout) => {
    if (layout.text === null) return;
    if (!Array.isArray(layout.text) || layout.text.length !== visibleBefore.length) {
      layout.text = null;
      return;
    }
    if (isVisible) {
      layout.text.splice(position, 0, insertedTextTransform(layout.text, position));
    } else {
      layout.text.splice(position, 1);
    }
  });
  state.selectedElement = null;
  return true;
}

function paletteForSettings(settings) {
  return state.options?.palettes.find((palette) => palette.key === settings?.palette_key) || null;
}

function currentPalette() {
  const settings = activeTask() ? taskSettings(activeTask()) : null;
  return paletteForSettings(settings);
}

function paletteRoleColors(palette) {
  if (!palette) {
    return ["#ffe34d", "#60ddea", "#ff759e", "#ffffff"];
  }
  return [
    palette.context_color,
    palette.quote_color,
    palette.emphasis_color,
    palette.neutral_color,
  ];
}

function lineRoleAt(settings, index) {
  const visible = (settings.copy_lines || [])
    .map((line, lineIndex) => (line.trim() ? lineIndex : -1))
    .filter((lineIndex) => lineIndex >= 0);
  const position = visible.indexOf(index);
  if (position < 0) return "neutral";
  if (visible.length === 1 || position === visible.length - 1) return "emphasis";
  if (position === 0) return "context";
  return "quote";
}

function paletteLineColor(settings, index) {
  const palette = paletteForSettings(settings);
  const role = lineRoleAt(settings, index);
  return normalizeHexColor(palette?.[`${role}_color`]) || "#ffffff";
}

function paletteLineStrokeColor(settings, index) {
  const palette = paletteForSettings(settings);
  const role = lineRoleAt(settings, index);
  return normalizeHexColor(palette?.[`${role}_stroke_color`])
    || normalizeHexColor(palette?.stroke_color)
    || "#111111";
}

function renderOptions() {
  elements["template-select"].innerHTML = state.options.templates
    .map((template) => `<option value="${template.key}">${template.label}</option>`)
    .join("");
  elements["palette-select"].innerHTML = state.options.palettes
    .map((palette) => `<option value="${palette.key}">${palette.label}</option>`)
    .join("");
  const font = state.options.default_font;
  if (font) {
    elements["font-status"].textContent = font.available
      ? `封面字体：${font.family}`
      : font.fallback
        ? `封面字体：${font.family} · 系统回退（${font.label}未配置）`
        : `封面字体：${font.family} · 无可用字体`;
    elements["font-status"].classList.toggle("warning", !font.available);
    elements["font-status"].title = font.warning || "";
  }
}

async function loadDefaultCoverFont() {
  if (!state.options?.default_font?.fallback && !state.options?.default_font?.available) {
    return;
  }
  if (!document.fonts?.load) {
    return;
  }
  try {
    await document.fonts.load('48px "AutoCover Seto"', "音音封面");
  } catch (error) {
    console.warn("濑户体网页预览加载失败，将使用浏览器回退字体", error);
  }
}

function renderLayoutVariants(settings) {
  const variants = settings?.variants || [];
  if (!variants.length) {
    elements["layout-variants"].innerHTML = '<div class="candidate-empty compact">正在识别...</div>';
    return;
  }
  elements["layout-variants"].innerHTML = variants.map((variant, index) => {
    const active = settings.template_key === variant.template_key
      && settings.palette_key === variant.palette_key;
    const sample = variant.lines.map((line) => line.text).join(" / ");
    return `
      <button class="variant-button ${active ? "active" : ""}" type="button" data-variant-index="${index}" title="${escapeHtml(variant.reason)}">
        <strong>${escapeHtml(variant.label)}</strong>
        <span>${escapeHtml(sample)}</span>
      </button>
    `;
  }).join("");
  elements["layout-variants"].querySelectorAll("[data-variant-index]").forEach((button) => {
    button.addEventListener("click", () => applyLayoutVariant(Number(button.dataset.variantIndex)));
  });
}

async function loadLayoutVariants(task, { applyRecommended = false } = {}) {
  if (!task) return;
  const settings = taskSettings(task);
  const titleAtRequest = settings.title;
  if (!titleAtRequest.trim()) return false;
  const payload = await api("/api/layout-variants", {
    method: "POST",
    body: JSON.stringify({ title: titleAtRequest }),
  });
  if (settings.title !== titleAtRequest) return false;
  settings.variants = payload.variants;
  if (applyRecommended && payload.variants.length) {
    const variant = payload.variants[0];
    settings.template_key = variant.template_key;
    settings.palette_key = variant.palette_key;
    settings.copy_lines = variant.lines.map((line) => line.text);
    settings.line_colors = null;
    settings.line_stroke_colors = null;
    clearTextLayouts(settings);
  }
  if (state.activeTaskId === task.id) {
    renderInspector(task);
  }
  renderTaskList();
  persistTaskDraft(task);
  return true;
}

function scheduleLayoutVariants(task) {
  window.clearTimeout(state.recommendTimer);
  if (!task) return;
  const settings = taskSettings(task);
  persistTaskDraft(task);
  if (!settings.title.trim()) {
    settings.variants = [];
    renderLayoutVariants(settings);
    setStatus("标题不能为空", "输入标题后会重新推荐排版", "error");
    return;
  }
  state.recommendTimer = window.setTimeout(async () => {
    try {
      const updated = await loadLayoutVariants(task, {
        applyRecommended: taskSettings(task).auto_style,
      });
      if (updated && state.activeTaskId === task.id) {
        await refreshPreview();
      }
    } catch (error) {
      setStatus("排版识别失败", error.message, "error");
    }
  }, 480);
}

function applyLayoutVariant(index) {
  const task = activeTask();
  if (!task) return;
  const settings = taskSettings(task);
  const variant = settings.variants[index];
  if (!variant) return;
  settings.auto_style = false;
  settings.template_key = variant.template_key;
  settings.palette_key = variant.palette_key;
  settings.copy_lines = variant.lines.map((line) => line.text);
  settings.line_colors = null;
  settings.line_stroke_colors = null;
  clearTextLayouts(settings);
  renderInspector(task);
  schedulePreview();
}

function renderTaskList() {
  elements["task-count"].textContent = `${state.tasks.length} 项`;
  if (!state.tasks.length) {
    elements["task-list"].innerHTML = '<div class="empty-list">目录中没有可用视频</div>';
    refreshInteractionState();
    return;
  }
  elements["task-list"].innerHTML = state.tasks.map((task, index) => `
    <div class="task-item ${task.id === state.activeTaskId ? "active" : ""}" data-task-row="${task.id}">
      <button class="task-select" type="button" data-task-id="${task.id}" title="${escapeHtml(`${taskSettings(task).title}\n${task.relative_path}`)}">
        <span class="task-index">${String(index + 1).padStart(2, "0")}</span>
        <span class="task-copy">
          <span class="task-title">${escapeHtml(taskSettings(task).title)}</span>
          <span class="task-file">${escapeHtml(task.relative_path)}</span>
        </span>
        <span class="task-status ${task.status}" title="${escapeHtml(task.error || task.status)}"></span>
      </button>
      <button class="task-remove" type="button" data-remove-task-id="${task.id}" title="从本次队列移除，不删除源视频" aria-label="从队列移除 ${escapeHtml(taskSettings(task).title)}">×</button>
    </div>
  `).join("");
  elements["task-list"].querySelectorAll("[data-task-id]").forEach((button) => {
    button.addEventListener("click", () => selectTask(button.dataset.taskId));
  });
  elements["task-list"].querySelectorAll("[data-remove-task-id]").forEach((button) => {
    button.addEventListener("click", () => removeTaskFromQueue(button.dataset.removeTaskId));
  });
  refreshInteractionState();
}

function escapeHtml(value) {
  const node = document.createElement("div");
  node.textContent = String(value ?? "");
  return node.innerHTML;
}

function formatTimestamp(seconds) {
  const minutes = Math.floor(seconds / 60);
  const remain = Math.floor(seconds % 60);
  return `${minutes}:${String(remain).padStart(2, "0")}`;
}

function formatTimelineTimestamp(seconds) {
  const safe = Math.max(0, Number(seconds) || 0);
  const hours = Math.floor(safe / 3600);
  const minutes = Math.floor((safe % 3600) / 60);
  const remain = (safe % 60).toFixed(1).padStart(4, "0");
  return hours
    ? `${hours}:${String(minutes).padStart(2, "0")}:${remain}`
    : `${minutes}:${remain}`;
}

function renderTimeline(task) {
  const duration = Math.max(0, Number(task?.video_duration) || 0);
  const selected = clamp(
    Number(task?.selected_timestamp) || 0,
    0,
    duration || 1,
  );
  elements["timeline-range"].max = duration ? duration.toFixed(3) : "1";
  elements["timeline-range"].value = selected.toFixed(3);
  elements["timeline-current"].textContent = formatTimelineTimestamp(selected);
  elements["timeline-duration"].textContent = formatTimelineTimestamp(duration);
  elements["timeline-status"].textContent = duration
    ? "拖动后松开即可提取这一帧"
    : "正在读取视频时长…";
  refreshInteractionState();
}

function renderCandidates(task) {
  const candidates = task?.candidates || [];
  elements["candidate-summary"].textContent = `${candidates.length} 张`;
  if (!candidates.length) {
    elements["candidate-strip"].innerHTML = '<div class="candidate-empty">暂无候选帧</div>';
    refreshInteractionState();
    return;
  }
  elements["candidate-strip"].innerHTML = candidates.map((candidate) => `
    <button class="candidate-button ${candidate.selected ? "selected" : ""}" type="button" data-media-token="${candidate.token}" title="${formatTimestamp(candidate.timestamp)}，评分 ${candidate.score}">
      <img src="/api/media/${candidate.token}" alt="${formatTimestamp(candidate.timestamp)} 候选帧" loading="lazy">
      <span class="candidate-meta"><span>${formatTimestamp(candidate.timestamp)}</span><span>${Math.round(candidate.score)}</span></span>
    </button>
  `).join("");
  elements["candidate-strip"].querySelectorAll("[data-media-token]").forEach((button) => {
    button.addEventListener("click", () => selectCandidate(button.dataset.mediaToken));
  });
  refreshInteractionState();
}

async function ensureTimeline(task) {
  if (!task) return;
  if (Number(task.video_duration) > 0) {
    renderTimeline(task);
    return;
  }
  const requestId = ++state.timelineRequestId;
  renderTimeline(task);
  try {
    const payload = await api(`/api/tasks/${task.id}/video-metadata`);
    if (requestId !== state.timelineRequestId || state.activeTaskId !== task.id) return;
    replaceTask(payload.task);
    renderTimeline(payload.task);
  } catch (error) {
    if (requestId !== state.timelineRequestId || state.activeTaskId !== task.id) return;
    elements["timeline-status"].textContent = "视频时间轴读取失败";
    setStatus("时间轴载入失败", error.message, "error");
  }
}

async function selectTimelineTimestamp(timestamp) {
  const task = activeTask();
  if (!task || isBusy() || !Number(task.video_duration)) return;
  const value = clamp(Number(timestamp) || 0, 0, Number(task.video_duration));
  setBusy(true, `正在提取 ${formatTimelineTimestamp(value)} 的画面...`);
  try {
    const payload = await api(`/api/tasks/${task.id}/select-timestamp`, {
      method: "POST",
      body: JSON.stringify({ timestamp: value }),
    });
    replaceTask(payload.task);
    renderCandidates(payload.task);
    renderTimeline(payload.task);
    await refreshPreview();
  } catch (error) {
    renderTimeline(activeTask());
    setStatus("精确选帧失败", error.message, "error");
  } finally {
    setBusy(false);
  }
}

function renderPalette() {
  const colors = paletteRoleColors(currentPalette());
  elements["palette-preview"].innerHTML = colors
    .map((color) => `<span class="palette-swatch" style="background:${color}" title="${color}"></span>`)
    .join("");
}

function stickerAsset(assetId) {
  return state.stickerAssets.find((asset) => asset.id === assetId) || null;
}

function filterStickerAssets(assets, selectedGroup, query) {
  const normalizedQuery = String(query || "").trim().toLocaleLowerCase("zh-CN");
  return assets.filter((asset) => {
    const groupMatches = !selectedGroup || asset.group === selectedGroup;
    const searchable = `${asset.name} ${asset.group} ${asset.relative_path || ""}`
      .toLocaleLowerCase("zh-CN");
    return groupMatches && (!normalizedQuery || searchable.includes(normalizedQuery));
  });
}

function renderStickerLibrary() {
  const selectedGroup = elements["sticker-group"].value;
  const query = elements["sticker-search"].value;
  const assets = filterStickerAssets(state.stickerAssets, selectedGroup, query);
  elements["sticker-result-count"].textContent = `当前 ${assets.length} 张`;
  if (!assets.length) {
    let message = "没有匹配贴图";
    if (state.stickerSummary?.available === false) {
      message = "未找到表情包目录，请检查默认目录或 AUTOCOVER_STICKER_DIR";
    } else if (!state.stickerAssets.length) {
      message = "表情包目录中没有可用的 PNG、JPEG 或 WebP 图片";
    }
    elements["sticker-grid"].innerHTML = `<div class="candidate-empty compact">${message}</div>`;
    refreshInteractionState();
    return;
  }
  elements["sticker-grid"].innerHTML = assets.map((asset) => `
    <button class="sticker-button" type="button" data-sticker-id="${asset.id}" title="${escapeHtml(asset.relative_path || `${asset.group}/${asset.name}`)}">
      <img src="/api/stickers/${asset.id}/image" alt="${escapeHtml(asset.name)}" loading="lazy">
      <span>${escapeHtml(asset.name)}</span>
    </button>
  `).join("");
  elements["sticker-grid"].querySelectorAll("[data-sticker-id]").forEach((button) => {
    button.addEventListener("click", () => addSticker(button.dataset.stickerId));
  });
  refreshInteractionState();
}

async function loadStickers(refresh = false) {
  const payload = await api(`/api/stickers${refresh ? "?refresh=1" : ""}`);
  state.stickerAssets = payload.assets;
  const fallbackCounts = new Map();
  payload.assets.forEach((asset) => {
    fallbackCounts.set(asset.group, (fallbackCounts.get(asset.group) || 0) + 1);
  });
  state.stickerSummary = payload.summary || {
    available: true,
    asset_count: payload.assets.length,
    group_count: fallbackCounts.size,
    invalid_count: 0,
    groups: [...fallbackCounts].map(([name, count]) => ({ name, count })),
  };
  const previous = elements["sticker-group"].value;
  const groups = state.stickerSummary.groups || [];
  const groupSelect = elements["sticker-group"];
  groupSelect.replaceChildren();
  const allOption = document.createElement("option");
  allOption.value = "";
  allOption.textContent = `全部主播（${state.stickerSummary.asset_count}）`;
  groupSelect.append(allOption);
  groups.forEach(({ name, count }) => {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = `${name}（${count}）`;
    groupSelect.append(option);
  });
  if (groups.some((group) => group.name === previous)) {
    elements["sticker-group"].value = previous;
  }
  const invalidSuffix = state.stickerSummary.invalid_count
    ? ` · 跳过 ${state.stickerSummary.invalid_count} 张损坏图片`
    : "";
  elements["sticker-library-summary"].textContent = state.stickerSummary.available
    ? `${state.stickerSummary.group_count} 位主播 · ${state.stickerSummary.asset_count} 张${invalidSuffix}`
    : "未找到表情包目录";
  renderStickerLibrary();
}

async function importLocalSticker(file) {
  const task = activeTask();
  if (!task || !file || isBusy()) return;
  const form = new FormData();
  form.append("file", file, file.name);
  let importedAssetId = null;
  setBusy(true, `正在导入 ${file.name}...`);
  try {
    const payload = await api("/api/stickers/import", {
      method: "POST",
      body: form,
    });
    state.stickerAssets = payload.assets || [];
    state.stickerSummary = payload.summary || null;
    await loadStickers();
    importedAssetId = payload.asset.id;
    setStatus("本地图片已加入封面", file.name);
  } catch (error) {
    setStatus("图片导入失败", error.message, "error");
  } finally {
    elements["upload-sticker-file"].value = "";
    setBusy(false);
  }
  if (importedAssetId) addSticker(importedAssetId);
}

function stickerUid() {
  if (globalThis.crypto?.randomUUID) {
    return globalThis.crypto.randomUUID();
  }
  return `sticker-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function defaultStickerTransform(index) {
  return {
    x: 0.72 - (index % 3) * 0.045,
    y: 0.62 - (Math.floor(index / 3) % 3) * 0.05,
    width: 0.18,
    rotation: 0,
  };
}

function addSticker(assetId) {
  const task = activeTask();
  if (isBusy() || !task || !stickerAsset(assetId)) return;
  const settings = taskSettings(task);
  const uid = stickerUid();
  const layout = ratioLayout(settings);
  const sticker = {
    uid,
    asset_id: assetId,
    ...defaultStickerTransform(layout.stickers.length),
  };
  layout.stickers.push(sticker);
  if (state.syncRatios) {
    ratioLayout(settings, otherRatio()).stickers.push({ ...sticker });
  }
  const index = layout.stickers.length - 1;
  state.selectedElement = { type: "sticker", index, uid };
  refreshPreview().catch((error) => console.error("添加贴图失败", error));
}

function deleteSticker(uid) {
  const task = activeTask();
  if (isBusy() || !task) return;
  const settings = taskSettings(task);
  const layouts = state.syncRatios
    ? Object.values(settings.layouts)
    : [ratioLayout(settings)];
  layouts.forEach((layout) => {
    layout.stickers = layout.stickers.filter((item) => item.uid !== uid);
  });
  state.selectedElement = null;
  refreshPreview().catch((error) => console.error("删除贴图失败", error));
}

function normalizeHexColor(value) {
  const digits = String(value || "").trim().replace(/^#/, "");
  return /^[0-9a-f]{6}$/i.test(digits) ? `#${digits.toLocaleLowerCase("en-US")}` : null;
}

function lineColorAt(settings, index) {
  return normalizeHexColor(settings.line_colors?.[index])
    || paletteLineColor(settings, index);
}

function lineStrokeColorAt(settings, index) {
  return normalizeHexColor(settings.line_stroke_colors?.[index])
    || paletteLineStrokeColor(settings, index);
}

function setLineColor(settings, index, color) {
  const normalized = normalizeHexColor(color);
  if (!normalized) return false;
  if (!settings.line_colors) {
    settings.line_colors = (settings.copy_lines || []).map((_, lineIndex) => (
      lineColorAt(settings, lineIndex)
    ));
  }
  settings.line_colors[index] = normalized;
  return true;
}

function setLineStrokeColor(settings, index, color) {
  const normalized = normalizeHexColor(color);
  if (!normalized) return false;
  if (!settings.line_stroke_colors) {
    settings.line_stroke_colors = (settings.copy_lines || []).map((_, lineIndex) => (
      lineStrokeColorAt(settings, lineIndex)
    ));
  }
  settings.line_stroke_colors[index] = normalized;
  return true;
}

function seedCopyLines(settings) {
  if (Array.isArray(settings.copy_lines)) return;
  const placements = Array.isArray(state.preview?.placements) ? state.preview.placements : [];
  if (placements.length) {
    settings.copy_lines = placements.map((placement) => placement.text);
    settings.line_colors = placements.map((placement) => placement.color);
    settings.line_stroke_colors = placements.map((placement) => placement.stroke_color);
    return;
  }
  settings.copy_lines = [];
  settings.line_colors = null;
  settings.line_stroke_colors = null;
}

function appendManualCopyLine(settings) {
  seedCopyLines(settings);
  if (settings.copy_lines.length >= MAX_MANUAL_COPY_LINES) return -1;
  const index = settings.copy_lines.length;
  const palette = paletteForSettings(settings);
  settings.copy_lines.push("");
  if (Array.isArray(settings.line_colors)) {
    settings.line_colors.push(
      normalizeHexColor(palette?.emphasis_color) || paletteLineColor(settings, index),
    );
  }
  if (Array.isArray(settings.line_stroke_colors)) {
    settings.line_stroke_colors.push(
      normalizeHexColor(palette?.emphasis_stroke_color)
      || normalizeHexColor(palette?.stroke_color)
      || paletteLineStrokeColor(settings, index),
    );
  }
  return index;
}

function removeManualCopyLine(settings, index) {
  if (!Array.isArray(settings.copy_lines) || index < 0 || index >= settings.copy_lines.length) {
    return false;
  }
  const visibleBefore = visibleCopyLineIndices(settings);
  const visiblePosition = visibleBefore.indexOf(index);
  Object.values(settings.layouts).forEach((layout) => {
    if (layout.text === null || visiblePosition < 0) return;
    if (!Array.isArray(layout.text) || layout.text.length !== visibleBefore.length) {
      layout.text = null;
      return;
    }
    layout.text.splice(visiblePosition, 1);
  });
  settings.copy_lines.splice(index, 1);
  if (Array.isArray(settings.line_colors)) settings.line_colors.splice(index, 1);
  if (Array.isArray(settings.line_stroke_colors)) {
    settings.line_stroke_colors.splice(index, 1);
  }
  if (!settings.copy_lines.length) {
    settings.copy_lines = null;
    settings.line_colors = null;
    settings.line_stroke_colors = null;
  }
  state.selectedElement = null;
  return true;
}

function updateAddCopyLineButton(settings) {
  const count = Array.isArray(settings?.copy_lines) ? settings.copy_lines.length : 0;
  const atLimit = count >= MAX_MANUAL_COPY_LINES;
  elements["add-copy-line"].disabled = isBusy() || !activeTask() || atLimit;
  elements["add-copy-line"].title = atLimit
    ? `手动文案最多 ${MAX_MANUAL_COPY_LINES} 行`
    : `添加一行手动文案（${count}/${MAX_MANUAL_COPY_LINES}）`;
}

function addManualCopyLine() {
  const task = activeTask();
  if (isBusy() || !task) return;
  const settings = taskSettings(task);
  const index = appendManualCopyLine(settings);
  if (index < 0) {
    setStatus("无法添加文字", `手动文案最多 ${MAX_MANUAL_COPY_LINES} 行`, "error");
    return;
  }
  settings.auto_style = false;
  state.activeColorLine = index;
  persistTaskDraft(task);
  renderCopyLines(settings);
  elements["copy-lines"].querySelector(`[data-line-text="${index}"]`)?.focus();
  setStatus("已添加文字行", `第 ${index + 1} 行，可直接输入并在预览中拖动`);
}

function deleteManualCopyLine(index) {
  const task = activeTask();
  if (isBusy() || !task) return;
  const settings = taskSettings(task);
  if (!removeManualCopyLine(settings, index)) return;
  settings.auto_style = false;
  const count = settings.copy_lines?.length || 0;
  state.activeColorLine = count ? clamp(index, 0, count - 1) : 0;
  renderCopyLines(settings);
  schedulePreview();
}

function renderCommonColors(settings) {
  if (!settings?.copy_lines?.length) {
    elements["common-colors"].replaceChildren();
    elements["common-stroke-colors"].replaceChildren();
    elements["stroke-color-input"].value = "";
    elements["stroke-color-input"].disabled = true;
    return;
  }
  elements["stroke-color-input"].disabled = isBusy();
  state.activeColorLine = clamp(state.activeColorLine, 0, settings.copy_lines.length - 1);
  const selectedColor = lineColorAt(settings, state.activeColorLine);
  const selectedStrokeColor = lineStrokeColorAt(settings, state.activeColorLine);
  elements["common-colors"].innerHTML = COMMON_LINE_COLORS.map((color) => `
    <button
      class="color-preset ${selectedColor === color ? "active" : ""}"
      type="button"
      data-common-color="${color}"
      style="--preset-color:${color}"
      title="${color}"
      aria-label="把第 ${state.activeColorLine + 1} 行改为 ${color}"
    ></button>
  `).join("");
  elements["common-colors"].querySelectorAll("[data-common-color]").forEach((button) => {
    button.addEventListener("click", () => {
      if (!setLineColor(settings, state.activeColorLine, button.dataset.commonColor)) return;
      renderCopyLines(settings);
      schedulePreview();
    });
  });
  elements["common-stroke-colors"].innerHTML = COMMON_STROKE_COLORS.map((color) => `
    <button
      class="color-preset ${selectedStrokeColor === color ? "active" : ""}"
      type="button"
      data-common-stroke-color="${color}"
      style="--preset-color:${color}"
      title="描边 ${color}"
      aria-label="把第 ${state.activeColorLine + 1} 行描边改为 ${color}"
    ></button>
  `).join("");
  elements["common-stroke-colors"].querySelectorAll("[data-common-stroke-color]").forEach((button) => {
    button.addEventListener("click", () => {
      if (!setLineStrokeColor(settings, state.activeColorLine, button.dataset.commonStrokeColor)) return;
      renderCopyLines(settings);
      schedulePreview();
    });
  });
  elements["stroke-color-input"].value = selectedStrokeColor.slice(1);
  elements["stroke-color-input"].setAttribute("aria-invalid", "false");
}

function selectColorLine(index, settings) {
  state.activeColorLine = index;
  elements["copy-lines"].querySelectorAll("[data-copy-line]").forEach((row) => {
    row.classList.toggle("active-color-line", Number(row.dataset.copyLine) === index);
  });
  renderCommonColors(settings);
}

function renderCopyLines(settings) {
  const lines = settings.copy_lines || [];
  updateAddCopyLineButton(settings);
  if (!lines.length) {
    elements["copy-lines"].innerHTML = '<div class="candidate-empty">添加手动文字，或预览后使用自动文案</div>';
    renderCommonColors(settings);
    return;
  }
  state.activeColorLine = clamp(state.activeColorLine, 0, lines.length - 1);
  elements["copy-lines"].innerHTML = lines.map((line, index) => {
    const color = lineColorAt(settings, index);
    const strokeColor = lineStrokeColorAt(settings, index);
    return `
      <div class="copy-line ${index === state.activeColorLine ? "active-color-line" : ""}" data-copy-line="${index}">
        <button class="line-color-preview" type="button" data-select-color-line="${index}" style="--line-color:${color};--stroke-color:${strokeColor}" title="文字 ${color}，描边 ${strokeColor}" aria-label="选择第 ${index + 1} 行颜色"></button>
        <label class="hex-color-input">
          <span>#</span>
          <input type="text" value="${color.slice(1)}" data-line-color="${index}" maxlength="7" spellcheck="false" aria-label="第 ${index + 1} 行十六进制颜色">
        </label>
        <input type="text" value="${escapeHtml(line)}" data-line-text="${index}" maxlength="40" aria-label="第 ${index + 1} 行文案">
        <button class="copy-line-remove" type="button" data-remove-copy-line="${index}" title="删除第 ${index + 1} 行文案" aria-label="删除第 ${index + 1} 行文案">×</button>
      </div>
    `;
  }).join("");
  elements["copy-lines"].querySelectorAll("[data-select-color-line]").forEach((button) => {
    button.addEventListener("click", () => {
      selectColorLine(Number(button.dataset.selectColorLine), settings);
      elements["copy-lines"].querySelector(`[data-line-color="${button.dataset.selectColorLine}"]`)?.focus();
    });
  });
  elements["copy-lines"].querySelectorAll("[data-line-text]").forEach((input) => {
    input.addEventListener("focus", () => {
      selectColorLine(Number(input.dataset.lineText), settings);
    });
    input.addEventListener("input", () => {
      const current = taskSettings(activeTask());
      const index = Number(input.dataset.lineText);
      updateManualCopyLine(current, index, input.value);
      current.auto_style = false;
      schedulePreview();
    });
  });
  elements["copy-lines"].querySelectorAll("[data-remove-copy-line]").forEach((button) => {
    button.addEventListener("click", () => {
      deleteManualCopyLine(Number(button.dataset.removeCopyLine));
    });
  });
  elements["copy-lines"].querySelectorAll("[data-line-color]").forEach((input) => {
    input.addEventListener("focus", () => {
      selectColorLine(Number(input.dataset.lineColor), settings);
    });
    input.addEventListener("input", () => {
      const current = taskSettings(activeTask());
      const index = Number(input.dataset.lineColor);
      const color = normalizeHexColor(input.value);
      input.setAttribute("aria-invalid", String(!color));
      if (!color || !setLineColor(current, index, color)) return;
      const preview = input.closest("[data-copy-line]")?.querySelector(".line-color-preview");
      preview?.style.setProperty("--line-color", color);
      renderCommonColors(current);
      schedulePreview();
    });
    input.addEventListener("blur", () => {
      const current = taskSettings(activeTask());
      const index = Number(input.dataset.lineColor);
      input.value = (normalizeHexColor(input.value) || lineColorAt(current, index)).slice(1);
      input.setAttribute("aria-invalid", "false");
    });
  });
  renderCommonColors(settings);
}

function renderInspector(task) {
  elements["save-current"].textContent = `导出当前 ${state.ratio === "4x3" ? "4:3" : "16:9"}`;
  elements["active-filename"].textContent = task?.filename || "未选择切片";
  elements["active-filename"].title = task?.filename || "";
  refreshInteractionState();
  if (!task) {
    return;
  }
  const settings = taskSettings(task);
  const layout = ratioLayout(settings);
  elements["title-input"].value = settings.title;
  elements["template-select"].value = settings.template_key;
  elements["palette-select"].value = settings.palette_key;
  elements["focus-x"].value = Math.round(layout.focus_x * 100);
  elements["focus-y"].value = Math.round(layout.focus_y * 100);
  elements["background-scale"].value = Math.round(layout.background_scale * 100);
  updateFocusLabels();
  renderLayoutVariants(settings);
  renderPalette();
  renderCopyLines(settings);
}

function updateFocusLabels() {
  elements["focus-x-value"].textContent = `${elements["focus-x"].value}%`;
  elements["focus-y-value"].textContent = `${elements["focus-y"].value}%`;
  elements["background-scale-value"].textContent = `${elements["background-scale"].value}%`;
}

async function scanWorkspace(
  config,
  { preserveDialog = false, autoSelect = false, restoreTaskKey = "" } = {},
) {
  setBusy(true, "正在扫描切片目录...");
  setWorkspaceError();
  let payload;
  try {
    window.clearTimeout(state.previewTimer);
    window.clearTimeout(state.recommendTimer);
    state.previewRequestId += 1;
    payload = await api("/api/workspace/scan", {
      method: "POST",
      body: JSON.stringify(config),
    });
  } catch (error) {
    setStatus("目录扫描失败", error.message, "error");
    if (elements["workspace-dialog"].open) {
      setWorkspaceError(error.message);
      elements["root-path"].focus();
    }
    throw error;
  } finally {
    setBusy(false);
  }

  state.workspaceConfig = config;
  state.tasks = payload.tasks;
  state.fileDraftPath = typeof payload.draft_path === "string" ? payload.draft_path : "";
  mergeDiskDrafts(payload.drafts, config.root);
  sortTasks();
  state.settings.clear();
  // 启动时只扫描并展示队列，不主动选中第一个视频。候选帧提取和预览
  // 会读取/解码整段媒体，必须等用户明确点击某个切片后再开始，避免打开页面就卡住。
  state.activeTaskId = autoSelect ? state.tasks[0]?.id || null : null;
  localStorage.setItem("autocover.workspace", JSON.stringify(config));
  elements["workspace-summary"].textContent = config.root;
  elements["workspace-summary"].title = config.root;
  renderTaskList();
  renderInspector(activeTask());
  renderCandidates(activeTask());
  renderTimeline(activeTask());
  const diskActive = Array.isArray(payload.drafts)
    ? payload.drafts.find((item) => item?.active && item?.previews)
    : null;
  const localActive = [...state.drafts.entries()].find(([key, item]) => (
    key.startsWith(`${normalizedWindowsPath(config.root)}\n`)
    && item?.active
  ));
  const effectiveRestoreKey = restoreTaskKey || (
    diskActive
      ? `${normalizedWindowsPath(config.root)}\n${normalizedWindowsPath(diskActive.relative_path)}`
      : localActive?.[0] || ""
  );
  const restoredTask = effectiveRestoreKey
    ? state.tasks.find((task) => taskDraftKey(task) === effectiveRestoreKey) || null
    : null;
  if (!autoSelect && restoredTask) {
    if (!preserveDialog && elements["workspace-dialog"].open) {
      elements["workspace-dialog"].close();
    }
    setStatus("正在恢复上次编辑", restoredTask.filename, "busy");
    await selectTask(restoredTask.id);
    return;
  }
  if (!autoSelect) {
    clearPreview(state.tasks.length ? "请从左侧选择要制作的切片" : "目录中没有可用视频");
    if (!preserveDialog && elements["workspace-dialog"].open) {
      elements["workspace-dialog"].close();
    }
    setStatus(
      "扫描完成",
      state.tasks.length ? `${state.tasks.length} 个切片，请选择要制作的切片` : "目录中没有可用视频",
    );
    return;
  }
  if (!preserveDialog && elements["workspace-dialog"].open) {
    elements["workspace-dialog"].close();
  }
  setStatus("扫描完成", `${state.tasks.length} 个切片`);
  if (!activeTask()) {
    clearPreview("目录中没有可用视频");
    return;
  }

  try {
    await loadLayoutVariants(activeTask(), { applyRecommended: true });
    await ensureCandidates(activeTask());
  } catch (error) {
    setStatus("预览初始化失败", error.message, "error");
  }
}

async function ensureCandidates(task, force = false, { skipPreview = false } = {}) {
  if (task.candidates.length && !force) {
    await ensureTimeline(task);
    if (!skipPreview) await refreshPreview();
    return;
  }
  setBusy(true, `正在为 ${task.filename} 提取候选帧...`);
  try {
    const payload = await api(`/api/tasks/${task.id}/candidates`, {
      method: "POST",
      body: JSON.stringify({ count: 12, force }),
    });
    replaceTask(payload.task);
    renderTaskList();
    renderCandidates(payload.task);
    await ensureTimeline(payload.task);
    if (!skipPreview) await refreshPreview();
  } catch (error) {
    const failed = { ...task, status: "error", error: error.message };
    replaceTask(failed);
    renderTaskList();
    clearPreview("候选帧生成失败");
    setStatus("选帧失败", error.message, "error");
    throw error;
  } finally {
    setBusy(false);
  }
}

async function selectTask(taskId) {
  if (state.activeTaskId === taskId) {
    return;
  }
  persistTaskDraft(activeTask(), { immediate: true });
  state.previewRequestId += 1;
  state.selectedElement = null;
  state.activeTaskId = taskId;
  try {
    sessionStorage.setItem(ACTIVE_TASK_SESSION_KEY, taskDraftKey(activeTask()));
  } catch (error) {
    // 禁用会话存储时仍允许正常制作封面。
  }
  if (taskDraft(activeTask())?.disk_saved) {
    api(`/api/tasks/${taskId}/draft-active`, { method: "POST", body: "{}" })
      .catch((error) => console.warn("记录当前封面草稿失败", error));
  }
  renderTaskList();
  renderInspector(activeTask());
  renderCandidates(activeTask());
  renderTimeline(activeTask());
  clearPreview("正在载入切片");
  try {
    const settings = taskSettings(activeTask());
    const draft = taskDraft(activeTask());
    if (restorePreviewFromDraft(activeTask(), draft)) {
      if (!settings.variants.length) {
        loadLayoutVariants(activeTask(), { applyRecommended: false }).catch(() => {});
      }
      ensureTimeline(activeTask()).catch(() => {});
      setStatus(
        "已恢复磁盘草稿",
        state.fileDraftPath || activeTask().filename,
      );
      return;
    }
    if (!settings.variants.length) {
      await loadLayoutVariants(activeTask(), {
        applyRecommended: settings.auto_style && !Array.isArray(settings.copy_lines),
      });
    }
    await ensureCandidates(activeTask(), false, { skipPreview: true });
    const savedTimestamp = draft?.selected_timestamp === null
      || draft?.selected_timestamp === undefined
      ? Number.NaN
      : Number(draft.selected_timestamp);
    const currentTimestamp = Number(activeTask()?.selected_timestamp);
    if (Number.isFinite(savedTimestamp) && (
      !Number.isFinite(currentTimestamp) || Math.abs(savedTimestamp - currentTimestamp) > 0.05
    )) {
      setBusy(true, `正在恢复上次选帧 ${formatTimelineTimestamp(savedTimestamp)}...`);
      try {
        const payload = await api(`/api/tasks/${taskId}/select-timestamp`, {
          method: "POST",
          body: JSON.stringify({ timestamp: savedTimestamp }),
        });
        replaceTask(payload.task);
        renderCandidates(payload.task);
        renderTimeline(payload.task);
      } finally {
        setBusy(false);
      }
    }
    await refreshPreview();
  } catch (error) {
    console.error("载入切片失败", error);
  }
}

async function removeTaskFromQueue(taskId) {
  const removeIndex = state.tasks.findIndex((task) => task.id === taskId);
  if (removeIndex < 0) return;
  const removedTask = state.tasks[removeIndex];
  const wasActive = state.activeTaskId === taskId;
  setBusy(true, `正在从队列移除 ${removedTask.filename}...`);
  try {
    const payload = await api(`/api/tasks/${taskId}`, { method: "DELETE" });
    state.tasks = payload.tasks;
    sortTasks();
    state.settings.delete(taskId);
    if (wasActive) {
      state.activeTaskId = null;
      state.previewRequestId += 1;
      state.selectedElement = null;
      const nextTask = state.tasks[Math.min(removeIndex, state.tasks.length - 1)] || null;
      renderTaskList();
      if (nextTask) {
        await selectTask(nextTask.id);
      } else {
        renderInspector(null);
        renderCandidates(null);
        renderTimeline(null);
        clearPreview("当前队列已清空");
      }
    } else {
      renderTaskList();
    }
    setStatus("已从队列移除", `${removedTask.filename}（源视频未删除）`);
  } catch (error) {
    setStatus("移除失败", error.message, "error");
  } finally {
    setBusy(false);
  }
}

async function selectCandidate(token) {
  const task = activeTask();
  if (!task) {
    return;
  }
  setBusy(true, "正在切换候选帧...");
  try {
    const payload = await api(`/api/tasks/${task.id}/select-frame`, {
      method: "POST",
      body: JSON.stringify({ media_token: token }),
    });
    replaceTask(payload.task);
    renderCandidates(payload.task);
    renderTimeline(payload.task);
    await refreshPreview();
  } catch (error) {
    setStatus("切换失败", error.message, "error");
  } finally {
    setBusy(false);
  }
}

function previewPayload(task, includeCanvas = true) {
  const settings = taskSettings(task);
  const activeLineIndices = (settings.copy_lines || [])
    .map((line, index) => (line.trim() ? index : -1))
    .filter((index) => index >= 0);
  const payload = {
    title: settings.title,
    template_key: settings.template_key,
    palette_key: settings.palette_key,
    draft_updated_at: normalizedDraftTimestamp(taskDraft(task)?.updated_at) || Date.now(),
    layouts: Object.fromEntries(Object.entries(settings.layouts).map(([ratio, layout]) => [
      ratio,
      {
        focus_x: layout.focus_x,
        focus_y: layout.focus_y,
        background_scale: layout.background_scale,
        text: layout.text?.length === activeLineIndices.length ? layout.text : null,
        stickers: layout.stickers.map((sticker) => ({
          asset_id: sticker.asset_id,
          x: sticker.x,
          y: sticker.y,
          width: sticker.width,
          rotation: sticker.rotation || 0,
          center_x: Boolean(sticker.center_x),
          center_y: Boolean(sticker.center_y),
        })),
      },
    ])),
  };
  if (activeLineIndices.length) {
    payload.copy_lines = activeLineIndices.map((index) => settings.copy_lines[index].trim());
    if (settings.line_colors) {
      payload.line_colors = activeLineIndices.map((index) => settings.line_colors[index]);
    }
    if (settings.line_stroke_colors) {
      payload.line_stroke_colors = activeLineIndices.map(
        (index) => settings.line_stroke_colors[index],
      );
    }
  }
  if (includeCanvas) {
    payload.canvas_key = state.ratio;
  }
  return payload;
}

function clamp(value, minimum, maximum) {
  return Math.min(maximum, Math.max(minimum, value));
}

function snapElementToCanvasCenter(
  candidateX,
  candidateY,
  elementWidth,
  elementHeight,
  frameWidth,
  frameHeight,
  threshold = CENTER_SNAP_THRESHOLD_PX,
) {
  const safeFrameWidth = Math.max(1, frameWidth);
  const safeFrameHeight = Math.max(1, frameHeight);
  const centeredX = (safeFrameWidth - elementWidth) / (2 * safeFrameWidth);
  const centeredY = (safeFrameHeight - elementHeight) / (2 * safeFrameHeight);
  const centerXDistance = Math.abs((candidateX - centeredX) * safeFrameWidth);
  const centerYDistance = Math.abs((candidateY - centeredY) * safeFrameHeight);
  const snapX = centerXDistance <= threshold;
  const snapY = centerYDistance <= threshold;
  return {
    x: snapX ? centeredX : candidateX,
    y: snapY ? centeredY : candidateY,
    snapX,
    snapY,
  };
}

function setAlignmentGuides(showVertical, showHorizontal) {
  const overlay = elements["cover-overlay"];
  overlay.querySelector('[data-alignment-guide="vertical"]')
    ?.classList.toggle("visible", showVertical);
  overlay.querySelector('[data-alignment-guide="horizontal"]')
    ?.classList.toggle("visible", showHorizontal);
}

function selectEditableElement(type, index, uid = null) {
  state.selectedElement = { type, index, uid };
  elements["cover-overlay"].querySelectorAll(".editable-element").forEach((node) => {
    const selected = node.dataset.elementType === type
      && Number(node.dataset.elementIndex) === index;
    node.classList.toggle("selected", selected);
  });
  if (type !== "text") return;
  const task = activeTask();
  if (!task) return;
  const settings = taskSettings(task);
  const sourceLineIndex = visibleCopyLineIndices(settings)[index];
  if (!Number.isInteger(sourceLineIndex)) return;
  activateInspectorTab(byId("inspector-tab-copy"));
  selectColorLine(sourceLineIndex, settings);
  elements["copy-lines"]
    .querySelector(`[data-copy-line="${sourceLineIndex}"]`)
    ?.scrollIntoView({ block: "nearest" });
}

function updateBackgroundPanState() {
  const task = activeTask();
  const scale = task ? ratioLayout(taskSettings(task)).background_scale : 1;
  const canPan = Boolean(task && state.preview && scale > 1 && !isBusy());
  elements["cover-frame"].classList.toggle("can-pan-background", canPan);
  elements["cover-frame"].setAttribute(
    "aria-label",
    canPan ? "封面画布；拖动背景调整放大后的取景" : "封面画布",
  );
}

function beginBackgroundPan(event) {
  if (event.button !== 0 || isBusy() || event.target.closest(".editable-element")) return;
  const task = activeTask();
  if (!task || !state.preview) return;
  const settings = taskSettings(task);
  const layout = ratioLayout(settings);
  const scale = Number(layout.background_scale) || 1;
  if (scale <= 1) return;

  event.preventDefault();
  const frame = elements["cover-frame"];
  const frameRect = frame.getBoundingClientRect();
  if (!frameRect.width || !frameRect.height) return;
  const pointerId = event.pointerId;
  const start = {
    pointerX: event.clientX,
    pointerY: event.clientY,
    focusX: layout.focus_x,
    focusY: layout.focus_y,
  };
  let moved = false;
  state.selectedElement = null;
  elements["cover-overlay"].querySelectorAll(".selected").forEach((node) => {
    node.classList.remove("selected");
  });
  showInteractivePreview(state.preview);
  frame.classList.add("is-panning-background");
  frame.setPointerCapture?.(pointerId);

  const move = (moveEvent) => {
    if (moveEvent.pointerId !== pointerId) return;
    const deltaX = moveEvent.clientX - start.pointerX;
    const deltaY = moveEvent.clientY - start.pointerY;
    if (Math.abs(deltaX) + Math.abs(deltaY) >= 2) moved = true;
    const panRange = Math.max(0.01, scale - 1);
    layout.focus_x = clamp(start.focusX - deltaX / frameRect.width / panRange, 0, 1);
    layout.focus_y = clamp(start.focusY - deltaY / frameRect.height / panRange, 0, 1);
    if (state.syncRatios) {
      const synced = ratioLayout(settings, otherRatio());
      synced.focus_x = layout.focus_x;
      synced.focus_y = layout.focus_y;
    }
    elements["focus-x"].value = Math.round(layout.focus_x * 100);
    elements["focus-y"].value = Math.round(layout.focus_y * 100);
    updateFocusLabels();
    applyInteractiveBackgroundTransform(settings);
  };
  const finish = (finishEvent) => {
    if (finishEvent.pointerId !== pointerId) return;
    frame.removeEventListener("pointermove", move);
    frame.removeEventListener("pointerup", finish);
    frame.removeEventListener("pointercancel", finish);
    if (frame.hasPointerCapture?.(pointerId)) frame.releasePointerCapture(pointerId);
    frame.classList.remove("is-panning-background");
    if (moved) {
      refreshPreview().catch((error) => console.error("更新背景取景失败", error));
    } else {
      showSettledPreview(state.preview);
    }
  };
  frame.addEventListener("pointermove", move);
  frame.addEventListener("pointerup", finish);
  frame.addEventListener("pointercancel", finish);
}

function applyKeyboardTransform(model, type, key, largeStep = false) {
  const moveStep = largeStep ? 0.02 : 0.005;
  const resizeStep = type === "text"
    ? (largeStep ? 12 : 4)
    : (largeStep ? 0.03 : 0.01);
  if (key === "ArrowLeft") {
    model.x = clamp(model.x - moveStep, 0, 1);
    model.center_x = false;
  } else if (key === "ArrowRight") {
    model.x = clamp(model.x + moveStep, 0, 1);
    model.center_x = false;
  } else if (key === "ArrowUp") {
    model.y = clamp(model.y - moveStep, 0, 1);
    model.center_y = false;
  } else if (key === "ArrowDown") {
    model.y = clamp(model.y + moveStep, 0, 1);
    model.center_y = false;
  }
  else if (key === "+" || key === "=") {
    if (type === "text") model.font_size = clamp(model.font_size + resizeStep, 24, 320);
    else model.width = clamp(model.width + resizeStep, 0.03, 0.80);
    return "resize";
  } else if (key === "-" || key === "_") {
    if (type === "text") model.font_size = clamp(model.font_size - resizeStep, 24, 320);
    else model.width = clamp(model.width - resizeStep, 0.03, 0.80);
    return "resize";
  } else {
    return null;
  }
  return "move";
}

function handleEditableElementKeydown(event, type, index) {
  if (event.target !== event.currentTarget || isBusy()) return;
  const task = activeTask();
  if (!task || !state.preview) return;
  const settings = taskSettings(task);
  const layout = ratioLayout(settings);
  if (event.key === "Delete" || event.key === "Backspace") {
    event.preventDefault();
    event.stopPropagation();
    if (type === "sticker") {
      const sticker = layout.stickers[index];
      if (sticker) deleteSticker(sticker.uid);
    } else {
      const copyLineIndex = visibleCopyLineIndices(settings)[index];
      if (Number.isInteger(copyLineIndex)) deleteManualCopyLine(copyLineIndex);
    }
    return;
  }

  const model = type === "text"
    ? ensureTextLayout(settings)[index]
    : layout.stickers[index];
  if (!model) return;
  const previousSize = type === "text" ? model.font_size : model.width;
  const action = applyKeyboardTransform(model, type, event.key, event.shiftKey);
  if (!action) return;
  event.preventDefault();
  event.stopPropagation();
  selectEditableElement(type, index, model.uid || null);
  if (action === "move") {
    event.currentTarget.style.left = `${model.x * 100}%`;
    event.currentTarget.style.top = `${model.y * 100}%`;
  } else {
    const nextSize = type === "text" ? model.font_size : model.width;
    event.currentTarget.style.transform = `scale(${nextSize / previousSize})`;
  }
  syncElementTransform(settings, type, index, model);
  schedulePreview();
}

function syncOverlayScale() {
  if (!state.preview) return;
  const frameWidth = elements["cover-frame"].clientWidth;
  const frameHeight = elements["cover-frame"].clientHeight;
  if (!frameWidth || !frameHeight) return;
  const previewRatio = state.preview.width / state.preview.height;
  let renderedWidth = frameWidth;
  let renderedHeight = renderedWidth / previewRatio;
  if (renderedHeight > frameHeight) {
    renderedHeight = frameHeight;
    renderedWidth = renderedHeight * previewRatio;
  }
  const overlay = elements["cover-overlay"];
  overlay.style.left = `${(frameWidth - renderedWidth) / 2}px`;
  overlay.style.top = `${(frameHeight - renderedHeight) / 2}px`;
  overlay.style.right = "auto";
  overlay.style.bottom = "auto";
  overlay.style.width = `${renderedWidth}px`;
  overlay.style.height = `${renderedHeight}px`;
  const scale = renderedWidth / state.preview.width;
  elements["cover-overlay"].querySelectorAll(".text-element").forEach((node) => {
    const placement = state.preview.placements[Number(node.dataset.elementIndex)];
    if (!placement) return;
    const width = placement.box[2] - placement.box[0];
    const height = placement.box[3] - placement.box[1];
    node.style.left = `${placement.box[0] / state.preview.width * 100}%`;
    node.style.top = `${placement.box[1] / state.preview.height * 100}%`;
    node.style.width = `${width / state.preview.width * 100}%`;
    node.style.height = `${height / state.preview.height * 100}%`;
    const content = node.querySelector(".editable-text-content");
    content.style.fontSize = `${placement.font_size * scale}px`;
    content.style.lineHeight = `${height * scale}px`;
    content.style.color = placement.color;
    content.style.webkitTextStroke = `${Math.max(1, placement.stroke_width * scale)}px ${placement.stroke_color}`;
    content.style.textShadow = `${Math.max(1, placement.stroke_width * scale)}px ${Math.max(1, placement.stroke_width * scale)}px 1px rgba(0,0,0,.72)`;
  });
  elements["cover-overlay"].querySelectorAll(".sticker-element").forEach((node) => {
    const placement = state.preview.stickers[Number(node.dataset.elementIndex)];
    if (!placement) return;
    const width = placement.box[2] - placement.box[0];
    const height = placement.box[3] - placement.box[1];
    node.style.left = `${placement.box[0] / state.preview.width * 100}%`;
    node.style.top = `${placement.box[1] / state.preview.height * 100}%`;
    node.style.width = `${width / state.preview.width * 100}%`;
    node.style.height = `${height / state.preview.height * 100}%`;
  });
}

function ensureTextLayout(settings) {
  const layout = ratioLayout(settings);
  if (!layout.text) {
    layout.text = (state.preview?.placements || []).map((placement) => ({
      x: placement.box[0] / state.preview.width,
      y: placement.box[1] / state.preview.height,
      scale: 1,
      font_size: placement.font_size,
    }));
  }
  return layout.text;
}

function reconcileLayoutWithPreview(settings, preview) {
  const layout = ratioLayout(settings, preview.canvas_key);
  if (layout.text?.length === preview.placements.length) {
    preview.placements.forEach((placement, index) => {
      layout.text[index].x = placement.box[0] / preview.width;
      layout.text[index].y = placement.box[1] / preview.height;
      layout.text[index].font_size = placement.font_size;
    });
  }
  preview.stickers.forEach((placement, index) => {
    const sticker = layout.stickers[index];
    if (!sticker) return;
    sticker.x = placement.box[0] / preview.width;
    sticker.y = placement.box[1] / preview.height;
  });
  if (state.syncRatios && (layout.text || layout.stickers.length)) {
    synchronizeCurrentLayout(settings);
  }
}

function beginElementInteraction(event, type, index, mode) {
  const task = activeTask();
  if (isBusy() || !task || !state.preview) return;
  event.preventDefault();
  event.stopPropagation();
  const settings = taskSettings(task);
  const layout = ratioLayout(settings);
  const node = event.currentTarget.closest(".editable-element");
  // 文字和贴图交互始终保留完整成品预览；纯背景层只用于背景取景拖动。
  // 拖动期间显示独立编辑层，用户可以看到元素的实时轨迹；背景和成品层
  // 不切换、不缩放，松开后再由最新预览替换。
  setElementEditingLayer(true);
  const frameRect = elements["cover-overlay"].getBoundingClientRect();
  const nodeRect = node.getBoundingClientRect();
  let model;
  if (type === "text") {
    model = ensureTextLayout(settings)[index];
  } else {
    model = layout.stickers[index];
  }
  if (!model) return;
  selectEditableElement(type, index, model.uid || null);

  const start = {
    pointerX: event.clientX,
    pointerY: event.clientY,
    x: model.x,
    y: model.y,
    scale: model.scale || 1,
    width: model.width || 0.18,
    fontSize: model.font_size || state.preview.placements[index]?.font_size || 96,
    size: Math.max(36, nodeRect.width, nodeRect.height),
  };
  if (mode === "resize") {
    node.style.transformOrigin = `${model.center_x ? "center" : "left"} ${model.center_y ? "center" : "top"}`;
  }

  const move = (moveEvent) => {
    const deltaX = moveEvent.clientX - start.pointerX;
    const deltaY = moveEvent.clientY - start.pointerY;
    if (mode === "move") {
      const snapped = snapElementToCanvasCenter(
        clamp(start.x + deltaX / frameRect.width, 0, 1),
        clamp(start.y + deltaY / frameRect.height, 0, 1),
        nodeRect.width,
        nodeRect.height,
        frameRect.width,
        frameRect.height,
      );
      model.x = snapped.x;
      model.y = snapped.y;
      model.center_x = snapped.snapX;
      model.center_y = snapped.snapY;
      node.style.left = `${model.x * 100}%`;
      node.style.top = `${model.y * 100}%`;
      setAlignmentGuides(snapped.snapX, snapped.snapY);
    } else {
      const ratio = clamp(1 + (deltaX + deltaY) / (2 * start.size), 0.35, 2.5);
      if (type === "text") {
        model.font_size = clamp(Math.round(start.fontSize * ratio), 24, 320);
        model.scale = 1;
        node.style.transform = `scale(${model.font_size / start.fontSize})`;
      } else {
        model.width = clamp(start.width * ratio, 0.03, 0.80);
        node.style.transform = `scale(${model.width / start.width})`;
      }
      setAlignmentGuides(Boolean(model.center_x), Boolean(model.center_y));
    }
    syncElementTransform(settings, type, index, model);
  };
  const finish = () => {
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", finish);
    window.removeEventListener("pointercancel", finish);
    node.style.transform = "";
    node.style.transformOrigin = "";
    setAlignmentGuides(false, false);
    refreshPreview().catch((error) => console.error("更新元素位置失败", error));
  };
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", finish, { once: true });
  window.addEventListener("pointercancel", finish, { once: true });
}

function renderCoverOverlay(preview) {
  state.preview = preview;
  const task = activeTask();
  if (!task) return;
  const settings = taskSettings(task);
  const layout = ratioLayout(settings);
  const selected = state.selectedElement;
  const textNodes = preview.placements.map((placement, index) => `
    <div class="editable-element text-element ${selected?.type === "text" && selected.index === index ? "selected" : ""}" data-element-type="text" data-element-index="${index}" tabindex="0" aria-label="第 ${index + 1} 行标题；方向键移动，加减号缩放，删除键移除">
      <span class="editable-text-content">${escapeHtml(placement.text)}</span>
      <span class="resize-handle" data-resize-handle title="缩放第 ${index + 1} 行标题"></span>
    </div>
  `).join("");
  const stickerNodes = preview.stickers.map((placement, index) => {
    const sticker = layout.stickers[index];
    const asset = stickerAsset(placement.asset_id);
    if (!sticker || !asset) return "";
    const isSelected = selected?.type === "sticker" && selected.uid === sticker.uid;
    return `
      <div class="editable-element sticker-element ${isSelected ? "selected" : ""}" data-element-type="sticker" data-element-index="${index}" tabindex="0" aria-label="贴图 ${escapeHtml(asset.name)}；方向键移动，加减号缩放，删除键移除">
        <img src="/api/stickers/${asset.id}/image" alt="${escapeHtml(asset.name)}" draggable="false">
        <button class="element-delete" type="button" data-delete-sticker="${sticker.uid}" title="删除贴图" aria-label="删除贴图">×</button>
        <span class="resize-handle" data-resize-handle title="缩放贴图"></span>
      </div>
    `;
  }).join("");
  const guideNodes = `
    <span class="alignment-guide vertical" data-alignment-guide="vertical" aria-hidden="true"></span>
    <span class="alignment-guide horizontal" data-alignment-guide="horizontal" aria-hidden="true"></span>
  `;
  elements["cover-overlay"].innerHTML = guideNodes + stickerNodes + textNodes;
  syncOverlayScale();

  elements["cover-overlay"].querySelectorAll(".editable-element").forEach((node) => {
    const type = node.dataset.elementType;
    const index = Number(node.dataset.elementIndex);
    node.addEventListener("pointerdown", (event) => {
      if (event.target.closest("[data-resize-handle], [data-delete-sticker]")) return;
      beginElementInteraction(event, type, index, "move");
    });
    node.addEventListener("focus", () => {
      const model = type === "text"
        ? ratioLayout(taskSettings(activeTask())).text?.[index]
        : ratioLayout(taskSettings(activeTask())).stickers[index];
      selectEditableElement(type, index, model?.uid || null);
    });
    node.addEventListener("keydown", (event) => {
      handleEditableElementKeydown(event, type, index);
    });
    node.querySelector("[data-resize-handle]")?.addEventListener("pointerdown", (event) => {
      beginElementInteraction(event, type, index, "resize");
    });
  });
  elements["cover-overlay"].querySelectorAll("[data-delete-sticker]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      deleteSticker(button.dataset.deleteSticker);
    });
  });
}

function mediaUrl(token) {
  return `/api/media/${token}?v=${Date.now()}`;
}

function clearInteractiveBackgroundTransform() {
  elements["cover-background-preview"].style.removeProperty("transform");
  elements["cover-background-preview"].style.removeProperty("transform-origin");
}

function applyInteractiveBackgroundTransform(settings = null) {
  const task = activeTask();
  const activeSettings = settings || (task ? taskSettings(task) : null);
  if (!activeSettings) {
    clearInteractiveBackgroundTransform();
    return;
  }
  const layout = ratioLayout(activeSettings);
  const scale = finiteNumber(layout.background_scale, 1, 2.5, 1);
  const offsetX = -finiteNumber(layout.focus_x, 0, 1, 0.5) * (scale - 1) * 100;
  const offsetY = -finiteNumber(layout.focus_y, 0, 1, 0.5) * (scale - 1) * 100;
  elements["cover-background-preview"].style.transformOrigin = "top left";
  elements["cover-background-preview"].style.transform = `translate3d(${offsetX}%, ${offsetY}%, 0) scale(${scale})`;
}

function previewLayerUrl(preview, tokenKey, urlKey) {
  const token = preview?.[tokenKey];
  if (!token) return "";
  if (!preview[urlKey]) preview[urlKey] = mediaUrl(token);
  return preview[urlKey];
}

function preparePreviewLayers(preview = state.preview) {
  if (!preview?.media_token) return;
  const settled = elements["cover-preview"];
  const background = elements["cover-background-preview"];
  const settledUrl = previewLayerUrl(preview, "media_token", "_media_url");
  const backgroundUrl = previewLayerUrl(
    preview,
    "background_media_token",
    "_background_media_url",
  ) || settledUrl;
  if (settled.dataset.mediaUrl !== settledUrl) {
    settled.dataset.mediaUrl = settledUrl;
    settled.src = settledUrl;
  }
  if (background.dataset.mediaUrl !== backgroundUrl) {
    background.dataset.mediaUrl = backgroundUrl;
    background.src = backgroundUrl;
  }
}

function activateInteractivePreviewLayer(preview = state.preview) {
  if (!preview || state.preview !== preview) return;
  const background = elements["cover-background-preview"];
  if (!background.complete || !background.naturalWidth) return;
  elements["cover-preview"].hidden = true;
  background.hidden = false;
  applyInteractiveBackgroundTransform();
  elements["cover-overlay"].classList.remove("preview-settled");
}

function setElementEditingLayer(active) {
  state.elementInteractionActive = Boolean(active);
  elements["cover-overlay"].classList.toggle(
    "preview-editing",
    state.elementInteractionActive,
  );
}

function showInteractivePreview(preview = state.preview) {
  if (!preview) return;
  state.interactivePreview = preview;
  preparePreviewLayers(preview);
  const background = elements["cover-background-preview"];
  if (background.complete && background.naturalWidth) {
    activateInteractivePreviewLayer(preview);
  } else {
    // 纯背景尚未载入时继续显示完整预览，避免把已经放大的成品图再次缩放。
    // 本地图片载入后再无缝切换，文字和背景拖动共用同一张预热好的底图。
    background.addEventListener("load", () => {
      if (state.interactivePreview === preview) activateInteractivePreviewLayer(preview);
    }, { once: true });
  }
  updateBackgroundPanState();
}

function showSettledPreview(preview = state.preview) {
  if (!preview?.media_token) return;
  state.interactivePreview = null;
  setElementEditingLayer(false);
  preparePreviewLayers(preview);
  clearInteractiveBackgroundTransform();
  elements["cover-background-preview"].hidden = true;
  elements["cover-preview"].hidden = false;
  elements["cover-overlay"].classList.add("preview-settled");
  updateBackgroundPanState();
}

function restorePreviewFromDraft(task, draft = taskDraft(task)) {
  const preview = draft?.previews?.[state.ratio];
  if (!preview?.media_token || !Array.isArray(preview.placements)) return false;
  state.preview = preview;
  elements["cover-preview"].hidden = false;
  elements["preview-empty"].hidden = true;
  renderCoverOverlay(preview);
  showSettledPreview(preview);
  elements["preview-state"].textContent = `${preview.width} × ${preview.height}`;
  return true;
}

function rememberDraftPreview(task, preview, response) {
  const key = taskDraftKey(task);
  if (!key || !preview?.media_token) return;
  const current = state.drafts.get(key) || {};
  const selectedTimestamp = Number(response?.task?.selected_timestamp);
  state.drafts.set(key, {
    ...current,
    updated_at: Date.now(),
    selected_timestamp: Number.isFinite(selectedTimestamp)
      ? selectedTimestamp
      : current.selected_timestamp ?? null,
    settings: serializeTaskSettings(taskSettings(task)),
    previews: {
      ...(current.previews || {}),
      [state.ratio]: preview,
    },
    active: true,
    disk_saved: Boolean(response?.draft_saved),
  });
  if (typeof response?.draft_path === "string") state.fileDraftPath = response.draft_path;
  flushStoredDrafts();
}

function previewHasContent() {
  return Boolean(
    state.preview?.media_token
    && (
      !elements["cover-preview"].hidden
      || !elements["cover-background-preview"].hidden
    )
  );
}

function beginPreviewLoading(requestId) {
  window.clearTimeout(state.previewLoaderTimer);
  state.previewLoaderTimer = null;

  // 编辑已有封面时保留当前画面，不再显示覆盖整个画布的“正在生成预览”。
  if (previewHasContent()) {
    elements["preview-loader"].hidden = true;
    return;
  }

  // 首次生成也延迟一点显示，快速完成的请求不会造成闪屏。
  state.previewLoaderTimer = window.setTimeout(() => {
    state.previewLoaderTimer = null;
    if (requestId !== state.previewRequestId || previewHasContent()) return;
    elements["preview-loader"].hidden = false;
    elements["preview-state"].textContent = "正在渲染";
  }, PREVIEW_LOADER_DELAY_MS);
}

function finishPreviewLoading(requestId) {
  if (requestId !== state.previewRequestId) return;
  window.clearTimeout(state.previewLoaderTimer);
  state.previewLoaderTimer = null;
  elements["preview-loader"].hidden = true;
}

async function refreshPreview() {
  const task = activeTask();
  if (!task || !task.candidates.length) {
    return;
  }
  const taskId = task.id;
  const ratio = state.ratio;
  const requestId = ++state.previewRequestId;
  const hadPreview = previewHasContent();
  const previousPreview = state.preview;
  persistTaskDraft(task);
  beginPreviewLoading(requestId);
  try {
    const payload = await api(`/api/tasks/${task.id}/preview`, {
      method: "POST",
      body: JSON.stringify(previewPayload(task)),
    });
    if (
      requestId !== state.previewRequestId
      || state.activeTaskId !== taskId
      || state.ratio !== ratio
    ) {
      return;
    }
    replaceTask(payload.task);
    const settings = taskSettings(task);
    if (!settings.copy_lines) {
      settings.copy_lines = payload.preview.placements.map((placement) => placement.text);
      settings.line_colors = payload.preview.placements.map((placement) => placement.color);
      settings.line_stroke_colors = payload.preview.placements.map(
        (placement) => placement.stroke_color,
      );
      renderCopyLines(settings);
    }
    reconcileLayoutWithPreview(settings, payload.preview);
    persistTaskDraft(task);
    elements["cover-preview"].hidden = false;
    elements["preview-empty"].hidden = true;
    renderCoverOverlay(payload.preview);
    showSettledPreview(payload.preview);
    rememberDraftPreview(task, payload.preview, payload);
    elements["preview-state"].textContent = `${payload.preview.width} × ${payload.preview.height}`;
    setStatus(
      payload.draft_saved ? "预览和磁盘草稿已保存" : "预览已更新",
      payload.draft_warning || state.fileDraftPath || task.filename,
      payload.draft_warning ? "error" : "ready",
    );
  } catch (error) {
    if (requestId !== state.previewRequestId) return;
    setElementEditingLayer(false);
    // 已经有预览时保留旧画面，避免一次网络/渲染失败把正在编辑的封面清空。
    if (!hadPreview) {
      clearPreview("预览生成失败");
    } else if (previousPreview?.width && previousPreview?.height) {
      elements["preview-state"].textContent = `${previousPreview.width} × ${previousPreview.height}`;
    }
    setStatus("预览失败", error.message, "error");
    throw error;
  } finally {
    finishPreviewLoading(requestId);
  }
}

function clearPreview(message) {
  window.clearTimeout(state.previewLoaderTimer);
  state.previewLoaderTimer = null;
  elements["preview-loader"].hidden = true;
  state.preview = null;
  state.interactivePreview = null;
  setElementEditingLayer(false);
  state.selectedElement = null;
  elements["cover-preview"].hidden = true;
  elements["cover-preview"].removeAttribute("src");
  elements["cover-preview"].removeAttribute("data-media-url");
  elements["cover-background-preview"].hidden = true;
  elements["cover-background-preview"].removeAttribute("src");
  elements["cover-background-preview"].removeAttribute("data-media-url");
  clearInteractiveBackgroundTransform();
  elements["cover-overlay"].replaceChildren();
  elements["cover-overlay"].removeAttribute("style");
  elements["cover-overlay"].classList.remove("preview-settled");
  updateBackgroundPanState();
  elements["preview-empty"].hidden = false;
  elements["preview-empty"].textContent = message;
  elements["preview-state"].textContent = message;
}

function schedulePreview() {
  persistTaskDraft(activeTask());
  window.clearTimeout(state.previewTimer);
  state.previewTimer = window.setTimeout(() => {
    refreshPreview().catch((error) => console.error("自动预览失败", error));
  }, 420);
}

async function saveCover(canvases) {
  const task = activeTask();
  if (isBusy() || !task) {
    return;
  }
  const currentOnly = canvases.length === 1;
  const ratioLabel = canvases[0] === "4x3" ? "4:3" : "16:9";
  window.clearTimeout(state.previewTimer);
  window.clearTimeout(state.recommendTimer);
  setBusy(true, currentOnly ? `正在导出 ${ratioLabel} 封面...` : "正在保存双比例封面...");
  try {
    const payload = previewPayload(task, false);
    payload.canvases = canvases;
    const result = await api(`/api/tasks/${task.id}/save`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    setStatus(
      currentOnly ? `${ratioLabel} 封面已导出` : "双比例封面已保存",
      result.outputs.map((item) => item.filename).join("、"),
    );
  } catch (error) {
    setStatus(currentOnly ? "单独导出失败" : "保存失败", error.message, "error");
  } finally {
    setBusy(false);
  }
}

function saveCurrentCover() {
  return saveCover([state.ratio]);
}

function saveActiveCover() {
  return saveCover(["4x3", "16x9"]);
}

async function batchExport() {
  if (isBusy() || !state.tasks.length) {
    return;
  }
  window.clearTimeout(state.previewTimer);
  window.clearTimeout(state.recommendTimer);
  setBusy(true, "正在批量生成封面...");
  let completed = 0;
  try {
    for (const originalTask of [...state.tasks]) {
      state.activeTaskId = originalTask.id;
      renderTaskList();
      renderInspector(activeTask());
      renderCandidates(activeTask());
      await ensureCandidates(activeTask());
      const current = activeTask();
      const payload = previewPayload(current, false);
      payload.canvases = ["4x3", "16x9"];
      await api(`/api/tasks/${current.id}/save`, {
        method: "POST",
        body: JSON.stringify(payload),
      });
      completed += 1;
      setStatus("正在批量导出", `${completed}/${state.tasks.length} ${current.filename}`, "busy");
    }
    setStatus("批量导出完成", `${completed} 个切片，${completed * 2} 张封面`);
  } catch (error) {
    setStatus("批量导出中断", `${completed}/${state.tasks.length}：${error.message}`, "error");
  } finally {
    setBusy(false);
  }
}

function openWorkspaceDialog(errorMessage = "") {
  const defaultInput = state.options?.default_input_dir || "input";
  const defaultOutput = state.options?.default_output_dir || "";
  if (state.workspaceConfig) {
    elements["root-path"].value = state.workspaceConfig.root || "";
    elements["title-file"].value = state.workspaceConfig.title_file || "";
    elements["output-path"].value = state.workspaceConfig.output_dir || defaultOutput;
    elements["recursive-scan"].checked = state.workspaceConfig.recursive !== false;
  } else {
    elements["root-path"].value = defaultInput;
    elements["output-path"].value = defaultOutput;
  }
  setWorkspaceError(errorMessage);
  elements["workspace-dialog"].showModal();
  elements["root-path"].focus();
}

function bindEditor() {
  elements["add-copy-line"].addEventListener("click", addManualCopyLine);
  elements["title-input"].addEventListener("input", () => {
    const task = activeTask();
    if (!task) return;
    const settings = taskSettings(task);
    settings.title = elements["title-input"].value;
    settings.copy_lines = null;
    settings.line_colors = null;
    settings.line_stroke_colors = null;
    settings.variants = [];
    clearTextLayouts(settings);
    renderTaskList();
    renderLayoutVariants(settings);
    renderCopyLines(settings);
    scheduleLayoutVariants(task);
  });
  elements["template-select"].addEventListener("change", () => {
    const settings = taskSettings(activeTask());
    settings.auto_style = false;
    settings.template_key = elements["template-select"].value;
    settings.copy_lines = null;
    settings.line_colors = null;
    settings.line_stroke_colors = null;
    clearTextLayouts(settings);
    renderCopyLines(settings);
    schedulePreview();
  });
  elements["palette-select"].addEventListener("change", () => {
    const settings = taskSettings(activeTask());
    settings.auto_style = false;
    settings.palette_key = elements["palette-select"].value;
    settings.line_colors = null;
    settings.line_stroke_colors = null;
    renderPalette();
    renderCopyLines(settings);
    schedulePreview();
  });
  elements["stroke-color-input"].addEventListener("input", () => {
    const task = activeTask();
    if (!task) return;
    const settings = taskSettings(task);
    const color = normalizeHexColor(elements["stroke-color-input"].value);
    elements["stroke-color-input"].setAttribute("aria-invalid", String(!color));
    if (!color || !setLineStrokeColor(settings, state.activeColorLine, color)) return;
    const preview = elements["copy-lines"].querySelector(
      `[data-select-color-line="${state.activeColorLine}"]`,
    );
    preview?.style.setProperty("--stroke-color", color);
    elements["common-stroke-colors"].querySelectorAll("[data-common-stroke-color]")
      .forEach((button) => {
        button.classList.toggle("active", button.dataset.commonStrokeColor === color);
      });
    schedulePreview();
  });
  elements["stroke-color-input"].addEventListener("blur", () => {
    const task = activeTask();
    if (!task) return;
    const settings = taskSettings(task);
    const color = normalizeHexColor(elements["stroke-color-input"].value)
      || lineStrokeColorAt(settings, state.activeColorLine);
    elements["stroke-color-input"].value = color.slice(1);
    elements["stroke-color-input"].setAttribute("aria-invalid", "false");
  });
  elements["sticker-group"].addEventListener("change", renderStickerLibrary);
  elements["sticker-search"].addEventListener("input", renderStickerLibrary);
  elements["upload-sticker"].addEventListener("click", () => {
    elements["upload-sticker-file"].click();
  });
  elements["upload-sticker-file"].addEventListener("change", () => {
    const [file] = elements["upload-sticker-file"].files || [];
    if (file) importLocalSticker(file).catch(() => {});
  });
  elements["refresh-stickers"].addEventListener("click", async () => {
    elements["refresh-stickers"].disabled = true;
    try {
      await loadStickers(true);
      if (state.stickerSummary?.available) {
        setStatus(
          "贴图库已刷新",
          `${state.stickerSummary.group_count} 位主播，${state.stickerSummary.asset_count} 张表情贴图`,
        );
      } else {
        setStatus("未找到贴图库", "请检查默认表情包目录或 AUTOCOVER_STICKER_DIR", "error");
      }
    } catch (error) {
      setStatus("贴图库刷新失败", error.message, "error");
    } finally {
      elements["refresh-stickers"].disabled = false;
    }
  });
  ["focus-x", "focus-y"].forEach((id) => {
    elements[id].addEventListener("input", () => {
      const settings = taskSettings(activeTask());
      const key = id.replace("-", "_");
      const layout = ratioLayout(settings);
      layout[key] = Number(elements[id].value) / 100;
      if (state.syncRatios) {
        ratioLayout(settings, otherRatio())[key] = layout[key];
      }
      updateFocusLabels();
      schedulePreview();
    });
  });
  elements["background-scale"].addEventListener("input", () => {
    const task = activeTask();
    if (!task) return;
    const settings = taskSettings(task);
    const layout = ratioLayout(settings);
    layout.background_scale = Number(elements["background-scale"].value) / 100;
    if (state.syncRatios) {
      ratioLayout(settings, otherRatio()).background_scale = layout.background_scale;
    }
    updateFocusLabels();
    updateBackgroundPanState();
    schedulePreview();
  });
}

function activateInspectorTab(button) {
  const tab = button.dataset.inspectorTab;
  document.querySelectorAll("[data-inspector-tab]").forEach((item) => {
    const active = item === button;
    item.classList.toggle("active", active);
    item.setAttribute("aria-selected", String(active));
    item.setAttribute("tabindex", active ? "0" : "-1");
  });
  document.querySelectorAll("[data-inspector-view]").forEach((view) => {
    view.hidden = view.dataset.inspectorView !== tab;
  });
}

function activateRatioTab(button) {
  if (button.disabled || isBusy()) return;
  state.ratio = button.dataset.ratio;
  document.querySelectorAll("[data-ratio]").forEach((item) => {
    const active = item === button;
    item.classList.toggle("active", active);
    item.setAttribute("aria-selected", String(active));
    item.setAttribute("tabindex", active ? "0" : "-1");
  });
  byId("cover-canvas-panel").setAttribute("aria-labelledby", button.id);
  elements["cover-frame"].className = `cover-frame ratio-${state.ratio}`;
  state.previewRequestId += 1;
  state.selectedElement = null;
  renderInspector(activeTask());
  if (!restorePreviewFromDraft(activeTask())) {
    refreshPreview().catch(() => {});
  }
}

function bindRovingTablist(selector) {
  const tablist = document.querySelector(selector);
  if (!tablist) return;
  tablist.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    const tabs = [...tablist.querySelectorAll('[role="tab"]')]
      .filter((tab) => !tab.disabled);
    const current = tabs.indexOf(event.target);
    if (current < 0 || !tabs.length) return;
    event.preventDefault();
    let next = current;
    if (event.key === "Home") next = 0;
    else if (event.key === "End") next = tabs.length - 1;
    else if (event.key === "ArrowRight") next = (current + 1) % tabs.length;
    else next = (current - 1 + tabs.length) % tabs.length;
    tabs[next].focus();
    tabs[next].click();
  });
}

function bindEvents() {
  elements["open-workspace"].addEventListener("click", () => openWorkspaceDialog());
  elements["root-path"].addEventListener("input", () => setWorkspaceError());
  document.querySelectorAll("[data-inspector-tab]").forEach((button) => {
    button.addEventListener("click", () => activateInspectorTab(button));
  });
  bindRovingTablist(".inspector-tabs");
  bindRovingTablist(".ratio-switch");
  elements.rescan.addEventListener("click", () => {
    const currentTaskKey = taskDraftKey(activeTask());
    scanWorkspace(state.workspaceConfig, {
      restoreTaskKey: reloadTaskDraftKey() || currentTaskKey,
    }).catch(() => {});
  });
  elements["task-sort"].addEventListener("change", (event) => {
    state.queueSort = QUEUE_SORT_KEYS.has(event.target.value)
      ? event.target.value
      : "folder_created_desc";
    localStorage.setItem("autocover.task-sort", state.queueSort);
    sortTasks();
    renderTaskList();
  });
  elements["scan-submit"].addEventListener("click", () => {
    const root = elements["root-path"].value.trim();
    if (!root) {
      setWorkspaceError("请输入要扫描的切片目录");
      elements["root-path"].focus();
      return;
    }
    const config = {
      root,
      title_file: elements["title-file"].value.trim() || null,
      output_dir: elements["output-path"].value.trim() || null,
      recursive: elements["recursive-scan"].checked,
    };
    scanWorkspace(config).catch(() => {});
  });
  elements["timeline-range"].addEventListener("input", (event) => {
    elements["timeline-current"].textContent = formatTimelineTimestamp(event.target.value);
    elements["timeline-status"].textContent = "松开滑块选取这一帧";
  });
  elements["timeline-range"].addEventListener("change", (event) => {
    selectTimelineTimestamp(event.target.value).catch(() => {});
  });
  document.querySelectorAll("[data-ratio]").forEach((button) => {
    button.addEventListener("click", () => activateRatioTab(button));
  });
  elements["sync-ratios"].addEventListener("change", () => {
    state.syncRatios = elements["sync-ratios"].checked;
    localStorage.setItem("autocover.sync-ratios", String(state.syncRatios));
    const task = activeTask();
    if (state.syncRatios && task) {
      synchronizeCurrentLayout(taskSettings(task));
      persistTaskDraft(task);
      setStatus("双比例同步已开启", `${state.ratio === "4x3" ? "4:3" : "16:9"} 为当前主编辑比例`);
    } else {
      setStatus("双比例同步已关闭", "两个比例可分别精调");
    }
  });
  elements["refresh-candidates"].addEventListener("click", () => ensureCandidates(activeTask(), true).catch(() => {}));
  elements["refresh-preview"].addEventListener("click", () => refreshPreview().catch(() => {}));
  elements["save-current"].addEventListener("click", saveCurrentCover);
  elements["save-cover"].addEventListener("click", saveActiveCover);
  elements["batch-export"].addEventListener("click", batchExport);
  elements["reset-copy"].addEventListener("click", () => {
    const task = activeTask();
    if (!task) return;
    const settings = taskSettings(activeTask());
    settings.auto_style = true;
    settings.copy_lines = null;
    settings.line_colors = null;
    settings.line_stroke_colors = null;
    clearTextLayouts(settings);
    renderCopyLines(settings);
    loadLayoutVariants(task, { applyRecommended: true })
      .then(() => refreshPreview())
      .catch((error) => setStatus("恢复自动文案失败", error.message, "error"));
  });
  elements["reset-layout"].addEventListener("click", () => {
    const task = activeTask();
    if (!task) return;
    const settings = taskSettings(task);
    const layouts = state.syncRatios
      ? Object.values(settings.layouts)
      : [ratioLayout(settings)];
    layouts.forEach((layout) => {
      layout.text = null;
      layout.focus_x = 0.5;
      layout.focus_y = 0.5;
      layout.background_scale = 1;
      layout.stickers = layout.stickers.map((sticker, index) => ({
        ...sticker,
        ...defaultStickerTransform(index),
      }));
    });
    renderInspector(task);
    state.selectedElement = null;
    refreshPreview().catch(() => {});
  });
  elements["cover-overlay"].addEventListener("pointerdown", (event) => {
    if (event.target === elements["cover-overlay"]) {
      state.selectedElement = null;
      elements["cover-overlay"].querySelectorAll(".selected").forEach((node) => {
        node.classList.remove("selected");
      });
    }
  });
  elements["cover-frame"].addEventListener("pointerdown", beginBackgroundPan);
  bindEditor();
}

async function boot() {
  cacheElements();
  loadStoredDrafts();
  const storedQueueSort = localStorage.getItem("autocover.task-sort");
  state.queueSort = QUEUE_SORT_KEYS.has(storedQueueSort)
    ? storedQueueSort
    : "folder_created_desc";
  elements["task-sort"].value = state.queueSort;
  state.syncRatios = localStorage.getItem("autocover.sync-ratios") !== "false";
  elements["sync-ratios"].checked = state.syncRatios;
  bindEvents();
  window.addEventListener("beforeunload", () => {
    persistTaskDraft(activeTask(), { immediate: true });
    flushStoredDrafts();
  });
  try {
    state.options = await api("/api/options");
    validateApiCompatibility(state.options);
    renderOptions();
    await loadDefaultCoverFont();
  } catch (error) {
    setStatus("初始化失败", error.message, "error");
    clearPreview(error.message);
    return;
  }
  try {
    await loadStickers();
  } catch (error) {
    elements["sticker-grid"].innerHTML = '<div class="candidate-empty compact">贴图库载入失败</div>';
    setStatus("贴图库载入失败", error.message, "error");
  }
  if (globalThis.ResizeObserver) {
    state.overlayObserver = new ResizeObserver(syncOverlayScale);
    state.overlayObserver.observe(elements["cover-frame"]);
  } else {
    window.addEventListener("resize", syncOverlayScale);
  }
  const stored = localStorage.getItem("autocover.workspace");
  if (stored) {
    try {
      const config = migrateWorkspaceConfig(JSON.parse(stored));
      // 恢复上次目录只建立队列，不自动处理第一个视频；点击队列项后才生成候选帧和预览。
      // 只有用户主动刷新当前页面时才恢复刚才编辑的任务；全新打开时仍不会自动处理第一个视频。
      await scanWorkspace(config, {
        autoSelect: false,
        restoreTaskKey: reloadTaskDraftKey(),
      });
      return;
    } catch (error) {
      localStorage.removeItem("autocover.workspace");
      openWorkspaceDialog(error.message);
      return;
    }
  }
  openWorkspaceDialog();
}

window.addEventListener("DOMContentLoaded", boot);
