#!/usr/bin/env python3
"""Fleet Dispatch POC — Phase 3: autonomy queue integration, N=10 concurrent servitor dispatch."""

import subprocess, time, json, datetime, os, sys, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = "http://localhost:7777"
LOG_PATH = os.path.expanduser("~/Imperium-ENV/Mars/Logs/fleet_dispatch_log.md")
N = 10

FALLBACK_TASKS = [
    ("python3 --version | Python version is 3.x", "fallback"),
    ("ls ~/Scripts/token-api/ | fleet_dispatch_poc.py is listed", "fallback"),
    ("curl -s localhost:7777/health | response contains a status field", "fallback"),
    ("date | output contains a valid year between 2020 and 2030", "fallback"),
    ("head -3 ~/Scripts/token-api/CLAUDE.md | first lines describe Token-API or port 7777", "fallback"),
]


def pull_tasks():
    """Pull tasks from fleet state autonomy_queue; fall back to Mars/Tasks scan, then hardcoded probes."""
    tasks = []
    try:
        with urllib.request.urlopen(f"{BASE}/api/fleet/state", timeout=5) as resp:
            state = json.loads(resp.read())
        q = state.get("autonomy_queue", {})
        tasks = [(t, "completable") for t in q.get("completable", [])]
        tasks += [(t, "researchable") for t in q.get("researchable", [])]
    except Exception as e:
        print(f"Warning: fleet state unavailable: {e}")
    if len(tasks) < 3:
        mars_tasks = _scan_mars_tasks(N - len(tasks))
        print(f"Queue sparse ({len(tasks)} items) — fabricated {len(mars_tasks)} from Mars/Tasks")
        tasks += mars_tasks
    for fb in FALLBACK_TASKS:
        if len(tasks) >= N:
            break
        tasks.append(fb)
    return tasks[:N]


def _scan_mars_tasks(limit: int) -> list:
    """Scan Mars/Tasks for autonomy: researchable files, fabricate guardsman tasks."""
    import glob as _glob
    tasks_dir = os.path.expanduser("~/Imperium-ENV/Mars/Tasks")
    assertions = [
        "task file has a title and autonomy frontmatter",
        "this task file exists and has actionable content",
        "file describes a concrete deliverable or subtask list",
        "task has clear scope with open tasks or subtasks listed",
        "task file is a valid Markdown note with frontmatter",
        "file references at least one tool, API, or system component",
        "task file has at least 5 lines of content",
        "task is non-empty and plausibly scoped for an agent",
        "file contains at least one section header or bullet list",
        "task describes a software or infrastructure concern",
    ]
    results = []
    phrase_idx = 0
    for path in sorted(_glob.glob(os.path.join(tasks_dir, "*.md"))):
        if len(results) >= limit:
            break
        try:
            with open(path) as f:
                content = f.read(500)
        except OSError:
            continue
        if "autonomy: researchable" not in content:
            continue
        assertion = assertions[phrase_idx % len(assertions)]
        phrase_idx += 1
        results.append((f'cat "{path}" | {assertion}', "researchable"))
    return results


def dispatch_one(task: str, category: str) -> dict:
    """Dispatch one task to MiniMax guardsman, return result dict."""
    t0 = time.time()
    r = subprocess.run(["guardsman", task], capture_output=True, text=True, timeout=120)
    return {
        "task": task, "category": category,
        "output": r.stdout.strip(), "stderr": r.stderr.strip(),
        "returncode": r.returncode, "elapsed_sec": round(time.time() - t0, 2),
    }


def dispatch_parallel(tasks: list) -> tuple:
    """Dispatch N tasks concurrently. Returns (results, wall_clock_sec)."""
    results = [None] * len(tasks)
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=N) as pool:
        futures = {pool.submit(dispatch_one, t, c): i for i, (t, c) in enumerate(tasks)}
        for fut in as_completed(futures):
            results[futures[fut]] = fut.result()
    return results, round(time.time() - t0, 2)


def log_parallel_results(results: list, wall_clock: float, seq_estimate: float) -> float:
    """Append all N results + speedup to fleet dispatch log. Returns speedup ratio."""
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M MST")
    speedup = round(seq_estimate / wall_clock, 2) if wall_clock > 0 else 0
    lines = [
        f"\n## {now} — Fleet Dispatch POC Phase 3 (Queue N={N})\n",
        f"**Wall-clock**: {wall_clock}s | **Sequential estimate**: {seq_estimate}s | **Speedup**: {speedup}x\n",
    ]
    for i, r in enumerate(results, 1):
        out = r["output"] or r["stderr"] or "(no output)"
        lines.append(
            f"\n### Task {i} ({r['category']}) — {r['elapsed_sec']}s\n"
            f"**Task**: {r['task'][:150]}\n"
            f"**RC**: {r['returncode']} | **Output**: `{out[:200]}`\n"
        )
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write("".join(lines))
    print(f"Logged to {LOG_PATH}")
    return speedup


def write_results_to_state(results: list, wall_clock: float) -> None:
    """Append dispatch summary to fleet state notes (read-then-patch to preserve existing)."""
    ok = sum(1 for r in results if r["returncode"] == 0)
    summary = (
        f"Phase 3 dispatch: {len(results)} tasks, {ok} OK, "
        f"wall-clock {wall_clock:.1f}s"
    )
    try:
        with urllib.request.urlopen(f"{BASE}/api/fleet/state", timeout=5) as resp:
            state = json.loads(resp.read())
        notes = state.get("notes", [])
        notes.append(summary)
        payload = json.dumps({"notes": notes}).encode()
        req = urllib.request.Request(
            f"{BASE}/api/fleet/state", data=payload,
            headers={"Content-Type": "application/json"}, method="PATCH",
        )
        urllib.request.urlopen(req)
        print(f"Written to fleet state: {summary}")
    except Exception as e:
        print(f"Warning: could not write to fleet state: {e}")


def run_parallel():
    print(f"Fleet Dispatch POC — Phase 3 (queue N={N})")
    tasks = pull_tasks()
    print(f"Dispatching {len(tasks)} tasks concurrently...")
    for i, (t, cat) in enumerate(tasks, 1):
        print(f"  [{i}] ({cat}) {t[:80]}")
    results, wall_clock = dispatch_parallel(tasks)
    seq_estimate = round(sum(r["elapsed_sec"] for r in results), 2)
    print("\nResults:")
    for i, r in enumerate(results, 1):
        out = (r["output"] or r["stderr"])[:100]
        print(f"  [{i}] RC={r['returncode']} elapsed={r['elapsed_sec']}s — {out}")
    speedup = log_parallel_results(results, wall_clock, seq_estimate)
    write_results_to_state(results, wall_clock)
    print(f"\nWall-clock: {wall_clock}s | Sequential estimate: {seq_estimate}s | Speedup: {speedup}x")
    print(f"SUMMARY: wall={wall_clock}s seq={seq_estimate}s speedup={speedup}x")


def run_single():
    print("Fleet Dispatch POC — Phase 1 (single task)")
    tasks = pull_tasks()
    task, category = tasks[0]
    print(f"Task ({category}): {task[:120]}\nDispatching servitor...")
    r = dispatch_one(task, category)
    out = r["output"] or r["stderr"] or "(no output)"
    print(f"Output: {out[:200]}\nElapsed: {r['elapsed_sec']}s, returncode={r['returncode']}")
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M MST")
    entry = (
        f"\n## {now} — Fleet Dispatch POC\n\n"
        f"**Task category**: {category}\n**Task**: {task[:200]}\n"
        f"**Elapsed**: {r['elapsed_sec']}s\n**Return code**: {r['returncode']}\n"
        f"**Output**:\n```\n{out[:500]}\n```\n"
    )
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(entry)
    print(f"Logged to {LOG_PATH}\nDone.")


if __name__ == "__main__":
    if "--parallel" in sys.argv or "--queue" in sys.argv:
        run_parallel()
    else:
        run_single()
