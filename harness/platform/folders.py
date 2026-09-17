"""Interactive desktop folder selection; never runs on the API event loop."""
import base64
import os
import subprocess
import threading

from harness.core import HarnessError

_picker_lock = threading.Lock()


def choose_folder() -> dict:
    if os.name != "nt":
        raise HarnessError("PICKER_UNAVAILABLE", "此平台尚未提供系统选择窗口，请填写目录路径", 501)
    if not _picker_lock.acquire(blocking=False):
        raise HarnessError("PICKER_BUSY", "已有文件夹选择窗口，请先完成或取消", 409)
    # No user text is interpolated into shell code. The helper shares the logged-in desktop.
    script = """
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = '选择项目源文件夹'
$dialog.ShowNewFolderButton = $false
$owner = New-Object System.Windows.Forms.Form
$owner.Text = 'Harness - 选择项目源文件夹'
$owner.TopMost = $true
$owner.ShowInTaskbar = $true
$owner.Width = 380
$owner.Height = 110
$owner.StartPosition = 'CenterScreen'
$owner.FormBorderStyle = 'FixedDialog'
$owner.MaximizeBox = $false
$label = New-Object System.Windows.Forms.Label
$label.Text = '请在系统窗口中选择项目源文件夹。'
$label.AutoSize = $true
$label.Left = 20
$label.Top = 20
$owner.Controls.Add($label)
$owner.Show()
$owner.Activate()
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 1000
$timer.Add_Tick({
    if (-not (Get-Process -Id __PARENT_PID__ -ErrorAction SilentlyContinue)) {
        Stop-Process -Id $PID
    }
})
$timer.Start()
try {
    if ($dialog.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {
        [Console]::Write([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($dialog.SelectedPath)))
    }
} finally { $timer.Stop(); $timer.Dispose(); $dialog.Dispose(); $owner.Dispose() }
"""
    script = script.replace("__PARENT_PID__", str(os.getpid()))
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-STA", "-EncodedCommand",
             base64.b64encode(script.encode("utf-16-le")).decode("ascii")],
            capture_output=True, timeout=180, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode:
            raise HarnessError("PICKER_UNAVAILABLE", "无法打开系统选择窗口，请填写目录路径", 503)
        path = base64.b64decode(result.stdout.strip()).decode("utf-8") if result.stdout.strip() else None
        return {"path": path, "cancelled": path is None}
    except subprocess.TimeoutExpired:
        raise HarnessError("PICKER_TIMEOUT", "选择窗口已超时，请重试或填写目录路径", 408) from None
    finally:
        _picker_lock.release()
