# 构建 fngateway（本机 Windows 上交叉编译 Linux 双架构）。
#
# 为什么可以在 Windows 上编：本模块零外部依赖（纯标准库），
# CGO_ENABLED=0 即可纯静态交叉编译，不需要 WSL 或 Docker。
# （对比 cli2api 版依赖 modernc.org/sqlite，也是纯 Go，但依赖体积大得多。）
#
# 用法：pwsh -File build.ps1 [-Version 2.9.0-1] [-OutDir ..\.local-build\bin]
param(
    [string]$Version = "dev",
    [string]$OutDir  = ""
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $OutDir) { $OutDir = Join-Path (Split-Path -Parent $here) ".local-build\bin" }
$OutDir = [System.IO.Path]::GetFullPath($OutDir)
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$go = "C:\Program Files\Go\bin\go.exe"
if (-not (Test-Path $go)) { $go = (Get-Command go).Source }

$env:CGO_ENABLED = "0"
$env:GOFLAGS = "-mod=mod"
$env:GOOS = "linux"

$ldflags = "-s -w -X main.version=$Version"

foreach ($arch in @("amd64", "arm64")) {
    $env:GOARCH = $arch
    $out = Join-Path $OutDir "fngateway-linux-$arch"
    Write-Host "==> linux/$arch -> $out" -ForegroundColor Cyan
    & $go build -trimpath -ldflags $ldflags -o $out .
    if ($LASTEXITCODE -ne 0) { throw "构建 linux/$arch 失败" }
}

Get-ChildItem $OutDir -Filter "fngateway-linux-*" |
    Select-Object Name, Length |
    Format-Table -AutoSize
