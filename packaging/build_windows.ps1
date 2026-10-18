# AutoSlice 桌面端 Windows 打包：在你自己的主力机上运行。
#   powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1            只打程序目录 dist\AutoSlice
#   powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1 -Installer 再用 Inno Setup 做安装包
#
# 做的事：建一个干净的虚拟环境 → 装依赖和 PyInstaller → 整理识别子进程要用的纯源码 → 打包
#        → 把 libmpv / ffmpeg 放到程序旁边 → 跑打包后的自检（--smoke）→（可选）编译安装包。
# 不下载 libmpv / ffmpeg / Inno Setup：它们从你机器上已有的位置复制，找不到就提示你放到 packaging\bin。
# 第一次构建如果报缺模块或缺资源，把整段输出发回来，通常补一行 hiddenimports/datas 就好。

param(
    [string]$PythonArgs = "-3.10",      # py 启动器参数，要和你平时跑桌面端的 Python 版本一致
    [string]$Version = "0.1.0",
    [switch]$Installer,
    [switch]$NoCutout,                  # 不带 OpenCV / onnxruntime（抠图功能会不可用或退回普通模式）
    [switch]$SkipInstall                # 虚拟环境已经装好依赖时跳过 pip
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Venv = Join-Path $Root "build\venv"
$VenvPython = Join-Path $Venv "Scripts\python.exe"
$Bin = Join-Path $Root "packaging\bin"

function Step($text) { Write-Host "`n== $text" -ForegroundColor Cyan }

Step "1/6 虚拟环境"
if (-not (Test-Path $VenvPython)) {
    & py $PythonArgs -m venv $Venv
    if ($LASTEXITCODE -ne 0) { throw "创建虚拟环境失败：确认 py $PythonArgs 可用" }
}
if (-not $SkipInstall) {
    & $VenvPython -m pip install --upgrade pip
    $packages = @("-r", "requirements-qt-desktop.txt", "Pillow>=10.0,<13.0", "pyinstaller>=6.0")
    if (-not $NoCutout) { $packages += @("numpy", "opencv-python-headless", "onnxruntime") }
    & $VenvPython -m pip install @packages
    if ($LASTEXITCODE -ne 0) { throw "安装依赖失败" }
}

Step "2/6 整理识别子进程用的纯源码（build\worker_src）"
$Worker = Join-Path $Root "build\worker_src"
if (Test-Path $Worker) { Remove-Item -Recurse -Force $Worker }
New-Item -ItemType Directory -Force -Path (Join-Path $Worker "autoslice") | Out-Null
# 只要 .py 和 .json：识别子进程在另一个 Python 里跑，要能 import autoslice，但不需要界面资源
& robocopy (Join-Path $Root "src\autoslice") (Join-Path $Worker "autoslice") *.py *.json /S /XD __pycache__ resources web /NFL /NDL /NJH /NJS | Out-Null
if ($LASTEXITCODE -ge 8) { throw "整理源码失败（robocopy 退出码 $LASTEXITCODE）" }

Step "3/6 准备 libmpv / ffmpeg（只复制，不下载）"
New-Item -ItemType Directory -Force -Path $Bin | Out-Null
function Ensure-Binary($name, $candidates) {
    $target = Join-Path $Bin $name
    if (Test-Path $target) { return }
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate)) { Copy-Item $candidate $target; Write-Host "  $name <- $candidate"; return }
    }
    Write-Warning "$name 没找到：安装版里相应功能会不可用。把它放到 packaging\bin 后重新打包。"
}
Ensure-Binary "libmpv-2.dll" @($env:AUTOSLICE_LIBMPV_PATH, (Join-Path $env:LOCALAPPDATA "AutoSlice\bin\libmpv-2.dll"))
foreach ($tool in @("ffmpeg.exe", "ffprobe.exe")) {
    $found = Get-Command $tool -ErrorAction SilentlyContinue
    Ensure-Binary $tool @($(if ($found) { $found.Source }))
}

Step "4/6 PyInstaller"
& $VenvPython -m PyInstaller packaging\autoslice-desktop.spec --noconfirm --clean --distpath dist --workpath build\pyinstaller
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败" }
$Dist = Join-Path $Root "dist\AutoSlice"
Copy-Item (Join-Path $Bin "*") $Dist -Force

Step "5/6 打包后自检"
$Smoke = Join-Path $Root "build\smoke.txt"
if (Test-Path $Smoke) { Remove-Item $Smoke }
$process = Start-Process -FilePath (Join-Path $Dist "AutoSlice.exe") -ArgumentList ("--smoke " + [char]34 + $Smoke + [char]34) -Wait -PassThru
if (Test-Path $Smoke) { Get-Content $Smoke -Encoding UTF8 | ForEach-Object { Write-Host "  $_" } }
if ($process.ExitCode -ne 0) { throw "打包后自检没过，见上面标 FAIL 的行" }

Step "6/6 安装包"
if ($Installer) {
    $iscc = (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source
    if (-not $iscc) {
        $iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
                  "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
    }
    if (-not $iscc) { throw "没找到 Inno Setup 6（ISCC.exe）。装好后重新运行，或只用 dist\AutoSlice 目录。" }
    & $iscc "/DAppVersion=$Version" (Join-Path $Root "packaging\installer.iss")
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup 编译失败" }
    Write-Host "安装包：dist\installer\AutoSlice-Setup-$Version.exe" -ForegroundColor Green
} else {
    Write-Host "跳过（加 -Installer 才做安装包）。程序目录：dist\AutoSlice\AutoSlice.exe" -ForegroundColor Green
}
