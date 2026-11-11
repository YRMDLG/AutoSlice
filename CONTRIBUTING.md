# 协作开发

当前 Qt 字幕与 AutoCover 开发分支为 `desktop-cover-opus55`，默认分支 `main`
保留现有 Web 工作流。其他实验分支有各自的开发历史，不应在不了解差异时整体合并。

## 获取与安装

Windows 10/11、Python 3.10 是主要开发环境。以下命令在 PowerShell 中执行：

```powershell
git clone --branch desktop-cover-opus55 https://github.com/YRMDLG/AutoSlice.git
cd AutoSlice
py -3.10 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -X utf8 -m pip install -r requirements-qt-desktop.txt
```

Qt 入口是 `.\.venv\Scripts\python.exe Qt桌面端.py`。FFmpeg/ffprobe 和 libmpv
是独立外部依赖；模型和 CUDA 环境不随 Git 仓库分发。封面内置濑户体，原版权及
文件校验见 [字体说明](src/autoslice_cover/resources/fonts/NOTICE.md)；字幕字体仍需本机安装。完整能力与依赖边界见
[README](README.md) 和 [桌面架构](docs/desktop-vnext/ARCHITECTURE.md)。

## 本机配置与数据

按需要复制 `autoslice.local.example.json`、`api_config.example.json` 为对应的本机
配置文件；已存在时不要覆盖。投稿目录优先使用 `AUTOSLICE_SUBMISSION_DIR` 环境变量，
其次使用 `autoslice.local.json`，未配置时使用项目内 `submissions/`。

本机配置、API 凭据、录播、字幕、个人字体、草稿、缓存、截图和 `work/` 测试产物均应
留在忽略目录。内置字体采用指定路径和 SHA-256 清单放行，不要把其他字体加入资源目录。
公开示例使用占位值；文档中的源码引用使用仓库相对路径。

## 实现与验证

AutoSlice 的实现位于 `src/autoslice`，AutoCover 的实现位于 `src/autoslice_cover`。
根目录及 `autocover_tool` 中的旧入口仅用于兼容。先阅读
[Desktop vNext 当前状态](docs/desktop-vnext/DESKTOP_STATUS.md)，再修改相关 owner。

在仓库根目录运行离线检查：

```powershell
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:QT_QPA_PLATFORM = "offscreen"
.\.venv\Scripts\python.exe -B scripts/compile_public.py
.\.venv\Scripts\python.exe -B scripts/validate_public_docs.py
.\.venv\Scripts\python.exe -B scripts/scan_public_release.py
.\.venv\Scripts\python.exe -B scripts/architecture_snapshot.py --check
git diff --check
```

结构变化导致快照过期时，应审查变化后运行 `scripts/architecture_snapshot.py` 更新
`architecture_baseline.json`，再执行 `--check`。JavaScript 变化还需 Node.js 语法检查。

`scripts/run_hermetic_tests.py -q` 是完整回归入口：它隔离私有配置，阻止真实网络、
模型加载和服务启动。已安装 FFmpeg 时，一部分测试会处理临时合成媒体；未安装时
相应媒体测试会跳过。不要把跳过或 offscreen 检查当作真实字体、GPU、mpv 或视觉验收。
`scripts/verify_distribution.py` 在仓库外验证源码、editable 和 wheel 安装边界。

GitHub Actions 对 `main`、`desktop-vnext-dev`、`desktop-cover-opus55`、`codex/**`
分支的 push 和所有 PR 自动运行检查。
Windows 作业安装 Qt 依赖并运行完整隔离测试；Linux 作业只验证明确列出的纯逻辑路径。

## 提交与直接推送

直接 push 需要 GitHub 仓库的 **Write 或更高权限**，并使用自己的 GitHub 身份认证。
公开仓库的读取权限不会自动授予写入权限。已有写入权限的协作者可以：

```powershell
git switch desktop-cover-opus55
git pull --ff-only origin desktop-cover-opus55
# 完成修改和定向测试后，逐个暂存相关文件
git add <本次修改的文件>
git diff --cached --check
git diff --cached
git commit -m "fix(desktop): 说明本次具体修复"
git push origin desktop-cover-opus55
```

拉取前应先提交自己的工作，避免覆盖未提交修改。推送被拒绝时重新 fetch，检查远程
新提交，解决冲突并重新验证；不要强推。提交使用 Conventional Commits，每次提交
包含一个完整逻辑变更。没有写入权限的贡献者使用 fork 和 PR。

需要合入 `main` 时，应单独审查开发分支的整体差异，并完成 Windows 媒体与实机验收。

## 朋友协作修改

已有写入权限的朋友可以沿用上面的直接推送流程，也可以从最新协作分支建立自己的
修改分支，完成后发起 PR，目标分支选择 `desktop-cover-opus55`：

```powershell
git switch desktop-cover-opus55
git pull --ff-only origin desktop-cover-opus55
git switch -c codex/friend-improvements
# 修改后执行定向测试，逐个暂存文件并提交
git push -u origin codex/friend-improvements
```

没有写入权限时，在 GitHub 上 fork 仓库，从同名协作分支开始修改，再向原仓库的
`desktop-cover-opus55` 提交 PR。仓库里的源码、测试、文档、界面字体和示例配置足够
接手开发；真实 API 配置、本机路径、录播、字幕、个人素材、草稿和学习数据由各自
在本机配置。复现问题需要额外数据时，先确认具体文件，再提供脱敏样本。

### 2026-11-10 上传快照的验证状态

本机 Python 3.10 的首次完整隔离回归共 2098 项：6 项失败、17 项跳过。
其中两项架构计数问题在同步快照后，已通过全部 88 项架构定向检查；未重新运行
整套回归。尚待接手检查的封面用例为：

- `CritiqueDialogTests.test_format_failure_names_the_stage_and_keeps_both_profiles`
- `CritiqueDialogTests.test_rejected_candidates_are_revised_from_original_with_feedback`
- `CritiqueDialogTests.test_retry_does_not_review_a_previously_rejected_rendering`
- `CritiqueDialogTests.test_two_rejected_rounds_stop_without_showing_internal_numbers`

架构计数问题是源码顶层函数数已为 1476，旧快照仍为 1474；四项封面失败是候选复核次数断言。
测试期间还出现了一次临时视频删除后的波形后台回调异常。此次按现状交接，没有为
上传而调整业务行为或测试断言。公开文件扫描、文档校验、源码编译、前端语法及
审查后的架构快照检查通过；真实媒体、模型、字体和窗口连续操作未在此次重新验收。

2026-11-11 接手处理：四项封面失败的原因是 1089fdf 起“修改效果复核”改为 4:3 与
16:9 分别并行复核（每轮按比例各调用一次），测试仍按每轮一次计数；已按新行为更新
断言，其中一项不再依赖并行复核的返回先后。业务代码未改。
