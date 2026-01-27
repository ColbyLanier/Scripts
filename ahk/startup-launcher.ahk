#Requires AutoHotkey v2.0
#SingleInstance Force

; ==================== STARTUP LAUNCHER ====================
; Run via Task Scheduler on login
; Provides quick app launch hotkeys for a limited time, then exits
; Completely separate from script-compiler.ahk reload cycle

global StartupTimerSeconds := 10

TrayTip "Startup Mode Active", "V=Vivaldi S=Spotify O=Obsidian`nC=Cursor U=Ubuntu B=Brave`n`nAuto-exits in " StartupTimerSeconds "s", 1

SetTimer ExitStartup, StartupTimerSeconds * 1000 * -1

ExitStartup() {
    TrayTip "Startup Launcher Exiting", "Normal key behavior restored", 1
    Sleep 1500  ; Let user see the notification
    ExitApp
}

; Escape to exit early
Escape::ExitApp

v:: Run "C:\Users\colby\AppData\Local\Vivaldi\Application\vivaldi.exe"
s:: Run "C:\Users\colby\AppData\Roaming\Spotify\Spotify.exe"
o:: Run "C:\Users\colby\AppData\Local\Programs\Obsidian\Obsidian.exe"
c:: Run "C:\Users\colby\AppData\Local\Programs\cursor\Cursor.exe"
u:: Run "wt.exe"
b:: Run "C:\Users\colby\AppData\Local\BraveSoftware\Brave-Browser\Application\brave.exe"
