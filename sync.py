#!/usr/bin/env python3
"""
Dominion MLS Sync — automated daily export from Paragon MLS.

Logs into CARMLS Paragon, loads the saved search "DOMINION - DAILY EXPIREDS",
sets the date to yesterday, exports CSV, and uploads to Dominion's Supabase
staging table.

Usage:
  python sync.py --setup     # first-time setup (credentials, Chromium, cron)
  python sync.py             # run sync now
  python sync.py --visible   # run with visible browser (for debugging)

Selectors based on Paragon current DOM (recorded 2026-07-15).
Re-record if Paragon updates UI: playwright codegen https://carmls.paragonrels.com
"""

import asyncio
import calendar
import csv
import getpass
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page, TimeoutError as PWTimeout
from supabase import create_client

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

PARAGON_LOGIN_URL = "https://carmls.paragonrels.com/ParagonLS/Default.mvc/Login"
PARAGON_HOME_URL  = "https://carmls.paragonrels.com/ParagonLS/Default.mvc"
SAVED_SEARCH_NAME = "DOMINION - DAILY EXPIREDS"
DOWNLOAD_DIR = ROOT / "data" / "downloads"
LOG_DIR = ROOT / "data" / "logs"
NAV_TIMEOUT = 30_000    # 30s
DOWNLOAD_TIMEOUT = 60_000  # 60s

logger = logging.getLogger("mls_sync")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class SyncError(Exception):
    """Sync failure with diagnostic context."""

    def __init__(self, message: str, step: str, suggestion: str, screenshot_path: str | None = None):
        super().__init__(message)
        self.step = step
        self.suggestion = suggestion
        self.screenshot_path = screenshot_path


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _setup_logging():
    if logger.handlers:
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / "sync.log"

    file_handler = logging.FileHandler(log_path)
    file_handler.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter("  %(message)s"))

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def _get_credentials() -> tuple[str, str]:
    username = os.environ.get("PARAGON_USERNAME", "")
    password = os.environ.get("PARAGON_PASSWORD", "")
    if not username or not password:
        raise SyncError(
            "PARAGON_USERNAME or PARAGON_PASSWORD not set",
            step="credentials",
            suggestion="Run `python sync.py --setup` to configure credentials.",
        )
    return username, password


def _get_supabase():
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key:
        raise SyncError(
            "SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set",
            step="credentials",
            suggestion="Add Supabase credentials to .env (get from Damon).",
        )
    return create_client(url, key)


# ---------------------------------------------------------------------------
# Screenshot helper
# ---------------------------------------------------------------------------

async def _screenshot_on_error(page: Page, step: str) -> str:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    path = DOWNLOAD_DIR / f"error_{step}_{ts}.png"
    try:
        await page.screenshot(path=str(path), full_page=True)
    except Exception:
        return "screenshot_failed"
    return str(path)


# ---------------------------------------------------------------------------
# Playwright automation
# ---------------------------------------------------------------------------

async def _login(page: Page, username: str, password: str):
    """Log into Paragon MLS. Handles concurrent session override automatically."""
    logger.info("Logging into Paragon...")
    await page.goto(PARAGON_LOGIN_URL, timeout=NAV_TIMEOUT)

    try:
        await page.get_by_role("textbox", name="Username").fill(username, timeout=NAV_TIMEOUT)
        await page.get_by_role("textbox", name="Password").fill(password, timeout=NAV_TIMEOUT)
    except PWTimeout:
        ss = await _screenshot_on_error(page, "login")
        raise SyncError(
            "Login form not found.",
            step="login",
            suggestion="Run: playwright codegen https://carmls.paragonrels.com",
            screenshot_path=ss,
        )

    # Click Login — may need second click to override concurrent session
    await page.get_by_role("button", name="Login").click()
    await page.wait_for_timeout(2000)

    # If still on login page (concurrent session prompt), click again
    if "Login" in page.url:
        logger.info("Concurrent session detected — overriding...")
        await page.get_by_role("button", name="Login").click()

    try:
        await page.wait_for_url(lambda url: "Login" not in url, timeout=NAV_TIMEOUT)
    except PWTimeout:
        ss = await _screenshot_on_error(page, "login")
        raise SyncError(
            "Login failed — credentials rejected or session override failed.",
            step="login",
            suggestion="Check PARAGON_USERNAME and PARAGON_PASSWORD in .env.",
            screenshot_path=ss,
        )

    # Navigate to home to trigger any post-login popups
    await page.goto(PARAGON_HOME_URL, timeout=NAV_TIMEOUT)
    await page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT)

    # Dismiss Board Messages or any modal popup
    try:
        close_btn = page.get_by_role("button", name="Close")
        if await close_btn.count() > 0:
            await close_btn.first.click(timeout=5000)
            logger.info("Closed Board Messages popup.")
    except Exception:
        pass  # No popup — fine

    logger.info("Login successful.")


async def _load_saved_search(page: Page):
    """Navigate to Saved Property Searches and open DOMINION - DAILY EXPIREDS."""
    logger.info("Opening saved search...")

    try:
        await page.locator("#search-nav").click(timeout=NAV_TIMEOUT)
        await page.locator("#app_banner_menu").get_by_text("Saved Property Searches").click(timeout=NAV_TIMEOUT)
        await page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT)
    except PWTimeout:
        ss = await _screenshot_on_error(page, "nav")
        raise SyncError(
            "Could not find Search navigation menu.",
            step="nav",
            suggestion="Paragon may have updated nav. Re-record: playwright codegen https://carmls.paragonrels.com",
            screenshot_path=ss,
        )

    # Saved searches list is inside tab1 iframe
    tab1 = page.locator('iframe[name="tab1"]').content_frame

    try:
        search_link = tab1.get_by_role("link", name=SAVED_SEARCH_NAME)
        await search_link.wait_for(timeout=NAV_TIMEOUT)
        await search_link.click()
        await page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT)
    except PWTimeout:
        ss = await _screenshot_on_error(page, "saved_search")
        raise SyncError(
            f'Saved search "{SAVED_SEARCH_NAME}" not found.',
            step="saved_search",
            suggestion=f'Create a saved search named "{SAVED_SEARCH_NAME}" in Paragon for Expired/Withdrawn listings.',
            screenshot_path=ss,
        )

    logger.info(f'Loaded saved search: {SAVED_SEARCH_NAME}')


async def _set_yesterday_date(page: Page):
    """Set the search date to yesterday using Paragon's calendar picker."""
    yesterday = datetime.now() - timedelta(days=1)
    day_str = str(yesterday.day)  # "14", "1", etc — no leading zero (calendar link text)

    logger.info(f"Setting date to yesterday: {yesterday.strftime('%Y-%m-%d')}")

    # Search form is in tab2_1_1 iframe
    form = page.locator('iframe[name="tab2_1_1"]').content_frame

    try:
        # Click "Select Date" (first one — the from-date field)
        await form.get_by_role("link", name="Select Date").first.click(timeout=NAV_TIMEOUT)
        await page.wait_for_timeout(1000)

        # If yesterday is in a different month than today (1st of month edge case),
        # click the back arrow on the calendar to go to previous month
        today = datetime.now()
        if yesterday.month != today.month:
            prev_arrow = form.locator("a.ui-datepicker-prev, .ui-datepicker-prev")
            if await prev_arrow.count() > 0:
                await prev_arrow.click()
                await page.wait_for_timeout(500)

        # Click yesterday's day number
        await form.get_by_role("link", name=day_str, exact=True).click(timeout=NAV_TIMEOUT)
        logger.info(f"Date set to {yesterday.strftime('%Y-%m-%d')}.")

    except PWTimeout:
        ss = await _screenshot_on_error(page, "date")
        raise SyncError(
            "Could not set date in search form.",
            step="date",
            suggestion="Calendar picker may have changed. Run sync.py --visible to inspect.",
            screenshot_path=ss,
        )


async def _run_search(page: Page):
    """Click Search button in the form frame."""
    form = page.locator('iframe[name="tab2_1_1"]').content_frame

    try:
        await form.get_by_role("button", name="Search").click(timeout=NAV_TIMEOUT)
        await page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT)
        logger.info("Search executed.")
    except PWTimeout:
        ss = await _screenshot_on_error(page, "search")
        raise SyncError(
            "Search button not found or results did not load.",
            step="search",
            suggestion="Run sync.py --visible to inspect the form frame.",
            screenshot_path=ss,
        )


async def _export_csv(page: Page) -> Path:
    """Select all results and export as CSV. Results live in nested iframes."""
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("Exporting CSV...")

    results_frame = page.locator('iframe[name="tab2_1_2"]').content_frame
    grid_frame = results_frame.locator('iframe[name="ifSpreadsheet"]').content_frame

    try:
        # Select all rows
        await grid_frame.locator("#cb_grid").check(timeout=NAV_TIMEOUT)
        logger.info("Selected all rows.")
    except PWTimeout:
        ss = await _screenshot_on_error(page, "select_all")
        raise SyncError(
            "Could not find results grid (#cb_grid). Search may have returned 0 results.",
            step="select_all",
            suggestion="Check if yesterday had any expired/withdrawn listings in CARMLS.",
            screenshot_path=ss,
        )

    try:
        # Export menu
        await results_frame.get_by_role("link", name="Export »").click(timeout=NAV_TIMEOUT)
        await results_frame.get_by_role("link", name="Export to CSV").click(timeout=NAV_TIMEOUT)

        # Export confirmation popup + download
        async with page.expect_download(timeout=DOWNLOAD_TIMEOUT) as download_info:
            async with page.expect_popup() as popup_info:
                await page.get_by_role("button", name="Export").click(timeout=NAV_TIMEOUT)
            popup = await popup_info.value
            await popup.close()

        download = await download_info.value

    except PWTimeout:
        ss = await _screenshot_on_error(page, "export")
        raise SyncError(
            "CSV export timed out or export controls not found.",
            step="export",
            suggestion="Run sync.py --visible to inspect the export flow.",
            screenshot_path=ss,
        )

    dest = DOWNLOAD_DIR / f"paragon_{datetime.now().strftime('%Y-%m-%d')}.csv"
    await download.save_as(str(dest))
    logger.info(f"CSV saved: {dest}")
    return dest


# ---------------------------------------------------------------------------
# CSV → Supabase staging
# ---------------------------------------------------------------------------

def _upload_to_staging(csv_path: Path) -> dict:
    """Parse CSV and upload rows to paragon_staging table in Supabase."""
    db = _get_supabase()

    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    if not rows:
        logger.info("CSV is empty — nothing to upload.")
        return {"uploaded": 0, "csv_rows": 0}

    header_map = {h.strip().lower(): h for h in rows[0].keys()}

    def get(row, *candidates):
        for c in candidates:
            for key, orig in header_map.items():
                if c in key:
                    val = row.get(orig, "").strip()
                    if val:
                        return val
        return ""

    staged = []
    for row in rows:
        address = get(row, "address", "street")
        if not address:
            continue

        staged.append({
            "address": address,
            "city": get(row, "city"),
            "state": get(row, "state") or "AR",
            "zip": get(row, "zip", "postal"),
            "price": get(row, "price", "list price"),
            "mls_number": get(row, "mls #", "mls number", "mls id"),
            "status": get(row, "status"),
            "dom": get(row, "dom", "days on market"),
            "beds": get(row, "beds", "bedroom"),
            "baths": get(row, "baths", "full bath", "bathroom"),
            "sqft": get(row, "sqft", "sq ft", "square"),
            "year_built": get(row, "year", "yrb"),
            "acreage": get(row, "acr", "acre"),
            "property_type": get(row, "type", "property type"),
            "list_office": get(row, "list ofc", "list office", "listing office"),
            "synced_at": datetime.now().isoformat(),
            "csv_date": datetime.now().strftime("%Y-%m-%d"),
        })

    if not staged:
        logger.info("No valid rows found in CSV.")
        return {"uploaded": 0, "csv_rows": len(rows)}

    db.table("paragon_staging").upsert(
        staged,
        on_conflict="mls_number",
    ).execute()

    logger.info(f"Uploaded {len(staged)} rows to paragon_staging.")
    return {"uploaded": len(staged), "csv_rows": len(rows)}


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def _cleanup_old(directory: Path, keep_days: int = 7):
    if not directory.exists():
        return
    cutoff = datetime.now() - timedelta(days=keep_days)
    for path in directory.iterdir():
        if path.stem.startswith("paragon_") or path.stem.startswith("error_"):
            try:
                mtime = datetime.fromtimestamp(path.stat().st_mtime)
                if mtime < cutoff:
                    path.unlink()
                    logger.info(f"Cleaned up: {path.name}")
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Main sync
# ---------------------------------------------------------------------------

async def sync(headless: bool = True) -> dict:
    """Full sync: login → saved search → set date → export CSV → upload to Supabase."""
    _setup_logging()

    try:
        username, password = _get_credentials()
        logger.info("=" * 40)
        logger.info("Starting Paragon MLS sync...")

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=headless)
            context = await browser.new_context(accept_downloads=True)
            page = await context.new_page()
            page.set_default_timeout(NAV_TIMEOUT)

            await _login(page, username, password)
            await _load_saved_search(page)
            await _set_yesterday_date(page)
            await _run_search(page)
            csv_path = await _export_csv(page)

            await browser.close()

        upload_result = _upload_to_staging(csv_path)
        logger.info(f"Done! {upload_result['uploaded']} leads staged for Dominion.")

        _cleanup_old(DOWNLOAD_DIR)

        return {"csv_path": str(csv_path), **upload_result}

    except SyncError as e:
        log_msg = (
            f"{e}\n"
            f"  Screenshot: {e.screenshot_path or 'none'}\n"
            f"  Step: {e.step}\n"
            f"  Suggestion: {e.suggestion}"
        )
        logger.error(log_msg)
        print(f"\n  SYNC FAILED: {e}")
        print(f"  Step: {e.step}")
        print(f"  Fix: {e.suggestion}")
        if e.screenshot_path:
            print(f"  Screenshot: {e.screenshot_path}")
        return {"error": str(e)}
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        print(f"\n  SYNC FAILED: {e}")
        print(f"  Check data/logs/sync.log for details.")
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Setup wizard
# ---------------------------------------------------------------------------

async def _test_login(username: str, password: str) -> bool:
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await _login(page, username, password)
            await browser.close()
        return True
    except Exception:
        return False


def _write_env(username: str, password: str, supabase_url: str, supabase_key: str):
    env_path = ROOT / ".env"
    lines = []
    if env_path.exists():
        existing = env_path.read_text().splitlines()
        lines = [l for l in existing
                 if not l.startswith("PARAGON_") and not l.startswith("SUPABASE_")]

    lines.append("")
    lines.append("# Paragon MLS (CARMLS)")
    lines.append(f"PARAGON_USERNAME={username}")
    lines.append(f"PARAGON_PASSWORD={password}")
    lines.append("")
    lines.append("# Dominion Supabase")
    lines.append(f"SUPABASE_URL={supabase_url}")
    lines.append(f"SUPABASE_SERVICE_ROLE_KEY={supabase_key}")
    lines.append("")

    env_path.write_text("\n".join(lines))


def _install_cron():
    project_dir = ROOT.resolve()
    venv_python = Path(sys.executable).resolve()

    cron_line = f"0 6 * * * cd {project_dir} && {venv_python} sync.py >> data/logs/cron.log 2>&1"

    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    existing = result.stdout if result.returncode == 0 else ""

    if "dominion-mls-sync" in existing or "sync.py" in existing:
        print("  Cron job already installed.")
        return

    new_crontab = existing.rstrip() + "\n# dominion-mls-sync\n" + cron_line + "\n"
    subprocess.run(["crontab", "-"], input=new_crontab, text=True, check=True)
    print(f"  Cron installed: daily at 6 AM CT")


async def setup():
    """Interactive first-time setup."""
    print()
    print("=" * 45)
    print("  DOMINION MLS SYNC — FIRST-TIME SETUP")
    print("=" * 45)
    print()

    print("Step 1: Paragon MLS credentials")
    print("  (Your CARMLS login for carmls.paragonrels.com)")
    print()
    username = input("  Paragon username: ").strip()
    password = getpass.getpass("  Paragon password: ").strip()

    if not username or not password:
        print("\n  ERROR: Username and password are required.")
        return

    print()
    print("Step 2: Dominion Supabase connection")
    print("  (Get these from Damon)")
    print()
    supabase_url = input("  Supabase URL: ").strip()
    supabase_key = input("  Supabase service role key: ").strip()

    if not supabase_url or not supabase_key:
        print("\n  ERROR: Supabase credentials are required.")
        return

    _write_env(username, password, supabase_url, supabase_key)
    print("\n  Credentials saved to .env")

    os.environ["PARAGON_USERNAME"] = username
    os.environ["PARAGON_PASSWORD"] = password
    os.environ["SUPABASE_URL"] = supabase_url
    os.environ["SUPABASE_SERVICE_ROLE_KEY"] = supabase_key

    print("\nStep 3: Installing browser...")
    subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
    print("  Chromium installed.")

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    print("\nStep 4: Testing Paragon login...")
    if await _test_login(username, password):
        print("  Login successful!")
    else:
        print("  WARNING: Login failed. Check credentials.")
        print("  Re-run: python sync.py --setup")
        return

    print()
    install = input("Step 5: Install daily cron job (6 AM)? [y/N]: ").strip().lower()
    if install == "y":
        _install_cron()
    else:
        print("  Skipped. Run manually: python sync.py")

    print()
    print("=" * 45)
    print("  SETUP COMPLETE!")
    print("  Test now: python sync.py")
    print("=" * 45)
    print()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if "--setup" in sys.argv:
        asyncio.run(setup())
    else:
        headless = "--visible" not in sys.argv
        result = asyncio.run(sync(headless=headless))
        if not result.get("error"):
            print()
            print(f"  Sync complete!")
            print(f"  CSV: {result.get('csv_path', 'n/a')}")
            print(f"  Rows uploaded: {result.get('uploaded', 0)}")
            print()
