#!/usr/bin/env python3
"""Check the running CPU limits service and its Omarchy bar panel.

Run after installing changes to backend/cpu-limits.py or the
CPU Limits plugin, and after `omarchy update`: it confirms that the
service publishes a fresh, applied status in the schema the panel reads, that
the panel answers over IPC, and that the shell logged no QML warning or error
for the plugin since it last started.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

STATUS = Path("/run/workstation-cpu-limits/status.json")
SCHEMA = 1
PLUGIN = "io.github.ltecosta.cpu-limits"


IGNORED = ("Handler was registered but will not be used", "outside of the config folder")


def run(*command):
    return subprocess.run(command, capture_output=True, text=True, timeout=30)


def plugin_log_problems(lines):
    """Shell log lines that report a QML warning or error from this plugin."""
    problems = []
    for line in lines:
        if f"plugins/{PLUGIN}/" not in line or not any(word in line for word in ("WARN", "ERROR", "Error")):
            continue
        if any(text in line for text in IGNORED):
            continue
        problems.append("shell log: " + line.split("m: ", 1)[-1].strip())
    return problems


def main() -> int:
    problems, notes = [], []
    state = run("systemctl", "is-active", "cpu-frequency-limits.service").stdout.strip()
    if state != "active":
        problems.append(f"cpu-frequency-limits.service is {state or 'unknown'}")
    try:
        status = json.loads(STATUS.read_text())
        age = time.time() - status["updated"]
        if status.get("schema") != SCHEMA:
            problems.append(f"status schema {status.get('schema')!r}, panel expects {SCHEMA}")
        if age > status.get("limits", {}).get("stale_after_s", 10):
            problems.append(f"status is {age:.0f} s old")
        if not status.get("applied"):
            problems.append(f"service reports: {status.get('error')}")
        notes += [f"warning: {w}" for w in status.get("warnings", [])]
        notes.append(f"profile {status.get('profile')}, revision {status.get('applied_revision')}")
    except (OSError, ValueError, KeyError) as error:
        problems.append(f"status unreadable: {error}")

    panel = run("omarchy-shell", PLUGIN, "state")
    try:
        state = json.loads(panel.stdout)
        notes.append(f"panel {state}")
        if panel.returncode or not isinstance(state, dict) or state.get("kind") == "error" or state.get("error"):
            problems.append(f"panel reports: {state.get('error') if isinstance(state, dict) else state}")
    except ValueError:
        problems.append(f"panel IPC did not answer: {(panel.stdout + panel.stderr).strip()}")

    pid = run("pgrep", "-o", "-x", "quickshell").stdout.strip()
    elapsed = run("ps", "-o", "etimes=", "-p", pid).stdout.strip() if pid else ""
    if not elapsed.isdigit():
        problems.append("Omarchy shell process not found; shell log not checked")
    else:
        journal = run("journalctl", "--user", "--no-pager", "-o", "cat", "--since", f"-{int(elapsed) + 1}s")
        if journal.returncode:
            problems.append(f"shell log unreadable: {journal.stderr.strip()}")
        problems += plugin_log_problems(journal.stdout.splitlines())

    for line in notes:
        print(line)
    for line in problems:
        print(f"problem: {line}", file=sys.stderr)
    print("CPU limits check " + ("failed" if problems else "passed"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
