# AutoSlice - 录播智能切片

AutoSlice 是面向 B 站录播的本机工作流工具：结合语音字幕、弹幕和可选人工时间轴生成话题报告与高光候选，再衔接字幕校对、硬字幕压制和 AutoCover 封面制作。

## 功能

- **话题与弹幕分析**：用字幕理解内容，用 ASS/XML 弹幕峰值发现线索；弹幕不会单独决定切片。
- **人工时间轴辅助**：导入 DOCX 时间轴并按北京时间校准；时间轴是证据，不是机械剪切指令。
- **高光候选与切片**：复核触发、前因后果、SC 起点、自然收尾和投稿价值，不按小时凑数量。
- **FunASR 字幕**：优先使用 Fun-ASR-Nano-2512、FSMN-VAD、标点和可选 CAM++，缺少推荐模型时可回退。
- **字幕校对与压制**：为精剪成片生成或配对 SRT，人工确认 AI 错字建议后输出硬字幕 MP4。
- **AutoCover 联动**：由同一个启动器管理封面工作台，并保存可恢复的本地编辑草稿。

## 快速开始

完整功能以 **Windows 10/11、Python 3.10** 为主平台。先在项目根目录执行：

```powershell
# Python 3.10 自带的旧 pip 可能不支持 pyproject editable 安装
python -m pip install --upgrade pip

# 推荐：安装统一项目及 AutoSlice/AutoCover 依赖
python -m pip install -e .

# 首次使用：下载推荐的 ASR/VAD/标点/CAM++ 模型
python setup_asr_model.py

# 可选：64 位 Windows + NVIDIA 显卡安装隔离 CUDA 运行时（约 1.9 GB）
python setup_gpu_runtime.py

# 启动 AutoSlice 与 AutoCover
python 启动.py

# 安装后也可使用同一入口
autoslice
```

然后打开 `http://127.0.0.1:5002`。`启动.py` 会同时管理 AutoSlice 和仓库内的 AutoCover；AutoCover 默认从 5010 端口开始，端口冲突时可能顺延。按 `Ctrl+C` 会停止本次启动的服务。

需要使用自己的录播、投稿、封面或贴图库时，复制本机配置示例；该文件已被 Git 忽略：

```powershell
Copy-Item autoslice.local.example.json autoslice.local.json
notepad autoslice.local.json
```

LLM API、代理、目录、模型和 LAN 设置见[配置说明](docs/配置说明.md)。

## 平台与能力边界

| 平台 | 支持级别 | 能力边界 |
|---|---|---|
| Windows 10/11 | 完整主平台 | 支持启动器、Windows 隔离 CUDA 运行时、FFmpeg/ffprobe、本机字体、剪映衔接、字幕压制和 AutoCover 完整媒体工作流。|
| Linux | 纯逻辑 CI / 部分 CPU 代码路径 | GitHub Actions 只运行显式白名单的架构、解析、评分、标题、路径和任务存储测试；不验证 Windows CUDA 隔离运行时、剪映、本机字体、FFmpeg 媒体处理或完整端到端工作流。|
| macOS | 未验证 | 没有 CI 和完整媒体验收，不承诺 GPU、字体、启动器或端到端媒体兼容性。|

**Linux logic-only GitHub Actions 不代表完整 Linux 支持。** 如果需要生成正式媒体、使用剪映或复现默认字体效果，请使用 Windows 10/11。

## 输入、输出与依赖

AutoSlice 的录播扫描、分析和切片支持大小写不敏感的 `.flv`、`.mp4`、`.mkv`、`.mov`、`.avi`。codec-copy 自动切片默认保留源容器，例如 MP4 输入输出 MP4；为兼容旧版本，读取既有结果时仍会识别历史 FLV 切片。字幕工作台仍以剪映等工具导出的精剪 MP4/SRT 流程为主。

`pyproject.toml` 是统一项目的依赖和命令行入口声明；
`requirements.txt` 保留给现有脚本和不使用 editable 安装的用户，内容与统一依赖同步。
AutoCover 仍可用自己的依赖文件单独运行：

| 来源 | 内容 |
|---|---|
| `pyproject.toml` / `requirements.txt` | `Flask`、`Pillow`、`FunASR`、`soxr`、`python-docx`、`requests` |
| `autocover_tool/requirements.txt` | 只单独运行 AutoCover 时使用的 `Flask`、`Pillow` |
| `setup_gpu_runtime.py` | Windows 隔离 GPU 运行时使用的 `torch`/`torchaudio` 范畴依赖；不属于通用根依赖 |
| 系统外部依赖 | `ffmpeg`、`ffprobe`，必须单独安装并加入 `PATH` |

## 完整工作流

`录播 → 字幕/弹幕分析 → 话题报告 → 自动切片 → 剪映精调 → 字幕校对与压制 → AutoCover → 投稿`

1. **准备录播**：放入受支持的视频；同名前缀的 SRT、ASS/XML 和可选 DOCX 时间轴会作为证据。
2. **分析与切片**：先生成逐话题报告，再对达到投稿价值的候选复核边界并切片。人工星标只能辅助，不能强行制造候选。
3. **字幕工作台**：精剪成片可配对现有 SRT，也可重新运行 FunASR；AI 校对只给建议，确认后才保存新字幕并压制，不覆盖源视频或源 SRT。
4. **AutoCover**：从切片或精调成片选帧，调整标题、贴图、比例和构图；磁盘草稿用于刷新或重启后的继续编辑。

详细操作见[日常工作流](docs/日常工作流.md)，错误处理见[故障排查](docs/故障排查.md)。

## 通用配置、任务历史与安全默认值

- 未识别到专属主播时使用 `generic` 通用配置，不会串用泽音的词表、错词映射、收播规则或标题风格；主播专属能力由 `streamer_profiles.json` 中的 profile 提供。
- 源码克隆模式的任务历史保存在 Git 忽略的本地 `.autoslice-state/tasks.sqlite3`；安装包脱离仓库运行时改用用户数据目录，也可通过 `AUTOSLICE_STATE_DIR` 或 `AUTOSLICE_TASK_DB` 显式隔离。服务重启后，遗留的 `queued`/`running` 任务会被标记为 `interrupted`；后台线程不会跨进程继续运行，但兼容的 ASR、话题分析和候选复核检查点可在重新发起任务时续跑。
- Web 服务默认仅绑定 loopback，并校验 Host、同源 Origin/Referer 或本机会话。LAN 模式必须显式配置至少 32 字符的强随机 token、允许的 Host、完整 Origin 和绝对路径根目录。
- LLM 代理默认为 `direct`，不会继承 `HTTP_PROXY`、`HTTPS_PROXY` 或 `ALL_PROXY`；需要系统代理或专用代理时再选择 `system` 或 `custom`。

## 文档入口

| 文档 | 适合谁 | 用途 |
|---|---|---|
| [配置说明](docs/配置说明.md) | 安装者、维护者 | 查询平台、依赖、LLM 代理、目录、profile、GPU、任务和安全配置 |
| [日常工作流](docs/日常工作流.md) | 切片与投稿人员 | 完成录播分析、人工时间轴、切片、字幕、AutoCover 和投稿检查 |
| [故障排查](docs/故障排查.md) | 操作人员、维护者 | 处理任务、HTTP、代理、安全、FFmpeg、FunASR、字幕和 AutoCover 错误 |
| [架构重构说明](docs/架构重构.md) | 开发者 | 了解渐进式模块化边界与当前架构状态 |

## 项目结构

```text
AutoSlice/
├── pyproject.toml             # 安装元数据、统一依赖与 autoslice 命令
├── 启动.py                    # 源码克隆的一键启动薄入口
├── src/
│   └── autoslice/             # AutoSlice 唯一生产实现
│       ├── web/               # Flask 路由、任务接口与 SSE
│       ├── analysis/          # 弹幕、时间轴、候选、边界与标题
│       ├── transcription/     # FunASR、SRT 与原子检查点
│       ├── llm/               # 模型契约、提示词、重试与传输
│       ├── resources/         # 随包分发的模板和静态资源
│       ├── pipeline.py        # 分析流水线与续跑编排
│       ├── reporting.py       # 报告与整理包
│       └── slicing.py         # FFmpeg 切片与成片复用
│   └── autoslice_cover/       # AutoCover 唯一生产实现与随包资源
├── app.py 等                  # 临时旧导入兼容 alias，不保存第二套实现
├── autocover_tool/            # AutoCover 旧启动、依赖、测试和数据兼容目录
├── tests/                     # unit/integration/architecture/support 测试
├── scripts/                   # 离线验证、发布和人工冒烟入口
├── requirements.txt           # 兼容安装依赖清单
└── docs/                      # 配置、工作流、排错与架构文档
```

新增代码应从 `autoslice.*` 包导入 owner；根目录 `app.py`、
`topic_engine.py` 等文件只为旧脚本过渡，不应继续加入业务逻辑。

## 已知限制

- GPU 不是硬性要求；隔离 CUDA 运行时不可用时会回退 CPU，但转录更慢。
- “排除背景音”只处理临时识别音轨，不修改源视频；单一混合音轨中的同时多人声无法保证完全分离。
- 默认字幕与封面效果依赖本机合法字体，字体文件不随仓库分发。
- 自动候选仍需在投稿前人工复核边界、字幕、标题和封面。

## 相关项目

- [DanmakuRender-5](https://github.com/SmallPeaches/DanmakuRender) — 录播与弹幕渲染
- [FunASR](https://github.com/modelscope/FunASR) — 语音识别引擎
- [auto-slice-video](https://github.com/timerring/auto-slice-video) — 弹幕密度分析参考

## License (MIT License)

MIT
