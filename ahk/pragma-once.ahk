#Requires AutoHotkey v2.0

#SingleInstance Off  ; Allow multiple scripts, but we handle uniqueness manually

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
; ^!+a::ToolTip(DllCall("GetSystemMetrics", "int", 864))
; ^!a::Send("{NumLock}")

; #HotIf !DllCall("GetSystemMetrics", "int", 86) ; Check if external keyboard is connected
; ; SetKeyDelay 0
; Hotkey "Numpad1", NumpadEndFn, "On"
; Hotkey "Numpad2", NumpadDownFn, "On"
; Hotkey "Numpad3", NumpadPgDnFn, "On"
; Hotkey "Numpad4", NumpadLeftFn, "On"
; Hotkey "Numpad5", NumpadClearFn, "On"
; Hotkey "Numpad6", NumpadRightFn, "On"
; Hotkey "Numpad7", NumpadHomeFn, "On"
; Hotkey "Numpad8", NumpadUpFn, "On"
; Hotkey "Numpad9", NumpadPgUpFn, "On"

; NumpadEndFn(*) {
;     BlockInput "On"
;     SendInput "{Blind}{vk61sc14F}"
;     BlockInput "Off"
; }
; NumpadDownFn(*) {
;     BlockInput "On"
;     SendInput "{Blind}{vk62sc150}"
;     BlockInput "Off"
; }
; NumpadPgDnFn(*) {
;     BlockInput "On"
;     SendInput "{Blind}{vk63sc151}"
;     BlockInput "Off"
; }
; NumpadLeftFn(*) {
;     BlockInput "On"
;     SendInput "{Blind}{vk64sc14B}"
;     BlockInput "Off"
; }
; NumpadClearFn(*) {
;     BlockInput "On"
;     SendInput "{Blind}{vk65sc14C}"
;     BlockInput "Off"
; }
; NumpadRightFn(*) {
;     BlockInput "On"
;     SendInput "{Blind}{vk66sc14D}"
;     BlockInput "Off"
; }
; NumpadHomeFn(*) {
;     BlockInput "On"
;     SendInput "{Blind}{vk67sc147}"
;     BlockInput "Off"
; }
; NumpadUpFn(*) {
;     BlockInput "On"
;     SendInput "{Blind}{vk68sc148}"
;     BlockInput "Off"
; }
; NumpadPgUpFn(*) {
;     BlockInput "On"
;     SendInput "{Blind}{vk69sc149}"
;     BlockInput "Off"
; }
; #HotIf

; Numpad1::return
; Numpad2::return
; Numpad3::return
; Numpad4::return
; Numpad5::return
; Numpad6::return
; Numpad7::return
; Numpad8::return
; Numpad9::return

; ; #HotIf