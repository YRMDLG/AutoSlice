# AutoSlice

AutoSlice 是一个在本机运行的录播分析与后期辅助工具，面向包含语音、弹幕和字幕的直播回放。它把话题整理、候选切片、字幕校对、字幕压制和 AutoCover 封面工作台放在同一个项目中。

## 完整工作流

```text
录播 → 字幕/弹幕分析 → 话题报告 → 自动切片 → 字幕校对与压制 → 自动封面 → 投稿
```

它不会因为某一小时没有爆点就硬凑固定数量的片段。弹幕峰值用于发现候选，模型还会核对完整对话、观众 SC 的起点、前因后果、自然边界和二次剪辑价值。普通话题可以保留在报告中，但不一定自动切片。

## 功能入口

- `http://127.0.0.1:5002/topic-v2`：话题分析、人工时间轴选择、自动切片
- `http://127.0.0.1:5002/subtitle-workflow`：字幕扫描、AI 校对建议、人工确认、预览和压制
- `http://127.0.0.1:5002/autocover`：跳转到 AutoCover 双比例封面工作台
- `python -m autocover_tool.autocover.cli --help`：在仓库根目录查看 AutoCover 独立命令行帮助

详细说明：

- [配置说明](docs/配置说明.md)
- [日常工作流](docs/日常工作流.md)
- [故障排查](docs/故障排查.md)
- [AutoCover 说明](autocover_tool/README.md)

## 快速开始

环境要求：Windows 10/11、Python 3.10 或更高版本、`ffmpeg` 和 `ffprobe`。首次使用话题分析还需要 FunASR 模型；可以使用 CPU，也可以配置可用的 CUDA 运行时。AutoCover 使用 Pillow，根目录的 `requirements.txt` 已包含所需依赖。

```powershell
git clone https://github.com/YRMDLG/AutoSlice.git
Set-Location AutoSlice

python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt

Copy-Item api_config.example.json api_config.json
notepad api_config.json

python 启动.py
```

启动器会在本机启动 AutoSlice（默认 `5002`）和 AutoCover（默认 `5010`）。录播放入 `recordings/`，分析结果和自动切片默认写入 `output/`，然后打开上面的入口页面。首次安装如果 PowerShell 禁止激活虚拟环境，可以直接使用 `.venv\Scripts\python.exe` 执行命令。

API 配置只保存在本机的 `api_config.json`，该文件已被 Git 忽略。不要把它提交到仓库。配置格式、`environment.example.ps1` 环境变量示例和协议差异见[配置说明](docs/配置说明.md)。

## 默认目录

```text
recordings/       录播、同名前缀的 SRT/ASS
timelines/        可选人工时间轴 DOCX
output/           话题报告、JSON、自动切片和精调任务
submissions/      字幕校对工作流的投稿素材
covers/           AutoCover 导出的封面
stickers/         本机贴图库，不随公开仓库分发
```

所有目录都可以用环境变量改到其他位置，不需要修改源代码。公开仓库不包含录播、字幕、弹幕、时间轴、字体、贴图、模型缓存或个人账号资料。

录播文件名采用 `主播名-YYYY-MM-DD ...`、`主播名-YYYY年MM月DD日...`
或 `主播名_YYYYMMDD...` 时，投稿标题会自动使用 `【主播名】` 前缀。已在
`streamer_profiles.json` 配置的主播会继续使用其专属称呼和 ASR 纠错规则；
未配置的主播会创建仅对当前任务生效的通用配置。详见[配置说明](docs/配置说明.md#4-主播识别与投稿标题前缀)。

## 安全边界

AutoSlice 只会读取项目内的 `api_config.json` 或明确设置的 `AUTOSLICE_API_*` 环境变量，不会读取其他程序的密钥文件。话题分析会把当前录播的字幕片段和有限的弹幕摘要发送到你配置的 LLM 服务；使用前请确认服务商的隐私政策。AutoCover 不调用外部 AI，也不会上传视频或图片。

## 许可证

本项目采用 [MIT License](LICENSE)。第三方依赖仍受其各自许可证约束；本仓库不包含未获再分发许可的外部 skill、字体或素材。
