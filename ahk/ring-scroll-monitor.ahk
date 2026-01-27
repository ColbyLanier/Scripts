#Requires AutoHotkey v2.0
#SingleInstance Force
Persistent

; Scroll Input Monitor - Detect all button codes from ring
; Press Ctrl+Shift+Escape to exit

#Include <AutoHotInterception>

; Set to 0 to monitor ALL mice and detect the ring's new ID
RING_DEVICE_ID := 0  ; Change back to specific ID once detected
MAX_DISPLAY := 30

global AHI := AutoHotInterception()
global scrollLevel := 0
global inputLog := []
global lastButton := "---"
global lastState := "---"

; Create GUI
global monitorGui := Gui("+AlwaysOnTop -Caption +ToolWindow", "Scroll Monitor")
monitorGui.BackColor := "0x1a1a1a"
monitorGui.SetFont("s10", "Consolas")

global levelText := monitorGui.Add("Text", "w250 cFFFFFF Center", "Level: 0")
global buttonText := monitorGui.Add("Text", "w250 c00FFFF Center", "Last Btn: --- State: ---")
global barDisplay := monitorGui.Add("Text", "w250 h300 c00FF00", "")
global rateText := monitorGui.Add("Text", "w250 cFFFF00 Center", "Rate: 0/sec")
global logText := monitorGui.Add("Text", "w250 h100 c888888", "Event log:")

monitorGui.Show("x10 y100 NoActivate")

; Get all devices and subscribe to all mice
devices := AHI.GetDeviceList()
subscribed := ""

for id, device in devices {
    if (device.IsMouse) {
        ; Subscribe to buttons 0-10 on each mouse
        Loop 11 {
            btn := A_Index - 1
            try {
                AHI.SubscribeMouseButton(id, btn, false, DeviceButtonCallback.Bind(id, btn))
            }
        }
        subscribed .= "ID" id " "
    }
}
levelText.Value := "Mice: " subscribed

TrayTip("Scroll Monitor", "Listening to mice: " subscribed "`nClick/scroll with ring to find its ID", 1)

; Rate calculation timer
SetTimer(UpdateRate, 100)

DeviceButtonCallback(deviceId, btn, state) {
    global scrollLevel, inputLog, MAX_DISPLAY, buttonText, logText

    ; Update button display immediately - show device ID prominently
    buttonText.Value := "Device: " deviceId " Btn: " btn " State: " state

    ; Log the event
    inputLog.Push({time: A_TickCount, device: deviceId, btn: btn, state: state})

    ; Update log text (last 5 events)
    logStr := "Event log:`n"
    startIdx := Max(1, inputLog.Length - 4)
    Loop Min(5, inputLog.Length) {
        idx := startIdx + A_Index - 1
        if (idx <= inputLog.Length) {
            entry := inputLog[idx]
            logStr .= "Dev" entry.device " Btn" entry.btn " St" entry.state "`n"
        }
    }
    logText.Value := logStr

    ; Visual feedback for button 5 (scroll)
    if (btn == 5) {
        if (state > 0) {
            scrollLevel := Min(scrollLevel + 1, MAX_DISPLAY)
        } else if (state < 0) {
            scrollLevel := Max(scrollLevel - 1, -MAX_DISPLAY)
        }
        UpdateDisplay()
    }
}

UpdateDisplay() {
    global scrollLevel, barDisplay, levelText

    ; Build visual bar
    bars := ""
    if (scrollLevel > 0) {
        Loop scrollLevel {
            bars .= "████████████████`n"
        }
    } else if (scrollLevel < 0) {
        Loop -scrollLevel {
            bars .= "▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓`n"
        }
    }
    barDisplay.Value := bars
    barDisplay.Opt(scrollLevel >= 0 ? "c00FF00" : "cFF0000")
}

UpdateRate() {
    global inputLog, rateText

    now := A_TickCount
    cutoff := now - 1000

    recentCount := 0
    newLog := []
    for entry in inputLog {
        if (entry.time > cutoff) {
            newLog.Push(entry)
            recentCount++
        }
    }
    inputLog := newLog

    rateText.Value := "Rate: " recentCount "/sec"
}

^+Escape::ExitApp
