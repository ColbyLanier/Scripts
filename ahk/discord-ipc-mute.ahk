#Requires AutoHotkey v2.0
#Warn
Persistent

; ========================================
; Discord IPC Voice Mute Hotkey
; ========================================
; Uses Discord's local IPC (named pipe) protocol to toggle
; the client's self-mute state without triggering UI sounds.

global IPC_CONFIG := {
    hotkey: "^!+m",
    hotkeyLabel: "Ctrl+Alt+Shift+M",
    clientId: "1436397537958563862",   ; IPC-mute app Client ID (user-owned)
    pipeMax: 9,
    showNotifications: true,
    logPath: A_ScriptDir . "\discord-ipc-mute.log"
}

global IPC_STATE := {
    pipeHandle: 0,
    pipeName: "",
    isMuted: "",
    lastNonce: "",
    authorized: false
}

IPC_Init()

IPC_Init() {
    Hotkey(IPC_CONFIG.hotkey, IPC_ToggleMute)
    OnExit(IPC_Cleanup)
    IPC_Log("Discord IPC mute hotkey ready (" IPC_CONFIG.hotkeyLabel ")")
}

IPC_ToggleMute(*) {
    if (!IPC_EnsureConnection()) {
        IPC_ShowNotification("Discord IPC unavailable. Is Discord running?", "warn")
        return
    }

    if (!IPC_STATE.authorized) {
        if (!IPC_Authorize()) {
            IPC_ShowNotification("Discord IPC authorization failed.", "error")
            return
        }
    }

    current := IPC_GetMuteState()
    target := (current = "") ? true : !current

    if (IPC_SetMute(target)) {
        IPC_ShowNotification(target ? "Discord self-mute enabled." : "Discord self-mute disabled.", target ? "info" : "info")
    } else {
        IPC_ShowNotification("Failed to toggle Discord mute (see log).", "error")
    }
}

; ========================================
; IPC connection management
; ========================================

IPC_EnsureConnection() {
    if (IPC_STATE.pipeHandle)
        return true

    loop (IPC_CONFIG.pipeMax + 1) {
        idx := A_Index - 1
        name := "\\.\pipe\discord-ipc-" idx
        handle := IPC_OpenPipe(name)
        if (!handle)
            continue
        try {
            IPC_STATE.pipeHandle := handle
            IPC_STATE.pipeName := name
            if (IPC_SendHandshake()) {
                IPC_Log("Connected to " name)
                return true
            }
        } catch Error as err {
            IPC_Log("Handshake failed on " name ": " err.Message, "ERROR")
        }
        IPC_ClosePipe(handle)
        IPC_STATE.pipeHandle := 0
        IPC_STATE.pipeName := ""
    }
    IPC_Log("Unable to find an active Discord IPC pipe", "ERROR")
    return false
}

IPC_SendHandshake() {
    payload := Format('{{"v":1,"client_id":"{0}"}}', IPC_CONFIG.clientId)
    IPC_SendFrame(0, payload)
    resp := IPC_ReadFrame(1500)
    if (!resp)
        throw Error("No handshake ACK received")
    IPC_Log("Handshake ACK opcode=" resp.opcode)
    return true
}

IPC_Cleanup(*) {
    IPC_ClosePipe(IPC_STATE.pipeHandle)
    IPC_STATE.pipeHandle := 0
    IPC_STATE.pipeName := ""
}

IPC_ClosePipe(handle) {
    if (handle) {
        DllCall("CloseHandle", "ptr", handle)
    }
}

; ========================================
; Voice settings helpers
; ========================================

IPC_GetMuteState() {
    resp := IPC_SendCommand("GET_VOICE_SETTINGS", Map("args", Map()))
    if resp && RegExMatch(resp, '"mute"\s*:\s*(true|false)', &match) {
        IPC_STATE.isMuted := (match[1] = "true")
        IPC_Log("Current Discord mute state: " (IPC_STATE.isMuted ? "true" : "false"))
    }
    return IPC_STATE.isMuted
}

IPC_SetMute(state) {
    resp := IPC_SendCommand("SET_VOICE_SETTINGS", Map("args", Map("mute", state)))
    if resp {
        IPC_STATE.isMuted := state
        IPC_Log("Discord mute set to " (state ? "true" : "false"))
        return true
    }
    return false
}
IPC_Authorize() {
    try {
        scopes := ["rpc", "rpc.api", "rpc.voice", "voice"]
        resp := IPC_SendCommand("AUTHORIZE", Map("args", Map(
            "client_id", IPC_CONFIG.clientId,
            "scopes", scopes,
            "prompt", "none"
        )))
        if !resp || !RegExMatch(resp, '"code"\s*:\s*"([^"]+)"', &m)
            throw Error("Missing authorization code")
        code := m[1]
        IPC_Log("Discord authorization code received")

        authResp := IPC_SendCommand("AUTHENTICATE", Map("args", Map("code", code)))
        if !authResp
            throw Error("Authenticate response missing data")
        IPC_STATE.authorized := true
        IPC_Log("Discord IPC authenticated")
        return true
    } catch Error as err {
        IPC_Log("Authorization failed: " err.Message, "ERROR")
        IPC_STATE.authorized := false
        return false
    }
}

IPC_SendCommand(cmd, argsObj := Map()) {
    if !IsObject(argsObj)
        argsObj := Map()
    nonce := IPC_NewNonce()
    payload := Map("cmd", cmd, "args", argsObj, "nonce", nonce)
    payloadJson := IPC_ToJson(payload)
    try {
        IPC_SendFrame(1, payloadJson)
        resp := IPC_WaitForNonce(nonce, 2000)
        if !resp
            throw Error("Timeout waiting for " cmd)
        return resp.payload
    } catch Error as err {
        IPC_Log(cmd " failed: " err.Message, "ERROR")
        IPC_ResetConnection()
        return ""
    }
}

IPC_ToJson(obj) {
    if IsObject(obj) {
        if obj is Array {
            parts := []
            for val in obj
                parts.Push(IPC_ToJson(val))
            return "[" . StrJoin(parts, ",") . "]"
        } else {
            parts := []
            for key, val in obj
                parts.Push('"' key '":' IPC_ToJson(val))
            return "{" . StrJoin(parts, ",") . "}"
        }
    } else if (obj = true)
        return "true"
    else if (obj = false)
        return "false"
    else if obj is Number
        return obj
    return '"' obj '"'
}

StrJoin(arr, sep) {
    out := ""
    for val in arr
        out .= (out = "" ? "" : sep) . val
    return out
}

IPC_ResetConnection() {
    IPC_ClosePipe(IPC_STATE.pipeHandle)
    IPC_STATE.pipeHandle := 0
    IPC_STATE.pipeName := ""
    IPC_STATE.authorized := false
    IPC_Log("Discord IPC connection reset", "WARN")
}

; ========================================
; IPC frame IO
; ========================================

IPC_SendFrame(opcode, payloadText) {
    if (!IPC_STATE.pipeHandle)
        throw Error("IPC pipe not connected")

    payloadBuf := IPC_StringToBuffer(payloadText)
    totalSize := 8 + payloadBuf.Size
    frame := Buffer(totalSize)
    NumPut("int", opcode, frame, 0)
    NumPut("int", payloadBuf.Size, frame, 4)
    DllCall("RtlMoveMemory", "ptr", frame.Ptr + 8, "ptr", payloadBuf.Ptr, "uptr", payloadBuf.Size)

    written := 0
    success := DllCall("WriteFile", "ptr", IPC_STATE.pipeHandle, "ptr", frame.Ptr, "uint", totalSize, "uint*", &written, "ptr", 0)
    if (!success || written != totalSize) {
        err := A_LastError
        IPC_Log("WriteFile failed (opcode " opcode ", err " IPC_Hex(err) ")", "ERROR")
        throw Error("WriteFile failed (opcode " opcode ")")
    }
}

IPC_ReadFrame(timeout := 1000) {
    if (!IPC_STATE.pipeHandle)
        return 0
    if (!IPC_WaitForData(timeout))
        return 0

    header := Buffer(8)
    read := 0
    success := DllCall("ReadFile", "ptr", IPC_STATE.pipeHandle, "ptr", header.Ptr, "uint", 8, "uint*", &read, "ptr", 0)
    if (!success || read != 8)
        throw Error("ReadFile header failed")

    opcode := NumGet(header, 0, "int")
    length := NumGet(header, 4, "int")
    payload := ""
    if (length > 0) {
        buf := Buffer(length)
        success := DllCall("ReadFile", "ptr", IPC_STATE.pipeHandle, "ptr", buf.Ptr, "uint", length, "uint*", &read, "ptr", 0)
        if (!success || read != length)
            throw Error("ReadFile payload failed")
        payload := StrGet(buf, "UTF-8")
    }
    return {opcode: opcode, payload: payload}
}

IPC_WaitForNonce(nonce, timeout := 1500) {
    start := A_TickCount
    while (A_TickCount - start < timeout) {
        frame := IPC_ReadFrame(timeout)
        if (!frame)
            continue
        if (frame.payload = "")
            continue
        if InStr(frame.payload, nonce)
            return frame
    }
    return 0
}

IPC_WaitForData(timeout := 1000) {
    if (!IPC_STATE.pipeHandle)
        return false
    deadline := A_TickCount + timeout
    while (A_TickCount < deadline) {
        avail := 0
        peek := DllCall("PeekNamedPipe", "ptr", IPC_STATE.pipeHandle, "ptr", 0, "uint", 0, "ptr", 0, "uint*", &avail, "ptr", 0)
        if (!peek)
            return false
        if (avail > 0)
            return true
        Sleep 15
    }
    return false
}

IPC_OpenPipe(name) {
    access := 0xC0000000  ; GENERIC_READ | GENERIC_WRITE
    share := 0x3          ; FILE_SHARE_READ | FILE_SHARE_WRITE
    handle := DllCall("CreateFileW"
        , "wstr", name
        , "uint", access
        , "uint", share
        , "ptr", 0
        , "uint", 3          ; OPEN_EXISTING
        , "uint", 0
        , "ptr", 0
        , "ptr")
    if (handle = -1) {
        err := A_LastError
        IPC_Log("Pipe " name " unavailable (err " IPC_Hex(err) ")", "WARN")
        return 0
    }
    IPC_Log("Pipe " name " opened", "INFO")
    return handle
}

IPC_StringToBuffer(text) {
    bytes := StrPut(text, "UTF-8")
    buf := Buffer(bytes - 1)
    StrPut(text, buf, "UTF-8")
    return buf
}

IPC_NewNonce() {
    static counter := 0
    counter++
    guid := Format("{:08x}-{:04x}-{:04x}-{:04x}-{:012x}"
        , A_TickCount
        , Random(0x0, 0xFFFF)
        , Random(0x0, 0xFFFF)
        , Random(0x0, 0xFFFF)
        , counter)
    IPC_STATE.lastNonce := guid
    return guid
}

; ========================================
; UX helpers
; ========================================

IPC_ShowNotification(text, severity := "info") {
    if (!IPC_CONFIG.showNotifications)
        return
    icons := Map("info", "Iconi", "warn", "Icon!", "error", "Iconx")
    opts := icons.Get(severity, "Iconi")
    TrayTip(text, "Discord IPC Mute", opts)
}

IPC_Log(message, level := "INFO") {
    stamp := FormatTime(, "yyyy-MM-dd HH:mm:ss")
    line := stamp " [" level "] " message "`n"
    try {
        FileAppend(line, IPC_CONFIG.logPath, "UTF-8")
    } catch {
    }
}

IPC_Hex(val) {
    return Format("0x{:08X}", val & 0xFFFFFFFF)
}
