---
plan_id: backend-hardening-next
plan_version: 1
schema_version: 1
---

# AutoSlice 后端安全、健壮性与发布治理专项计划

> 本文件只保存专项计划规格。当前活动计划、当前 Action 和运行时状态只以根目录
> `ACTIVE_PLAN.md` 为准。

## 1. 目标

在不破坏已经完成的前端成果和现有 loopback 本机工作流的前提下，完成以下收口：

- LAN 模式的最终有效路径、登记产物和响应脱敏边界；
- CI 独立治理门禁和 wheel 反例；
- 包发现、时间轴精确匹配、深层 JSON 和空媒体健壮性；
- AutoCover 图片输入预算和异常隔离；
- 架构文档、Action ledger 和 LLM transport 治理；
- 适用 Action 的测试、提交、当前 CI 和公开总账证据。

H4 真实验收不属于普通代码加固，必须单独授权。

## 2. 权威边界与状态

- 本计划不复制审阅时的 HEAD、测试数量、CI run ID、文件行号或链接数量作为永久事实；
- 每个 Action 开始前必须重新读取 `ACTIVE_PLAN.md`、Git、计划版本、ledger、workflow 和相关源码；
- Action 和计划状态只使用 `pending`、`skipped`、`completed`；
- `skipped` 必须记录 `reason`、`scope` 和 `reopen_when`；
- 部署证据使用独立的 `evidence_status`、`blocker_reason`、`reopen_when`，不得新增
  `blocked` 作为 `ACTIVE_PLAN.md` 状态；
- 未确认 LAN 根的来源、所有者、ACL 或可写性时，相关 LAN 能力必须 fail-closed；
- 一个 Action 一个逻辑提交；只有当前 commit 对应的 Windows/Linux CI 双绿后才能登记完成；
- commit 和 push 分别服从用户授权，不使用历史 CI 代替当前证据；
- `execution_mode` 固定为 `single-action-stop`，完成一个 Action 后立即停止。

## 3. 范围外事项

- 不调用真实 LLM、FunASR、FFmpeg、CUDA、GPU 或用户媒体；
- 不执行整场录播字幕压制；
- 不启动真实 Flask、AutoCover、浏览器或 LAN 监听；
- 不修改全局规则、全局技能或其他项目；
- 不做依赖 CVE 在线审计、版本升级或无关架构重写；
- 不开放任意 `path=`、整场源视频或任意安装目录枚举；
- 不执行 `H4-REAL-SMOKE`，除非获得逐项真实资源授权。

## 4. Action 总览

| Action | 依赖 | 目标 | 条件性 |
|---|---|---|---|
| H0-SEC-PATH-BOUNDARY | 无 | LAN 最终路径和响应边界 | 启用 LAN 前必须完成 |
| H0-CI-GATES | 无 | 三项独立 CI 治理门禁 | 否 |
| H1-BAD-WHEEL | H0-CI-GATES | 自动证明坏 wheel 会失败 | 否 |
| H1-PACKAGE-DISCOVERY | H1-BAD-WHEEL | 受限自动包发现 | 否 |
| H2-TIME-MATCH | H1-PACKAGE-DISCOVERY | 精确 manifest 时间匹配 | 否 |
| H2-MEDIA-ROBUST | H2-TIME-MATCH | 深层 JSON 和空媒体健壮性 | 否 |
| H2-COVER-INPUT-ROBUST | H1-PACKAGE-DISCOVERY | 图片预算和异常隔离 | 否 |
| H3-ARCH-DOC | H2-MEDIA-ROBUST、H2-COVER-INPUT-ROBUST | 架构 owner 与快照说明 | 否 |
| H3-LEDGER-GUARD | H3-ARCH-DOC | 计划、总账和公开链接校验 | 否 |
| H3-LLM-TRANSPORT-HARDENING | H3-LEDGER-GUARD | 非 loopback HTTP 风险控制 | 使用前必须完成 |
| H4-REAL-SMOKE | 所有适用 H0～H3 | 有限真实闭环验收 | 永远单独授权 |

## 5. Phase H0：安全前置和 CI 门禁

### Action H0-SEC-PATH-BOUNDARY

**目标：**所有最终被读取、枚举、写入或交给媒体服务的路径都经过 canonical path、
allowed-root、任务归属和资源类型检查。

**范围：**

```text
architecture_baseline.json
tests/architecture/test_architecture_contracts.py
src/autoslice/security_policy.py
src/autoslice/web/app.py
src/autoslice/media_preview.py
src/autoslice_cover/app.py
src/autoslice_cover/paths.py
src/autoslice_cover/workspace.py
src/autoslice_cover/stickers.py
tests/unit/test_security_policy.py
tests/unit/autoslice_cover/test_workspace.py
tests/integration/test_app.py
tests/integration/test_media_preview.py
tests/integration/autoslice_cover/test_app.py
```

**必须实现：**

1. 显式路径和省略参数后的默认路径都验证最终 effective path；
2. 覆盖 AutoSlice 视频、输出、时间轴、投稿和两类上传目录；
3. 覆盖 AutoCover input、output、cache、sticker、user asset、workspace 和 draft；
4. manifest、artifact、report、timeline、clip 和 media-token 文件在登记和消费阶段二次校验；
5. 资源根按来源、所有者、ACL、可写性和写入入口分类，不按变量名推断；
6. canonicalization、符号链接、Windows reparse point 和删除后重建同名文件 fail-closed；
7. LAN 成功 JSON、错误 JSON 和 HTML 初始化数据不泄露不必要的绝对路径；
8. loopback 只回归受影响路由、共享 serializer 和页面；纯 LAN 修复不强迫无关契约迁移；
9. 应用拥有资源通过资源 ID、白名单或受控 API，不把整个安装目录加入 allowed roots；
10. 边界拒绝使用稳定 4xx/503，不返回 traceback、token、字幕正文或真实路径。

**定向验证：**

```powershell
python -B -m unittest tests.unit.test_security_policy -q
python -B -m unittest tests.integration.test_app -q
python -B -m unittest tests.integration.autoslice_cover.test_app -q
python -B scripts/architecture_snapshot.py --check
git diff --check
```

**完成条件：**隔离临时目录中的 allowed-root 正反例、默认目录、登记产物、链接越界、
LAN 脱敏和 loopback 定向回归全部通过；独立提交及当前 Windows/Linux CI 双绿。

**条件语义：**只有用户明确决定长期 loopback-only 时才允许 `skipped`，并记录
`reason/scope/reopen_when`；否则保持 `pending`。完成前不得宣称 LAN 安全可用。

### Action H0-CI-GATES

**目标：**在 Windows 和 Linux job 中分别增加三个可独立定位的治理 step：

```powershell
python -B scripts/architecture_snapshot.py --check
python -B scripts/validate_public_docs.py
python -B scripts/scan_public_release.py
```

**范围：**`.github/workflows/tests.yml`。

**定向验证：**

```powershell
python -B scripts/architecture_snapshot.py --check
python -B scripts/validate_public_docs.py
python -B scripts/scan_public_release.py
git diff --check
```

**完成条件：**workflow 进入新 commit；该 commit 触发新 CI；Windows/Linux 日志都分别
显示三个独立 step 且通过。历史 run 或只有本地返回 0 均不算完成。

## 6. Phase H1：发布与 wheel 防复发

### Action H1-BAD-WHEEL

**目标：**自动证明发布门禁能拦截已知漏包形态，而不只证明当前 wheel 成功。

**必须实现：**

- 在隔离 source copy 中制造受控坏 wheel；
- 仓库外环境安装后，缺失深层包的导入必须失败；
- 失败原因必须是 wheel 缺包，而不是源码树或当前工作目录污染；
- 正常 wheel 同轮仍须成功；
- 临时目录结束后清理，不修改真实源码树。

**定向验证：**

```powershell
python -B scripts/verify_distribution.py
python -B scripts/smoke_packaging.py
python -B scripts/architecture_snapshot.py --check
git diff --check
```

**完成条件：**坏 wheel 反例红、正常 wheel 绿，仓库外深层导入和两个 CLI 通过；独立提交
及当前 CI 双绿。

### Action H1-PACKAGE-DISCOVERY

**目标：**将手工包列表改为受限自动发现，同时保持发布资源和 CLI 契约。

**必须实现：**

- 使用 `src` 布局下的受限自动包发现；
- 保持 `namespaces = false` 或等价约束；
- 不把 `resources` 目录变成 Python 包；
- 保留模板、静态文件、JSON 资源和 `autoslice`、`autoslice-setup-asr`、`autoslice-cover` CLI；
- 不升级依赖、版本或无关构建配置；
- 验证源码态、editable 和 wheel。

**定向验证：**

```powershell
python -B scripts/smoke_packaging.py
python -B scripts/verify_distribution.py
python -B -m unittest tests.architecture.test_architecture_contracts -q
python -B scripts/architecture_snapshot.py --check
git diff --check
```

**完成条件：**三种安装形态一致，资源和 CLI 均存在，坏 wheel 门禁继续有效；独立提交及
当前 CI 双绿。

## 7. Phase H2：时间轴、媒体和图片输入

### Action H2-TIME-MATCH

**目标：**优先使用有效精确字段匹配唯一 `clip_id`，同时兼容旧整数 manifest。

**契约：**

- 精确字段必须成对、为非 bool 的有限数值且 `end > start` 才优先；
- 缺失、null、单边、类型错误、NaN、Infinity 或无效区间时，回退成对有效旧字段；
- `0` 和 `0.0` 合法；
- 两组有效且容差外冲突时不猜；容差内按明确唯一匹配契约处理；
- 多个匹配、空 ID、无效 ID 或路径字符 ID 不附加；
- 不修改时间轴编辑、重切片或 `clip_marks`。

**定向验证：**

```powershell
python -B -m unittest tests.integration.test_app.TimelineApiTests -q
python -B -m unittest tests.unit.test_refinement_cover_contract -q
python -B scripts/architecture_snapshot.py --check
git diff --check
```

**完成条件：**覆盖 null 生产者字段、单边、bool、0、无效区间、容差内外、多匹配和旧整数
样本；独立提交及当前 CI 双绿。

### Action H2-MEDIA-ROBUST

**目标：**深层/超大 JSON 和零字节媒体进入稳定、脱敏且按调用方区分的契约。

**必须实现：**

- 命名并测试最大深度、节点数、数组长度、对象字段数、请求体大小和必要的字符串总长度；
- 在业务递归前检查结构预算；`RecursionError` 只作最后兜底；
- 时间轴读取明确返回不完整或既有拒绝契约；
- 媒体 token、媒体流和上传校验稳定 4xx；
- `SecurityPolicy` 遍历失败必须 fail-closed；
- 已登记 manifest 按调用方拒绝或返回不完整，不静默猜测；
- 零字节媒体不签发 token；GET、HEAD、Range 不产生错误长度；
- 保留跨任务、任意 path、symlink、源视频、过期 token 和 TTL 保护。

**定向验证：**

```powershell
python -B -m unittest tests.integration.test_media_preview -q
python -B -m unittest tests.integration.test_app.TimelineApiTests -q
python -B -m unittest tests.unit.test_security_policy -q
python -B -m ruff check src/autoslice/media_preview.py tests/integration/test_media_preview.py
python -B scripts/architecture_snapshot.py --check
git diff --check
```

**完成条件：**每个路由矩阵均有深度/节点预算正反例；零字节和正常 1 字节语义正确；
错误不泄露路径、token 或异常文本；独立提交及当前 CI 双绿。

### Action H2-COVER-INPUT-ROBUST

**目标：**为贴图导入、扫描和应用初始化建立可审计解码预算与异常隔离。

**必须实现：**

- 命名最大原始字节、像素、宽度、高度和动画帧数预算；
- 元数据检查与实际解码分别验证；
- 捕获 Pillow `DecompressionBombError`、`UnidentifiedImageError` 和 `OSError`；
- 初始化扫描、刷新扫描、导入后扫描和残留坏文件下次初始化均稳定；
- 正常 PNG/JPEG/WebP 回归；GIF 不作为既定支持格式；
- 使用 Flask 测试客户端，不监听真实端口；
- 单个坏贴图不阻断正常贴图，不删除用户已有原文件；
- 错误和日志不泄露路径、traceback 或 Pillow 内部文本。

**定向验证：**

```powershell
python -B -m unittest tests.unit.autoslice_cover.test_stickers -q
python -B -m unittest tests.integration.autoslice_cover.test_app -q
python -B scripts/architecture_snapshot.py --check
git diff --check
```

**完成条件：**每项预算覆盖刚好允许/超过边界；坏贴图初始化与扫描、正常 WebP 解码、
不删除原文件均有测试；独立提交及当前 CI 双绿。

## 8. Phase H3：架构与治理

### Action H3-ARCH-DOC

**目标：**更新架构 owner、baseline 和快照来源，避免把数字写成永久事实。

**必须实现：**

- 明确完整时间轴和 `media_preview` 的独立 owner；
- `quality_overview` 只作为可截断摘要；
- 记录数字生成方式和快照日期，不把行数/数量作为永久完成条件；
- 不混入业务重构。

**定向验证：**

```powershell
python -B scripts/architecture_snapshot.py --check
python -B scripts/validate_public_docs.py
python -B scripts/scan_public_release.py
git diff --check
```

**完成条件：**架构文档、baseline 和代码 owner 一致；独立提交及当前 CI 双绿。

### Action H3-LEDGER-GUARD

**目标：**动态校验公开计划、总账、Action ID、依赖和仓库内链接。

**必须实现：**

- 动态扫描所有 commit 链接，不硬编码当前 occurrence 或行数；
- 校验 `plan_ref`、`ledger_ref`、Action ID 唯一、依赖存在且无环；
- 校验仓库内相对链接；
- hermetic 检查不访问 GitHub 网络；
- 公开文件不得含本机路径、Token、Cookie、用户媒体或 API 配置。

**定向验证：**

```powershell
python -B scripts/validate_public_docs.py
python -B scripts/scan_public_release.py
python -B -m unittest tests.architecture.test_architecture_contracts -q
git diff --check
```

**完成条件：**有效链接通过，受控失效链接和重复 ID 反例失败；独立提交及当前 CI 双绿。

### Action H3-LLM-TRANSPORT-HARDENING

**目标：**保留本机 HTTP，同时控制非 loopback 明文 endpoint 的凭据与内容风险。

**必须实现：**

- `127.0.0.1`、`localhost` 和等价 loopback HTTP 保持可用；
- 非 loopback `http://` 默认警告或拒绝；
- 不安全内网 HTTP 需要明确 opt-in；
- 测试使用 mock transport，不调用真实 endpoint；
- token、字幕、prompt 和响应不进入日志；
- 文档明确 HTTP 不提供机密性，不声称已完成依赖 CVE 审计。

**定向验证：**

```powershell
python -B -m unittest tests.unit.test_llm_client tests.unit.test_llm_contracts -q
python -B scripts/validate_public_docs.py
python -B scripts/scan_public_release.py
python -B scripts/architecture_snapshot.py --check
git diff --check
```

**条件语义：**未使用非 loopback endpoint 时可 `skipped`，但必须记录
`reason/scope/reopen_when`；任何启用前必须重新打开。

## 9. Phase H4：有限真实验收

### Action H4-REAL-SMOKE

只有在以下条件全部满足后才可单独授权：

- 所有适用 H0～H3 为 `completed`；
- 不适用条件 Action 已 `skipped` 并记录 `reason/scope/reopen_when`；
- 当前 commit 对应的新 Windows/Linux CI 双绿；
- `ACTIVE_PLAN.md`、本计划、ledger 和 Git preflight 通过；
- 本次真实录播、最终 SRT、FunASR、FFmpeg、CUDA/GPU、Luna、Terra 和 AutoCover
  输入按实际使用范围逐项授权。

完整长录播不做整场字幕压制；字幕压制只使用最终短片或代表性短样本。不得覆盖用户
原始媒体、SRT、人工时间轴或草稿。Luna/Terra fallback 不得伪装成 AI 成功。H4 失败时
建立新的修复 Action，不把 H4 标记为完成。

## 10. 每个 Action 的统一门禁

除 Action 自己的定向命令外，完成前还必须运行：

```powershell
python -B scripts/run_hermetic_tests.py -q
python -B scripts/run_linux_logic_tests.py -q
python -B scripts/architecture_snapshot.py --check
python -B scripts/validate_public_docs.py
python -B scripts/scan_public_release.py
python -B scripts/compile_public.py
python -B -m compileall -q src tests scripts
git diff --check
```

涉及打包时追加：

```powershell
python -B scripts/smoke_packaging.py
python -B scripts/verify_distribution.py
```

涉及 AutoCover JavaScript 时追加：

```powershell
node --check src/autoslice_cover/resources/static/app.js
```

完成流程固定为：

```text
preflight → 实现 → 定向测试 → 修复并重跑 → 完整本地门禁
→ 明确授权后提交 → 明确授权后推送 → 当前 commit 的 Windows/Linux CI 双绿
→ 更新 ledger 与 ACTIVE_PLAN → 停止
```

## 11. 停止条件

出现以下任一情况立即停止并报告，不自动回滚或猜测：

- HEAD、工作区、`ACTIVE_PLAN.md`、计划版本或 Action 依赖发生漂移；
- 出现无法归因的用户修改或敏感信息；
- 关键测试被 skip、测试范围下降或 CI 失败；
- 路径所有者、ACL、可写性或安全边界无法证明；
- 需要真实媒体、模型、FFmpeg、GPU、外部 endpoint 或扩大到无关模块；
- 需要修改全局规则、技能或其他项目。

本计划不包含用户媒体路径、字幕正文、模型输出、Token、Cookie、API 配置或本机维护笔记。
