#Requires AutoHotkey v2.0

#SingleInstance Off  ; Allow multiple scripts, but we handle uniqueness manually

SetCapsLockState "AlwaysOff"
SetScrollLockState "AlwaysOff"

PragmaOnce(scriptPath, hwnd) {
    DetectHiddenWindows True
    SetTitleMatchMode 3
    query := scriptPath " ahk_class AutoHotkey"
    ; Check if another instance of this specific script is already running
    if existingHwnd := WinExist(query) {
        ToolTip("Balls")
        ProcessClose(WinGetPID(existingHwnd))  ; Close the existing instance
        Sleep 100  ; Give it time to close
        PragmaOnce(scriptPath, hwnd)
        ToolTip()
    } else {
        WinSetTitle scriptPath, "ahk_id " hwnd
    }
}
PragmaOnce(A_ScriptFullPath, A_ScriptHwnd)

#Include audio-monitor.ahk
#Include discord-ipc-mute.ahk
#Include *i private.ahk  ; Optional include - won't error if missing

^Up:: Send "{Up}{Up}{Up}"
^Down:: Send "{Down}{Down}{Down}"

^!r::Reload()
^!h::KeyHistory()

^!s::{
    Send("^+n")
    Sleep 1500
    Send("{F8}")
    Sleep 100
    Send("askcivic.com")
    Sleep 500
    Send("{Enter}")
    Sleep 500
    Send("^+i")
}

global tvConnected := false

^!t:: {  ; Ctrl+Alt+T - Toggle TV connection
    global tvConnected

    Send "#k"
    Sleep 800
    if (!tvConnected) {
	Sleep 250
        Send "{Tab}{Enter}"
        tvConnected := true
    } else {
        Send "{Tab}{Tab}{Enter}"
        tvConnected := false
    }
    Send "{esc}"
}

^!+s::{
    Send("^+n")
    Sleep 1500
    Send("{F8}")
    Sleep 100
    Send("dev.askcivic.com")
    Sleep 500
    Send("{Enter}")
    Sleep 500
    Send("^+i")
}

^!f::Send("Name three things in the Alabama administrative code that are abnormal in the nation. cite sources.")


^!o::{
    Send("!{f4}")
    Sleep 100
    Send("!{Space}")
    Sleep 100
    Send ".Obsidian{Enter}"
}


^!w:: {                       ; Ctrl+Alt+W

    Run "ms-settings:mobile-devices"
    WinWaitActive "Settingt"

    Send "{Tab}"
    Sleep 100
    Send "{Tab}"
    Sleep 100
    Send "{Space}" ; focus the toggle, hit Space
}

