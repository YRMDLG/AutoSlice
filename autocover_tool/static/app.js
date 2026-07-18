"use strict";

const EXPECTED_SERVICE_ID = "autocover";
const EXPECTED_API_VERSION = 4;
const LEGACY_DEFAULT_INPUT_DIR = "output";
const COMMON_LINE_COLORS = Object.freeze([
  "#ffe34d", "#ffffff", "#f04444", "#d06e95",
  "#6850c7", "#32ddf2", "#ff8ebc", "#171717",
]);
const COMMON_STROKE_COLORS = Object.freeze(["#111111", "#ffffff"]);

const state = {
  options: null,
  tasks: [],
  activeTaskId: null,
  ratio: "4x3",
  settings: new Map(),
  workspaceConfig: null,
  previewTimer: null,
  recommendTimer: null,
  busyCount: 0,
  stickerAssets: [],
  selectedElement: null,
  preview: null,
  previewRequestId: 0,
  overlayObserver: null,
  activeColorLine: 0,
  syncRatios: true,
};

const elements = {};

function byId(id) {
  return document.getElementById(id);
}

function cacheElements() {
  [
    "workbench", "workspace-summary", "open-workspace", "batch-export", "rescan",
    "task-count", "task-list", "preview-state", "cover-frame", "cover-preview",
    "preview-empty", "preview-loader", "candidate-summary", "candidate-strip",
    "refresh-candidates", "active-filename", "reset-copy", "editor-controls",
    "title-input", "layout-variants", "template-select", "palette-select", "palette-preview", "copy-lines",
    "common-colors", "common-stroke-colors", "stroke-color-input", "sync-ratios",
    "cover-overlay", "refresh-stickers", "sticker-group", "sticker-search", "sticker-grid",
    "focus-x", "focus-y", "focus-x-value", "focus-y-value", "refresh-preview",
    "reset-layout", "save-current", "save-cover", "status-dot", "status-text", "status-detail", "workspace-dialog",
    "workspace-form", "root-path", "title-file", "output-path", "recursive-scan",
    "font-status", "scan-submit",
  ].forEach((id) => {
    elements[id] = byId(id);
  });
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
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
  const defaultInput = state.options?.default_input_dir || LEGACY_DEFAULT_INPUT_DIR;
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
  elements["status-dot"].className = `status-dot ${kind === "ready" ? "" : kind}`.trim();
}

function setBusy(active, message = "处理中...") {
  state.busyCount = Math.max(0, state.busyCount + (active ? 1 : -1));
  const busy = state.busyCount > 0;
  elements.workbench.setAttribute("aria-busy", String(busy));
  if (busy) {
    setStatus(message, "", "busy");
  } else if (elements["status-dot"].classList.contains("busy")) {
    setStatus("就绪");
  }
}

function activeTask() {
  return state.tasks.find((task) => task.id === state.activeTaskId) || null;
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
      "4x3": { text: null, stickers: [], focus_x: 0.5, focus_y: 0.5 },
      "16x9": { text: null, stickers: [], focus_x: 0.5, focus_y: 0.5 },
    },
  };
}

function taskSettings(task) {
  if (!state.settings.has(task.id)) {
    state.settings.set(task.id, defaultSettings(task));
  }
  return state.settings.get(task.id);
}

function ratioLayout(settings, ratio = state.ratio) {
  if (!settings.layouts[ratio]) {
    settings.layouts[ratio] = { text: null, stickers: [], focus_x: 0.5, focus_y: 0.5 };
  }
  const layout = settings.layouts[ratio];
  if (!Number.isFinite(layout.focus_x)) layout.focus_x = 0.5;
  if (!Number.isFinite(layout.focus_y)) layout.focus_y = 0.5;
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

function currentPalette() {
  const settings = activeTask() ? taskSettings(activeTask()) : null;
  return state.options?.palettes.find((palette) => palette.key === settings?.palette_key) || null;
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
  const palette = currentPalette();
  const role = lineRoleAt(settings, index);
  return normalizeHexColor(palette?.[`${role}_color`]) || "#ffffff";
}

function paletteLineStrokeColor(settings, index) {
  const palette = currentPalette();
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
      ? `封面字体：${font.label} · ${font.family}`
      : `封面字体：${font.family}（${font.label}未配置）`;
    elements["font-status"].classList.toggle("warning", !font.available);
    elements["font-status"].title = font.warning || "";
  }
}

async function loadDefaultCoverFont() {
  if (!state.options?.default_font?.available || !document.fonts?.load) {
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
  return true;
}

function scheduleLayoutVariants(task) {
  window.clearTimeout(state.recommendTimer);
  if (!task) return;
  const settings = taskSettings(task);
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
  elements["batch-export"].disabled = state.tasks.length === 0;
  elements.rescan.disabled = !state.workspaceConfig;
  if (!state.tasks.length) {
    elements["task-list"].innerHTML = '<div class="empty-list">目录中没有可用视频</div>';
    return;
  }
  elements["task-list"].innerHTML = state.tasks.map((task, index) => `
    <div class="task-item ${task.id === state.activeTaskId ? "active" : ""}" data-task-row="${task.id}">
      <button class="task-select" type="button" data-task-id="${task.id}">
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

function renderCandidates(task) {
  const candidates = task?.candidates || [];
  elements["candidate-summary"].textContent = `${candidates.length} 张`;
  elements["refresh-candidates"].disabled = !task;
  if (!candidates.length) {
    elements["candidate-strip"].innerHTML = '<div class="candidate-empty">暂无候选帧</div>';
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

function renderStickerLibrary() {
  const selectedGroup = elements["sticker-group"].value;
  const query = elements["sticker-search"].value.trim().toLocaleLowerCase("zh-CN");
  const assets = state.stickerAssets.filter((asset) => {
    const groupMatches = !selectedGroup || asset.group === selectedGroup;
    const queryMatches = !query || `${asset.name} ${asset.group}`.toLocaleLowerCase("zh-CN").includes(query);
    return groupMatches && queryMatches;
  });
  if (!assets.length) {
    elements["sticker-grid"].innerHTML = '<div class="candidate-empty compact">没有匹配贴图</div>';
    return;
  }
  elements["sticker-grid"].innerHTML = assets.map((asset) => `
    <button class="sticker-button" type="button" data-sticker-id="${asset.id}" title="${escapeHtml(asset.group)} / ${escapeHtml(asset.name)}">
      <img src="/api/stickers/${asset.id}/image" alt="${escapeHtml(asset.name)}" loading="lazy">
      <span>${escapeHtml(asset.name)}</span>
    </button>
  `).join("");
  elements["sticker-grid"].querySelectorAll("[data-sticker-id]").forEach((button) => {
    button.addEventListener("click", () => addSticker(button.dataset.stickerId));
  });
}

async function loadStickers(refresh = false) {
  const payload = await api(`/api/stickers${refresh ? "?refresh=1" : ""}`);
  state.stickerAssets = payload.assets;
  const previous = elements["sticker-group"].value;
  const groups = [...new Set(payload.assets.map((asset) => asset.group))];
  elements["sticker-group"].innerHTML = [
    '<option value="">全部表情包</option>',
    ...groups.map((group) => `<option value="${escapeHtml(group)}">${escapeHtml(group)}</option>`),
  ].join("");
  if (groups.includes(previous)) {
    elements["sticker-group"].value = previous;
  }
  renderStickerLibrary();
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
  if (!task || !stickerAsset(assetId)) return;
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
  if (!task) return;
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

function renderCommonColors(settings) {
  if (!settings?.copy_lines?.length) {
    elements["common-colors"].replaceChildren();
    elements["common-stroke-colors"].replaceChildren();
    elements["stroke-color-input"].value = "";
    elements["stroke-color-input"].disabled = true;
    return;
  }
  elements["stroke-color-input"].disabled = false;
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
  if (!lines.length) {
    elements["copy-lines"].innerHTML = '<div class="candidate-empty">预览后显示自动文案</div>';
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
      const wasVisible = Boolean(current.copy_lines[index].trim());
      current.copy_lines[index] = input.value;
      if (wasVisible !== Boolean(input.value.trim())) {
        clearTextLayouts(current);
      }
      schedulePreview();
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
  const enabled = Boolean(task);
  elements["editor-controls"].disabled = !enabled;
  elements["reset-copy"].disabled = !enabled;
  elements["refresh-preview"].disabled = !enabled;
  elements["save-current"].disabled = !enabled;
  elements["save-cover"].disabled = !enabled;
  elements["save-current"].textContent = `导出当前 ${state.ratio === "4x3" ? "4:3" : "16:9"}`;
  elements["active-filename"].textContent = task?.filename || "未选择切片";
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
  updateFocusLabels();
  renderLayoutVariants(settings);
  renderPalette();
  renderCopyLines(settings);
}

function updateFocusLabels() {
  elements["focus-x-value"].textContent = `${elements["focus-x"].value}%`;
  elements["focus-y-value"].textContent = `${elements["focus-y"].value}%`;
}

async function scanWorkspace(config, { preserveDialog = false } = {}) {
  setBusy(true, "正在扫描切片目录...");
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
    throw error;
  } finally {
    setBusy(false);
  }

  state.workspaceConfig = config;
  state.tasks = payload.tasks;
  state.settings.clear();
  state.activeTaskId = state.tasks[0]?.id || null;
  localStorage.setItem("autocover.workspace", JSON.stringify(config));
  elements["workspace-summary"].textContent = config.root;
  renderTaskList();
  renderInspector(activeTask());
  renderCandidates(activeTask());
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

async function ensureCandidates(task, force = false) {
  if (task.candidates.length && !force) {
    await refreshPreview();
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
    await refreshPreview();
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
  state.previewRequestId += 1;
  state.selectedElement = null;
  state.activeTaskId = taskId;
  renderTaskList();
  renderInspector(activeTask());
  renderCandidates(activeTask());
  clearPreview("正在载入切片");
  try {
    const settings = taskSettings(activeTask());
    if (!settings.variants.length) {
      await loadLayoutVariants(activeTask(), { applyRecommended: settings.auto_style });
    }
    await ensureCandidates(activeTask());
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
    layouts: Object.fromEntries(Object.entries(settings.layouts).map(([ratio, layout]) => [
      ratio,
      {
        focus_x: layout.focus_x,
        focus_y: layout.focus_y,
        text: layout.text?.length === activeLineIndices.length ? layout.text : null,
        stickers: layout.stickers.map((sticker) => ({
          asset_id: sticker.asset_id,
          x: sticker.x,
          y: sticker.y,
          width: sticker.width,
          rotation: sticker.rotation || 0,
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

function selectEditableElement(type, index, uid = null) {
  state.selectedElement = { type, index, uid };
  elements["cover-overlay"].querySelectorAll(".editable-element").forEach((node) => {
    const selected = node.dataset.elementType === type
      && Number(node.dataset.elementIndex) === index;
    node.classList.toggle("selected", selected);
  });
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
  if (!task || !state.preview) return;
  event.preventDefault();
  event.stopPropagation();
  const settings = taskSettings(task);
  const layout = ratioLayout(settings);
  const node = event.currentTarget.closest(".editable-element");
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
    size: Math.max(36, nodeRect.width, nodeRect.height),
  };

  const move = (moveEvent) => {
    const deltaX = moveEvent.clientX - start.pointerX;
    const deltaY = moveEvent.clientY - start.pointerY;
    if (mode === "move") {
      model.x = clamp(start.x + deltaX / frameRect.width, 0, 1);
      model.y = clamp(start.y + deltaY / frameRect.height, 0, 1);
      node.style.left = `${model.x * 100}%`;
      node.style.top = `${model.y * 100}%`;
    } else {
      const ratio = clamp(1 + (deltaX + deltaY) / (2 * start.size), 0.35, 2.5);
      if (type === "text") {
        model.scale = clamp(start.scale * ratio, 0.45, 2.0);
        node.style.transform = `scale(${model.scale / start.scale})`;
      } else {
        model.width = clamp(start.width * ratio, 0.03, 0.80);
        node.style.transform = `scale(${model.width / start.width})`;
      }
    }
    syncElementTransform(settings, type, index, model);
  };
  const finish = () => {
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", finish);
    window.removeEventListener("pointercancel", finish);
    node.style.transform = "";
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
    <div class="editable-element text-element ${selected?.type === "text" && selected.index === index ? "selected" : ""}" data-element-type="text" data-element-index="${index}" tabindex="0" aria-label="拖动第 ${index + 1} 行标题">
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
      <div class="editable-element sticker-element ${isSelected ? "selected" : ""}" data-element-type="sticker" data-element-index="${index}" tabindex="0" aria-label="拖动贴图 ${escapeHtml(asset.name)}">
        <img src="/api/stickers/${asset.id}/image" alt="${escapeHtml(asset.name)}" draggable="false">
        <button class="element-delete" type="button" data-delete-sticker="${sticker.uid}" title="删除贴图" aria-label="删除贴图">×</button>
        <span class="resize-handle" data-resize-handle title="缩放贴图"></span>
      </div>
    `;
  }).join("");
  elements["cover-overlay"].innerHTML = stickerNodes + textNodes;
  syncOverlayScale();

  elements["cover-overlay"].querySelectorAll(".editable-element").forEach((node) => {
    const type = node.dataset.elementType;
    const index = Number(node.dataset.elementIndex);
    node.addEventListener("pointerdown", (event) => {
      if (event.target.closest("[data-resize-handle], [data-delete-sticker]")) return;
      beginElementInteraction(event, type, index, "move");
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

async function refreshPreview() {
  const task = activeTask();
  if (!task || !task.candidates.length) {
    return;
  }
  const taskId = task.id;
  const ratio = state.ratio;
  const requestId = ++state.previewRequestId;
  elements["preview-loader"].hidden = false;
  elements["preview-state"].textContent = "正在渲染";
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
    const backgroundToken = payload.preview.background_media_token || payload.preview.media_token;
    elements["cover-preview"].src = `/api/media/${backgroundToken}?v=${Date.now()}`;
    elements["cover-preview"].hidden = false;
    elements["preview-empty"].hidden = true;
    renderCoverOverlay(payload.preview);
    elements["preview-state"].textContent = `${payload.preview.width} × ${payload.preview.height}`;
    setStatus("预览已更新", task.filename);
  } catch (error) {
    if (requestId !== state.previewRequestId) return;
    clearPreview("预览生成失败");
    setStatus("预览失败", error.message, "error");
    throw error;
  } finally {
    if (requestId === state.previewRequestId) {
      elements["preview-loader"].hidden = true;
    }
  }
}

function clearPreview(message) {
  state.preview = null;
  state.selectedElement = null;
  elements["cover-preview"].hidden = true;
  elements["cover-preview"].removeAttribute("src");
  elements["cover-overlay"].replaceChildren();
  elements["cover-overlay"].removeAttribute("style");
  elements["preview-empty"].hidden = false;
  elements["preview-empty"].textContent = message;
  elements["preview-state"].textContent = message;
}

function schedulePreview() {
  window.clearTimeout(state.previewTimer);
  state.previewTimer = window.setTimeout(() => {
    refreshPreview().catch((error) => console.error("自动预览失败", error));
  }, 420);
}

async function saveCover(canvases) {
  const task = activeTask();
  if (!task) {
    return;
  }
  const currentOnly = canvases.length === 1;
  const ratioLabel = canvases[0] === "4x3" ? "4:3" : "16:9";
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
  if (!state.tasks.length) {
    return;
  }
  elements["batch-export"].disabled = true;
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
    elements["batch-export"].disabled = false;
    setBusy(false);
  }
}

function openWorkspaceDialog() {
  const defaultInput = state.options?.default_input_dir || LEGACY_DEFAULT_INPUT_DIR;
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
  elements["workspace-dialog"].showModal();
  elements["root-path"].focus();
}

function bindEditor() {
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
  elements["refresh-stickers"].addEventListener("click", async () => {
    elements["refresh-stickers"].disabled = true;
    try {
      await loadStickers(true);
      setStatus("贴图库已刷新", `${state.stickerAssets.length} 张表情贴图`);
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
}

function bindEvents() {
  elements["open-workspace"].addEventListener("click", openWorkspaceDialog);
  elements.rescan.addEventListener("click", () => {
    scanWorkspace(state.workspaceConfig).catch(() => {});
  });
  elements["scan-submit"].addEventListener("click", () => {
    const root = elements["root-path"].value.trim();
    if (!root) {
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
  document.querySelectorAll("[data-ratio]").forEach((button) => {
    button.addEventListener("click", () => {
      state.ratio = button.dataset.ratio;
      document.querySelectorAll("[data-ratio]").forEach((item) => {
        const active = item.dataset.ratio === state.ratio;
        item.classList.toggle("active", active);
        item.setAttribute("aria-selected", String(active));
      });
      elements["cover-frame"].className = `cover-frame ratio-${state.ratio}`;
      state.previewRequestId += 1;
      state.selectedElement = null;
      renderInspector(activeTask());
      refreshPreview().catch(() => {});
    });
  });
  elements["sync-ratios"].addEventListener("change", () => {
    state.syncRatios = elements["sync-ratios"].checked;
    localStorage.setItem("autocover.sync-ratios", String(state.syncRatios));
    const task = activeTask();
    if (state.syncRatios && task) {
      synchronizeCurrentLayout(taskSettings(task));
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
  bindEditor();
}

async function boot() {
  cacheElements();
  state.syncRatios = localStorage.getItem("autocover.sync-ratios") !== "false";
  elements["sync-ratios"].checked = state.syncRatios;
  bindEvents();
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
      await scanWorkspace(config);
      return;
    } catch (error) {
      localStorage.removeItem("autocover.workspace");
    }
  }
  openWorkspaceDialog();
}

window.addEventListener("DOMContentLoaded", boot);
