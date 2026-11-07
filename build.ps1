$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

python -m pip install --disable-pip-version-check -q -r requirements-build.txt
if ($LASTEXITCODE -ne 0) { throw 'Không cài được công cụ build.' }

$ffmpegOutput = python -c "from static_ffmpeg.run import get_or_fetch_platform_executables_else_raise; print(get_or_fetch_platform_executables_else_raise()[0])"
if ($LASTEXITCODE -ne 0) { throw 'Không tải được FFmpeg.' }
$ffmpeg = ($ffmpegOutput | Select-Object -Last 1).Trim()
if (-not (Test-Path -LiteralPath $ffmpeg -PathType Leaf)) { throw "Không tìm thấy FFmpeg: $ffmpeg" }

$workspace = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$buildTarget = Join-Path $workspace 'dist\ScreenMicRecorder'
if (Test-Path -LiteralPath $buildTarget) {
    $resolvedTarget = (Resolve-Path -LiteralPath $buildTarget).Path
    if (-not $resolvedTarget.StartsWith($workspace + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Thư mục build nằm ngoài dự án: $resolvedTarget"
    }
}

python -m PyInstaller --noconfirm --clean --onedir --windowed --name ScreenMicRecorder --exclude-module static_ffmpeg --add-binary "$ffmpeg;." app.py
if ($LASTEXITCODE -ne 0) { throw 'Đóng gói ứng dụng thất bại.' }

Copy-Item -LiteralPath README.md -Destination dist\ScreenMicRecorder\README.md -Force
Copy-Item -LiteralPath NOTICE-FFMPEG.txt -Destination dist\ScreenMicRecorder\NOTICE-FFMPEG.txt -Force
Copy-Item -LiteralPath NOTICE-PYAUDIOWPATCH.txt -Destination dist\ScreenMicRecorder\NOTICE-PYAUDIOWPATCH.txt -Force
Compress-Archive -Path dist\ScreenMicRecorder -DestinationPath dist\ScreenMicRecorder-Windows.zip -Force
Write-Host "Đã tạo: $PSScriptRoot\dist\ScreenMicRecorder\ScreenMicRecorder.exe"
Write-Host "Bản để sao chép: $PSScriptRoot\dist\ScreenMicRecorder-Windows.zip"
