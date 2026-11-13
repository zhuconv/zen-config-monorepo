#!/usr/bin/env python3
"""Checks after each Zen update that Tab Autofill still works.

install.sh copies this file to ~/Library/Application Support/zen-tab-autofill/
and runs it from a LaunchAgent (at login, hourly, and when Zen.app changes).
It does nothing until the installed Zen build changes. For a new build it
checks that the loader files are still in place and runs the end-to-end test
through the real loader in a throwaway profile. It shows a macOS notification
only when something is broken, once per Zen build.

    healthcheck.py              # what the LaunchAgent runs
    healthcheck.py --force      # check now, even if this build passed before
    healthcheck.py --notify-test

Standard library only; runs on the system Python (/usr/bin/python3).
"""

import argparse
import configparser
import datetime
import fcntl
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.json"  # written by install.sh: {"app": ..., "profile": ...}
STATE = HERE / "state.json"
TEST = HERE / "test" / "run_test.py"
SCRIPT_NAME = "tab-autofill.uc.js"
LOADER_FILES = ["config.js", "defaults/pref/config-prefs.js"]
TITLE = "Zen Tab Autofill"


def log(message):
    print(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {message}", flush=True)


def notify(message):
    # Pass the text as arguments so no AppleScript quoting is needed.
    subprocess.run(
        [
            "osascript",
            "-e", "on run argv",
            "-e", 'display notification (item 2 of argv) with title (item 1 of argv) sound name "Basso"',
            "-e", "end run",
            TITLE,
            message,
        ],
        check=False,
    )


def zen_build(app):
    ini = configparser.ConfigParser()
    ini.read(app / "Contents" / "Resources" / "application.ini")
    return ini["App"]["Version"], ini["App"]["BuildID"]


def find_problems(app, chrome):
    problems = []
    resources = app / "Contents" / "Resources"
    for name in LOADER_FILES:
        if not (resources / name).exists():
            problems.append(f"Zen.app 里的加载器文件 {name} 不见了")
    for path in (chrome / "utils" / "boot.sys.mjs", chrome / "JS" / SCRIPT_NAME):
        if not path.exists():
            problems.append(f"profile 里的 {path.relative_to(chrome.parent)} 不见了")
    if problems:
        return problems

    # A real breakage fails every time; a one-off timing hiccup does not, so a
    # failed run gets one retry before it counts.
    for attempt in (1, 2):
        result = subprocess.run(
            [sys.executable, str(TEST), "--via-loader", str(chrome)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            timeout=900,
        )
        if result.returncode == 0:
            if attempt > 1:
                log("  passed on the second run")
            return problems
        log(f"  test run {attempt} failed:")
        for line in result.stdout.strip().splitlines():
            if not line.startswith("  PASS"):
                log("    " + line.rstrip())
    problems.append("自动测试没通过，详情见 ~/Library/Logs/zen-tab-autofill.log")
    return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force", action="store_true", help="check even if this Zen build passed before")
    parser.add_argument("--notify-test", action="store_true", help="show a sample notification and exit")
    args = parser.parse_args()

    if args.notify_test:
        notify("这是一条测试通知：以后只有 mod 出问题时才会弹出这样的通知。")
        return 0

    # One check at a time (the LaunchAgent can fire for several reasons at once).
    lock = open(HERE / ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0

    config = json.loads(CONFIG.read_text())
    app = Path(config["app"])
    chrome = Path(config["profile"]) / "chrome"
    state = json.loads(STATE.read_text()) if STATE.exists() else {}

    version, build = zen_build(app)
    if not args.force and state.get("build") == build:
        return 0

    # Zen may be in the middle of applying an update; let it settle.
    ini = app / "Contents" / "Resources" / "application.ini"
    while time.time() - ini.stat().st_mtime < 120:
        time.sleep(15)
    version, build = zen_build(app)

    log(f"checking Zen {version} ({build})")
    problems = find_problems(app, chrome)
    state = {
        "build": build,
        "version": version,
        "checked": datetime.datetime.now().isoformat(timespec="seconds"),
        "ok": not problems,
        "problems": problems,
    }
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")

    if problems:
        for problem in problems:
            log("problem: " + problem)
        notify(f"Zen {version} 更新后 mod 出问题了：{problems[0]}。找 Claude 修一下。")
        return 1
    log(f"Zen {version}: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
