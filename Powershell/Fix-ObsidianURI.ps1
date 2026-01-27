# Fix-ObsidianURI.ps1
# Registers the obsidian:// protocol handler on Windows
# Run as Administrator: powershell -ExecutionPolicy Bypass -File Fix-ObsidianURI.ps1

param(
    [string]$ObsidianPath = "",
    [switch]$Force
)

# Try to find Obsidian automatically if path not provided
if (-not $ObsidianPath) {
    $possiblePaths = @(
        "$env:LOCALAPPDATA\Obsidian\Obsidian.exe",
        "$env:PROGRAMFILES\Obsidian\Obsidian.exe",
        "${env:PROGRAMFILES(x86)}\Obsidian\Obsidian.exe",
        "$env:USERPROFILE\AppData\Local\Obsidian\Obsidian.exe"
    )

    foreach ($path in $possiblePaths) {
        if (Test-Path $path) {
            $ObsidianPath = $path
            break
        }
    }
}

if (-not $ObsidianPath -or -not (Test-Path $ObsidianPath)) {
    Write-Error "Obsidian.exe not found. Please provide the path using -ObsidianPath parameter."
    Write-Host "Example: .\Fix-ObsidianURI.ps1 -ObsidianPath 'C:\Users\YourName\AppData\Local\Obsidian\Obsidian.exe'"
    exit 1
}

Write-Host "Using Obsidian at: $ObsidianPath" -ForegroundColor Green

# Check if running as admin
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin) {
    Write-Warning "Not running as Administrator. Will attempt to register in HKCU (current user only)."
    $regBase = "HKCU:\Software\Classes"
} else {
    Write-Host "Running as Administrator. Registering in HKLM (all users)." -ForegroundColor Green
    $regBase = "HKLM:\Software\Classes"
}

# Check for existing registration
$existingReg = Get-ItemProperty -Path "$regBase\obsidian" -ErrorAction SilentlyContinue
if ($existingReg -and -not $Force) {
    Write-Host "Existing obsidian:// protocol registration found:" -ForegroundColor Yellow
    $existingCommand = Get-ItemProperty -Path "$regBase\obsidian\shell\open\command" -ErrorAction SilentlyContinue
    if ($existingCommand) {
        Write-Host "  Current handler: $($existingCommand.'(default)')" -ForegroundColor Yellow
    }
    Write-Host "Use -Force to overwrite." -ForegroundColor Yellow

    $response = Read-Host "Overwrite existing registration? (y/N)"
    if ($response -ne 'y' -and $response -ne 'Y') {
        Write-Host "Cancelled." -ForegroundColor Red
        exit 0
    }
}

try {
    # Create the protocol key
    Write-Host "Creating obsidian:// protocol handler..." -ForegroundColor Cyan

    # Main protocol key
    $protocolKey = "$regBase\obsidian"
    if (-not (Test-Path $protocolKey)) {
        New-Item -Path $protocolKey -Force | Out-Null
    }
    Set-ItemProperty -Path $protocolKey -Name "(Default)" -Value "URL:Obsidian Protocol"
    Set-ItemProperty -Path $protocolKey -Name "URL Protocol" -Value ""

    # Default Icon
    $iconKey = "$protocolKey\DefaultIcon"
    if (-not (Test-Path $iconKey)) {
        New-Item -Path $iconKey -Force | Out-Null
    }
    Set-ItemProperty -Path $iconKey -Name "(Default)" -Value "`"$ObsidianPath`",0"

    # Shell\Open\Command
    $shellKey = "$protocolKey\shell"
    if (-not (Test-Path $shellKey)) {
        New-Item -Path $shellKey -Force | Out-Null
    }

    $openKey = "$shellKey\open"
    if (-not (Test-Path $openKey)) {
        New-Item -Path $openKey -Force | Out-Null
    }

    $commandKey = "$openKey\command"
    if (-not (Test-Path $commandKey)) {
        New-Item -Path $commandKey -Force | Out-Null
    }
    Set-ItemProperty -Path $commandKey -Name "(Default)" -Value "`"$ObsidianPath`" `"%1`""

    Write-Host "Successfully registered obsidian:// protocol handler!" -ForegroundColor Green
    Write-Host ""
    Write-Host "Registry path: $protocolKey" -ForegroundColor Cyan
    Write-Host "Command: `"$ObsidianPath`" `"%1`"" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Testing the registration..." -ForegroundColor Yellow

    # Verify registration
    $verification = Get-ItemProperty -Path "$protocolKey\shell\open\command" -ErrorAction SilentlyContinue
    if ($verification) {
        Write-Host "Verification successful!" -ForegroundColor Green
        Write-Host ""
        Write-Host "You can test by running:" -ForegroundColor White
        Write-Host "  Start-Process 'obsidian://open'" -ForegroundColor Cyan
        Write-Host "or by clicking an obsidian:// link in your browser or Stream Deck." -ForegroundColor White
    } else {
        Write-Warning "Could not verify registration. Please check manually."
    }

} catch {
    Write-Error "Failed to register protocol handler: $_"
    exit 1
}

Write-Host ""
Write-Host "If URIs still open File Explorer, try:" -ForegroundColor Yellow
Write-Host "  1. Restart your browser/Stream Deck software" -ForegroundColor White
Write-Host "  2. Check Windows Settings > Apps > Default Apps > Choose default apps by protocol" -ForegroundColor White
Write-Host "  3. Log out and back in, or restart Windows" -ForegroundColor White
