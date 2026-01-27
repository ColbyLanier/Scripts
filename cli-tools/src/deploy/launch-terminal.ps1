# PowerShell script to launch Windows Terminal with foreground focus
# This script uses Windows API to force the terminal window to foreground

param(
    [string]$Title = "Terminal",
    [string]$WrapperScript,
    [string]$Environment = "development",
    [string]$Flag = "",
    [string]$ProjectDir = ""
)

# Launch Windows Terminal with WSL profile
$wtArgs = "-p Ubuntu --title `"$Title`" wsl.exe bash `"$WrapperScript`" `"$Environment`" `"$Flag`" `"$ProjectDir`""
Start-Process "wt.exe" -ArgumentList $wtArgs

# Wait briefly for window to spawn
Start-Sleep -Milliseconds 500

# Find and activate the Windows Terminal window
Add-Type @"
    using System;
    using System.Runtime.InteropServices;
    public class Win32 {
        [DllImport("user32.dll")]
        [return: MarshalAs(UnmanagedType.Bool)]
        public static extern bool SetForegroundWindow(IntPtr hWnd);

        [DllImport("user32.dll")]
        public static extern IntPtr FindWindow(string lpClassName, string lpWindowName);
    }
"@

# Try to find the window by title and bring it to foreground
$maxAttempts = 10
for ($i = 0; $i -lt $maxAttempts; $i++) {
    $hwnd = [Win32]::FindWindow($null, $Title)
    if ($hwnd -ne [IntPtr]::Zero) {
        [Win32]::SetForegroundWindow($hwnd)
        break
    }
    Start-Sleep -Milliseconds 200
}
