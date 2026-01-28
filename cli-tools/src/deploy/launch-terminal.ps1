# PowerShell script to launch Windows Terminal on the secondary (mini) monitor
# without stealing focus from the main monitor.

param(
    [string]$Title = "Terminal",
    [string]$WrapperScript,
    [string]$Environment = "development",
    [string]$Flag = "",
    [string]$ProjectDir = ""
)

# Detect secondary monitor bounds
Add-Type -AssemblyName System.Windows.Forms
$screens = [System.Windows.Forms.Screen]::AllScreens
$secondary = $screens | Where-Object { -not $_.Primary } | Select-Object -First 1

if ($secondary) {
    $area = $secondary.WorkingArea
    $posX = $area.X
    $posY = $area.Y
    # Estimate columns/rows from ~80% of secondary monitor pixel area
    # Typical character cell: ~8px wide, ~16px tall
    $cols = [int](($area.Width * 0.8) / 8)
    $rows = [int](($area.Height * 0.8) / 16)
} else {
    # Fallback: top-right of primary if no secondary monitor
    $primary = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
    $posX = [int]($primary.Right - $primary.Width * 0.4)
    $posY = $primary.Y
    $cols = [int](($primary.Width * 0.4) / 8)
    $rows = [int](($primary.Height * 0.5) / 16)
}

# Build wt.exe arguments: --pos takes pixels, --size takes columns,rows
$wtArgs = "--pos $posX,$posY --size $cols,$rows -p Ubuntu --title `"$Title`" wsl.exe bash `"$WrapperScript`" `"$Environment`" `"$Flag`" `"$ProjectDir`""
Start-Process "wt.exe" -ArgumentList $wtArgs
