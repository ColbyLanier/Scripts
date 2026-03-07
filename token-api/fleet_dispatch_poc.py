#!/usr/bin/env python3
"""Fleet Dispatch POC — Phase 5: dispatch validation + retry stubs."""

import subprocess, time, json, datetime, os, sys, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = "http://localhost:7777"
LOG_PATH = os.path.expanduser("~/Imperium-ENV/Mars/Logs/fleet_dispatch_log.md")
N = 10
DAILY_BUDGET_USD = 2.00

FALLBACK_TASKS = [
    ("python3 --version | Python version is 3.x", "fallback"),
    ("ls ~/Scripts/token-api/ | fleet_dispatch_poc.py is listed", "fallback"),
    ("curl -s localhost:7777/health | response contains a status field", "fallback"),
    ("date | output contains a valid year between 2020 and 2030", "fallback"),
    ("head -3 ~/Scripts/token-api/CLAUDE.md | first lines describe Token-API or port 7777", "fallback"),
]


def _normalize_queue_item(item) -> str:
    """Convert a queue item (str or dict) to a guardsman-compatible string."""
    if isinstance(item, str):
        return item
    desc = item.get("description") or item.get("task") or str(item)
    desc = desc.replace('"', "'")[:100]
    return f'echo "{desc}" | output is non-empty and describes a real task or question'


def pull_tasks():
    """Pull tasks from fleet state autonomy_queue; fall back to Mars/Tasks scan, then hardcoded probes."""
    tasks = []
    try:
        with urllib.request.urlopen(f"{BASE}/api/fleet/state", timeout=5) as resp:
            state = json.loads(resp.read())
        q = state.get("autonomy_queue", {})
        tasks = [(_normalize_queue_item(t), "completable") for t in q.get("completable", [])]
        tasks += [(_normalize_queue_item(t), "researchable") for t in q.get("researchable", [])]
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
        f"\n## {now} — Fleet Dispatch POC Phase 5 (Queue N={N})\n",
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
    """Write dispatch_results to fleet state + append summary note."""
    ok = sum(1 for r in results if r["returncode"] == 0)
    summary = (
        f"Phase 5 dispatch: {len(results)} tasks, {ok} OK, "
        f"wall-clock {wall_clock:.1f}s"
    )
    dispatch_results = {
        "last_run": datetime.datetime.utcnow().isoformat() + "Z",
        "count": len(results),
        "ok": ok,
        "wall_clock_sec": wall_clock,
        "results": [
            {"task": r["task"][:120], "category": r["category"],
             "returncode": r["returncode"], "elapsed_sec": r["elapsed_sec"],
             "output": (r["output"] or r["stderr"])[:200]}
            for r in results[:10]
        ],
    }
    try:
        with urllib.request.urlopen(f"{BASE}/api/fleet/state", timeout=5) as resp:
            state = json.loads(resp.read())
        notes = state.get("notes", [])
        notes.append(summary)
        payload = json.dumps({"notes": notes, "dispatch_results": dispatch_results}).encode()
        req = urllib.request.Request(
            f"{BASE}/api/fleet/state", data=payload,
            headers={"Content-Type": "application/json"}, method="PATCH",
        )
        urllib.request.urlopen(req)
        print(f"Written to fleet state: {summary}")
    except Exception as e:
        print(f"Warning: could not write to fleet state: {e}")


def get_daily_spend() -> float:
    """Read today's accumulated dispatch spend from fleet state."""
    try:
        with urllib.request.urlopen(f"{BASE}/api/fleet/state", timeout=5) as resp:
            state = json.loads(resp.read())
        return float(state.get("daily_dispatch_spend_usd", 0.0))
    except Exception:
        return 0.0

def update_daily_spend(delta_usd: float) -> float:
    """Add delta to today's spend in fleet state. Returns new total."""
    current = get_daily_spend()
    new_total = current + delta_usd
    try:
        payload = json.dumps({"daily_dispatch_spend_usd": new_total}).encode()
        req = urllib.request.Request(
            f"{BASE}/api/fleet/state", data=payload,
            headers={"Content-Type": "application/json"}, method="PATCH",
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"Warning: could not update spend: {e}")
    return new_total


def check_backpressure() -> tuple:
    """Returns (should_dispatch, reason) — checks budget, running jobs, queue depth."""
    spend = get_daily_spend()
    if spend >= DAILY_BUDGET_USD:
        return False, f"daily budget exhausted (${spend:.2f} >= ${DAILY_BUDGET_USD:.2f})"
    running = 0
    try:
        with urllib.request.urlopen(f"{BASE}/api/cron/jobs", timeout=5) as resp:
            jobs = json.loads(resp.read())
            if isinstance(jobs, dict):
                jobs = jobs.get("jobs", [])
            running = sum(1 for j in jobs if j.get("is_running"))
        if running >= 3:
            return False, f"fleet saturated ({running} jobs running)"
    except Exception as e:
        print(f"Warning: could not check running jobs: {e}")
    depth = -1
    try:
        with urllib.request.urlopen(f"{BASE}/api/fleet/state", timeout=5) as resp:
            q = json.loads(resp.read()).get("autonomy_queue", {})
        depth = len(q.get("completable", [])) + len(q.get("researchable", []))
        if depth == 0:
            return False, "autonomy queue empty"
    except Exception as e:
        print(f"Warning: could not check queue depth: {e}")
    return True, f"ok (${spend:.2f} spent, {running} running, {depth} queued)"


def write_retry_queue(failed: list) -> None:
    """Write failed tasks to dispatch_retry_queue in fleet state (stub — not re-dispatched yet)."""
    if not failed:
        return
    try:
        payload = json.dumps({"dispatch_retry_queue": failed}).encode()
        req = urllib.request.Request(
            f"{BASE}/api/fleet/state", data=payload,
            headers={"Content-Type": "application/json"}, method="PATCH",
        )
        urllib.request.urlopen(req, timeout=5)
        print(f"[retry] {len(failed)} failed tasks queued for retry")
    except Exception as e:
        print(f"Warning: could not write retry queue: {e}")


def run_parallel():
    print(f"Fleet Dispatch POC — Phase 5 (queue N={N})")
    should_dispatch, reason = check_backpressure()
    print(f"[backpressure] {reason}")
    if not should_dispatch:
        print("[dispatch] Skipping — backpressure active.")
        sys.exit(0)

    tasks = pull_tasks()
    if not tasks:
        print("[dispatch] Queue empty, nothing to dispatch.")
        sys.exit(0)

    n = min(len(tasks), N)
    print(f"[dispatch] Dispatching {n} tasks...")
    for i, (t, cat) in enumerate(tasks[:n], 1):
        print(f"  [{i}] ({cat}) {t[:80]}")
    results, wall_clock = dispatch_parallel(tasks[:n])
    seq_estimate = round(sum(r["elapsed_sec"] for r in results), 2)
    print("\nResults:")
    for i, r in enumerate(results, 1):
        out = (r["output"] or r["stderr"])[:100]
        print(f"  [{i}] RC={r['returncode']} elapsed={r['elapsed_sec']}s — {out}")
    speedup = log_parallel_results(results, wall_clock, seq_estimate)
    write_results_to_state(results, wall_clock)
    failed = [r for r in results if r["returncode"] != 0]
    write_retry_queue(failed)
    new_total = update_daily_spend(0.0)  # guardsman is free
    print(f"[cost] Daily spend: ${new_total:.2f} / ${DAILY_BUDGET_USD:.2f}")
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
