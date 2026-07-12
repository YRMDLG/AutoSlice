"""
AutoSlice 一键启动
用法: python 启动.py
Ctrl+C 完全停止
"""

import os, sys, subprocess

os.chdir(os.path.dirname(os.path.abspath(__file__)))

print("=" * 50)
print("  AutoSlice - 录播话题分析与智能切片")
print("=" * 50)

# 自动安装依赖
print("\n[1/2] 检查依赖...")
os.environ.setdefault("MODELSCOPE_LOCAL_ONLY", "1")
os.environ.setdefault("AUTOSLICE_FUNASR_DEVICE", "cpu")
env = {**os.environ, "HTTP_PROXY": "", "HTTPS_PROXY": ""}
subprocess.run(
    [sys.executable, "-m", "pip", "install", "-r", "requirements.txt", "-q"],
    env=env
)

# 直接导入 Flask，Ctrl+C 会完全停止
print("[2/2] 启动 Web 服务...")
print("\n  浏览器打开: http://localhost:5002")
print("  按 Ctrl+C 停止\n")
print("=" * 50 + "\n")

# 把 app 的启动代码直接放这里，一个进程，Ctrl+C 全停
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app import app

print("AutoSlice Web 已启动: http://localhost:5002")
print("控制台将实时显示所有任务进度")
app.run(host="0.0.0.0", port=5002, debug=False, threaded=True)
