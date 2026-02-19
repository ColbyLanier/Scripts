#Requires AutoHotkey v2.0
#SingleInstance Force

; ==================== MONITOR LAUNCHER ====================
; Launches Windows Terminal with `monitor` TUI on the leftmost monitor
; Trigger: Task Scheduler on logon, or manually: schtasks /Run /TN "MonitorLauncher"

SetTitleMatchMode 2  ; Partial title match

; Launch Windows Terminal minimized to avoid flash on main monitor
Run('wt.exe --title "token-monitor" -p "Ubuntu" -- wsl.exe -d Ubuntu -e bash -lic "monitor"', , "Min")

; Wait for window and grab handle before the TUI changes the title
if !WinWait("token-monitor ahk_exe WindowsTerminal.exe",, 15) {
    TrayTip "Monitor Launcher", "Window didn't appear within 15s", 3
    Sleep 2000
    ExitApp
}
hwnd := WinExist()
Sleep 500

; Find the leftmost monitor
leftMon := 1
leftX := 99999
Loop MonitorGetCount() {
    MonitorGetWorkArea(A_Index, &l)
    if (l < leftX) {
        leftX := l
        leftMon := A_Index
    }
}

; Move to leftmost monitor while still minimized, then maximize in place
MonitorGetWorkArea(leftMon, &mL, &mT, &mR, &mB)
WinRestore("ahk_id " hwnd)
Sleep 50
WinMove(mL, mT, mR - mL, mB - mT, "ahk_id " hwnd)
Sleep 50
WinMaximize("ahk_id " hwnd)

ExitApp
