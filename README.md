# AutoSlice - 泽音Melody 录播智能切片

自动识别 B 站录播中的精彩片段，基于弹幕密度分析 + AI 语音字幕上下文，切出完整的高光时刻。

## 功能

- **弹幕密度模式**：分析 .ass 弹幕文件，滑动窗口找弹幕密度峰值，配合 SRT 语音字幕追溯前因后果
- **时间轴模式**：导入朋友手动标记的 .docx 时间轴，按标记时刻精确切片
- **看视频检测**：自动识别主播看视频的时间段，确保切片不截断
- **Web 界面**：浏览器操作，选路径、选模式、一键切片，SSE 实时进度
- **语音识别**：内建 FunASR Paraformer 中文识别，自动生成 SRT 字幕

## 快速开始

```powershell
# 安装依赖并启动
python 启动.py

# 浏览器打开
http://localhost:5002
```

首次运行会自动下载 FunASR 模型（约 1GB），后续使用缓存。

## 项目结构

```
AutoSlice/
├── 启动.py              # 一键启动，自动安装依赖
├── app.py               # Flask Web 服务 + SSE 实时推送
├── core.py              # 切片核心引擎
├── requirements.txt     # Python 依赖
├── templates/
│   └── index.html       # Web 操作界面
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
funasr
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

- FunASR 模型需要约 4GB 空闲内存，与录播进程同时运行时可能内存不足
- 内存不足时 ASR 自动跳过，降级为纯弹幕密度切片
- Windows 终端需 UTF-8 编码，避免中文乱码

## 相关项目

- [DanmakuRender-5](https://github.com/SmallPeaches/DanmakuRender) — 录播 + 弹幕渲染
- [FunASR](https://github.com/modelscope/FunASR) — 语音识别引擎
- [auto-slice-video](https://github.com/timerring/auto-slice-video) — 弹幕密度分析

## License

MIT
