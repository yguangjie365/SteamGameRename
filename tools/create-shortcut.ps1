# 在桌面创建“SteamGameRename”快捷方式（无需管理员权限）。
# 由 创建桌面快捷方式.bat 调用；也可手动运行：
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\create-shortcut.ps1

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $root 'SteamGameRename.bat'
if (-not (Test-Path -LiteralPath $launcher)) {
    Write-Error "找不到启动脚本: $launcher"
    exit 1
}

$desktop = [Environment]::GetFolderPath('Desktop')
$linkPath = Join-Path $desktop 'SteamGameRename.lnk'

$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($linkPath)
$link.TargetPath = Join-Path $env:WINDIR 'System32\cmd.exe'
$link.Arguments = '/c ""' + $launcher + '""'
$link.WorkingDirectory = $root
$link.Description = 'SteamGameRename · 本地 Steam 游戏中文名编辑工具（自动打开浏览器）'
$link.WindowStyle = 1

$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if ($python) { $link.IconLocation = "$python,0" }

$link.Save()
Write-Output "已创建快捷方式: $linkPath"
