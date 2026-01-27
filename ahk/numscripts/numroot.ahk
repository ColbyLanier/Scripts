#Requires AutoHotkey v2.0
#Include create\obsidian-note.ahk
#include manage\obsidian-note.ahk

; Halts input and returns the next key pressed. 

KeyWaitAny(Options:="") {
    ih := InputHook(Options) 
    if !InStr(Options, "V") 
       ih.VisibleNonText := false 
    ih.KeyOpt("{All}","E") ; End
    ih.Start() 
    ErrorLevel := ih.Wait() ; Store EndReason in ErrorLevel 
    return ih.EndKey ; Return the key name
}

KeyWaitNum(Options:="") {
    ih := InputHook(Options)
    if !InStr(Options, "V") 
       ih.VisibleNonText := false 
    ih.KeyOpt("{Numpad1}{Numpad2}{Numpad3}{Numpad4}{Numpad5}{Numpad6}{Numpad7}{Numpad8}{Numpad9}{Numpad0}{NumpadAdd}{NumpadDot}{NumpadEnter}{Enter}{Esc}","E") ; End
    ih.Start() 
    ErrorLevel := ih.Wait() ; Store EndReason in ErrorLevel 
    PSC := StrReplace(ih.EndKey, "Numpad", '')
    if (PSC == "Escape") {
        throw Error("Esc")
    }
    try {
        return Integer(PSC)
    } catch {
        return PSC
    }
}

wrap_macro(input_func) {
    if laptopMode {
        laptopState := false
    }
    try {
        ToolTip("#!/bin/bash")
        SetNumLockState("On")
        input := KeyWaitNum()
        input_func(input)
    } catch as e {
        SetNumLockState("Off")
        ToolTip(e.Message)
        Sleep(500)
    } finally {
        SetNumLockState("Off")
        ToolTip()
        laptopState := true
    }
}

Create(input) {
    Obs_Note(input)
    ; obs_create(input)

    ; activeWindow := WinGetProcessName("A")
    ; ToolTip("Reading " input " in " activeWindow)
    ; switch { ; Functions are in subfolder
    ;     case InStr(activeWindow, "Obsidian"):
    ;         obs_create(input)
    ;     case InStr(activeWindow, "Vivaldi"):
    ;         ; viv_create(input)
    ;     case InStr(activeWindow, "Cursor"):
    ;         ; cur_create(input)
    ; }
}

Manage(input) {
    obs_manage(input)
}

Navigate(input) {
    ; ToolTip("Navigate")
    switch (input) {
        case 1:
            Run('obsidian://advanced-uri?vault=Personal-ENV&workspace=1-Obsidian',,'Hide')
        case 2:
            Run('obsidian://advanced-uri?vault=Personal-ENV&workspace=2-Civic',, 'Hide')
        case 3:
            Run('obsidian://advanced-uri?vault=Personal-ENV&workspace=3-Algorithms',, 'Hide')
        case 4:
            Run('obsidian://advanced-uri?vault=Personal-ENV&workspace=4-Computing',, 'Hide')
        case 5:
            Run('obsidian://advanced-uri?vault=Personal-ENV&workspace=5-Personal',, 'Hide')
        case 6:
            Run('obsidian://advanced-uri?vault=Personal-ENV&workspace=0-Admin',, 'Hide')
    }
}

#HotIf !GetKeyState("NumLock", "T")
    NumpadDiv::wrap_macro(Create)
    NumpadMult::wrap_macro(Manage)
    NumpadSub::wrap_macro(Navigate)
#HotIf

; Laptop Controls

ToggleLaptopMode() {
    ToolTip(!laptopMode)
    global laptopMode
    laptopMode := !laptopMode
    Sleep(500)
    ToolTip()
}
global laptopMode
SC175::ToggleLaptopMode()
#HotIf laptopMode
    global laptopState
    RAlt & Space::Send("{NumLock}")
    Tab::return
    #HotIf GetKeyState("Tab", "P")
        Space::Ctrl
        d::Send '{Blind}{Right}'
        s::Send '{Blind}{Down}'
        a::Send '{Blind}{Left}'    
        w::Send '{Blind}{Up}'
        q::Send '{Blind}{Home}'
        e::Send '{Blind}{End}'
        z::Send '{Blind}{PgUp}'
        x::Send '{Blind}{PgDn}'
#HotIf 
#HotIf laptopMode & laptopState
    Numpad1::NumpadEnd
    Numpad2::NumpadDown
    Numpad3::NumpadPgDn
    Numpad4::NumpadLeft
    Numpad5::NumpadClear
    Numpad6::NumpadRight
    Numpad7::NumpadHome
    Numpad8::NumpadUp
    Numpad9::NumpadPgUp
#HotIf