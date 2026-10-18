# 打包成安装包（在你的主力机上做）

> 状态：**脚本已写好，没在任何机器上构建过**（写代码的机器是别人的电脑，没装、也不该装 PyInstaller / Inno Setup）。第一次构建可能要补一两行 `hiddenimports` / `datas`，把整段输出发回来就行。

## 前提（都在你的主力机上，都是你自己装）

- `py -3.10` 可用（和你现在跑桌面端用的版本一致）。
- `libmpv-2.dll`：已经在 `AUTOSLICE_LIBMPV_PATH` 或 `%LOCALAPPDATA%\AutoSlice\bin\` 的话脚本会自动复制；否则放进 `packaging\bin\`。
- `ffmpeg.exe`、`ffprobe.exe`：在 PATH 里的话脚本会自动复制；否则放进 `packaging\bin\`。
- 要做安装包才需要 [Inno Setup 6](https://jrsoftware.org/isinfo.php)（装好后脚本会自己找 `ISCC.exe`）。
- 想要图标：放一个 `packaging\autoslice.ico`，没有就用默认图标。

脚本只会**复制**你机器上已有的文件，不会下载 libmpv / ffmpeg / Inno Setup。PyInstaller 和依赖会 `pip install` 到 `build\venv`（一个干净的虚拟环境，不碰你平时的环境）。

## 运行

```bash
powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
```

加 `-Installer` 同时做安装包，`-NoCutout` 不带 OpenCV / onnxruntime（抠图会不可用或退回普通模式），`-SkipInstall` 在依赖已装好时跳过 pip，`-Version 0.2.0` 指定版本号。

产物：

- `dist\AutoSlice\AutoSlice.exe`（目录版，可以直接拷走用）
- `dist\installer\AutoSlice-Setup-<版本>.exe`（装到当前用户目录，不要管理员权限；卸载只删程序，**你的草稿、缓存、设置、识别模型都在 `%LOCALAPPDATA%\AutoSlice\`，一律保留**）

脚本最后会跑 `AutoSlice.exe --smoke`：不开窗口，检查关键库、字体和主播配置、识别模块、识别子进程用的源码是否都带上了，任何一项 `FAIL` 都会让构建失败。

## 安装版和识别（重点）

安装包**不带** torch、funasr、modelscope（带上有好几个 GB，而且识别本来就该在有显卡的环境里跑）。所以安装版里要在 **设置 → 字幕识别 → “识别用的 Python”** 填你装了 torch 和识别库的那个 `python.exe`；没填点“识别字幕”会直接告诉你该去填。识别子进程需要 import `autoslice`，打包时把纯源码（只含 `.py` 和 `.json`）放进了 `worker_src`，安装版会自动把它放进子进程的 `PYTHONPATH`。

## 没做、需要你决定的

- 旧的 Tk 界面（`桌面端.py`、`autoslice.desktop.app`）还在，是否删除等你定。
- 代码签名：没有证书，Windows SmartScreen 第一次会提示“未知发布者”。
- 自动更新：没做。
