"""安装步骤文案：命令行和设置页共用。"""

SETUP_TEXT = """\
在你跑识别的那台机器（有显卡的主机）上，用装 AutoSlice 的那个 Python：

1) 装 PyTorch（带 CUDA，选和你驱动对得上的版本）：https://pytorch.org/get-started/locally/
2) 装 Qwen3-ASR：
       pip install -U qwen-asr
   建议给识别单独建虚拟环境。不要单独升级 transformers：qwen-asr 会声明匹配版本
   （0.0.6 固定为 4.57.6）；有依赖冲突时，在独立环境中重新安装当前版本的 qwen-asr。
3) 模型有两条路，任选一条：
   a. 不用管：第一次识别时自动从 Hugging Face 下载（识别模型约 4 GB + 对齐模型约 1~2 GB，合计大约 5~6 GB，以下载时显示的为准）。
   b. 国内网络下不动就用 ModelScope：
          pip install -U modelscope
          modelscope download --model Qwen/Qwen3-ASR-1.7B
          modelscope download --model Qwen/Qwen3-ForcedAligner-0.6B
      下完在设置里把“识别模型 / 对齐模型”改成对应的本地文件夹。
4) 验证：
       python -m autoslice.desktop.asr doctor --probe
5) 用你自己的旧字幕选引擎（只读你的旧文件夹）：
       python -m autoslice.desktop.asr bench --root <你的旧切片文件夹> --engines qwen3-asr,funasr
   两个引擎需要在这个 Python 中都可用；依赖冲突时，分别在各自环境中运行 bench --engines <引擎> --out <不同报告名>。
"""
