"""
Rosetta Stone Automation Tool
-------------------------------
Automatically completes easy click-based lessons (multiple choice & matching).
Skips speaking and typing exercises.
Pauses and asks you what to do when it's unsure.

HOW TO RUN:
  1. pip install -r requirements.txt
  2. playwright install chromium
  3. python main.py
"""

import time
import random
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ── Settings ──────────────────────────────────────────────────────────────────
ROSETTA_URL   = "https://totale.rosettastone.com/"
LOGIN_TIMEOUT = 120   # seconds you have to log in before the bot starts
CLICK_DELAY   = 0.8   # seconds between clicks (looks more human)
# ─────────────────────────────────────────────────────────────────────────────


def wait(seconds: float):
    """Simple pause."""
    time.sleep(seconds)


def ask_user(message: str) -> str:
    """Print a message and wait for the user to press Enter."""
    print(f"\n{'='*60}")
    print(f"  ACTION NEEDED: {message}")
    print(f"{'='*60}")
    return input("  Press Enter when ready (or type 'skip' to skip this): ").strip().lower()


# ── Activity detectors ────────────────────────────────────────────────────────

def is_speaking_exercise(page) -> bool:
    """Return True if this looks like a speaking/microphone exercise."""
    indicators = [
        "[data-qa='speak-button']",
        ".microphone",
        "[class*='microphone']",
        "[class*='record']",
        "[class*='speak']",
        "text=Speak",
        "text=Say",
    ]
    for sel in indicators:
        try:
            if page.locator(sel).count() > 0:
                return True
        except Exception:
            pass
    return False


def is_typing_exercise(page) -> bool:
    """Return True if this looks like a typing/keyboard exercise."""
    indicators = [
        "input[type='text']",
        "textarea",
        "[class*='keyboard']",
        "[class*='type-']",
        "text=Type what you hear",
        "text=Type the missing",
    ]
    for sel in indicators:
        try:
            if page.locator(sel).count() > 0:
                return True
        except Exception:
            pass
    return False


def is_multiple_choice(page) -> bool:
    """Return True if this looks like a multiple-choice / image-click activity."""
    selectors = [
        "[data-qa='choice']",
        "[class*='choice']",
        "[class*='option']",
        "[class*='answer']",
        "[role='radio']",
        "[role='option']",
    ]
    for sel in selectors:
        try:
            if page.locator(sel).count() >= 2:
                return True
        except Exception:
            pass
    return False


def is_matching_exercise(page) -> bool:
    """Return True if this looks like a matching (drag-and-drop or pair-click) activity."""
    selectors = [
        "[class*='match']",
        "[class*='pair']",
        "[data-qa='match']",
    ]
    for sel in selectors:
        try:
            if page.locator(sel).count() >= 2:
                return True
        except Exception:
            pass
    return False


# ── Activity handlers ─────────────────────────────────────────────────────────

def handle_multiple_choice(page):
    """
    Click the first available answer choice.
    Rosetta Stone highlights correct answers; we click one option at a time
    and wait to see if the lesson advances.
    """
    print("  [Multiple Choice] Clicking an answer...")

    choice_selectors = [
        "[data-qa='choice']",
        "[class*='choice-item']",
        "[class*='ChoiceItem']",
        "[class*='answer-option']",
        "[role='radio']",
        "[role='option']",
        "[class*='option']",
    ]

    choices = []
    for sel in choice_selectors:
        try:
            found = page.locator(sel).all()
            if len(found) >= 2:
                choices = found
                break
        except Exception:
            pass

    if not choices:
        print("  [Multiple Choice] Could not find choices — pausing.")
        ask_user("Could not find answer choices on this screen. Navigate past it manually.")
        return

    # Try each choice until one works (some Rosetta activities need the right answer)
    for choice in choices:
        try:
            if choice.is_visible():
                choice.click()
                wait(CLICK_DELAY)

                # Check if a "continue" or "next" button appeared
                if click_continue_if_present(page):
                    return  # moved on successfully

                # Small pause then check again
                wait(1.0)
                if click_continue_if_present(page):
                    return
        except Exception as e:
            print(f"  [Multiple Choice] Click error: {e}")
            continue


def handle_matching(page):
    """
    For matching exercises: click items in order hoping Rosetta accepts them.
    Rosetta Stone matching usually auto-advances when all pairs are matched.
    """
    print("  [Matching] Clicking match items in order...")

    match_selectors = [
        "[class*='match-item']",
        "[class*='MatchItem']",
        "[class*='pair-item']",
        "[data-qa='match']",
    ]

    items = []
    for sel in match_selectors:
        try:
            found = page.locator(sel).all()
            if len(found) >= 2:
                items = found
                break
        except Exception:
            pass

    if not items:
        print("  [Matching] Could not find match items — pausing.")
        ask_user("Could not find matching items. Navigate past it manually.")
        return

    for item in items:
        try:
            if item.is_visible():
                item.click()
                wait(CLICK_DELAY)
        except Exception as e:
            print(f"  [Matching] Click error: {e}")

    wait(1.5)
    click_continue_if_present(page)


def click_continue_if_present(page) -> bool:
    """
    Click Continue / Next / Submit if visible. Returns True if clicked.
    """
    continue_selectors = [
        "[data-qa='continue-button']",
        "[data-qa='next-button']",
        "button:has-text('Continue')",
        "button:has-text('Next')",
        "button:has-text('Submit')",
        "button:has-text('Done')",
        "[class*='continue']",
        "[class*='next-button']",
    ]
    for sel in continue_selectors:
        try:
            btn = page.locator(sel).first
            if btn.is_visible():
                btn.click()
                wait(CLICK_DELAY)
                print("  [Nav] Clicked Continue/Next.")
                return True
        except Exception:
            pass
    return False


# ── Lesson navigation ─────────────────────────────────────────────────────────

def find_and_click_incomplete_lesson(page) -> bool:
    """
    Look for a lesson that isn't complete yet and click it.
    Returns True if a lesson was found and clicked.
    """
    incomplete_selectors = [
        # Common Rosetta Stone lesson tile patterns
        "[data-qa='lesson']:not([class*='complete'])",
        "[class*='lesson-tile']:not([class*='complete'])",
        "[class*='LessonTile']:not([class*='complete'])",
        "[class*='unit-tile']:not([class*='complete'])",
        "[aria-label*='lesson']:not([aria-label*='complete'])",
        # Fallback: any lesson link
        "[data-qa='lesson']",
        "[class*='lesson-tile']",
    ]

    for sel in incomplete_selectors:
        try:
            tiles = page.locator(sel).all()
            for tile in tiles:
                if tile.is_visible():
                    label = tile.get_attribute("aria-label") or ""
                    print(f"  [Lessons] Found lesson: {label or '(no label)'}")
                    tile.click()
                    wait(2)
                    return True
        except Exception:
            pass

    return False


def complete_current_screen(page):
    """
    Figure out what type of activity is on screen and handle it.
    """
    wait(1.5)  # let the screen settle

    if is_speaking_exercise(page):
        print("  [Skip] Speaking exercise detected — skipping.")
        response = ask_user("Speaking exercise found. Press Enter to try clicking Skip/Next, or type 'skip'.")
        if response != "skip":
            # Try to find a skip button
            skipped = False
            for sel in ["button:has-text('Skip')", "[data-qa='skip']", "button:has-text('Continue')"]:
                try:
                    btn = page.locator(sel).first
                    if btn.is_visible():
                        btn.click()
                        skipped = True
                        break
                except Exception:
                    pass
            if not skipped:
                print("  Could not skip automatically — please navigate manually then press Enter.")
                input("  Press Enter when done: ")
        return

    if is_typing_exercise(page):
        print("  [Skip] Typing exercise detected — skipping.")
        response = ask_user("Typing exercise found. Press Enter to try clicking Skip/Next, or type 'skip'.")
        if response != "skip":
            skipped = False
            for sel in ["button:has-text('Skip')", "[data-qa='skip']", "button:has-text('Continue')"]:
                try:
                    btn = page.locator(sel).first
                    if btn.is_visible():
                        btn.click()
                        skipped = True
                        break
                except Exception:
                    pass
            if not skipped:
                print("  Could not skip automatically — please navigate manually then press Enter.")
                input("  Press Enter when done: ")
        return

    if is_matching_exercise(page):
        handle_matching(page)
        return

    if is_multiple_choice(page):
        handle_multiple_choice(page)
        return

    # Unknown screen — ask the user
    print("  [Unknown] Don't recognise this screen.")
    ask_user("Unknown activity type. Please complete it manually or navigate to the next one.")


# ── Main loop ─────────────────────────────────────────────────────────────────

def run():
    print("\nRosetta Stone Automation Tool")
    print("="*60)
    print("Starting browser... you will log in manually.")
    print("="*60)

    with sync_playwright() as p:
        # Launch a visible (non-headless) browser
        browser = p.chromium.launch(headless=False, slow_mo=50)
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        # ── Step 1: Open Rosetta Stone ────────────────────────────────────────
        print(f"\n[1/4] Opening {ROSETTA_URL} ...")
        page.goto(ROSETTA_URL)
        wait(2)

        # ── Step 2: Wait for manual login ────────────────────────────────────
        print(f"\n[2/4] Please log in to Rosetta Stone in the browser window.")
        print(f"      You have {LOGIN_TIMEOUT} seconds.")
        print("      After logging in, press Enter here to continue.\n")
        input("  Press Enter after you have logged in: ")
        wait(3)

        print("\n[3/4] Starting lesson automation. Press Ctrl+C at any time to stop.\n")

        # ── Step 3: Main automation loop ──────────────────────────────────────
        lessons_done = 0
        max_screens   = 500   # safety limit so it doesn't run forever

        for screen_number in range(max_screens):
            print(f"\n--- Screen {screen_number + 1} ---")
            print(f"    URL: {page.url}")

            try:
                complete_current_screen(page)
            except KeyboardInterrupt:
                print("\n\nStopped by user.")
                break
            except Exception as e:
                print(f"  [Error] {e}")
                ask_user("An error occurred. Please fix anything needed then press Enter to continue.")

            wait(1)

            # Check if we finished an exercise and need to find the next lesson
            if "lesson" not in page.url.lower() and "activity" not in page.url.lower():
                print("  [Nav] Not inside a lesson — looking for next incomplete lesson...")
                found = find_and_click_incomplete_lesson(page)
                if not found:
                    print("  [Nav] No incomplete lessons found on this page.")
                    response = ask_user(
                        "No incomplete lessons found. Navigate to a new unit/lesson manually, "
                        "then press Enter to continue. Type 'quit' to exit."
                    )
                    if response == "quit":
                        break
                else:
                    lessons_done += 1
                    print(f"  [Nav] Opened lesson #{lessons_done}.")

        print(f"\n[4/4] Done! Completed {lessons_done} lesson(s).")
        print("Closing browser in 5 seconds...")
        wait(5)
        browser.close()


if __name__ == "__main__":
    run()
