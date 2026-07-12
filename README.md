# AutoSlice

AutoSlice 是一个本地运行的录播话题分析与智能切片工具。它结合语音字幕、弹幕密度和兼容 OpenAI/Anthropic 协议的 LLM，生成逐话题时间轴，并导出保留前因后果的短视频素材。

## 功能

- 没有字幕时使用 FunASR 自动生成 SRT
- 按完整语义整理逐话题时间轴，而不是只记录弹幕爆点
- 结合弹幕峰值判断值得切片的片段
- 自动补齐话题前因、后续反应以及 SC/礼物上下文
- 单个实际切片最长 5 分钟，相邻切片不会重叠
- 支持整场墙钟时间的 docx 人工时间轴，并适配分段录播
- Flask Web 界面与 SSE 实时任务进度
- 重跑时清理旧的自动生成切片，保留用户手工命名文件

## 环境要求

- Python 3.10+
- ffmpeg 和 ffprobe 已加入 `PATH`
- Windows 10/11；其他平台可直接运行 Flask，但一键启动脚本主要按 Windows 验证
- FunASR 模型约需 1 GB 磁盘空间，CPU 转录需要一定时间和内存

## 快速开始

```powershell
git clone https://github.com/YRMDLG/AutoSlice.git
cd AutoSlice

Copy-Item api_config.example.json api_config.json
notepad api_config.json

python 启动.py
```

打开 [http://localhost:5002/topic-v2](http://localhost:5002/topic-v2)，录播默认放在 `recordings/`，输出默认写入 `output/`。

## LLM 配置

`api_config.json` 仅保存在本机，已被 Git 忽略：

```json
{
  "base_url": "https://api.example.com/v1",
  "token": "YOUR_API_TOKEN",
  "model": "deepseek-v4-pro"
}
```

`base_url` 填 API 的版本根地址，不要附加 `/chat/completions` 或 `/messages`。以 `sk-` 开头的 token 使用 OpenAI 兼容协议，其他 token 使用 Anthropic 兼容协议。

也可以不创建配置文件，改用环境变量：

```powershell
$env:AUTOSLICE_API_BASE_URL = "https://api.example.com/v1"
$env:AUTOSLICE_API_TOKEN = "YOUR_API_TOKEN"
$env:AUTOSLICE_LLM_MODEL = "deepseek-v4-pro"
python 启动.py
```

AutoSlice 不会读取 Claude、Codex 或其他应用的配置文件。

## 可选环境变量

| 变量 | 默认值 | 用途 |
|------|--------|------|
| `AUTOSLICE_VIDEO_DIR` | `recordings` | Web 页面默认录播目录 |
| `AUTOSLICE_OUTPUT_DIR` | `output` | 自动切片输出目录 |
| `AUTOSLICE_TIMELINE_DIR` | `timelines` | 人工时间轴目录 |
| `AUTOSLICE_FUNASR_DEVICE` | `cpu` | FunASR 运行设备 |
| `AUTOSLICE_STREAMER_NAME` | 空 | 可选的主播正式名 |
| `AUTOSLICE_STREAMER_NICKNAME` | `主播` | 报告中的展示称呼 |
| `AUTOSLICE_FAN_ALIASES` | 展示称呼 | SC 原文中需要保留的称呼，逗号分隔 |

例如：

```powershell
$env:AUTOSLICE_STREAMER_NAME = "示例主播"
$env:AUTOSLICE_STREAMER_NICKNAME = "小音"
$env:AUTOSLICE_FAN_ALIASES = "小音,音姐"
```

## 输入文件

同一录播建议使用相同文件名前缀：

```text
recordings/
├── 示例录播.flv
├── 示例录播.ass   # 可选，弹幕密度分析
└── 示例录播.srt   # 可选；缺少时自动转录
```

人工时间轴放入 `timelines/`。时间轴记录的是现实钟点时，程序会根据录播文件名中的开播时间换算成视频播放进度；分段录播只保留落在当前分段内的记录。

## 分析与切片流程

1. 检查同名 SRT；没有则从视频提取音频并用 FunASR 转录。
2. 从 ASS 计算弹幕密度和峰值。
3. 将字幕按 10 分钟处理块发送给 LLM，生成结构化话题。
4. 汇总完整时间轴，并根据弹幕峰值选择可切片话题。
5. 将现实钟点或 LLM 时间统一换算为视频内秒数。
6. 补充上下文、拆分仅因上下文相碰的片段，并用 ffmpeg 导出。

报告中的时间和 JSON 的 `start/end` 都是视频内时间，不是现实钟点。

## 测试

```powershell
python -B -m unittest test_public.py -v
python -B -m py_compile app.py core.py topic_engine.py 启动.py
```

## 隐私与安全

- 不要提交 `api_config.json`、`.env`、模型缓存、录播、字幕、弹幕或切片结果。
- 公开仓库只包含占位配置；程序不会自动读取其他开发工具的凭据。
- LLM 分析会把当前字幕块发送到你配置的 API 服务，请根据服务商条款自行判断内容是否适合上传。
- 上传人工时间轴前，先检查其中是否包含真实姓名、联系方式或其他私人信息。

## 已知限制

- LLM 的分析质量取决于字幕识别质量和模型服务稳定性。
- `-c copy` 切片受视频关键帧影响，实际媒体时长可能比 JSON 多约 1 秒。
- 首次使用 FunASR 时需要提前准备模型缓存；网络不可用且本地无模型时会给出明确错误。

## License

[MIT](LICENSE)
