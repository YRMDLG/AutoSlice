# 复制为本机私有脚本或逐行执行。请先替换 API 地址、token 和模型名。
$env:AUTOSLICE_API_BASE_URL = "https://api.example.com/v1"
$env:AUTOSLICE_API_TOKEN = "YOUR_API_TOKEN"
$env:AUTOSLICE_API_TYPE = "openai"
$env:AUTOSLICE_ANALYSIS_MODEL = "YOUR_ANALYSIS_MODEL"
$env:AUTOSLICE_LLM_MODEL = "YOUR_REVIEW_MODEL"

# 以下目录使用项目内默认值；按需改为自己的本机目录。
$env:AUTOSLICE_VIDEO_DIR = Join-Path $PSScriptRoot "recordings"
$env:AUTOSLICE_OUTPUT_DIR = Join-Path $PSScriptRoot "output"
$env:AUTOSLICE_TIMELINE_DIR = Join-Path $PSScriptRoot "timelines"
$env:AUTOSLICE_SUBMISSION_DIR = Join-Path $PSScriptRoot "submissions"
$env:AUTOCOVER_INPUT_DIR = Join-Path $PSScriptRoot "output"
$env:AUTOCOVER_OUTPUT_DIR = Join-Path $PSScriptRoot "covers"
$env:AUTOCOVER_STICKER_DIR = Join-Path $PSScriptRoot "stickers"

# CPU 最兼容；确认 CUDA 运行时可用后可改为 cuda:0 或 auto。
$env:AUTOSLICE_FUNASR_DEVICE = "cpu"
