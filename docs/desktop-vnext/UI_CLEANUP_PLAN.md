# AutoCover Beta UI Cleanup — 改动清单与实施对照

> **状态：部分实现已随 2026-10-07 发布整理提交，实机验收待完成。**
> 本文写作时仓库位于 `desktop-vnext-dev` 分支、提交 `2711f2c`。当时 `git diff HEAD` 为空，仅表示已跟踪文件没有 diff；仓库仍可能存在未跟踪/忽略的审计、截图、测试产物和工作目录。
> 文中所有行号对应 `2711f2c`。实施前请重新核对 HEAD、tracked diff、untracked files 和 ignored artifacts。
>
> 原始核查和改动建议保留作历史依据；当前进度以以下实施对照及 `DESKTOP_STATUS.md` 为准。

## 2026-10-07 实施对照

| 条目 | 当前代码状态 | 剩余验收 |
|---|---|---|
| P0-1 输出契约 | 导出信息已加入可见工具栏，完整路径通过 tooltip 展示 | 小窗口下文件名、比例与目录可读性 |
| P0-2 操作图标 | 撤销、重做、折叠和 AI 入口改用 SVG 图标 | 真实 Windows 字体和 DPI |
| P1-1 另一比例预览 | 默认隐藏，通过按钮按需排队渲染 | 反复切比例、开关与旧回调 |
| P1-2 空状态 | 画布显示前往字幕页选择项目和视频的引导 | 首次使用流程；旧空面板暂保留 |
| P1-3 来源帧 | 附近帧增加 checked 状态与缩略图样式 | 选帧、锁帧和播放头变化 |
| P2-1/P2-2 一致性 | 画布颜色使用主题令牌，状态及导出文案收口 | 真实截图与错误可见性 |

本轮还整理了 AI 候选、Undo/Redo、延迟保存与预览的文档状态一致性，以及 Qt/Pillow
的字体解析。以下章节中的行号、基线数量和待实施表述属于 `2711f2c` 时的历史快照。

---

## 1. 为什么有这份文档

Desktop vNext 的 `DESKTOP_STATUS.md` / `ROADMAP.md` 把「AutoCover Beta UI Cleanup + 统一实机验收」列为下一阶段最高优先级，但只给了方向，没落到具体文件和可验证条目。本文补上这一层：每条问题都有代码位置、预期效果、风险和验收方式，便于逐条批注和分批执行。

**重要前提**：代码里已经有一部分 UI Cleanup 落地了（最近三次提交 `51b98b6` → `253796d` → `2711f2c`），右侧面板已是 `QStackedWidget` 上下文栈 + 空状态 + 面板折叠。UX 审计里的 P0「拖字跳动」也已修复（`cover_canvas.py` 已有 `_corner_at` / `_snap_to_center` / `_set_alignment_guides` / hover 态）。

所以**不要按旧审计文档重复施工**。本文只列在 `2711f2c` 上仍然成立的问题。

---

## 2. 现状核查（已在 `2711f2c` 上确认）

| # | 事实 | 证据 |
|---|---|---|
| 1 | `export_summary` 被完整赋值，但**从未加入任何布局** | 创建于 `cover.py:201-203`；全文无 `addWidget(self.export_summary)`；赋值点 `1210 / 1222 / 2037 / 2060` |
| 2 | 另一比例检查图是**常驻 48px 固定行** | `cover.py:248-256`，`setFixedHeight(48)` 在 `254` |
| 3 | 撤销/重做/面板折叠/AI 入口使用 **Unicode 字符或 emoji** 当图标 | `cover.py:154 ↶`、`160 ↷`、`190 ▶`、`382 ◀`、`389 ▶`、`298 ✨` |
| 4 | 画布有 3 处**硬编码颜色**，绕过主题系统 | `cover_canvas.py:99`、`692`、`702` |
| 5 | 附近帧按钮 48–56px，图标 80×45，**只有时间戳、无选中反馈** | `cover.py:351-353`；文档要求「当前选中帧必须有明显反馈」（AUTOCOVER_SPEC 第 2 节） |
| 6 | 无项目时画布只显示占位文案，**没有下一步引导** | `cover.py:1395` → `加载底图后在这里预览` |
| 7 | `_build_empty_panel` 是**死代码** | `_hide_panel()` 与 `_show_empty_panel()` 恒成对出现（`365/367`、`1406/1408`），面板被隐藏，里面那套素材入口用户永远看不到 |

第 7 条是排查中的意外发现：它意味着「右侧面板空状态」这个方向是伪命题，真正的空状态在**画布区**（`CoverCanvas` 是 `QLabel`，无位图时回落到 QLabel 绘制文本）。后续所有空状态工作都应针对画布区，不要去修那个面板。

---

## 3. 改动清单

按优先级排。P0 是「有缺陷」级别，P1 是「体验收口」，P2 是「一致性」。

### P0-1 让输出契约真正可见

**问题**：`export_summary` 算完了但没人看得见。用户点「导出」之前无法确认导出的是什么比例、什么文件名、写到哪。这是 `AUTOCOVER_UX_AUDIT.md` 里 P1「16:9 和导出入口不明显」的代码级根因。

**改法**：
1. `cover.py:198-203` 的隐藏控件区保留 `export_summary`，但取消 `setWordWrap(True)`（改为单行）。
2. 在画布下方新增一行常驻状态条：左侧放 `canvas_hint`，右侧放 `export_summary`。
3. 状态条替换掉原检查图行的位置（见 P1-1），高度约 15–20px。

**预期**：进入封面页即可看到 `输出 AutoCover-xxx.jpg · 4:3 1440×1080`。

**风险**：低。新增控件不改动任何状态机。

**验收**：截图确认状态行可见；`test_desktop_cover.py` 中既有对 `export_summary.text()` 的断言应保持通过。

---

### P0-2 图标改用项目已有的 SVG 图标系统

**问题**：`↶ ↷` 在目标字体下**渲染成两个几乎不可见的小点**（实机截图确认）；`▶ ◀` 和 emoji `✨` 属同类问题。这类字符图标在不同 Windows 字体环境下不可靠。

**改法**：
1. `qt_preview/icons.py` 的 `_PATHS` 增加 `undo` / `redo` / `chevron-left` / `chevron-right` / `sparkle` 五个矢量路径。
2. `cover.py` 顶部引入 `from .qt_preview.icons import icon as desktop_icon` 和 `from .qt_preview.theme import COLORS`（`timeline.py`、`window.py` 已是这个用法）。
3. 替换 `154 / 160 / 190 / 298` 四处按钮文本；`382 / 389` 的折叠状态切换改为换图标而非换文字。

**预期**：撤销/重做/折叠在任意字体环境下都正常显示，且与字幕页图标同源。

**风险**：低。`icon()` 带 `@lru_cache`，无性能顾虑。

**验收**：实机截图确认图标可见；现有测试不依赖这些按钮文本，应无影响。

---

### P1-1 把垂直空间还给画布

**问题**：检查图常驻 48px（`cover.py:254`），不管用不用都占位，把 4:3 主画布上下夹住。

**改法**：
1. 抽掉固定的 `check_row`，把 `check_preview` 变成独立控件，**默认 `setVisible(False)`**。
2. 工具栏加一个 checkable 的「另一比例」开关，展开时显示（高度收到 40px），收起时隐藏。
3. `_queue_check_preview()` 增加可见性前置判断——**收起时连渲染排队一起跳过**，不为看不见的图付渲染成本（符合 D-015）。
4. 开关与比例切换按钮同组（都是画布视图控制），不要挨着导出按钮。

**预期**：画布高度增加约 39px（实测 738 → 777，+5.3%）。

**风险**：中。涉及异步任务可见性。必须确认 `_check_preview_ready` 在控件隐藏时不会因 `size()` 非法而崩溃——建议展开时再触发一次渲染。

**验收**：切到 4:3 / 16:9 并反复开关，确认无异常、无残留旧图；`test_desktop_cover.py` 的预览一致性测试保持通过。

---

### P1-2 无项目首屏给出明确引导

**问题**：`cover.py:1395` 只显示 `加载底图后在这里预览`。用户既不知道当前是"没选项目"这个状态，也不知道要去字幕页选项目。对应 `AUTOCOVER_UX_AUDIT.md` P1「首屏没把现在是什么状态、下一步做什么说清楚」。

**改法**：
1. 无项目时画布文案改为两行居中引导，例如 `封面页还没有项目` + `先到「字幕」页打开一个投稿项目，再回来做封面`。
2. `_update_canvas_hint()`（`cover.py:1175`）改为状态感知：无项目时给引导语，有项目时才给操作提示。
3. 顺带处置死代码 `_build_empty_panel`：要么删除（连带 `_show_empty_panel`），要么改造成真正可见的空状态。**建议先删除**——它现在不可见，留着会误导后续开发。

**风险**：中。删除会影响 `panel_stack` 的页序与相关测试。**改文案前务必用最短特征子串 grep 测试**（教训见第 6 节）。

**验收**：`test_stale_frame_callback_is_ignored_after_context_reset` 断言了 `assertIn("加载底图", canvas.text())`，必须同步更新为新空状态文案。这是本次唯一预期需要改测试的地方，改动前先跟用户确认。

---

### P1-3 附近帧条给出来源帧反馈

**问题**：7 个缩略图只有时间戳，当前底图来自哪一帧看不出来，而 `AUTOCOVER_SPEC.md` 明确要求「当前选中帧必须有明显反馈」。

**改法**：
1. 帧按钮改 `setCheckable(True)`，加 `objectName`。
2. `theme.py` 增加选中样式（accent 描边）。
3. 新增 `_sync_frame_selection()`：按 `draft.selected_timestamp` 与各按钮时间戳匹配（容差 0.21s，偏移步长 0.4s），在 `_refresh_nearby_frame_strip()` 末尾统一对位。换帧、移播放头后自动重算。

**预期**：当前底图来源帧在帧条里高亮；底图来自「当前帧」或任意取帧时，帧条全部不高亮（因为不在视野内）。

**风险**：低。纯显示层，不碰取帧逻辑。

**验收**：选帧后截图确认高亮正确；切换帧与移动播放头后确认高亮跟随。

---

### P2-1 视觉令牌收口

把 `cover_canvas.py:99 / 692 / 702` 的硬编码色（`#111820`、`#202a33`、`#080b0e`）改为引用 `theme.COLORS`，让主题单点可控。低风险，建议与 P0-2 同一批做（都要动 `theme.py` / `icons.py`）。

---

### P2-2 文案降噪

工具栏状态文案从整句改为状态词，解释移入 tooltip：

| 位置 | 现值 | 拟改 |
|---|---|---|
| `cover.py:1391` | `尚未加载封面草稿` | `未加载草稿` |
| `cover.py:1467` | `当前视频不可用：{video_path}` | `视频不可用`（路径进 tooltip） |
| `cover.py:1469` | `已恢复封面草稿` | `已恢复草稿` |
| `cover.py:1471` | `暂无草稿，编辑会自动保存` | `新草稿 · 自动保存` |
| `cover.py:1736` | `草稿已保存到本机应用数据` | `已保存` |

导出按钮文案同步明确化：`导出当前 4:3 主封面` → `导出 4:3`（比例进按钮），`双比例` → `导出 4:3 + 16:9`；旁边的可见导出状态条补上尺寸、目录和“不覆盖已有文件”。tooltip 只做补充，不能成为用户获知输出比例和文件信息的唯一渠道。

**风险**：低。但 `test_desktop_cover.py:286 / 307` 对 `export_button.text()` 有 `assertIn("4:3", ...)` 断言，改文案时保留比例字样即可通过。

---

### 复审补充：状态可见性与缩略图比例（2026-10-05）

后续设计审计和代码复核补充确认了三类不应漏掉的问题：

1. **checkable 状态必须肉眼可见。** 4:3/16:9、A/B、对齐、锁帧和附近帧等控件不能只有内部 checked 状态；必须在真实 Windows 主题和字体下看得出当前选择。
2. **附近帧缩略图不能被全局 QSS 压扁。** 需要同时检查按钮高度、iconSize、全局 `QPushButton` 最小高度和小窗口下时间戳/时间码截断，目标是保持缩略图原始比例与可读标签。
3. **状态出口必须显式。** `export_summary`、错误/警告、AI 失败和安全区提示不能只是“代码里更新了一个 QLabel”；是否可见应由显式状态决定，错误不能被 routine 文案覆盖。不要把所有隐藏控件粗暴塞进一条状态栏，而应按导出、画布、对象上下文和错误语义分流。

这三项属于 UI Cleanup 的当前有效补充；全局 14px 字号、AI 面板默认展开、四个常驻控制点和全面进度条仍需实机验证，不在本计划中冻结。

---

## 4. 建议执行顺序

| 批次 | 内容 | 理由 |
|---|---|---|
| 第 1 批 | P0-1、P0-2、P2-1、P2-2 | 都是低风险、不碰状态机，可一次验证 |
| 第 2 批 | P1-3 + checkable 选中态 + 附近帧缩略图比例/小窗口截断 | 主要是显示层，依赖第 1 批的主题与图标改动 |
| 第 3 批 | P1-1、P1-2 + 显式错误/状态出口 | 涉及异步可见性和面板结构，需要单独验证；P1-2 需先确认测试改动 |

每批之间跑一次回归，互不影响。

---

## 5. 验证方式

**回归测试**（约 85 秒）：

```powershell
# 在仓库根目录执行；使用已安装 Qt 测试依赖的 Python 3.10
$env:PYTHONPATH = (Join-Path $PWD "src")
$env:QT_QPA_PLATFORM = "offscreen"
python -m unittest tests.unit.test_desktop_cover tests.unit.test_desktop_cover_canvas_phase2 tests.unit.test_desktop_cover_model tests.unit.test_desktop_cover_copy tests.unit.test_desktop_app tests.unit.test_desktop_interaction -q
```

> 注意：项目的 Qt venv（`%LOCALAPPDATA%\AutoSlice\qt-preview-venv`）**没有 pytest**。跑测试要用系统 Python 3.10（自带 pytest 8.4.2 + PySide6）。基线：`2711f2c` 上 58 passed。

**视觉验收**：单测只证明契约，不证明视觉（D-017）。建议做一个 offscreen 截图脚本（构造临时项目 → 实例化 `CoverEditorWidget` → `widget.grab().save(png)`）作为视觉回归工具。offscreen 环境缺中文字体，文字会渲染成方框，属正常现象，不是缺陷。

**人工验收**：按 `ROADMAP.md` 的门槛，用真实项目、真实字体、目标窗口尺寸走一遍「打开 → 选帧 → 改文案 → 调整 → 导出」。

---

## 6. 两条实施纪律（本次踩到的坑）

1. **改文案前用最短特征子串 grep 测试。** 本次改空状态文案时，只 grep 了完整串 `加载底图后在这里预览`（无结果）就以为没测试依赖，实际测试断言的是子串 `加载底图`，导致改完测试挂掉。**短于完整文案的特征串才是有效搜索键。**

2. **不要按旧 UX 审计重复施工。** 审计写于 `2026-10-02`，其中 P0「拖文字跳动」「选中反馈/吸附」已在 `dc73333` 之后的提交中修复。以本文第 2 节的核查结果为准。

---

## 7. 明确不做

沿用 `ROADMAP.md` 的暂缓清单，本文不涉及：视频剪辑、复杂图层/蒙版/滤镜、模板市场、默认运行 AI、默认全片扫描、自动投稿、批量导出。

另外两条本次明确排除：

- **旧版候选帧质量评分迁移**（曝光/清晰度/饱和度打分、烧录字幕风险提示，来自 `AutoCover`）。这属于功能开发而非 UI 收口，范围和风险都大，应单独立项。
- **`window.py` 2728 行 / `cover.py` 2065 行的拆分**。等 UI 通过实机验收后再动。（2026-10-08：用户要求先整理代码，已在 `desktop-cover-opus55` 分支按职责拆分，行为不变，见 DESKTOP_STATUS。）

---

## 8. 还原方式

本文写作时**工作区是干净的**（`git diff HEAD` 为空），无需还原。

后续每批改动落地前，先建还原点再动手：

```bash
# 在仓库根目录执行；.autoslice-state/ 已在 .gitignore，不污染仓库
BK=".autoslice-state/ui-backup-$(date +%Y-%m-%d)"
mkdir -p "$BK"
cp src/autoslice/desktop/cover.py "$BK/"
cp src/autoslice/desktop/cover_canvas.py "$BK/"
cp src/autoslice/desktop/qt_preview/icons.py "$BK/"
cp src/autoslice/desktop/qt_preview/theme.py "$BK/"
git rev-parse HEAD > "$BK/HEAD.txt"
```

还原到原始状态：

```bash
git checkout -- src/autoslice/desktop/cover.py src/autoslice/desktop/cover_canvas.py \
  src/autoslice/desktop/qt_preview/icons.py src/autoslice/desktop/qt_preview/theme.py \
  tests/unit/test_desktop_cover.py
```

> 本机注意：WorkBuddy 的删除钩子会把 `rm` 改送回收站，且在 Git Bash 通道下路径转换会失败。删文件优先用 PowerShell 通道的 `Remove-Item -LiteralPath`，或直接改用重命名。
