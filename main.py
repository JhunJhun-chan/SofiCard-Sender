"""
Discord bulk card sender with humanised pacing.

Reads codes from data/remaining.txt (or prompts for paste), sends
`sv <code>` into the focused Discord window, and tracks progress in
data/sent.txt, data/remaining.txt, and data/log.txt.

Hotkeys:
    F8  - Pause / Resume
    F9  - Emergency stop

Config (config.json, all keys optional - defaults shown):
    {
        "startup_delay":          5.0,

        "pre_paste_min":          0.4,
        "pre_paste_max":          1.2,
        "post_paste_min":         0.3,
        "post_paste_max":         0.9,

        "channel_cooldown":      10.0,
        "cooldown_jitter_min":    2.0,
        "cooldown_jitter_max":    6.0,

        "think_chance":           0.25,
        "think_min":              3.0,
        "think_max":             15.0,

        "break_every_min":        8,
        "break_every_max":       15,
        "break_duration_min":    30.0,
        "break_duration_max":   120.0,

        "review_chance":          0.15,
        "review_min":             1.5,
        "review_max":             4.0,

        "log_max_lines":        500,
        "clear_log_on_exit":    true,

        "require_discord_focus":  true,
        "restore_clipboard":      true,
        "code_pattern":           "[a-zA-Z0-9]{5,8}"
    }
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import sys
import time
from pathlib import Path
from typing import Optional

import pyautogui
import pyperclip

try:
    import keyboard
except ImportError:
    keyboard = None  # type: ignore

try:
    import pygetwindow as gw
except ImportError:
    gw = None  # type: ignore


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DATA_FOLDER = Path("data")
SENT_FILE = DATA_FOLDER / "sent.txt"
REMAINING_FILE = DATA_FOLDER / "remaining.txt"
LOG_FILE = DATA_FOLDER / "log.txt"
LOCK_FILE = DATA_FOLDER / ".lock"

DEFAULT_CONFIG = {
    # Startup
    "startup_delay": 5.0,

    # Per-card timing (paste pipeline)
    "pre_paste_min": 0.4,
    "pre_paste_max": 1.2,
    "post_paste_min": 0.3,
    "post_paste_max": 0.9,

    # Channel cooldown floor + jitter
    "channel_cooldown": 10.0,
    "cooldown_jitter_min": 2.0,
    "cooldown_jitter_max": 6.0,

    # Occasional "think" pause
    "think_chance": 0.25,
    "think_min": 3.0,
    "think_max": 15.0,

    # Long breaks
    "break_every_min": 8,
    "break_every_max": 15,
    "break_duration_min": 30.0,
    "break_duration_max": 120.0,

    # Pre-Enter "review" hesitation
    "review_chance": 0.15,
    "review_min": 1.5,
    "review_max": 4.0,

    # Log behaviour
    "log_max_lines": 500,
    "clear_log_on_exit": True,

    # Behaviour
    "require_discord_focus": True,
    "restore_clipboard": True,
    "code_pattern": "[a-zA-Z0-9]{5,8}",
}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log = logging.getLogger("sender")


def setup_logging() -> None:
    DATA_FOLDER.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def shutdown_logging_and_clear() -> None:
    """
    Delete log.txt on clean exit. Only safe because we no longer
    hold a FileHandler open on it.
    """
    try:
        LOG_FILE.unlink(missing_ok=True)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------
def atomic_write(path: Path, text: str) -> None:
    """Write text to path atomically (tmp file + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def load_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def append_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def append_line_rotated(path: Path, line: str, max_lines: int) -> None:
    """
    Append a line to a log file, trimming from the top if the file
    exceeds max_lines. max_lines <= 0 disables rotation.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    if max_lines <= 0:
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        return

    existing: list[str] = []
    if path.exists():
        existing = path.read_text(encoding="utf-8").splitlines()

    existing.append(line)

    if len(existing) > max_lines:
        existing = existing[-max_lines:]

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(existing) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def load_config(path: Path) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if path.exists():
        try:
            user_cfg = json.loads(path.read_text(encoding="utf-8"))
            cfg.update(user_cfg)
        except json.JSONDecodeError as e:
            log.warning("config.json is invalid JSON (%s); using defaults", e)
    return cfg


# ---------------------------------------------------------------------------
# Code parsing
# ---------------------------------------------------------------------------
def parse_codes(raw: str, sent: set[str], pattern: str) -> list[str]:
    """Extract valid-looking codes from pasted text, dedup, drop already-sent."""
    regex = re.compile(pattern)
    tokens = re.split(r"[,\s]+", raw)
    seen: dict[str, None] = {}
    for token in tokens:
        token = token.strip()
        if token and regex.fullmatch(token) and token not in sent:
            seen[token] = None
    return list(seen)


# ---------------------------------------------------------------------------
# Timing helpers
# ---------------------------------------------------------------------------
def rand_delay(lo: float, hi: float, label: str) -> float:
    if hi < lo:
        lo, hi = hi, lo
    d = random.uniform(lo, hi)
    log.info("Waiting %.2fs %s", d, label)
    time.sleep(d)
    return d


def humanized_wait(cfg: dict, cards_since_break: int) -> int:
    """
    Sleep for a human-looking amount of time between cards.
    Returns the updated cards_since_break counter.
    """
    # 1. Base cooldown + jitter (hard floor)
    base = cfg["channel_cooldown"]
    jitter = random.uniform(cfg["cooldown_jitter_min"], cfg["cooldown_jitter_max"])
    total = base + jitter

    # 2. Occasional short "think" pause
    if random.random() < cfg["think_chance"]:
        think = random.uniform(cfg["think_min"], cfg["think_max"])
        log.info("Humanisation: thinking for %.1fs", think)
        total += think

    # 3. Occasional long break
    break_threshold = random.randint(cfg["break_every_min"], cfg["break_every_max"])
    if cards_since_break >= break_threshold:
        brk = random.uniform(cfg["break_duration_min"], cfg["break_duration_max"])
        log.info(
            "Humanisation: taking a %.0fs break after %d cards",
            brk, cards_since_break,
        )
        total += brk
        cards_since_break = 0

    log.info("Waiting %.2fs before next card (cooldown %.0fs)", total, base)
    time.sleep(total)
    return cards_since_break


def human_press_enter(cfg: dict) -> None:
    """Press Enter (no review pause)."""
    pyautogui.press("enter")


# ---------------------------------------------------------------------------
# Window focus
# ---------------------------------------------------------------------------
def discord_focused() -> bool:
    if gw is None:
        return True  # can't check -> assume ok
    try:
        title = gw.getActiveWindowTitle() or ""
    except Exception:
        return False
    return "discord" in title.lower()


# ---------------------------------------------------------------------------
# Lockfile
# ---------------------------------------------------------------------------
class InstanceLock:
    def __init__(self, path: Path):
        self.path = path

    def __enter__(self):
        DATA_FOLDER.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                pid = int(self.path.read_text().strip())
                if _pid_alive(pid):
                    raise SystemExit(
                        f"Another instance is already running (PID {pid}). "
                        f"Remove {self.path} if this is stale."
                    )
            except ValueError:
                pass
        self.path.write_text(str(os.getpid()))
        return self

    def __exit__(self, *exc):
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        else:
            os.kill(pid, 0)
            return True
    except (OSError, PermissionError):
        return False


# ---------------------------------------------------------------------------
# Pause / Stop control
# ---------------------------------------------------------------------------
class Control:
    def __init__(self):
        self.paused = False
        self.running = True
        self._registered = False

    def register(self) -> None:
        if keyboard is None:
            log.warning("keyboard module not available; F8/F9 hotkeys disabled")
            return
        try:
            keyboard.add_hotkey("f8", self._toggle_pause)
            keyboard.add_hotkey("f9", self._stop)
            self._registered = True
        except Exception as e:
            log.warning("Could not register hotkeys (%s); F8/F9 disabled", e)

    def unregister(self) -> None:
        if not self._registered:
            return
        try:
            keyboard.remove_hotkey("f8")
            keyboard.remove_hotkey("f9")
        except Exception:
            pass

    def _toggle_pause(self) -> None:
        self.paused = not self.paused
        log.info("=== %s ===", "PAUSED" if self.paused else "RESUMED")

    def _stop(self) -> None:
        self.running = False
        log.info("=== STOP REQUESTED ===")


# ---------------------------------------------------------------------------
# Input collection
# ---------------------------------------------------------------------------
def collect_codes_if_needed(cfg: dict, sent: set[str]) -> list[str]:
    """If remaining.txt is empty, prompt the user to paste and parse."""
    remaining = load_lines(REMAINING_FILE)
    if remaining:
        return remaining

    print("\nPaste your entire Sofi page below.")
    print("Press Enter on an empty line when finished.\n")

    lines: list[str] = []
    try:
        while True:
            line = input()
            if line == "":
                break
            lines.append(line)
    except (EOFError, KeyboardInterrupt):
        print()

    raw = "\n".join(lines)
    codes = parse_codes(raw, sent, cfg["code_pattern"])

    if codes:
        atomic_write(REMAINING_FILE, "\n".join(codes))
    return codes


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------
def send_card(code: str, cfg: dict) -> None:
    """Paste `sv <code>` into the focused window and press Enter."""
    command = f"sv {code}"

    prev_clipboard: Optional[str] = None
    if cfg["restore_clipboard"]:
        try:
            prev_clipboard = pyperclip.paste()
        except Exception:
            prev_clipboard = None

    pyperclip.copy(command)

    rand_delay(cfg["pre_paste_min"], cfg["pre_paste_max"], "before pasting")
    pyautogui.hotkey("ctrl", "v")

    rand_delay(cfg["post_paste_min"], cfg["post_paste_max"], "before pressing Enter")
    human_press_enter(cfg)

    if prev_clipboard is not None:
        try:
            pyperclip.copy(prev_clipboard)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="Discord bulk card sender.")
    parser.add_argument("--config", default="config.json",
                        help="path to config file")
    parser.add_argument("--dry-run", action="store_true",
                        help="parse and print codes without sending")
    args = parser.parse_args()

    setup_logging()
    cfg = load_config(Path(args.config))

    pyautogui.PAUSE = 0.05
    pyautogui.FAILSAFE = True  # move mouse to a corner to abort

    clean_exit = False
    exit_code = 0

    try:
        with InstanceLock(LOCK_FILE):
            sent_codes = set(load_lines(SENT_FILE))
            remaining = collect_codes_if_needed(cfg, sent_codes)

            if not remaining:
                log.info("No cards to send.")
                clean_exit = True
                return 0

            total = len(remaining)
            completed = 0
            cards_since_break = 0
            start = time.monotonic()

            log.info("Loaded %d cards.", total)

            if args.dry_run:
                for code in remaining:
                    log.info("[dry-run] would send: sv %s", code)
                clean_exit = True
                return 0

            print("Switch to the Discord channel now.")
            print(f"Starting in {cfg['startup_delay']:.1f}s...\n")
            time.sleep(cfg["startup_delay"])

            control = Control()
            control.register()

            # Persist the (possibly reduced) remaining list up front.
            atomic_write(REMAINING_FILE, "\n".join(remaining))

            try:
                while control.running:
                    if control.paused:
                        time.sleep(0.3)
                        continue

                    if not remaining:
                        break

                    if cfg["require_discord_focus"] and not discord_focused():
                        log.warning("Discord not focused; waiting 2s...")
                        time.sleep(2.0)
                        continue

                    code = remaining[0]

                    # Log intent first (crash-safe, rotated).
                    append_line_rotated(
                        LOG_FILE,
                        f"{time.strftime('%Y-%m-%d %H:%M:%S')} -> sv {code}",
                        cfg["log_max_lines"],
                    )

                    send_card(code, cfg)

                    # Mark sent. sent.txt is NEVER rotated/cleared.
                    append_line(SENT_FILE, code)
                    completed += 1

                    # Persist remaining.
                    remaining.pop(0)
                    atomic_write(REMAINING_FILE, "\n".join(remaining))

                    elapsed = time.monotonic() - start
                    avg = elapsed / completed
                    eta = avg * len(remaining)
                    log.info(
                        "[%d/%d] sent sv %s | avg %.1fs/card | ETA %.1f min",
                        completed, total, code, avg, eta / 60.0,
                    )

                    if remaining:
                        cards_since_break += 1
                        cards_since_break = humanized_wait(cfg, cards_since_break)

            except KeyboardInterrupt:
                log.info("Interrupted by user.")
                clean_exit = True
            except pyautogui.FailSafeException:
                log.warning("Failsafe triggered (mouse in corner). Stopping.")
                clean_exit = True
            finally:
                control.unregister()

            runtime = time.monotonic() - start
            log.info("=" * 40)
            log.info("Completed : %d", completed)
            log.info("Runtime   : %dm %ds", int(runtime // 60), int(runtime % 60))
            if completed:
                log.info("Average   : %.1fs/card", runtime / completed)
            log.info("=" * 40)

            clean_exit = True

    finally:
        # Only clear the log if we exited cleanly (finished, F9, Ctrl+C,
        # or failsafe). On a crash / unhandled exception, we keep it.
        if clean_exit and cfg.get("clear_log_on_exit"):
            shutdown_logging_and_clear()

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
    