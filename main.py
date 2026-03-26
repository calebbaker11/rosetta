#!/usr/bin/env python3
"""
Rosetta Stone Automation Bot  v2
==================================
Automatically completes click-based activities (multiple-choice, image
matching).  Skips speaking and typing.  Pauses when unsure.

HOW TO RUN
----------
  Windows : double-click  run.bat
  Mac/Linux: open Terminal, type  bash run.sh

  Manual fallback:
    pip install playwright pynput
    python main.py          (Windows)
    python3 main.py         (Mac/Linux)

HOTKEYS  (work even when the browser window is in focus)
--------
  S  – skip the current screen
  P  – pause the bot
  R  – resume after a pause
  N  – approve / trigger the next action  (useful when require_approval=true)
  Q  – quit and save progress
"""

# ─── Standard library ────────────────────────────────────────────────────────
import json
import logging
import os
import subprocess
import sys
import time
import threading
from datetime import datetime

# ─── Auto-setup: install playwright + chromium before anything else ───────────
def _ensure_playwright():
    """
    Check that playwright is installed and that the Chromium browser binary
    exists.  If either is missing, install/download it automatically.
    This runs before the rest of the script so the user never has to type
    extra commands.
    """
    # 1. Make sure the playwright Python package is installed.
    try:
        import playwright  # noqa: F401
    except ImportError:
        print("[Setup] playwright not found — installing now…")
        subprocess.check_call([sys.executable, "-m", "pip", "install",
                               "playwright", "pynput"])
        print("[Setup] playwright installed.")

    # 2. Make sure the Chromium binary is present.
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            # Just check the executable path — don't launch yet.
            exe = pw.chromium.executable_path
            if not os.path.exists(exe):
                raise FileNotFoundError(exe)
        print("[Setup] Chromium OK.")
    except Exception:
        print("[Setup] Chromium browser not found — downloading now "
              "(this only happens once, may take a minute)…")
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=False,
        )
        if result.returncode != 0:
            print("\n[Setup] Auto-download failed.  Try running this manually:")
            print("        python3 -m playwright install chromium")
            sys.exit(1)
        print("[Setup] Chromium installed.")

_ensure_playwright()
from pathlib import Path

# ─── Third-party ─────────────────────────────────────────────────────────────
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# Optional: global hotkeys.  The bot still works if pynput is unavailable.
try:
    from pynput import keyboard as pynput_kb
    _PYNPUT = True
except Exception:
    _PYNPUT = False

# ═══════════════════════════════════════════════════════════════════════════════
#  PATHS
# ═══════════════════════════════════════════════════════════════════════════════
BASE   = Path(__file__).parent
CFG_F  = BASE / "config.json"
STATE_F= BASE / "state.json"
LOG_DIR= BASE / "logs"
SS_DIR = BASE / "screenshots"

LOG_DIR.mkdir(exist_ok=True)
SS_DIR.mkdir(exist_ok=True)

# ═══════════════════════════════════════════════════════════════════════════════
#  CONFIG  (config.json is the source of truth; these are safe defaults)
# ═══════════════════════════════════════════════════════════════════════════════
_DEFAULTS = {
    "rosetta_url":          "https://totale.rosettastone.com/",
    "login_timeout":        120,
    "max_screens":          1000,
    "slow_mo":              50,
    "confidence_threshold": 0.55,
    "click_delay":          0.8,
    "retry_count":          3,
    "retry_delay":          2.0,
    "auto_skip_speaking":   True,
    "auto_skip_typing":     True,
    "auto_skip_reorder":    False,
    "require_approval":     False,
    "screenshot_on_error":  True,
}

def load_config() -> dict:
    cfg = dict(_DEFAULTS)
    if CFG_F.exists():
        try:
            data = json.loads(CFG_F.read_text())
            # strip comment keys that start with _
            cfg.update({k: v for k, v in data.items() if not k.startswith("_")})
        except Exception as e:
            print(f"[Config] Could not read config.json: {e}  — using defaults.")
    return cfg

CFG = load_config()

# ═══════════════════════════════════════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════════════════════════════════════
_ts     = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
_logfile= LOG_DIR / f"bot_{_ts}.log"

logging.basicConfig(
    level   = logging.DEBUG,
    format  = "%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt = "%H:%M:%S",
    handlers=[
        logging.FileHandler(_logfile, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("rsbot")
log.info(f"Log file: {_logfile}")

# ═══════════════════════════════════════════════════════════════════════════════
#  STATE  (saved to state.json so the bot can resume)
# ═══════════════════════════════════════════════════════════════════════════════
def load_state() -> dict:
    blank = {
        "session_start":    _ts,
        "last_url":         "",
        "completed":        0,
        "skipped":          0,
        "errors":           0,
        "completed_urls":   [],
        "skipped_screens":  [],
    }
    if STATE_F.exists():
        try:
            saved = json.loads(STATE_F.read_text())
            blank.update(saved)
            log.info(f"Resumed state — {saved.get('completed', 0)} lessons done previously.")
        except Exception:
            pass
    return blank

def save_state(state: dict):
    try:
        STATE_F.write_text(json.dumps(state, indent=2))
    except Exception as e:
        log.warning(f"Could not save state: {e}")

STATE = load_state()

# ═══════════════════════════════════════════════════════════════════════════════
#  CONTROL FLAGS  (shared between hotkey thread and main thread)
# ═══════════════════════════════════════════════════════════════════════════════
_pause_evt   = threading.Event()   # set   = bot is paused
_quit_evt    = threading.Event()   # set   = quit requested
_skip_evt    = threading.Event()   # set   = skip current screen
_approve_evt = threading.Event()   # set   = user pressed N

def _on_press(key):
    """Called by pynput on every key press."""
    try:
        k = key.char.upper() if hasattr(key, "char") and key.char else None
    except Exception:
        k = None

    if k == "S":
        _skip_evt.set()
        log.info("[Hotkey] S pressed — skip flagged.")
    elif k == "P":
        _pause_evt.set()
        log.info("[Hotkey] P pressed — pausing.")
        print("\n[PAUSED]  Press R to resume, Q to quit.")
    elif k == "R":
        _pause_evt.clear()
        log.info("[Hotkey] R pressed — resuming.")
        print("[RESUMED]")
    elif k == "Q":
        _quit_evt.set()
        log.info("[Hotkey] Q pressed — quit flagged.")
        print("\n[QUIT] Finishing up and saving…")
    elif k == "N":
        _approve_evt.set()
        log.info("[Hotkey] N pressed — action approved.")

def start_hotkey_listener():
    if not _PYNPUT:
        log.warning("pynput not available — hotkeys disabled.  "
                    "Install with:  pip install pynput")
        return
    t = threading.Thread(
        target=lambda: pynput_kb.Listener(on_press=_on_press).run(),
        daemon=True,
    )
    t.start()
    log.info("Hotkey listener started  (S=skip  P=pause  R=resume  N=approve  Q=quit)")

# ═══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════════════════
def snooze(seconds: float):
    """Sleep in small increments so we can react to quit/skip quickly."""
    end = time.time() + seconds
    while time.time() < end:
        if _quit_evt.is_set():
            return
        time.sleep(0.1)

def check_pause():
    """Block here while the bot is paused."""
    while _pause_evt.is_set():
        time.sleep(0.2)
        if _quit_evt.is_set():
            return

def ask_user(msg: str) -> str:
    """Print a notice and wait for Enter (fallback when hotkeys can't help)."""
    print(f"\n{'─'*60}")
    print(f"  ACTION NEEDED:  {msg}")
    print(f"{'─'*60}")
    return input("  Press Enter when done  (or type 'skip'/'quit'): ").strip().lower()

def screenshot(page, label: str):
    """Save a screenshot; used on errors."""
    if not CFG["screenshot_on_error"]:
        return
    try:
        path = SS_DIR / f"{label}_{datetime.now().strftime('%H%M%S')}.png"
        page.screenshot(path=str(path))
        log.info(f"Screenshot saved → {path}")
    except Exception as e:
        log.debug(f"Screenshot failed: {e}")

def wait_for_stable(page, ms: int = 1500):
    """Wait for network to settle."""
    try:
        page.wait_for_load_state("networkidle", timeout=ms)
    except Exception:
        pass

def el_exists(page, selector: str) -> bool:
    try:
        return page.locator(selector).count() > 0
    except Exception:
        return False

def el_visible(page, selector: str) -> bool:
    try:
        loc = page.locator(selector).first
        return loc.is_visible()
    except Exception:
        return False

def with_retry(fn, label: str = "action"):
    """Run fn(), retrying up to CFG['retry_count'] times on errors."""
    for attempt in range(CFG["retry_count"] + 1):
        try:
            return fn()
        except PWTimeout:
            log.warning(f"[Retry] Timeout on '{label}'  attempt {attempt+1}")
        except Exception as e:
            log.warning(f"[Retry] Error on '{label}': {e}  attempt {attempt+1}")
        if attempt < CFG["retry_count"]:
            snooze(CFG["retry_delay"])
    log.error(f"[Retry] '{label}' failed after {CFG['retry_count']+1} attempts.")
    return None

# ═══════════════════════════════════════════════════════════════════════════════
#  SCREEN CLASSIFIER
#  Returns (screen_type:str, confidence:float 0‒1)
# ═══════════════════════════════════════════════════════════════════════════════

# Each entry is (selector_or_text, weight).
# Positive weight = evidence FOR this screen type.
_SIGNALS: dict[str, list[tuple]] = {

    "COMPLETION": [
        ("text=Lesson Complete",          0.8),
        ("text=Unit Complete",            0.8),
        ("text=Great job",                0.5),
        ("[class*='complete-screen']",    0.7),
        ("[class*='CompletionScreen']",   0.7),
        ("[data-qa='completion']",        0.9),
    ],

    "SPEAKING": [
        ("[data-qa='speak-button']",      0.9),
        ("[class*='microphone']",         0.8),
        ("[class*='record-button']",      0.8),
        ("text=Speak",                    0.5),
        ("text=Say the word",             0.6),
        ("[class*='SpeakActivity']",      0.9),
        ("[class*='speak-activity']",     0.9),
    ],

    "TYPING": [
        ("input[type='text']",            0.9),
        ("textarea",                      0.7),
        ("[class*='keyboard']",           0.7),
        ("text=Type what you hear",       0.9),
        ("text=Type the missing",         0.9),
        ("[class*='TypingActivity']",     0.9),
    ],

    "REORDER": [
        ("[class*='reorder']",            0.8),
        ("[class*='Reorder']",            0.8),
        ("[class*='sentence-build']",     0.7),
        ("[class*='word-bank']",          0.7),
        ("text=Put the words in order",   0.9),
    ],

    "MATCHING": [
        ("[class*='match-item']",         0.7),
        ("[class*='MatchItem']",          0.7),
        ("[class*='pair']",               0.5),
        ("[data-qa='match']",             0.9),
        ("[class*='MatchingActivity']",   0.9),
    ],

    "MULTIPLE_CHOICE": [
        ("[data-qa='choice']",            0.9),
        ("[class*='choice-item']",        0.7),
        ("[class*='ChoiceItem']",         0.7),
        ("[role='radio']",                0.6),
        ("[class*='answer-option']",      0.7),
        ("[class*='MultipleChoice']",     0.9),
    ],

    "LESSON_PAGE": [
        ("[data-qa='lesson-title']",      0.8),
        ("[class*='lesson-header']",      0.6),
        ("text=Core Lesson",              0.7),
        ("text=Milestone",                0.5),
    ],

    "UNIT_PAGE": [
        ("[data-qa='unit-header']",       0.8),
        ("[class*='unit-overview']",      0.7),
        ("text=Unit",                     0.3),
        ("[class*='lesson-list']",        0.6),
    ],

    "DASHBOARD": [
        ("[class*='course-overview']",    0.7),
        ("[data-qa='unit']",              0.7),
        ("text=Your Language Journey",    0.8),
        ("[class*='dashboard']",          0.6),
    ],

    "LOADING": [
        ("[class*='spinner']",            0.8),
        ("[class*='loading']",            0.7),
        ("[role='progressbar']",          0.6),
    ],
}

# Visual-fallback selectors (broader, checked when primary selectors score low)
_VISUAL_FALLBACK: dict[str, list[str]] = {
    "MULTIPLE_CHOICE": [
        "button[class*='option']",
        "li[class*='choice']",
        "[tabindex][class*='item']",
        "img[class*='choice']",
    ],
    "MATCHING": [
        "[draggable='true']",
        "[class*='tile']",
        "button[class*='tile']",
    ],
    "SPEAKING": [
        "button[class*='mic']",
        "svg[class*='micro']",
    ],
}


def classify_screen(page) -> tuple[str, float]:
    """
    Score every screen type and return the best match with its confidence.
    Falls back to visual selectors when primary score is below threshold.
    """
    url = page.url.lower()
    scores: dict[str, float] = {k: 0.0 for k in _SIGNALS}

    # URL hints
    if "dashboard" in url or "learn" in url:
        scores["DASHBOARD"] += 0.3
    if "unit" in url:
        scores["UNIT_PAGE"] += 0.3
    if "lesson" in url or "activity" in url:
        scores["LESSON_PAGE"] += 0.2

    # DOM signals
    for screen_type, signals in _SIGNALS.items():
        total_weight = sum(w for _, w in signals)
        earned = 0.0
        for selector, weight in signals:
            try:
                if selector.startswith("text="):
                    found = page.get_by_text(selector[5:]).count() > 0
                else:
                    found = page.locator(selector).count() > 0
                if found:
                    earned += weight
            except Exception:
                pass
        if total_weight > 0:
            scores[screen_type] += earned / total_weight

    best       = max(scores, key=scores.get)
    confidence = min(scores[best], 1.0)

    # Visual fallback: if confidence is low, try broader selectors
    if confidence < CFG["confidence_threshold"]:
        for screen_type, fallbacks in _VISUAL_FALLBACK.items():
            for sel in fallbacks:
                try:
                    if page.locator(sel).count() >= 2:
                        scores[screen_type] += 0.4
                        log.debug(f"[Classify] Visual fallback matched '{sel}' → {screen_type}")
                        break
                except Exception:
                    pass
        best       = max(scores, key=scores.get)
        confidence = min(scores[best], 1.0)

    log.debug(f"[Classify] {best}  confidence={confidence:.2f}  url={page.url[:80]}")
    return best, confidence

# ═══════════════════════════════════════════════════════════════════════════════
#  CONTINUE / NEXT BUTTON
# ═══════════════════════════════════════════════════════════════════════════════
_CONTINUE_SELS = [
    "[data-qa='continue-button']",
    "[data-qa='next-button']",
    "button:has-text('Continue')",
    "button:has-text('Next')",
    "button:has-text('Submit')",
    "button:has-text('Done')",
    "button:has-text('Got it')",
    "[class*='continue-btn']",
    "[class*='ContinueButton']",
    "[class*='next-button']",
]

def click_continue(page) -> bool:
    """Click Continue/Next if visible.  Returns True if clicked."""
    for sel in _CONTINUE_SELS:
        try:
            btn = page.locator(sel).first
            if btn.is_visible():
                btn.click()
                snooze(CFG["click_delay"])
                log.debug("[Nav] Clicked Continue/Next.")
                return True
        except Exception:
            pass
    return False

# ═══════════════════════════════════════════════════════════════════════════════
#  ACTIVITY HANDLERS
# ═══════════════════════════════════════════════════════════════════════════════

def _maybe_approve(label: str):
    """If require_approval is on, pause until user presses N."""
    if not CFG["require_approval"]:
        return
    _approve_evt.clear()
    print(f"\n[Approval needed]  About to: {label}")
    print("  Press N to approve, S to skip, or P to pause.")
    while not _approve_evt.is_set() and not _skip_evt.is_set() and not _quit_evt.is_set():
        time.sleep(0.1)


def handle_multiple_choice(page) -> bool:
    """Click through available choices; move on after a correct one."""
    log.info("[Action] Multiple choice — trying choices in order.")
    _maybe_approve("click a multiple-choice answer")

    primary_sels = [
        "[data-qa='choice']",
        "[class*='choice-item']",
        "[class*='ChoiceItem']",
        "[class*='answer-option']",
        "[role='radio']",
        "[class*='option-item']",
    ]
    fallback_sels = [
        "button[class*='option']",
        "li[class*='choice']",
        "img[class*='choice']",
    ]

    choices = []
    for sel in primary_sels:
        try:
            found = page.locator(sel).all()
            if len(found) >= 2:
                choices = found
                log.debug(f"[MC] Found {len(choices)} choices with '{sel}'")
                break
        except Exception:
            pass

    # Visual fallback
    if not choices:
        for sel in fallback_sels:
            try:
                found = page.locator(sel).all()
                if len(found) >= 2:
                    choices = found
                    log.debug(f"[MC] Visual fallback: {len(choices)} choices with '{sel}'")
                    break
            except Exception:
                pass

    if not choices:
        log.warning("[MC] No choices found — pausing for manual input.")
        screenshot(page, "mc_no_choices")
        ask_user("Could not find answer choices.  Complete this screen manually.")
        return False

    for i, choice in enumerate(choices):
        if _skip_evt.is_set() or _quit_evt.is_set():
            break
        try:
            if not choice.is_visible():
                continue
            log.debug(f"[MC] Clicking choice {i+1}/{len(choices)}")
            choice.click()
            snooze(CFG["click_delay"])
            if click_continue(page):
                return True
            snooze(0.8)
            if click_continue(page):
                return True
        except Exception as e:
            log.debug(f"[MC] Click error on choice {i+1}: {e}")

    # Last try
    click_continue(page)
    return True


def handle_matching(page) -> bool:
    """Click match items sequentially; Rosetta auto-advances when all matched."""
    log.info("[Action] Matching exercise — clicking items in order.")
    _maybe_approve("click matching items")

    primary_sels = [
        "[data-qa='match']",
        "[class*='match-item']",
        "[class*='MatchItem']",
        "[class*='pair-item']",
    ]
    fallback_sels = [
        "[draggable='true']",
        "button[class*='tile']",
        "[class*='tile']:not([class*='inactive'])",
    ]

    items = []
    for sel in primary_sels + fallback_sels:
        try:
            found = page.locator(sel).all()
            if len(found) >= 2:
                items = found
                log.debug(f"[Match] {len(items)} items found with '{sel}'")
                break
        except Exception:
            pass

    if not items:
        log.warning("[Match] No match items found — pausing.")
        screenshot(page, "match_no_items")
        ask_user("Could not find matching items.  Complete this manually.")
        return False

    for item in items:
        if _skip_evt.is_set() or _quit_evt.is_set():
            break
        try:
            if item.is_visible():
                item.click()
                snooze(CFG["click_delay"])
        except Exception as e:
            log.debug(f"[Match] Click error: {e}")

    snooze(1.2)
    click_continue(page)
    return True


def handle_skip(page, reason: str) -> bool:
    """
    Try to skip an unsupported exercise type (speaking, typing, reorder).
    Falls back to asking the user if no skip button exists.
    """
    log.info(f"[Skip] {reason}")
    skip_sels = [
        "button:has-text('Skip')",
        "[data-qa='skip']",
        "[data-qa='skip-button']",
        "[class*='skip-btn']",
    ]
    for sel in skip_sels:
        try:
            btn = page.locator(sel).first
            if btn.is_visible():
                btn.click()
                snooze(CFG["click_delay"])
                log.info("[Skip] Clicked Skip button.")
                return True
        except Exception:
            pass

    # No skip button — try Continue
    if click_continue(page):
        return True

    # Give up and ask
    response = ask_user(
        f"{reason}  No Skip button found.  "
        "Please complete or skip this manually, then press Enter."
    )
    return response != "quit"


def handle_completion(page) -> bool:
    """Handle lesson-complete / unit-complete screens."""
    log.info("[Action] Completion screen — clicking Continue.")
    STATE["completed"] += 1
    STATE["completed_urls"].append(page.url)
    save_state(STATE)
    snooze(0.5)
    click_continue(page)
    return True


def handle_reorder(page) -> bool:
    """Sentence-reorder: skip by default (configurable)."""
    if CFG["auto_skip_reorder"]:
        return handle_skip(page, "Reorder exercise — auto-skipping.")
    log.info("[Action] Reorder exercise — pausing for manual input.")
    ask_user("Reorder exercise detected.  Complete it manually then press Enter.")
    return True


def handle_lesson_page(page) -> bool:
    """We landed on a lesson overview — look for a Start/Begin button."""
    log.info("[Action] Lesson page — looking for Start button.")
    start_sels = [
        "button:has-text('Start')",
        "button:has-text('Begin')",
        "button:has-text('Continue')",
        "[data-qa='start-lesson']",
        "[class*='start-button']",
    ]
    for sel in start_sels:
        try:
            btn = page.locator(sel).first
            if btn.is_visible():
                btn.click()
                snooze(CFG["click_delay"])
                return True
        except Exception:
            pass

    click_continue(page)
    return True


def handle_unknown(page, confidence: float) -> bool:
    """Unknown or low-confidence screen — pause and ask the user."""
    msg = (
        f"Unknown screen (confidence={confidence:.0%}).  "
        "Please navigate past it manually."
    )
    log.warning(f"[Unknown] {msg}  url={page.url}")
    screenshot(page, "unknown")
    STATE["skipped"] += 1
    save_state(STATE)
    response = ask_user(msg)
    if response == "quit":
        _quit_evt.set()
    return True

# ═══════════════════════════════════════════════════════════════════════════════
#  NAVIGATE TO NEXT INCOMPLETE LESSON
# ═══════════════════════════════════════════════════════════════════════════════
_LESSON_SELS = [
    "[data-qa='lesson']:not([class*='complete'])",
    "[class*='lesson-tile']:not([class*='complete'])",
    "[class*='LessonTile']:not([class*='complete'])",
    "[class*='lesson-node']:not([class*='complete'])",
    # Broad fallbacks
    "[data-qa='lesson']",
    "[class*='lesson-tile']",
    "a[href*='lesson']",
]

def find_next_lesson(page) -> bool:
    """Click the first visible incomplete lesson tile.  Returns True if found."""
    for sel in _LESSON_SELS:
        try:
            tiles = page.locator(sel).all()
            for tile in tiles:
                if tile.is_visible():
                    label = tile.get_attribute("aria-label") or "(no label)"
                    # Skip already-done URLs
                    href = tile.get_attribute("href") or ""
                    if href and href in STATE["completed_urls"]:
                        continue
                    log.info(f"[Nav] Clicking lesson: {label}")
                    tile.click()
                    snooze(2)
                    return True
        except Exception:
            pass
    return False

# ═══════════════════════════════════════════════════════════════════════════════
#  STATUS DISPLAY
# ═══════════════════════════════════════════════════════════════════════════════
def print_status(screen_type: str, confidence: float):
    bar_len  = 20
    filled   = int(confidence * bar_len)
    bar      = "█" * filled + "░" * (bar_len - filled)
    hotkeys  = "S=skip  P=pause  R=resume  N=approve  Q=quit"
    print(
        f"\n{'─'*60}\n"
        f"  Screen : {screen_type}\n"
        f"  Conf.  : [{bar}] {confidence:.0%}\n"
        f"  Done   : {STATE['completed']}  Skipped: {STATE['skipped']}  Errors: {STATE['errors']}\n"
        f"  Keys   : {hotkeys}\n"
        f"{'─'*60}"
    )

# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN LOOP
# ═══════════════════════════════════════════════════════════════════════════════
def process_screen(page) -> bool:
    """Classify and handle the current screen.  Returns False if we should quit."""

    check_pause()
    if _quit_evt.is_set():
        return False

    # Clear one-shot skip flag at the start of each new screen
    _skip_evt.clear()

    wait_for_stable(page)
    screen_type, confidence = classify_screen(page)
    print_status(screen_type, confidence)

    # User pressed S before we acted
    if _skip_evt.is_set():
        log.info("[Skip] User skipped before action.")
        STATE["skipped"] += 1
        save_state(STATE)
        return True

    # Route to the right handler
    if screen_type == "LOADING":
        log.info("[Loading] Waiting for page to settle…")
        snooze(2)
        return True

    if screen_type == "COMPLETION":
        handle_completion(page)
        return True

    if screen_type == "SPEAKING":
        if CFG["auto_skip_speaking"]:
            handle_skip(page, "Speaking exercise — auto-skipping.")
            STATE["skipped"] += 1
            save_state(STATE)
        else:
            ask_user("Speaking exercise.  Complete it manually then press Enter.")
        return True

    if screen_type == "TYPING":
        if CFG["auto_skip_typing"]:
            handle_skip(page, "Typing exercise — auto-skipping.")
            STATE["skipped"] += 1
            save_state(STATE)
        else:
            ask_user("Typing exercise.  Complete it manually then press Enter.")
        return True

    if screen_type == "REORDER":
        handle_reorder(page)
        return True

    if screen_type == "MATCHING":
        with_retry(lambda: handle_matching(page), "matching")
        return True

    if screen_type == "MULTIPLE_CHOICE":
        with_retry(lambda: handle_multiple_choice(page), "multiple_choice")
        return True

    if screen_type in ("DASHBOARD", "UNIT_PAGE"):
        log.info(f"[Nav] On {screen_type} — looking for next lesson.")
        found = find_next_lesson(page)
        if not found:
            response = ask_user(
                "No incomplete lessons found on this page.  "
                "Navigate to a unit manually, then press Enter.  "
                "Type 'quit' to exit."
            )
            if response == "quit":
                _quit_evt.set()
                return False
        return True

    if screen_type == "LESSON_PAGE":
        handle_lesson_page(page)
        return True

    # Low confidence or UNKNOWN
    if confidence < CFG["confidence_threshold"]:
        handle_unknown(page, confidence)
        return not _quit_evt.is_set()

    # Anything else with OK confidence — try Continue
    log.info(f"[Action] Unhandled type '{screen_type}' — trying Continue.")
    click_continue(page)
    return True


# ═══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════
def main():
    print("\n" + "═"*60)
    print("  Rosetta Stone Automation Bot  v2")
    print("═"*60)
    print(f"  Config  : {CFG_F}")
    print(f"  State   : {STATE_F}")
    print(f"  Logs    : {_logfile}")
    print("═"*60)

    start_hotkey_listener()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=False,
            slow_mo=CFG["slow_mo"],
        )
        ctx  = browser.new_context(
            viewport   = {"width": 1280, "height": 800},
            user_agent = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = ctx.new_page()

        # ── Open Rosetta Stone ────────────────────────────────────────────────
        url = STATE.get("last_url") or CFG["rosetta_url"]
        log.info(f"Opening {url}")
        page.goto(url)
        snooze(2)

        # ── Wait for manual login ─────────────────────────────────────────────
        print(f"\n[Step 1/3]  Please log in to Rosetta Stone in the browser window.")
        print(f"            You have {CFG['login_timeout']} seconds.")
        print("            Then come back here and press Enter.\n")
        input("  Press Enter once you are logged in: ")
        snooze(2)
        STATE["last_url"] = page.url
        save_state(STATE)

        print("\n[Step 2/3]  Bot is running.  Use hotkeys to control it.")
        print("            Press Ctrl+C in this window to force-quit.\n")

        # ── Main loop ─────────────────────────────────────────────────────────
        for screen_num in range(CFG["max_screens"]):
            if _quit_evt.is_set():
                break
            log.info(f"── Screen {screen_num+1}  url={page.url[:100]}")
            STATE["last_url"] = page.url
            save_state(STATE)

            try:
                ok = process_screen(page)
            except KeyboardInterrupt:
                log.info("KeyboardInterrupt — stopping.")
                break
            except Exception as e:
                log.error(f"Unhandled error on screen {screen_num+1}: {e}", exc_info=True)
                screenshot(page, f"error_screen_{screen_num+1}")
                STATE["errors"] += 1
                save_state(STATE)
                response = ask_user(f"Error: {e}\nFix manually then press Enter, or type 'quit'.")
                if response == "quit":
                    break

            if not ok or _quit_evt.is_set():
                break

            snooze(0.5)

        # ── Wrap up ───────────────────────────────────────────────────────────
        save_state(STATE)
        print(f"\n[Step 3/3]  Session complete.")
        print(f"  Lessons completed : {STATE['completed']}")
        print(f"  Screens skipped   : {STATE['skipped']}")
        print(f"  Errors            : {STATE['errors']}")
        print(f"  State saved to    : {STATE_F}")
        print(f"  Log saved to      : {_logfile}")
        print("\n  Closing browser in 5 seconds…")
        snooze(5)
        browser.close()


if __name__ == "__main__":
    main()
