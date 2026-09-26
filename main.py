"""
Discord bulk card sender with configurable pacing and persistent recovery.

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
INFLIGHT_FILE = DATA_FOLDER / "inflight.txt"

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
def validate_config(cfg: dict) -> None:
    """Validate configuration values and raise a clear error for invalid settings."""
    non_negative = (
        "startup_delay",
        "pre_paste_min",
        "pre_paste_max",
        "post_paste_min",
        "post_paste_max",
        "channel_cooldown",
        "cooldown_jitter_min",
        "cooldown_jitter_max",
        "think_min",
        "think_max",
        "break_duration_min",
        "break_duration_max",
        "review_min",
        "review_max",
    )
    for key in non_negative:
        value = cfg[key]
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            raise ValueError(f"{key} must be a non-negative number")

    for key in ("think_chance", "review_chance"):
        value = cfg[key]
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 1:
            raise ValueError(f"{key} must be a number between 0 and 1")

    for key in ("break_every_min", "break_every_max", "log_max_lines"):
        value = cfg[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"{key} must be a non-negative integer")

    if cfg["break_every_min"] > cfg["break_every_max"]:
        raise ValueError("break_every_min cannot be greater than break_every_max")

    if not isinstance(cfg["require_discord_focus"], bool):
        raise ValueError("require_discord_focus must be true or false")

    if not isinstance(cfg["restore_clipboard"], bool):
        raise ValueError("restore_clipboard must be true or false")

    if not isinstance(cfg["clear_log_on_exit"], bool):
        raise ValueError("clear_log_on_exit must be true or false")

    if not isinstance(cfg["code_pattern"], str) or not cfg["code_pattern"]:
        raise ValueError("code_pattern must be a non-empty regular expression")

    try:
        re.compile(cfg["code_pattern"])
    except re.error as exc:
        raise ValueError(f"code_pattern is invalid: {exc}") from exc


def load_config(path: Path) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if path.exists():
        try:
            user_cfg = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(user_cfg, dict):
                raise ValueError("config.json must contain a JSON object")
            cfg.update(user_cfg)
        except json.JSONDecodeError as e:
            raise ValueError(f"config.json is invalid JSON: {e}") from e

    validate_config(cfg)
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
    """Optionally pause before pressing Enter, then press Enter."""
    if random.random() < cfg["review_chance"]:
        review = random.uniform(cfg["review_min"], cfg["review_max"])
        log.info("Review pause: %.1fs", review)
        time.sleep(review)
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
        pid_text = str(os.getpid())

        for _ in range(2):
            try:
                with self.path.open("x", encoding="utf-8") as f:
                    f.write(pid_text)
                return self
            except FileExistsError:
                try:
                    existing_pid = int(self.path.read_text(encoding="utf-8").strip())
                except (OSError, ValueError):
                    existing_pid = 0

                if existing_pid and _pid_alive(existing_pid):
                    raise SystemExit(
                        f"Another instance is already running (PID {existing_pid})."
                    )

                # The lock is stale. Remove it and retry atomic creation.
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass

        raise SystemExit("Could not acquire the instance lock safely.")

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
def collect_codes_if_needed(
    cfg: dict,
    sent: set[str],
    *,
    persist: bool = True,
) -> list[str]:
    """Load remaining codes or prompt for input, optionally persisting new input."""
    remaining = [code for code in load_lines(REMAINING_FILE) if code not in sent]
    if remaining:
        if persist:
            atomic_write(REMAINING_FILE, "\n".join(remaining))
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

    if persist and codes:
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

    try:
        pyperclip.copy(command)

        rand_delay(cfg["pre_paste_min"], cfg["pre_paste_max"], "before pasting")
        pyautogui.hotkey("ctrl", "v")

        rand_delay(
            cfg["post_paste_min"],
            cfg["post_paste_max"],
            "before pressing Enter",
        )
        human_press_enter(cfg)
    finally:
        if prev_clipboard is not None:
            try:
                pyperclip.copy(prev_clipboard)
            except Exception as exc:
                log.warning("Could not restore clipboard: %s", exc)


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------
def load_inflight() -> Optional[str]:
    """Return the code recorded as in-flight, if any."""
    lines = load_lines(INFLIGHT_FILE)
    return lines[0] if lines else None


def clear_inflight() -> None:
    """Remove the in-flight marker."""
    try:
        INFLIGHT_FILE.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("Could not clear in-flight state: %s", exc)


def recover_inflight(sent_codes: set[str]) -> None:
    """
    Handle a code left in the in-flight marker after an interrupted run.

    If the code is already in sent.txt, the marker is stale and can be cleared.
    Otherwise ask the user whether to mark it sent or retry it.
    """
    code = load_inflight()
    if not code:
        return

    if code in sent_codes:
        log.info("Clearing stale in-flight marker for already-sent code %s.", code)
        clear_inflight()
        return

    print(
        "\nA previous run was interrupted while processing this code:\n"
        f"    sv {code}\n"
        "The command may or may not have reached Discord."
    )
    while True:
        choice = input(
            "Choose [m]ark as sent, [r]etry it, or [c]ancel: "
        ).strip().lower()

        if choice == "m":
            append_line(SENT_FILE, code)
            sent_codes.add(code)
            clear_inflight()
            log.warning("Marked in-flight code as sent: %s", code)
            return

        if choice == "r":
            clear_inflight()
            log.warning("Retrying in-flight code: %s", code)
            return

        if choice == "c":
            raise SystemExit("Recovery cancelled by user.")

        print("Please enter m, r, or c.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="Discord bulk card sender.")
    parser.add_argument(
        "--config",
        default="config.json",
        help="path to config file",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="parse and print codes without sending or changing runtime state",
    )
    args = parser.parse_args()

    setup_logging()

    try:
        cfg = load_config(Path(args.config))
    except ValueError as exc:
        log.error("Configuration error: %s", exc)
        return 2

    pyautogui.PAUSE = 0.05
    pyautogui.FAILSAFE = True  # move mouse to a corner to abort

    clean_exit = False
    exit_code = 0

    try:
        with InstanceLock(LOCK_FILE):
            sent_codes = set(load_lines(SENT_FILE))

            # Dry-run is deliberately read-only with respect to runtime state.
            remaining = collect_codes_if_needed(
                cfg,
                sent_codes,
                persist=not args.dry_run,
            )

            if not remaining:
                log.info("No cards to process.")
                clean_exit = True
                return 0

            if args.dry_run:
                log.info("Dry run: %d code(s) would be processed.", len(remaining))
                for code in remaining:
                    log.info("[dry-run] would send: sv %s", code)
                clean_exit = True
                return 0

            # Resolve any previous interrupted operation before continuing.
            recover_inflight(sent_codes)

            # Remove anything that became sent during recovery.
            remaining = [code for code in remaining if code not in sent_codes]
            atomic_write(REMAINING_FILE, "\n".join(remaining))

            if not remaining:
                log.info("No unsent cards remain after recovery.")
                clean_exit = True
                return 0

            total = len(remaining)
            completed = 0
            cards_since_break = 0
            start = time.monotonic()

            log.info("Loaded %d cards.", total)

            print("Switch to the Discord channel now.")
            print(f"Starting in {cfg['startup_delay']:.1f}s...\n")
            time.sleep(cfg["startup_delay"])

            control = Control()
            control.register()

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

                    # Record intent before GUI automation. If the process crashes
                    # during send_card(), the next startup can request recovery.
                    atomic_write(INFLIGHT_FILE, code)

                    append_line_rotated(
                        LOG_FILE,
                        f"{time.strftime('%Y-%m-%d %H:%M:%S')} -> sv {code}",
                        cfg["log_max_lines"],
                    )

                    send_card(code, cfg)

                    # A successful return from send_card() means the local
                    # automation completed. Persist completion before clearing
                    # the in-flight marker.
                    append_line(SENT_FILE, code)
                    completed += 1

                    remaining.pop(0)
                    atomic_write(REMAINING_FILE, "\n".join(remaining))
                    clear_inflight()

                    elapsed = time.monotonic() - start
                    avg = elapsed / completed
                    eta = avg * len(remaining)

                    log.info(
                        "[%d/%d] sent sv %s | avg %.1fs/card | ETA %.1f min",
                        completed,
                        total,
                        code,
                        avg,
                        eta / 60.0,
                    )

                    if remaining:
                        cards_since_break += 1
                        cards_since_break = humanized_wait(
                            cfg,
                            cards_since_break,
                        )

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
            log.info(
                "Runtime   : %dm %ds",
                int(runtime // 60),
                int(runtime % 60),
            )
            if completed:
                log.info("Average   : %.1fs/card", runtime / completed)
            log.info("=" * 40)

            clean_exit = True

    finally:
        if clean_exit and cfg.get("clear_log_on_exit"):
            shutdown_logging_and_clear()

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
    
