"""安装步骤文案：命令行和设置页共用。"""

SETUP_TEXT = """\
在你跑识别的那台机器（有显卡的主机）上，用装 AutoSlice 的那个 Python：

1) 装 PyTorch（带 CUDA，选和你驱动对得上的版本）：https://pytorch.org/get-started/locally/
2) 装 Qwen3-ASR：
       pip install -U qwen-asr
   （官方建议 Python 3.12；如果装完报 transformers 版本不够，再 pip install -U "transformers>=5.13.0"）
3) 模型有两条路，任选一条：
   a. 不用管：第一次识别时自动从 Hugging Face 下载（识别模型约 3.5 GB + 对齐模型约 1.2 GB）。
   b. 国内网络下不动就用 ModelScope：
          pip install -U modelscope
          modelscope download --model Qwen/Qwen3-ASR-1.7B
          modelscope download --model Qwen/Qwen3-ForcedAligner-0.6B
      下完在设置里把“识别模型 / 对齐模型”改成对应的本地文件夹。
4) 验证：
       python -m autoslice.desktop.asr doctor --probe
5) 用你自己的旧字幕选引擎（只读你的旧文件夹）：
       python -m autoslice.desktop.asr bench --root <你的旧切片文件夹> --engines qwen3-asr,funasr
"""
