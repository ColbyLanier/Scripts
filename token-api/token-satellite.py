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

# ============ TTS State ============
tts_current_process: Optional[subprocess.Popen] = None
tts_skip_event = threading.Event()

# Full path — bare "cmd.exe" isn't on PATH under systemd
CMD_EXE = "/mnt/c/Windows/System32/cmd.exe"

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


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "token-satellite",
        "timestamp": datetime.now().isoformat(),
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
    global tts_current_process

    if tts_current_process is not None and tts_current_process.poll() is None:
        raise HTTPException(status_code=409, detail="Already speaking")

    tts_skip_event.clear()

    escaped = (request.message
               .replace("\\", "\\\\")
               .replace("'", "''")
               .replace("$", "\\$")
               .replace("`", "\\`"))

    ps_script = f"""
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$synth.SelectVoice('{request.voice}')
$synth.Rate = [int]{request.rate}
$synth.Speak('{escaped}')
"""

    logger.info(f"TTS: Speaking {len(request.message)} chars with {request.voice} (rate={request.rate})")

    try:
        process = subprocess.Popen(
            ["powershell.exe"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        tts_current_process = process
        process.stdin.write(ps_script.encode())
        process.stdin.close()

        # Poll loop: check for completion or skip every 100ms
        while process.poll() is None:
            if tts_skip_event.is_set():
                logger.info("TTS: Skip event received, killing PowerShell")
                process.kill()
                process.wait(timeout=3)
                tts_current_process = None
                return {"success": True, "skipped": True}
            time.sleep(0.1)

        tts_current_process = None
        success = process.returncode == 0
        if not success:
            stderr = process.stderr.read().decode().strip()
            logger.warning(f"TTS: PowerShell exited with code {process.returncode}: {stderr}")
        else:
            logger.info("TTS: Speech completed")

        return {"success": success, "skipped": False}

    except Exception as e:
        tts_current_process = None
        logger.error(f"TTS: Error: {e}")
        return {"success": False, "skipped": False, "error": str(e)}


@app.post("/tts/skip")
async def tts_skip():
    """Skip current TTS playback by setting skip event and killing PowerShell."""
    global tts_current_process

    if tts_current_process is None or tts_current_process.poll() is not None:
        return {"success": True, "was_speaking": False}

    tts_skip_event.set()

    # Also kill directly in case poll loop hasn't caught it yet
    try:
        tts_current_process.kill()
        tts_current_process.wait(timeout=3)
    except Exception as e:
        logger.warning(f"TTS skip: Error killing process: {e}")

    tts_current_process = None
    logger.info("TTS: Skipped via /tts/skip")
    return {"success": True, "was_speaking": True}


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

    # 3. Schedule exit after response is sent (systemd Restart=always brings us back)
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
