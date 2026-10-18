# 桌面端字幕识别（ASR）

> 状态：**框架和界面已完成；2026-10-18 已在主力机 Windows / Python 3.10 / RTX 4060（8GB）上跑通真实 Qwen 模型加载、3 秒静音探针和官方 4.2 秒中文样本的逐字时间对齐**，试跑显存峰值约 5.57GB。真实录播的识别精度、长音频稳定性和双引擎对比仍待 `bench` 验证。决策记录见 [DECISIONS.md](DECISIONS.md) D-032。

## 怎么用

字幕页选一个**没有字幕**的视频，顶部出现“识别字幕”。点一下：识别在单独的子进程里跑，进度条走完后自动载入字幕，接着校对。识别中可以取消，取消或失败后再点会从上次的位置接着识别。

设置页“字幕识别”：选引擎、模型（Hugging Face 名字或本机文件夹）、运行在显卡还是 CPU、识别用的 Python（装了 torch 和识别库的那个环境）、是否带热词、是否先压背景音乐。底下有“自检”“加载试跑”“安装步骤”。

只对**没有字幕的视频**提供识别，不会覆盖已有的 `.srt`（已有字幕的校对状态是按条序号记的，覆盖会让校对状态对不上）。

## 在你的主机上第一次用（按顺序）

请给 Qwen 识别单独准备 Python 环境，让 `qwen-asr` 安装它声明的依赖；不要单独升级 transformers。`qwen-asr 0.0.6` 声明的是 `transformers==4.57.6`，自检会读取已安装包的元数据核对，而不套用其他加载路线的版本要求。缺少元数据或解析库时会提示“兼容性未验证”。

主力机已安装独立环境 `%LOCALAPPDATA%\AutoSlice\qwen-asr-py310\Scripts\python.exe`，使用 `qwen-asr 0.0.6`、`transformers 4.57.6`、`torch 2.12.1+cu130`，并已写入桌面端设置的“识别用的 Python”。命令行使用 `scripts\asr_tool.cmd` 时仍需把 `AUTOSLICE_ASR_PYTHON` 设为这个路径。安装日志、依赖版本清单和模型试跑结果保存在 `%LOCALAPPDATA%\AutoSlice\logs\qwen-*20261018*`。

`bench --engines qwen3-asr,funasr` 要求两个引擎在同一个环境中都可用；若依赖冲突，分别在各自环境运行 `bench --engines qwen3-asr --out bench-qwen.md` 和 `bench --engines funasr --out bench-funasr.md`，使用相同的旧切片目录和取样参数。

命令也可以用 `scripts\asr_tool.cmd <子命令>` 运行：它会把仓库的 `src` 放进 `PYTHONPATH`，并用环境变量 `AUTOSLICE_ASR_PYTHON` 指定的 Python（没设就用 PATH 里的 `python`）——也就是你装了 torch 的那个环境，不用先 `pip install` 本项目。

```bash
python -m autoslice.desktop.asr setup            # 打印安装步骤（不会替你装）
python -m autoslice.desktop.asr doctor           # 缺什么、显卡够不够，一次说清
python -m autoslice.desktop.asr doctor --probe   # 真的加载模型试跑 3 秒静音：验证接口、热词参数、显存
python -m autoslice.desktop.asr bench --root <你的旧切片文件夹> --engines qwen3-asr,funasr --out bench.md
```

把 `bench.md` 带回来，就能按数据定默认引擎，而不是按论文。

## 为什么默认是 Qwen3-ASR 1.7B

调研结论（来源：Qwen3-ASR 官方仓库和技术报告、FireRedASR2S 仓库和论文、FunASR 官方资料；没有自己跑分）：

| 引擎 | 中文准确率 | 抗噪 | 热词 | 字级时间 | 8GB 显存 | 备注 |
|---|---|---|---|---|---|---|
| **Qwen3-ASR 1.7B + 对齐模型 0.6B** | 开源里第一梯队 | **开源里最好**（官方评测） | 有（提示里写词表） | 有（对齐模型，单次 ≤5 分钟） | 够（估算 5~6GB，以 `doctor --probe` 实测为准） | 默认；`qwen-asr` 官方包 |
| FireRedASR2（AED/LLM） | 公开基准平均字错率最低 | 一般 | 无 | 需另配 | AED 可、LLM 版大概率不够 | 只在 Linux 验证过，单段 ≤60 秒；**没接入**，要比的话在 `engines/` 加一个文件 |
| FunASR Paraformer-large（原网页版） | 好 | 一般 | 基本无效 | 有 | 够 | 之前在 2060 上用的，留作对照基线 |
| Whisper large-v3 | 中文明显弱于上面几个 | 好 | 弱 | 有（不准） | 够 | 不考虑 |

直播的难点是背景音乐、游戏音、口癖、外来词（ins、emoji）和主播专名；公开榜单大多是干净朗读，所以**最终以 `bench` 在你自己的字幕上的结果为准**。

## 它怎么提高准确度（引擎之外）

1. **热词**：主播名、提督/舰长这类固定词、主播配置里的专名，加上你在“学习旧切片”里**确认过**的术语（最多 60 个，只放正确写法，不放常见误识别写法）。
2. **你确认过的固定纠错自动生效**：分段时按视频所在的项目文件夹认主播，套用该主播配置里的固定纠错（含学习旧切片确认写入的）。这点有测试：配了“紫雨亭→子语庭”的主播识别出来就是“子语庭”，别的主播不会被改。
3. **切块在静音处**：每块不超过引擎的最大长度，优先在停顿中点切；硬切时两头各留 0.4 秒上下文，只保留中点落在本块里的字，不重复也不丢。
4. **沿用你习惯的断句**：一条 ≤16 字、停顿 0.65 秒切、不留标点，用的就是旧识别验证过的分段规划器，换引擎不改排版习惯。
5. **英文词不再被切开**：顺手修了共享拆分函数里一个老问题，超长字幕硬切时会把 `emoji` 切成 `emoj | i`（旧网页版同样受益）。
6. 可选“先压低背景音乐和底噪”（旧识别用过的前景滤镜），默认关，对着 `bench` 看开了是否更好。

## 结构

```
desktop/asr/
  types.py      Word / Piece / Chunk、AsrCancelled / AsrError
  text.py       标点挂回字、整句均匀拆字、字错率（编辑距离）
  audio.py      ffmpeg 抽 16k 单声道、silencedetect、按静音切块
  engines/      统一接口 + 注册表；qwen3.py、funasr_legacy.py、scripted.py（测试用假引擎）
  pipeline.py   视频→SRT：切块、检查点续跑、断句、原子写出、不覆盖已有字幕
  hotwords.py   热词（固定词 + 主播配置 + 你确认过的术语）
  jobs.py / worker.py   界面和识别子进程之间的约定（任务 JSON + 标准输出事件行）
  doctor.py     环境自检与加载试跑
  bench.py      用校对字幕对比引擎（字错率、热词命中、速度、显存）
  settings.py   asr.json
qt_app/  asr_process.py（进程事件封装）、asr_panel.py（设置卡片、提示条）、window_asr.py（混入窗口）
```

为什么用子进程：显存随进程结束一定释放；模型或 CUDA 崩了不会带走界面；可以用另一个装了 torch 的 Python 环境跑（设置里的“识别用的 Python”，源码目录自动放进它的 `PYTHONPATH`）。取消就是结束进程，检查点已经落盘。

Windows 启动时先把等待握手的 Python 放入 Job Object，确认成功后才允许加载识别模块；取消、关闭界面或 worker 正常退出后都会清理其后代进程。每个视频的缓存用系统文件锁保护，锁覆盖识别、发布和清理；同一视频不能被界面和命令行同时写入。音频先写独立临时 WAV，成功后原子发布。

识别途中出现同名字幕时，默认发布会拒绝覆盖，保留用户文件、完整检查点及缓存中的 `recognized.srt`。没有未保存修改时界面会刷新载入用户字幕。可用相同设置执行 `run <原视频> --output <其他路径>` 复用检查点另存结果；显式 `--overwrite` 才允许覆盖。失败及待恢复缓存不会按天数自动删除。

## 已验证的范围与待验证项

- **Qwen3 适配已在主力机通过真模型试跑**。`doctor --probe` 通过桌面端同一套子进程启动方式运行，确认 `context` 参数可用、两个本地模型能加载以及静音接口调用成功；官方中文短样本返回了 13 个字的有效时间戳。不同库版本的返回结构兼容仍需各自验证，不能据此推定真实录播的识别精度。
- `qwen-asr 0.0.6` 已在上述 Windows / Python 3.10 独立环境安装并通过依赖检查、导入和 CUDA 试跑；其它 Python、驱动或硬件组合仍需运行 `doctor --probe`。
- 本机从官方 ModelScope 下载的识别模型为 4.70GB，对齐模型为 1.84GB，合计 **6.54GB、22 个文件**，已逐文件核对官方大小与 SHA-256。模型位于 `%LOCALAPPDATA%\AutoSlice\models\Qwen\`，桌面设置已指向本地目录；校验清单见 `%LOCALAPPDATA%\AutoSlice\downloads\qwen-model-download-20261018.json`。
- 没有“说话人过滤”（旧识别的 CAM++ 只留主讲人）：Qwen3 路线下没有对应的东西。如果直播里常有他人语音，用 `bench` 看实际影响再决定要不要补。
- FireRedASR2 没接入（原因见上表）。如果 `bench` 里 Qwen3 的字错率不理想，它是第一个要补的候选，接口已经留好。
- 每个视频的识别缓存（整段 16k 音频，1 小时约 115MB）在 `%LOCALAPPDATA%\AutoSlice\cache\asr\`，成功后自动删，失败/取消后保留用于续跑。

## 后续

- **把引擎接给旧的切片流水线**：流水线的字幕阶段是注入 `ensure_srt` 可调用对象，写一个符合 `TranscriptionService` 签名的小适配就能让它也用新引擎（等桌面端接手切片流程时一起做）。
- 双引擎分歧标注：两个引擎识别结果不一致的地方在字幕表里标“待确认”，把人工校对集中到最可疑的位置。
- 用校对后的字幕反哺：把你每次校对的结果沉淀成热词和固定纠错（学习旧切片已经有一半）。
