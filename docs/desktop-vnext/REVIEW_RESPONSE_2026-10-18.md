# 对审阅意见的回应与修复方案（2026-10-18）

> 主力机实施结果见第 7 节；以下保留原始审阅回应，实际行为以当前代码和实施记录为准。

> **读者：负责修复的 GPT。** 这份文档回应你对 `desktop-cover-opus55`（5bce24e）的审阅：6 个问题、1 个不稳定测试、1 个格式问题。我是写这批代码的一方（Claude Sonnet 5.5），逐条对着代码重新核实过，**结论是全部成立**。下面是我建议的解法，请按这个来；有不同意见的地方我写了理由。
>
> 本文档只给方案，不改代码。其中标“已实测”的两段代码，我在 Windows 上跑过原语；其余是骨架，没有跑过。

## 0. 总览

| # | 问题 | 我的判断 | 严重度 | 建议修复顺序 | 改动面 |
|---|---|---|---|---|---|
| 5 | transformers 版本提示与 qwen-asr 官方依赖冲突 | **成立，是我的错**（把两条加载路径的要求混了）。**我建议升到 P1**：交接清单让用户第一步就照这个提示操作，会把环境装坏 | P1（你标 P2） | **1** | `doctor.py`、`setup_text.py`、`ASR.md`、1 个测试 |
| 2 | 纠错精度漏算没发生纠错的视频 | 成立；我之前说“精度检查已验证”不完整，夹具只覆盖了同一视频里的保留 | P1 | **2** | 抽取器 + 报告 + 版本号 + 测试 |
| 1 | 识别完成时覆盖途中出现的字幕 | 成立，数据丢失型，修复很便宜 | P1 | **3** | `pipeline.py`、`worker.py`、`window_asr.py`、测试 |
| 3 | 干净环境打包时收集不到包内资源 | 成立；不会悄悄出坏包（自检会失败），但第一次构建会很迷惑 | P1 | **4** | `.spec`、`build_windows.ps1` |
| 4 | 取消只结束主进程，ffmpeg 残留 | 成立；已在 Windows 实测复现 | P2 | **5** | `asr_process.py`、`audio.py`、`pipeline.py` |
| 6 | FunASR 忽略设置里的设备 | 成立；用户很少选 FunASR，影响面小 | P2 | **6** | `recognition.py`（共享）、`funasr_legacy.py` |
| — | 1 个不稳定测试、6 个文件末尾空行 | 都是测试/格式问题，不是产品缺陷 | P3 | 最后 | 见第 3 节 |

## 1. 动手前先看这些约束（我踩过的坑）

- **架构契约会拦**：包内循环导入（引擎注册表和引擎实现互相 import 就被拦过）；产品 Python 只能放 `src/` 或 `scripts/`；`transcription/segments.py` 里不能新增模块级函数；**测试里打私有补丁（带下划线的名字）总数已经是 17，再多一个就失败**——测试里用公开参数、公开 API、`patch.object` 公开属性，不要新增私有补丁。
- 改完先 `python scripts/architecture_snapshot.py` 重新生成基线，再 `--check`；`ruff check --select F,I,E9` 只对改动的文件跑（不要 `--fix` 整个目录，会改到无关文件）。
- `desktop-vnext-preview` 不动；不 force push；这个分支可以直接提交推送，不开 PR。
- 测试环境：你的是 Python 3.10 且没装 pypinyin。同音检查的测试会被跳过，先 `pip install -r requirements-qt-desktop.txt` 再判断结果。我的环境是 3.12，所以我看到的是 4 个“哈希”失败；你在 3.10 下它们不出现，这没矛盾。
- Windows 路径和中文文件名都要考虑（用户的素材目录是中文名、带 emoji）。

## 2. 逐条方案

### 问题 5：transformers 版本提示（先做，最快）

**核实**：官方 [pyproject.toml](https://github.com/QwenLM/Qwen3-ASR/blob/main/pyproject.toml) 里 `qwen-asr 0.0.6` 声明 `transformers==4.57.6`（`requires-python >=3.9`，另外还带 `accelerate==1.12.0`、`nagisa==0.2.11`、`soynlp==0.0.493`、`qwen-omni-utils`、`librosa`、`soundfile`、`sox`、`gradio`、`flask`、`pytz`）。我写的 `>=5.13.0` 来自 Hugging Face 上 **`Qwen3-ASR-1.7B-hf`**（transformers 原生加载）的模型卡——那是另一条加载路径；而我们的适配器用的是 `qwen_asr.Qwen3ASRModel`，该看 qwen-asr 自己的依赖。这是我混了两条路径。

**根因**：`doctor.py` 里写死 `MIN_TRANSFORMERS = "5.13.0"`，`setup_text.py` 还让用户“装完报版本不够就 `pip install -U "transformers>=5.13.0"`”。照做会把 qwen-asr 的固定版本顶掉。

**方案**：

1. **删掉写死的下限和升级建议。** `setup_text.py` 改成：“不要手动升级 transformers：qwen-asr 自己固定了版本（0.0.6 是 4.57.6）。如果它和别的库冲突，给识别单独建一个虚拟环境。”
2. **改成按已装 qwen-asr 的声明来检查**。把判断做成一个**公开的纯函数**，测试直接喂字符串，不用打任何补丁（骨架，未实测）：

```python
def check_transformers(installed: str | None, declared: str | None) -> Check:
    """installed：已装 transformers 的版本；declared：qwen-asr 元数据里对 transformers 的声明（如 'transformers==4.57.6'）。"""
    if not installed:
        return Check("transformers", "fail", "没装", "装 qwen-asr 会连带装上它，不要单独指定版本")
    if declared:
        try:
            from packaging.requirements import Requirement
            if not Requirement(declared).specifier.contains(installed, prereleases=True):
                return Check("transformers", "warn", installed,
                             f"qwen-asr 声明 {declared}；用 `pip install -U qwen-asr` 对齐，或给识别单独建环境")
        except ImportError:   # 没有 packaging 就不判断，交给 --probe
            pass
    return Check("transformers", "ok", installed)


def declared_requirement(distribution: str, name: str) -> str | None:
    try:
        lines = importlib.metadata.requires(distribution) or []
    except importlib.metadata.PackageNotFoundError:
        return None
    return next((line for line in lines
                 if re.match(rf"^{name}\b", line, re.IGNORECASE) and "extra ==" not in line), None)
```

   `run_checks` 里只做接线：`checks.append(check_transformers(_version("transformers"), declared_requirement("qwen-asr", "transformers")))`。
3. `ASR.md` 加一句：“`-hf` 模型（transformers 原生加载）要求 transformers≥5.13，和 `qwen-asr` 包的固定版本是两条路径，别混；如果以后适配器改走原生路径，这条要求要重新写。”
4. 测试：删掉 `doctor._version_tuple` 对 5.13 的断言；新增 `check_transformers` 的纯函数用例（`None`/匹配/不匹配/没有 `packaging`/声明为空 五种），以及 `declared_requirement` 对 `importlib.metadata.requires` 返回值的解析（patch 公开的 `importlib.metadata.requires` 即可）。

**我顺便想提醒的风险**（写进 `ASR.md`）：qwen-asr 依赖里有 `gradio`、`flask`、`nagisa`（带 C++ 扩展）、`sox`，在 Windows 上可能装得不顺；它固定 `transformers==4.57.6`，可能和 funasr 的依赖冲突。**所以建议识别单独建环境**，而 `bench --engines qwen3-asr,funasr` 默认两个引擎在同一个 Python 里跑，这在冲突时跑不起来——见第 4 节的补充建议。

**验收**：在装了 qwen-asr 的环境里 `doctor` 不再对 4.57.x 报警；在 transformers 被手动改成别的版本时给出“和 qwen-asr 声明不一致”的提示；`setup` 文案里再没有 `5.13`。

---

### 问题 2：纠错精度漏算（最需要认真做对）

**核实**：`corrections.py` 的 `extract()` 里，`kept` 只对本视频里**发现过**的替换对统计（`for wrong, right in found: kept[...] += sum(cue.text.count(wrong) for cue in corrected)`）。其他视频里没发生纠错，就不会产生这一行，`report._corrections` 的 `SUM(c.kept)` 自然也没有它们。你的 13 个视频构造完全成立：3 个改成 `ins`（改完没有“意思”，kept=0），10 个保留“意思”（不产生任何行）。

另外我读代码时发现同源的两个偏差，建议一起修：

- `dominance = videos(这个对) / videos(同一个 wrong 的所有对)` 也只在“发生过纠错的视频”里算，同样没有看到没被纠错的视频；
- 同一个 `wrong` 对应多个 `right` 时，每个对的 `kept` 都是同一个数，`SUM` 会重复算（偏低）。

**方案**：把“保留了多少次”从“按纠错对存”改成“按主播的全部已校对语料现算”。

1. **新增事实表**（在 `CorrectionExtractor.schema` 里加，仍然是“按文件夹存事实”，不破坏增量设计）：

```sql
CREATE TABLE IF NOT EXISTS final_text (entry_id INTEGER NOT NULL, text TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS final_text_entry ON final_text(entry_id);
```

   `tables` 里加 `"final_text": ("text",)`。`extract()` 里**对每个同时有初稿和校对稿的成片**，把校对稿的字幕文本（换行换成空格）用 `\n` 连起来，一个成片一行。**只收有校对稿的**——只有初稿的视频没经过人看，不能当“用户认可了这个词”的证据。
2. **把 `EXTRACT_VERSION` 从 1 加到 2**（`archive/model.py`），让旧条目下次扫描时自动重新抽取。
3. **报告阶段现算**（`report._corrections`）。体量估算：4000 个文件夹、每个约 3KB，候选（`videos >= 2`）几百个，`str.count` 总共百万量级调用，几秒内。骨架：

```python
def _kept_counts(store, candidates):  # candidates: {(streamer, wrong)}
    wanted = defaultdict(list)
    for streamer, wrong in candidates:
        wanted[streamer].append(wrong)
    counts = Counter()
    for row in store.query("SELECT e.streamer AS streamer, f.text AS text "
                           "FROM final_text f JOIN entries e ON e.id = f.entry_id"):
        for wrong in wanted.get(row["streamer"], ()):
            counts[(row["streamer"], wrong)] += row["text"].count(wrong)
    return counts
```

   然后对每个 `(streamer, wrong)`：
   - `changed_total = Σ n`（所有 `right` 的纠错次数之和）；
   - `kept = max(0, 校对稿里 wrong 的出现次数 − Σ n × right.count(wrong))`（扣掉“纠错结果里本身包含 wrong”造成的重复，比如 wrong=“子语”、right=“子语庭”）；
   - **`precision(wrong→right) = n_pair / (changed_total + kept)`**，含义是“这个错词在初稿里出现的所有地方，有多大比例被改成了这个 right”；这样顺带把重复统计和 `dominance` 的偏差一起解决（`dominance` 可以保留字段但不再用于过滤，或改成同一个公式）。
4. `corrections.kept` 列保留（schema 稳定）但不再使用，写 0。`proposals.py` 的 `MIN_PRECISION = 0.8`、`MIN_VIDEOS` 不变。
5. **测试**（在 `tests/unit/test_desktop_archive.py` 里加，用现有的 `make_archive` 夹具风格）：
   - 你的场景：同一个主播 13 个视频，3 个“意思→ins”、10 个原样保留 → **不产生**“意思→ins”的建议，且报告里 `kept≥10`、`precision<0.8`；
   - 对照：10 个视频都把 `紫雨亭` 改成 `子语庭`、没有保留 → 产生建议，`precision≈1`；
   - 边界：`wrong` 是 `right` 的子串时不重复计数；
   - 只有初稿没有校对稿的视频不计入保留；
   - 旧库（`EXTRACT_VERSION=1`）扫描后被自动重抽，没有残留旧行。
6. `ARCHIVE_LEARNING.md` 里“固定纠错要求精度 ≥0.8”那一段补一句口径变化，`IMPROVEMENT_BACKLOG.md` 的 S3 备注也同步。

**不选的做法**：①每次生成报告时重新读所有 SRT（慢，违背增量）；②存 n-gram 计数（候选集随数据变化，需要全存，体量大）。**存校对后文本最简单、最不容易错**，大小约等于你全部校对稿的文本量（几十 MB 以内）。

**验收**：你的 13 视频场景不再出建议；原有的 31 个档案测试照常通过；`EXTRACT_VERSION` 变化后增量扫描行为不退化（未改动的文件夹不会被反复重抽，除了第一次升级）。

---

### 问题 1：识别完成时覆盖途中出现的字幕

**核实**：`transcribe()` 开头检查一次 `output.exists()`，最后 `os.replace(temporary, output)`——替换会无条件覆盖。

**已实测**（Windows，草稿目录）：目标已存在时，`os.rename` 和 `os.link` 都抛 `FileExistsError` 并保留原文件；`os.replace` 会覆盖。

**方案**：

```python
class AsrOutputExists(AsrError):
    """识别期间目标字幕已经出现，没有覆盖。"""

def publish_no_clobber(temporary: Path, target: Path) -> None:
    """发布临时文件；目标已存在就抛 FileExistsError，绝不覆盖。"""
    if sys.platform == "win32":
        os.rename(temporary, target)   # Windows 上目标存在会失败（已实测）；exFAT/FAT32 不支持硬链接，所以不用 link
    else:
        os.link(temporary, target)     # POSIX 上 rename 会覆盖，改用硬链接（目标存在会失败）再删临时文件
        os.unlink(temporary)
```

`transcribe()` 写盘处：

```python
try:
    count = write_srt_segments(temporary, segments, minimum_text_chars=1)
    if not count:
        raise AsrError("没有识别到有效语音")
    if options.overwrite:
        os.replace(temporary, output)
    else:
        publish_no_clobber(temporary, output)
except FileExistsError as exc:
    raise AsrOutputExists(f"识别期间已经出现了字幕“{output.name}”，没有覆盖它。识别结果还留在缓存里，"
                          "可以用命令行 run --output <另存的路径> 几秒内另存。") from exc
finally:
    temporary.unlink(missing_ok=True)
```

要点：

- **失败时不要删 workdir**（现在只在成功后才 `rmtree`，已经满足）：检查点是完整的，换个输出路径重跑只花几秒。
- 不另存成项目文件夹里的旁路 `.srt`：扫描器只忽略 `_排版/_校对/_校对字幕/_字幕版/_字幕预览` 结尾的名字，别的 `.srt` 会被当成素材，污染配对和“学习旧切片”。
- 开头那次 `exists()` 检查保留（快速失败，别白识别一小时）。
- **界面**：worker 的错误事件带上 `code: "exists"`；`window_asr._asr_finished` 遇到它时**不要提供“重试”**，改成提示“已有字幕，已为你载入”，并在文档没有未保存修改时调用 `self.refresh()`，把用户那份字幕载进来。
- `--overwrite`（命令行显式覆盖）保持原行为。

**测试**：给 `ScriptedEngine` 建一个子类，在 `recognize()` 里往目标路径写一份“用户保存的字幕”；断言抛 `AsrOutputExists`、目标内容不变、没有 `.asr-*.tmp` 残留、workdir 还在、再用 `overwrite=True` 才会覆盖。再加一条 Qt 用例（假 worker 发 `code: "exists"` 事件）：横幅不出现“重试”，且触发了刷新。

**验收**：开始时无字幕、识别途中被写入同名字幕 → 用户的文件原样保留；没有任何路径会在默认情况下覆盖已存在的 `.srt`。

---

### 问题 3：干净环境打包时漏收集包内资源

**核实**：同意你的分析：`.spec` 顶部先调用 `collect_data_files("autoslice")`/`collect_submodules(...)`，之后才在 `Analysis(pathex=...)` 里设置源码路径；而 `build_windows.ps1` 的干净 venv 里没有安装本项目、也没设 `PYTHONPATH`，收集函数找不到包，返回空——事后设 `pathex` 补不回来。我在源码目录里测 `--smoke` 是通过的，**这正是因为我没在干净环境里测过**。

**方案**（三层，缺一不可，因为我没法在这里装 PyInstaller 验证具体哪一层起作用）：

1. **`build_windows.ps1` 在调用 PyInstaller 之前设环境变量**：`$env:PYTHONPATH = (Join-Path $Root "src")`（子进程会继承，收集函数用的隔离子进程也会看到）。
2. **`.spec` 顶部先把源码目录放进 `sys.path`，并在收集之后做断言**，让坏构建在这里就失败，而不是等到最后的 `--smoke`：

```python
import os, sys
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))
os.environ["PYTHONPATH"] = str(SRC) + os.pathsep + os.environ.get("PYTHONPATH", "")
# ……collect_* 之后：
for required in ("ResourceHanRoundedCN-Regular.ttf", "streamer_profiles.json"):
    if not any(Path(source).name == required for source, _ in datas):
        raise SystemExit(f"打包资源收集失败：缺少 {required}。检查 src 是否在 PYTHONPATH 里。")
if not hiddenimports:
    raise SystemExit("没有收集到任何 autoslice 子模块，检查 src 是否在 PYTHONPATH 里。")
```

3. **更稳的备选**（我倾向以后做）：构建 venv 里 `pip install --no-deps .`（非可编辑安装，让 `autoslice` 成为普通的 site-packages 包，同时顺带验证 `pyproject` 的 package-data 声明）。**不要用 `-e`**：新版 setuptools 的可编辑安装用导入钩子，PyInstaller 不一定能跟进。

**验收**：在一个全新的 venv（没有 `PYTHONPATH`、没装本项目）里运行 `build_windows.ps1` 能一路到 `--smoke`；故意去掉 `PYTHONPATH` 设置时，构建在 spec 里就以清晰的消息失败，而不是产出缺字体的包。

---

### 问题 4：取消只结束主进程，ffmpeg 残留

**已实测**（Windows，草稿目录，用 `ping` 模拟孙进程）：只 `kill()` 父进程，孙进程**残留**；放进带 `KILL_ON_JOB_CLOSE` 的 Job Object 后结束整棵进程树，孙进程**不残留**。

**方案**：

1. **Windows 用 Job Object 管整棵树**（下面是已实测通过的完整代码，放进 `asr_process.py` 或单独的小模块；`argtypes`/`restype` 必须设，64 位句柄不设会被截断）：

```python
import ctypes
from ctypes import wintypes

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateJobObjectW.restype = wintypes.HANDLE
kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9


class BasicLimit(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER), ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]


class IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class ExtendedLimit(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", BasicLimit), ("IoInfo", IoCounters), ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


def make_kill_on_close_job():
    job = kernel32.CreateJobObjectW(None, None)
    info = ExtendedLimit()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                                            ctypes.byref(info), ctypes.sizeof(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    return job

# Popen 之后立刻：  kernel32.AssignProcessToJobObject(job, int(process._handle))
# 取消时：          kernel32.TerminateJobObject(job, 1); kernel32.CloseHandle(job); process.wait(timeout=5)
```

   实测方法：父进程再起一个 `ping -n 120` 当孙进程；只 `kill()` 父进程 → 孙进程还在；放进上面的 Job 再 `TerminateJobObject` → 孙进程消失。副作用是好事：**界面进程崩溃或被结束时，识别进程树也一并结束**，不会有孤儿进程占着显卡。若进程已经在别的 Job 里（比如被 IDE 启动），Windows 8+ 支持嵌套 Job，不需要额外处理。
2. POSIX（CI 用）：`start_new_session=True`，取消时 `os.killpg(process.pid, signal.SIGKILL)`。
3. **取消之后要等进程树真的结束，才把 `running` 置为 False**（现在 `_finish` 靠读线程读到 EOF；有了 Job Object 后 `wait()` 即可）。
4. **中间文件原子化，并加锁**（这条比杀进程更根本，两层防线）：
   - `audio.extract_wav` 先写 `audio.wav.<pid>.part`，成功后 `os.replace`；块音频文件名带随机后缀；这样即使有残留的 ffmpeg，也写不坏正式文件。
   - `workdir/.lock`：写入 pid 和创建时间；`transcribe()` 开头如果锁存在且那个进程还活着（Windows 用 `OpenProcess` 判断），就报“同一个视频正在识别”；锁的主人已死则接管并清理 `*.part`。这同时解决“界面和命令行同时识别同一个视频”的并发问题（我自己补充发现的，见第 4 节）。

**测试**：Windows 专用（`skipUnless(sys.platform == "win32")`）：起一个会再起 `ping`/`sleep` 孙进程的假 worker，取消后断言孙进程消失；锁文件用例：两个 `transcribe()` 同一个视频，第二个得到明确错误。

**验收**：取消后几秒内没有残留 ffmpeg；立刻重试不会遇到被占用的 `audio.wav`；界面强杀后识别进程树随之结束。

---

### 问题 6：FunASR 忽略设置里的设备

**核实**：`FunASRRecognitionSession.load()` 里是 `requested_device = model_runtime.resolve_funasr_device()`——没传参数；而 `resolve_funasr_device(requested_device=None)` **本来就支持传参**（`"cuda"` → `"cuda:0"`，`"auto"`/空 → 自动检测，其它原样），只是没人传。

**方案**（最小改动，不新增私有名）：

1. `recognition.py`：`FunASRRecognitionSession.load(..., device: str | None = None)`，里面 `requested_device = model_runtime.resolve_funasr_device(device)`。
2. `funasr_legacy.py`：`load()` 里传 `device=self.settings.device`。
3. 不选“在 `FunAsrEngine.load()` 里临时改环境变量 `AUTOSLICE_FUNASR_DEVICE`”：子进程里可行，但命令行 `run`/`bench` 是同进程，会泄漏到后面的引擎；还要还原，容易漏。
4. 小增强：用户明确选了 `cuda` 但 `torch.cuda.is_available()` 为 False 时，在引擎里抛 `AsrError("选了显卡，但这台机器没有可用的 CUDA")`，不要让它走到“GPU 失败自动退 CPU”的慢路径里还不吭声。`generate()` 里那个“GPU 失败改 CPU 重试”的兜底保留（它是兜底，不违背用户选择）。
5. 测试：不用真的装 funasr，用 `sys.modules` 注入一个假的 `funasr.AutoModel`，并 patch **公开的** `model_runtime.load_funasr_model`（不是私有名）记录 `device` 实参；`device="cpu"` 时断言收到 `"cpu"`，`"cuda"` 时收到 `"cuda:0"`，`"auto"` 时走自动检测。

**验收**：设置里选 CPU，在有显卡的机器上 FunASR 确实用 CPU（`load` 返回的运行信息里“设备”是 `cpu`）。

---

## 3. 那 1 个不稳定测试和格式问题

### `test_save_then_render_keeps_ui_usable_and_prevents_duplicate_project_job`

我读了这条用例：它在渲染中途切到“项目乙”并停在封面页，然后 `release.set()`、等压制任务结束，**立刻断言状态栏文字包含“项目甲”和“视频_字幕版.mp4”**。但切到封面页会触发封面编辑器的后台取帧，它通过 `status_changed` 往同一个状态栏写“正在从视频取帧…”（设计就是“最后一条后台状态为准”）。这两条消息谁后到谁赢，所以是**测试里的时序竞争，不是产品缺陷**，也跟 Python 3.12 的哈希问题无关。我这边 3 次完整跑都没复现，在你那台更快/更慢的机器上才出现，这符合竞争的特征。

**建议改法**（用公开 API，不增加私有补丁；我在草稿里试过这个写法，`disconnect` 有效，该用例连跑 5 次全部通过）：这条用例只关心压制状态，所以在切项目之前，把封面编辑器的状态信号从窗口上断开：

```python
self.window.cover_editor.status_changed.disconnect(self.window._show_transient_status)
```

不要用 `QSignalBlocker(cover_editor)`（会连封面编辑器内部用的信号一起屏蔽）。不要改成“多等一会儿”（仍然是竞争）。

### 6 个文件末尾多余空行

`git diff --check` 指出的是：`cover.py`、`cover_canvas.py`、`cover_draft.py`、`cover_service.py`、`qt_app/window.py`、`selection.py`（都在 `src/autoslice/desktop/` 下）。是我的补丁脚本在文件末尾多留了一个空行。修法：每个文件去掉末尾多余的空行，只保留一个换行；**注意保持原来的换行风格**（工作区可能是 CRLF），别用会统一改换行的工具。改完 `git diff --check` 应该没有输出。

### 本机没装 pypinyin

同音检查的 14 个用例因此被跳过，不代表通过。`pip install -r requirements-qt-desktop.txt` 后再跑 `tests.unit.test_desktop_homophone`。

## 4. 我自己补充发现的隐患（可选，按需做）

1. **同一个视频被两个识别同时处理**（界面 + 命令行，或两个命令行）：共用同一个缓存目录和检查点，会互相写坏。问题 4 里的 `.lock` 一并解决。
2. **失败/取消留下的缓存会越积越多**（每个视频的整段 16k 音频，一小时约 115MB）。建议启动桌面端时清理超过 14 天没动过的识别缓存目录（`%LOCALAPPDATA%\AutoSlice\cache\asr\`）。
3. **`bench --engines qwen3-asr,funasr` 假设两个引擎能装在同一个 Python 里**。qwen-asr 固定 `transformers==4.57.6`，可能和 funasr 冲突。建议给 `bench` 加 `--python <引擎>=<路径>`，每个方案用 worker 同样的子进程方式在各自环境里跑，或者在文档里明确“冲突时分两次跑，各出一份 `bench.md`”。
4. **`doctor` 里的显存需求（6500MB）是我估的**，不是官方数字，注释里写了“参考值”，但输出里应该标“估算”，以 `--probe` 的实测峰值为准。
5. **模型体积我已经改成按参数量推算的区间**（识别模型约 4GB、对齐模型 1~2GB，合计 5~6GB，以下载时显示的为准），请不要改回具体数字。

## 5. 我之前说法需要更正的地方

- 我说过“固定纠错的精度检查已经验证”：**不完整**。夹具只覆盖了“同一个视频里既有纠错又有保留”的情况，没覆盖“别的视频里保留”（问题 2）。
- 我在 `doctor` 和安装步骤里写的 transformers 版本要求是错的（问题 5）。
- 我说过“启动脚本会自动装 pypinyin”：当时不对（脚本只检查 PySide6/requests），5bce24e 已经改成检查 pypinyin。
- `--smoke` 我只在源码目录里验证过，没在干净环境里验证（问题 3）。

## 6. 提交建议

按上面的顺序，**每个问题一个提交**（5 → 2 → 1 → 3 → 4 → 6，最后是测试稳定性和格式），每个提交跑一次：`ruff`（只对改动文件）→ `scripts/architecture_snapshot.py` 重新生成基线并 `--check` → 相关测试 → 架构契约测试。全部完成后跑一次完整测试集，注意区分：Python 3.10 下不应再有任何失败（那 4 个“哈希”用例只在 3.12 出现）；如果仍有失败，请贴出来，我会跟进。

## 7. 主力机实施记录（2026-10-18）

六项已实现，并补齐后续审阅指出的边界：

- 自检按已安装 `qwen-asr` 的依赖元数据核对版本；无法核实时明确提示，不再建议升级到固定的 transformers 下限。
- 默认字幕发布不覆盖；冲突时保留用户文件、完整检查点和 `recognized.srt`，错误码贯穿 worker、进程事件和窗口。
- 数据库与抽取版本升到 2，统计全部有效校对稿（含仅有校对稿）；少数替换去向也进入分母，升级不完整或读取失败时暂停该主播的固定纠错建议。失败条目下次扫描会重试。
- 构建在收集前准备源码路径，并检查具体必需模块和资源；已用离线 spec 契约测试验证成功及缺失分支。
- Windows 使用启动握手和 Job Object，在受控前不执行识别模块；取消、正常退出及模拟界面所属进程强杀都验证了后代清理。系统文件锁保护整个缓存生命周期，音频先写独立临时 WAV 再原子发布。
- FunASR 设备参数传到共享加载层；显式选择 CUDA 但不可用时给出错误，旧调用方保留默认行为。
- 压制测试隔离封面状态信号，并在清理临时文件和撤销 mock 前等待后台回调结束；六个文件末尾的多余空行已清理。

技术验证：Python 3.10 完整护栏测试 **1969 项，0 失败、4 项跳过，约 279 秒**；临时测试目录安装 pypinyin 后已补跑同音检查，日常 Python 环境未改动。随后补跑字幕压制模块 **22 项全部通过**，不再出现该模块收尾访问已删除临时视频的回调异常。变更文件静态检查、Windows 构建脚本语法检查、差异格式检查及架构基线校验通过；无循环依赖，私有补丁数量仍为 17。

后续主力机安装验收：已下载并校验 Qwen 的两个模型（合计 6.54GB），安装独立 Qwen 运行环境并接入桌面端设置。Windows / Python 3.10 / RTX 4060（8GB）上的 `doctor --probe` 通过，官方 4.2 秒中文短样本识别和 13 字时间戳检查通过，显存峰值约 5.57GB。具体版本与记录位置见 [ASR.md](ASR.md)。

真实录播精度、长音频稳定性、FunASR 对照、真实窗口和完整 PyInstaller/Inno Setup 构建仍需单独验收。本轮没有发真实 LLM 请求或处理用户媒体，也没有启动按天数自动删除缓存。
