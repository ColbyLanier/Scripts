#Requires AutoHotkey v2.0
#Include pragma-once.ahk
PragmaOnce(A_ScriptFullPath, A_ScriptHwnd)
^!r::Reload()
^!h::KeyHistory()
^!l::ToolTip "laptopMode: " laptopMode " | laptopState: " laptopState
^!e::Send "cmlanier@civicinitiatives.com"
#HotIf laptopMode
    RButton::Send "^w"
#HotIf 
global laptopState := true
global laptopMode := false
; Global Settings
SetCapsLockState "AlwaysOff"
SetScrollLockState "AlwaysOff"
; a::SendInput "{Blind}{sc046}"
; z::SendInput "{Blind}{vk91}"

#Include runjs.ahk


; numpad chain inputs
; #Include numscripts\numroot.ahk

; navigation.ahk
; #Include navigation.ahk

^Up:: Send "{Up}{Up}{Up}"
^Down:: Send "{Down}{Down}{Down}"

; z::Send ']'

Alt & z::{
    Send '"[[]]", {Left}{Left}{Left}{Left}{Left}'
}

!x:: {
    Send '<%  %>{Left}{Left}{Left}'
}

^!w:: {                       ; Ctrl+Alt+W
    Run "ms-settings:mobile-devices"
    WinWaitActive "Settings"
    Send "{Tab}"
    Sleep 100
    Send "{Tab}"
    Sleep 100
    Send "{Space}" ; focus the toggle, hit Space
    ; WinWaitActive "Manage mobile devices"
    ; Send "{Tab}"
    ; Sleep 250
    ; Send "{Tab}"
    ; Sleep 250
    ; Send "{Tab}"
    ; Sleep 250
    ; Send "{Down}"
    ; Sleep 250
    ; Send "{Down}"
    ; Sleep 250
    ; Send "{Space}"           ; toggle back on
    ; Sleep 500
    ; Send "{Space}"
}