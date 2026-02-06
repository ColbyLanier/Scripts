# PowerShell script to launch Windows Terminal on the secondary (mini) monitor
# without stealing focus from the main monitor.
# Also handles Docker Desktop startup before launching the deployment.

param(
    [string]$Title = "Terminal",
    [string]$WrapperScript,
    [string]$Environment = "development",
    [string]$Flag = "",
    [string]$ProjectDir = ""
)

# --- Docker Desktop Management ---
# Check if Docker is running by testing the docker command via WSL
$dockerRunning = $false
try {
    $result = wsl.exe docker info 2>&1
    if ($LASTEXITCODE -eq 0) {
        $dockerRunning = $true
    }
} catch {
    $dockerRunning = $false
}

if (-not $dockerRunning) {
    # Check if Docker is paused (engine stopped but app running)
    $dockerProcess = Get-Process "Docker Desktop" -ErrorAction SilentlyContinue

    if ($dockerProcess) {
        # Docker Desktop is running but engine might be paused - just need to unpause
        # The Makefile will handle waiting for it to be ready
        Write-Host "Docker Desktop is running but engine not responding - may be paused"
    } else {
        # Docker Desktop not running at all - start it
        Write-Host "Starting Docker Desktop..."
        $dockerPath = "C:\Program Files\Docker\Docker\Docker Desktop.exe"
        if (Test-Path $dockerPath) {
            Start-Process $dockerPath
            # Brief pause to let Docker Desktop begin initializing
            Start-Sleep -Seconds 2
        } else {
            Write-Host "Warning: Docker Desktop not found at expected path"
        }
    }
}

# --- Monitor Detection ---
# Detect secondary monitor bounds
Add-Type -AssemblyName System.Windows.Forms
$screens = [System.Windows.Forms.Screen]::AllScreens
# Skip the first secondary monitor (mini monitor), use the second one (actual side monitor)
$nonPrimary = $screens | Where-Object { -not $_.Primary }
$secondary = $nonPrimary | Select-Object -Skip 1 -First 1
if (-not $secondary) {
    # Fall back to first non-primary if only one secondary exists
    $secondary = $nonPrimary | Select-Object -First 1
}

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
