"""
Token Satellite: Windows companion server for token-api.

Stateless FastAPI app running on WSL (port 7777). Executes Windows-side
commands on behalf of token-api — same pattern as the phone's MacroDroid
HTTP server.

Endpoints:
    GET  /health      — heartbeat
    POST /enforce     — close a Windows process (brave, minecraft)
    GET  /processes   — list distraction-relevant processes
    POST /tts/speak   — speak text via Windows SAPI (blocking)
    POST /tts/skip    — skip current TTS playback
    POST /restart     — git pull + restart
"""

import json
import os
import subprocess
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
logger = logging.getLogger("token_satellite")

app = FastAPI(title="Token Satellite", version="1.0.0")

# Full paths — bare exes aren't on PATH under systemd
CMD_EXE = "/mnt/c/Windows/System32/cmd.exe"
POWERSHELL_EXE = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"

# PowerShell script for persistent TTS engine.
# Uses SpeakAsync so the main loop stays responsive to skip/poll commands.
# Protocol: JSON commands on stdin, line responses on stdout.
#   {"action":"speak","voice":"...","rate":N,"message":"..."} → "OK"
#   {"action":"poll"}                                          → "Speaking" | "Ready"
#   {"action":"skip"}                                          → "OK"
#   "quit"                                                     → exits
TTS_ENGINE_PS = r"""
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
[Console]::WriteLine("READY")
[Console]::Out.Flush()

while ($true) {
    $line = [Console]::ReadLine()
    if ($line -eq $null -or $line -eq "quit") { break }
    try { $cmd = $line | ConvertFrom-Json } catch { continue }

    switch ($cmd.action) {
        "speak" {
            try { $synth.SelectVoice($cmd.voice) } catch {
                [Console]::WriteLine("VOICE_ERR")
                [Console]::Out.Flush()
                continue
            }
            $synth.Rate = [int]$cmd.rate
            $synth.SpeakAsync($cmd.message) | Out-Null
            [Console]::WriteLine("OK")
            [Console]::Out.Flush()
        }
        "poll" {
            [Console]::WriteLine($synth.State.ToString())
            [Console]::Out.Flush()
        }
        "skip" {
            $synth.SpeakAsyncCancelAll()
            [Console]::WriteLine("OK")
            [Console]::Out.Flush()
        }
    }
}
$synth.Dispose()
"""

# Write PS script to Windows-accessible path so PowerShell can run it
TTS_SCRIPT_WSL_PATH = "/mnt/c/temp/token_tts_engine.ps1"
TTS_SCRIPT_WIN_PATH = r"C:\temp\token_tts_engine.ps1"


class TTSEngine:
    """Persistent PowerShell process for Windows SAPI TTS.

    Eliminates ~3-4s cold-start per message by keeping the synthesizer loaded.
    Thread-safe: speak() runs in threadpool, skip() can interrupt from another thread.
    """

    def __init__(self):
        self._process: Optional[subprocess.Popen] = None
        self._io_lock = threading.Lock()
        self._speaking = False
        self._was_skipped = False

    def _write_script(self):
        """Write the PS script to a Windows-accessible path."""
        os.makedirs("/mnt/c/temp", exist_ok=True)
        with open(TTS_SCRIPT_WSL_PATH, "w") as f:
            f.write(TTS_ENGINE_PS)

    def start(self):
        """Start the persistent PowerShell process."""
        self._write_script()
        self._process = subprocess.Popen(
            [POWERSHELL_EXE, "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", TTS_SCRIPT_WIN_PATH],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        # Wait for READY signal (timeout 15s for cold start)
        line = self._readline_raw(timeout=15)
        if line != "READY":
            logger.error(f"TTS engine: Expected READY, got: {line}")
            self._kill()
            raise RuntimeError(f"TTS engine failed to start: {line}")
        logger.info("TTS engine: Persistent PowerShell started")

    def _readline_raw(self, timeout=5):
        """Read one line from stdout with timeout. No lock needed (called during init)."""
        import select
        fileno = self._process.stdout.fileno()
        ready, _, _ = select.select([fileno], [], [], timeout)
        if ready:
            return self._process.stdout.readline().strip()
        return None

    def _send(self, cmd):
        """Send JSON command to PS stdin. Caller must hold _io_lock."""
        self._process.stdin.write(json.dumps(cmd) + "\n")
        self._process.stdin.flush()

    def _readline(self):
        """Read one line from PS stdout. Caller must hold _io_lock."""
        return self._process.stdout.readline().strip()

    def _kill(self):
        """Kill the PS process."""
        if self._process and self._process.poll() is None:
            try:
                self._process.kill()
                self._process.wait(timeout=3)
            except Exception:
                pass
        self._process = None

    def _ensure_running(self):
        """Start or restart the PS process if needed."""
        if self._process is None or self._process.poll() is not None:
            if self._process is not None:
                logger.warning("TTS engine: Process died, restarting")
            self.start()

    @property
    def is_speaking(self):
        return self._speaking

    def speak(self, message: str, voice: str, rate: int = 0) -> dict:
        """Speak text. Blocks until done or skipped. Returns result dict."""
        self._ensure_running()
        self._speaking = True
        self._was_skipped = False

        # Send speak command
        with self._io_lock:
            self._send({"action": "speak", "voice": voice, "rate": rate, "message": message})
            resp = self._readline()

        if resp == "VOICE_ERR":
            self._speaking = False
            return {"success": False, "error": f"Voice not found: {voice}"}
        if resp != "OK":
            self._speaking = False
            return {"success": False, "error": f"Unexpected response: {resp}"}

        # Poll for completion — release lock between polls so skip() can send
        while True:
            time.sleep(0.1)
            with self._io_lock:
                self._send({"action": "poll"})
                state = self._readline()
            if state == "Ready":
                break
            if state is None or state == "":
                # Process died
                self._speaking = False
                self._process = None
                return {"success": False, "error": "TTS engine process died"}

        self._speaking = False
        return {"success": True, "skipped": self._was_skipped}

    def skip(self) -> bool:
        """Cancel current speech. Returns True if was speaking."""
        if not self._speaking or self._process is None:
            return False
        with self._io_lock:
            self._send({"action": "skip"})
            resp = self._readline()
        self._was_skipped = True
        return True

    def shutdown(self):
        """Gracefully stop the PS process."""
        if self._process and self._process.poll() is None:
            try:
                with self._io_lock:
                    self._process.stdin.write("quit\n")
                    self._process.stdin.flush()
                self._process.wait(timeout=5)
            except Exception:
                self._kill()
        logger.info("TTS engine: Shutdown")


# Global TTS engine instance
tts_engine = TTSEngine()

# Mapping of app aliases to Windows executables
APP_TARGETS = {
    "brave": "brave.exe",
    "minecraft": "javaw.exe",
    "spotify": "Spotify.exe",
}

# Processes that must NEVER be enforced
PROTECTED_PROCESSES = {"vivaldi.exe"}


class EnforceRequest(BaseModel):
    app: str
    action: str = "close"


class TTSSpeakRequest(BaseModel):
    message: str
    voice: str = "Microsoft David"
    rate: int = 0


@app.on_event("startup")
async def startup_event():
    """Warm up the TTS engine on server start."""
    try:
        tts_engine.start()
    except Exception as e:
        logger.warning(f"TTS engine warm-up failed (will retry on first speak): {e}")


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "token-satellite",
        "timestamp": datetime.now().isoformat(),
        "tts_engine": "running" if tts_engine._process and tts_engine._process.poll() is None else "stopped",
    }


@app.post("/enforce")
async def enforce(request: EnforceRequest):
    """Close a Windows process by app alias."""
    app_name = request.app.lower()
    action = request.action.lower()

    if action != "close":
        raise HTTPException(status_code=400, detail=f"Unsupported action: {action}")

    if app_name not in APP_TARGETS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown app '{app_name}'. Valid: {list(APP_TARGETS.keys())}",
        )

    exe = APP_TARGETS[app_name]

    if exe.lower() in {p.lower() for p in PROTECTED_PROCESSES}:
        logger.warning(f"BLOCKED: Refusing to close protected process {exe}")
        raise HTTPException(status_code=403, detail=f"{exe} is protected")

    logger.info(f"ENFORCE: Closing {exe} (app={app_name})")

    try:
        result = subprocess.run(
            [CMD_EXE, "/c", "taskkill", "/IM", exe, "/F"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        success = result.returncode == 0
        logger.info(f"ENFORCE: taskkill {exe} -> rc={result.returncode} stdout={result.stdout.strip()}")
        return {
            "success": success,
            "app": app_name,
            "exe": exe,
            "returncode": result.returncode,
            "output": result.stdout.strip() or result.stderr.strip(),
        }
    except subprocess.TimeoutExpired:
        logger.error(f"ENFORCE: taskkill {exe} timed out")
        return {"success": False, "app": app_name, "exe": exe, "error": "timeout"}
    except Exception as e:
        logger.error(f"ENFORCE: taskkill {exe} failed: {e}")
        return {"success": False, "app": app_name, "exe": exe, "error": str(e)}


@app.get("/processes")
async def list_processes():
    """List running distraction-relevant processes (for debugging)."""
    all_targets = set(APP_TARGETS.values())
    running = []

    try:
        result = subprocess.run(
            [CMD_EXE, "/c", "tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        for line in result.stdout.strip().split("\n"):
            line = line.strip().strip('"')
            if not line:
                continue
            # CSV format: "name.exe","PID","Session Name","Session#","Mem Usage"
            parts = line.split('","')
            if parts:
                proc_name = parts[0].strip('"')
                if proc_name.lower() in {t.lower() for t in all_targets}:
                    running.append(proc_name)
    except Exception as e:
        logger.error(f"Failed to list processes: {e}")
        return {"error": str(e), "running": []}

    return {
        "running": running,
        "monitored": list(APP_TARGETS.keys()),
        "protected": list(PROTECTED_PROCESSES),
    }


@app.post("/tts/speak")
def tts_speak(request: TTSSpeakRequest):
    """Speak text using Windows SAPI. Blocks until speech completes or is skipped."""
    if tts_engine.is_speaking:
        raise HTTPException(status_code=409, detail="Already speaking")

    logger.info(f"TTS: Speaking {len(request.message)} chars with {request.voice} (rate={request.rate})")
    result = tts_engine.speak(request.message, request.voice, request.rate)

    if result.get("skipped"):
        logger.info("TTS: Speech skipped")
    elif result.get("success"):
        logger.info("TTS: Speech completed")
    else:
        logger.warning(f"TTS: Failed: {result.get('error')}")

    return result


@app.post("/tts/skip")
async def tts_skip():
    """Skip current TTS playback."""
    was_speaking = tts_engine.skip()
    logger.info(f"TTS: Skip requested (was_speaking={was_speaking})")
    return {"success": True, "was_speaking": was_speaking}


@app.post("/restart")
async def restart_satellite(pull: bool = True):
    """Git pull, write TUI signals, then exit for systemd restart."""
    result = {"pull": None, "tui_signals": False, "restarting": True}

    # 1. Git pull
    if pull:
        try:
            proc = subprocess.run(
                ["git", "-C", str(Path.home() / "Scripts"), "pull", "--ff-only"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            result["pull"] = {
                "success": proc.returncode == 0,
                "output": proc.stdout.strip() or proc.stderr.strip(),
            }
        except Exception as e:
            result["pull"] = {"success": False, "error": str(e)}

    # 2. Write TUI restart signals
    signal_dir = Path.home() / ".claude"
    signal_dir.mkdir(parents=True, exist_ok=True)
    signal_data = json.dumps({"reason": "token-restart", "timestamp": datetime.now().isoformat()})
    for suffix in ("desktop", "mobile"):
        (signal_dir / f"tui-restart-{suffix}.signal").write_text(signal_data)
    result["tui_signals"] = True

    # 3. Shutdown TTS engine cleanly
    tts_engine.shutdown()

    # 4. Schedule exit after response is sent (systemd Restart=always brings us back)
    def delayed_exit():
        time.sleep(0.5)
        logger.info("RESTART: Exiting for systemd restart")
        os._exit(0)

    threading.Thread(target=delayed_exit, daemon=True).start()

    logger.info(f"RESTART: pull={result['pull']}, signals written, exiting in 0.5s")
    return result


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=7777)
