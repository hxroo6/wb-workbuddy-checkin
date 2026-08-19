<#
.SYNOPSIS
    卸载 WorkBuddy 每日签到定时任务。
.EXAMPLE
    .\scripts\uninstall_task_windows.ps1
#>
param(
    [string]$TaskName = "WBCheckinDaily"
)

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $existing) {
    Write-Host "任务 $TaskName 不存在，无需卸载。"
    exit 0
}
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "✔ 已卸载定时任务 $TaskName"
