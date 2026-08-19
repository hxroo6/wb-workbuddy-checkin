<#
.SYNOPSIS
    安装 WorkBuddy 每日签到定时任务（Windows 任务计划程序）。
.DESCRIPTION
    用法（PowerShell 5.1+）：
      .\scripts\install_task_windows.ps1 -ProjectDir "F:\WB自动切换多账号领取积分" `
                                         -PythonPath "C:\Users\33225\.workbuddy\binaries\python\versions\3.13.12\python.exe" `
                                         -Time "09:00"
    不传 -PythonPath 时自动使用 py 启动器找到的 Python。
    注册的是"当前用户"任务，无需管理员权限；登录用户会话内运行。
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectDir,
    [string]$PythonPath = "",
    [string]$Time = "09:00",
    [string]$TaskName = "WBCheckinDaily",
    [string]$DataDir = ""
)

$ErrorActionPreference = "Stop"

# ---- 定位 Python ----
if (-not $PythonPath) {
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        $PythonPath = (& py -c "import sys; print(sys.executable)").Trim()
    } else {
        $PythonPath = (Get-Command python -ErrorAction SilentlyContinue).Source
    }
}
if (-not $PythonPath -or -not (Test-Path $PythonPath)) {
    Write-Error "未找到 Python，请用 -PythonPath 参数指定完整路径。"
    exit 1
}

$mainPy = Join-Path $ProjectDir "main.py"
if (-not (Test-Path $mainPy)) {
    Write-Error "未找到 $mainPy，请确认 -ProjectDir 正确。"
    exit 1
}

# ---- 校验时间格式 ----
if ($Time -notmatch "^\d{1,2}:\d{2}$") {
    Write-Error "时间格式应为 HH:MM，例如 09:00"
    exit 1
}
$hour, $minute = $Time.Split(":")

# ---- 先删除已存在的同名任务，再重新注册（幂等） ----
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "已删除旧的同名任务 $TaskName"
}

# ---- 数据目录（默认取项目下的 .data，F 盘） ----
if (-not $DataDir) {
    $DataDir = Join-Path $ProjectDir ".data"
}

$action = New-ScheduledTaskAction -Execute $PythonPath -Argument "`"$mainPy`" --data-dir `"$DataDir`" checkin" -WorkingDirectory $ProjectDir
$trigger = New-ScheduledTaskTrigger -Daily -At ([datetime]::Today.AddHours([int]$hour).AddMinutes([int]$minute))
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Description "WorkBuddy 多账号每日签到与领取礼包（$Time）" | Out-Null

Write-Host ""
Write-Host "✔ 已注册任务：$TaskName"
Write-Host "  执行命令：$PythonPath `"$mainPy`" --data-dir `"$DataDir`" checkin"
Write-Host "  触发时间：每天 $Time"
Write-Host "  查看/手动运行：任务计划程序 → 任务计划程序库 → $TaskName（右键可运行）"
Write-Host "  卸载：.\scripts\uninstall_task_windows.ps1 -TaskName $TaskName"
