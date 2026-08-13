# AutoSlice - 录播智能切片

自动识别 B 站录播中的精彩片段，基于弹幕密度分析 + AI 语音字幕上下文，切出完整的高光时刻。

## 功能

- **弹幕密度模式**：分析 .ass 弹幕文件，滑动窗口找弹幕密度峰值，配合 SRT 语音字幕追溯前因后果
- **时间轴模式**：导入朋友手动标记的 .docx 时间轴，按标记时刻精确切片
- **看视频检测**：自动识别主播看视频的时间段，确保切片不截断
- **Web 界面**：浏览器操作，选路径、选模式、一键切片，SSE 实时进度
- **语音识别**：优先使用 Fun-ASR-Nano-2512 + FSMN-VAD，自动生成带时间戳 SRT；旧 Paraformer 作为回退
- **字幕校对与压制**：扫描精剪成片；缺少 SRT 时先用 FunASR 自动识别，再确认 AI 错字建议并生成硬字幕 MP4
- **自动封面联动**：同一个启动脚本管理 AutoCover，并从 AutoSlice 顶部导航直接进入封面工作台

## 完整工作流

`录播 → 字幕/弹幕分析 → 话题报告 → 自动切片 → 字幕校对与压制 → 自动封面 → 投稿`

AutoSlice 负责前四步和字幕工作台，AutoCover 负责成片封面。人工时间轴只作为分析线索，
实际切点仍由字幕上下文、弹幕互动和最终复核共同决定；程序不会按每小时固定数量凑切片。

## 快速开始

```powershell
# 推荐：一次安装当前推荐的高精度中文模型（Fun-ASR-Nano + VAD + 标点）
python setup_asr_model.py

# 推荐：NVIDIA 显卡首次运行时安装隔离 GPU 环境（约 1.9 GB）
python setup_gpu_runtime.py

# 启动；隔离环境健康时自动使用 GPU，否则使用 CPU
python 启动.py

# 浏览器打开
http://localhost:5002
```

`启动.py` 会同时启动 AutoSlice（默认 5002）和仓库内的 AutoCover
（默认 5010）。AutoCover 已运行时会直接复用；端口冲突时会自动顺延，
顶部“自动封面”入口始终跳转到本次实际地址。按 `Ctrl+C` 会同时停止本次
启动的两个服务，不会关闭原本已经独立运行的 AutoCover。

语音模型安装脚本使用免费的开源 Fun-ASR-Nano-2512，并安装普通 Paraformer
回退所需的 VAD 和标点模型；只需成功执行一次，之后会优先使用本地缓存和字幕热词。下载失败时
原有 Paraformer 缓存仍可回退使用。GPU 安装脚本只写入
`%LOCALAPPDATA%\AutoSlice`，不会替换系统 Python 的 CPU PyTorch。
启动后，智能分析和字幕校对页面会直接提示当前 FunASR 模型、设备、是否回退，以及热词与
固定专名纠错的调整位置；无需靠控制台猜测实际用了哪个模型。

所有公开默认目录都位于仓库内。需要继续使用自己已有的录播、投稿、封面或贴图库时，
复制 `autoslice.local.example.json` 为本机的 `autoslice.local.json`，再填入自己的目录。
该文件已被 Git 忽略，且显式设置的同名环境变量优先于它：

```powershell
Copy-Item autoslice.local.example.json autoslice.local.json
notepad autoslice.local.json
```

更多 API、模型、目录和局域网配置请见 [配置说明](docs/配置说明.md)，日常操作步骤请见
[日常工作流](docs/日常工作流.md)，常见问题请见 [故障排查](docs/故障排查.md)。

## 字幕校对与压制

启动 AutoSlice 后，从顶部导航进入“字幕校对+压制”，或直接打开：

```text
http://localhost:5002/subtitle-workflow
```

默认扫描 `submissions/`。每个投稿子目录可以只放精剪后的 MP4；已有 SRT
时会自动配对，没有 SRT 时也会进入队列并显示“待识别”。剪映导出的 MP4/SRT
文件名可以不同，只要同一目录中各有一个源文件即可自动配对。

推荐流程：

1. 点击“扫描”，从左侧选择精剪成片。
2. 如果显示“待识别”，点击“自动识别字幕”，等待同名 SRT 生成并自动加载。
3. 点击“AI 检查错字”。模型只给出建议，不会直接覆盖字幕。
4. 对照原文、理由和置信度逐条确认，也可直接修改正文。
5. 点击“预览字幕”，检查字体、描边和位置。
6. 点击“开始压制”，等待后台任务完成。

默认字幕样式严格对应当前剪映参数：

| 参数 | 默认值 |
|------|--------|
| 字体 | Noto Sans S Chinese Black |
| 剪映字号 | 20 |
| 字体颜色 | `#ffffff` |
| 描边颜色 | `#d06e95` |
| 描边粗细 | 100 |
| 位置 | X=0，Y=-788 |

默认视频参数为 `1920x1080`、VBR 8000 Kbps、H.264、MP4、60fps、
Rec.709 SDR。优先使用 NVIDIA NVENC，探针或压制失败时自动回退
`libx264`。视频保留原内嵌音轨；剪映导出页底部未勾选“音频导出”只表示
不额外导出 MP3，不表示视频应当静音。

所有产物都写入原投稿子目录，并且不会覆盖源 MP4/SRT：

```text
原视频.srt
原字幕_字幕校对建议.json
原字幕_校对.srt
原字幕_校对_字幕样式.ass
原字幕_校对_字幕样式.json
原视频_字幕版.mp4
```

AI 校对按最多 30 条字幕一批、最多两路并行执行。只有置信度至少 0.95、
且不增删有效字符的建议会默认勾选；其他建议仍会显示，必须人工确认。

## 项目结构

```
AutoSlice/
├── 启动.py              # 一键启动，自动选择隔离 GPU/CPU 运行时
├── setup_asr_model.py   # 一次性安装推荐的高精度中文 ASR/VAD/标点模型
├── setup_gpu_runtime.py # 一次性安装并校验 CUDA PyTorch
├── app.py               # Flask Web 服务 + SSE 实时推送
├── core.py              # 切片核心引擎
├── subtitle_workflow.py # 字幕配对、AI 校对、ASS 样式与硬字幕压制
├── requirements.txt     # Python 依赖
├── templates/
│   ├── index.html       # Web 操作界面
│   └── subtitle_workflow.html # 字幕校对与压制页面
└── static/              # 静态资源
```

## 切片模式

### 弹幕密度模式
- 扫描 .ass 弹幕 → 滑动窗口找峰值 → 自适应阈值过滤 → SRT 字幕上下文扩展 → 合并重叠 → 切片
- 密度阈值：峰值 × 0.45（只切真正高密度爆点）

### 时间轴模式
- 导入朋友标记的 .docx 时间轴 → 解析时间戳 + ⭐ 评分 → 上下文扩展 → 切片

## 依赖

```
flask >= 3.0
funasr >= 1.4.1
soxr >= 1.0
python-docx
torch
torchaudio
ffmpeg（系统安装）
```

## 配置参数

核心参数在 `core.py` 顶部：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| DENSITY_RATIO | 0.45 | 密度阈值，峰值×比例 |
| MAX_EXPAND | 60s | 前因后果最大扩展 |
| CONTEXT_GAP | 4.0s | 字幕间隔断开阈值 |
| DANMAKU_WINDOW | 60s | 弹幕密度窗口 |

## 已知限制

- RTX 2060 6GB 已通过 FunASR CUDA 转录测试；GPU 模型加载失败会明确回退 CPU
- 未安装隔离 CUDA 运行时也可正常使用，只是首次生成 SRT 会更慢
- Windows 终端需 UTF-8 编码，避免中文乱码

## 相关项目

- [DanmakuRender-5](https://github.com/SmallPeaches/DanmakuRender) — 录播 + 弹幕渲染
- [FunASR](https://github.com/modelscope/FunASR) — 语音识别引擎
- [auto-slice-video](https://github.com/timerring/auto-slice-video) — 弹幕密度分析

## License (MIT License)

MIT
