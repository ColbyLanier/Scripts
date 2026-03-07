#!/usr/bin/env python3
"""
Fleet Dispatch POC — Phase 1: single-threaded servitor dispatch.
Pulls one task from autonomy_queue, dispatches to MiniMax (guardsman),
logs result + timing to Mars/Logs/fleet_dispatch_log.md.
"""

import subprocess
import time
import json
import datetime
import os
import urllib.request
import urllib.error

BASE = "http://localhost:7777"
LOG_PATH = os.path.expanduser("~/Imperium-ENV/Mars/Logs/fleet_dispatch_log.md")

# Fallback task: read CLAUDE.md summary and assert it describes Token-API
FALLBACK_TASK = (
    "cat ~/Scripts/token-api/CLAUDE.md | head -30 | "
    "summarize the purpose of the Token-API system in one sentence"
)
FALLBACK_CATEGORY = "fallback"


def pull_task():
    """Pull one task from autonomy queue, or return fallback."""
    try:
        with urllib.request.urlopen(f"{BASE}/api/fleet/state", timeout=5) as resp:
            state = json.loads(resp.read())
        queue = state.get("autonomy_queue", {})
        completable = queue.get("completable", [])
        researchable = queue.get("researchable", [])
        if completable:
            return completable[0], "completable"
        if researchable:
            return researchable[0], "researchable"
    except Exception as e:
        print(f"Warning: could not reach fleet state: {e}")
    return FALLBACK_TASK, FALLBACK_CATEGORY


def dispatch_servitor(task: str) -> dict:
    """Dispatch task to MiniMax guardsman, return result."""
    t0 = time.time()
    result = subprocess.run(
        ["guardsman", task],
        capture_output=True, text=True, timeout=60
    )
    elapsed = time.time() - t0
    return {
        "output": result.stdout.strip(),
        "stderr": result.stderr.strip(),
        "returncode": result.returncode,
        "elapsed_sec": round(elapsed, 2),
    }


def log_result(task: str, category: str, result: dict):
    """Append result to fleet dispatch log."""
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M MST")
    output = result["output"] or result["stderr"] or "(no output)"
    entry = (
        f"\n## {now} — Fleet Dispatch POC\n\n"
        f"**Task category**: {category}\n"
        f"**Task**: {task[:200]}\n"
        f"**Elapsed**: {result['elapsed_sec']}s\n"
        f"**Return code**: {result['returncode']}\n"
        f"**Output**:\n```\n{output[:500]}\n```\n"
    )
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(entry)
    print(f"Logged to {LOG_PATH}")


def main():
    print("Fleet Dispatch POC — Phase 1")
    task, category = pull_task()
    print(f"Task ({category}): {task[:120]}")
    print("Dispatching servitor...")
    result = dispatch_servitor(task)
    print(f"Output: {(result['output'] or result['stderr'])[:200]}")
    print(f"Elapsed: {result['elapsed_sec']}s, returncode={result['returncode']}")
    log_result(task, category, result)
    print("Done.")


if __name__ == "__main__":
    main()
