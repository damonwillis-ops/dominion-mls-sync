#!/usr/bin/env python3
"""
Dominion MLS Sync — automated daily export from Paragon MLS.

Logs into CARMLS Paragon, searches for expired/withdrawn listings,
exports CSV, and uploads rows to Dominion's Supabase staging table.

Usage:
  python sync.py --setup     # first-time setup (credentials, Chromium, cron)
  python sync.py             # run sync now
  python sync.py --visible   # run with visible browser (for debugging)

Selectors are based on Paragon's current DOM. If Paragon redesigns their UI,
re-record selectors with: playwright codegen https://ims.paragonrels.com
"""

import asyncio
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

PARAGON_LOGIN_URL = "https://ims.paragonrels.com/ParagonLS/default.mvc/Login"
DOWNLOAD_DIR = ROOT / "data" / "downloads"
LOG_DIR = ROOT / "data" / "logs"
NAV_TIMEOUT = 30_000   # 30s
DOWNLOAD_TIMEOUT = 60_000  # 60s

logger = logging.getLogger("mls_sync")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class SyncError(Exception):
    """Sync failure with diagnostic context for Claude to parse."""

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
    path = DOWNLOAD_DIR / f"error_{ts}.png"
    try:
        await page.screenshot(path=str(path))
    except Exception:
        return "screenshot_failed"
    return str(path)


# ---------------------------------------------------------------------------
# Playwright automation
# ---------------------------------------------------------------------------

async def _login(page: Page, username: str, password: str):
    """Log into Paragon MLS."""
    logger.info("Logging into Paragon...")
    await page.goto(PARAGON_LOGIN_URL, timeout=NAV_TIMEOUT)

    try:
        await page.fill("#UserName", username, timeout=NAV_TIMEOUT)
        await page.fill("#Password", password, timeout=NAV_TIMEOUT)
    except PWTimeout:
        ss = await _screenshot_on_error(page, "login")
        raise SyncError(
            "Login form selectors not found — Paragon may have changed their login page.",
            step="login",
            suggestion="Run `playwright codegen https://ims.paragonrels.com` to inspect current selectors.",
            screenshot_path=ss,
        )

    await page.click("input[type='submit'], button[type='submit']", timeout=NAV_TIMEOUT)

    try:
        await page.wait_for_url(lambda url: "Login" not in url, timeout=NAV_TIMEOUT)
    except PWTimeout:
        ss = await _screenshot_on_error(page, "login")
        error_text = await page.text_content(".validation-summary-errors, .error-message") or ""
        raise SyncError(
            f"Login failed — credentials rejected. {error_text.strip()}",
            step="login",
            suggestion="Check PARAGON_USERNAME and PARAGON_PASSWORD in .env.",
            screenshot_path=ss,
        )

    logger.info("Login successful.")


async def _run_search(page: Page):
    """Navigate to search, set expired/withdrawn criteria, execute."""
    logger.info("Setting search criteria...")

    await page.click("text=Search", timeout=NAV_TIMEOUT)
    await page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT)

    # Status = Expired + Withdrawn
    try:
        status_selector = "[data-field='Status'], #Status, select[name*='Status']"
        await page.click(status_selector, timeout=NAV_TIMEOUT)

        for status_val in ["Expired", "Withdrawn", "EXP", "WITH"]:
            option = page.locator(f"text='{status_val}'")
            if await option.count() > 0:
                await option.first.click()
    except PWTimeout:
        ss = await _screenshot_on_error(page, "search")
        raise SyncError(
            "Could not find Status field in search form.",
            step="search",
            suggestion="Paragon updated search UI. Run `playwright codegen https://ims.paragonrels.com` to re-record selectors.",
            screenshot_path=ss,
        )

    # Date range — yesterday to today (daily delta)
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%m/%d/%Y")
    today = datetime.now().strftime("%m/%d/%Y")

    date_fields = page.locator("input[data-field*='Date'], input[name*='Date'], input[placeholder*='date']")
    if await date_fields.count() >= 2:
        await date_fields.nth(0).fill(yesterday)
        await date_fields.nth(1).fill(today)
    else:
        logger.warning("Could not find date range fields — searching without date filter.")

    # Property Type = Residential
    res_option = page.locator("text='Residential'")
    if await res_option.count() > 0:
        await res_option.first.click()

    # Execute search
    search_btn = page.locator("button:has-text('Search'), input[value='Search'], a:has-text('Search')")
    await search_btn.first.click(timeout=NAV_TIMEOUT)

    try:
        await page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT)
    except PWTimeout:
        ss = await _screenshot_on_error(page, "search")
        raise SyncError(
            "Search results did not load.",
            step="search",
            suggestion="MLS may be slow. Try again: python sync.py",
            screenshot_path=ss,
        )

    logger.info("Search complete, results loaded.")


async def _export_csv(page: Page) -> Path:
    """Select all results and export as CSV."""
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("Exporting CSV...")

    # Select all
    select_all = page.locator("input[type='checkbox'][id*='SelectAll'], text='Select All', input[title*='Select All']")
    if await select_all.count() > 0:
        await select_all.first.click()

    # Export — trigger click inside expect_download
    export_btn = page.locator("text='Export', text='Download', button:has-text('Export'), a:has-text('Export')")

    try:
        async with page.expect_download(timeout=DOWNLOAD_TIMEOUT) as download_info:
            try:
                await export_btn.first.click(timeout=NAV_TIMEOUT)
            except PWTimeout:
                ss = await _screenshot_on_error(page, "export")
                raise SyncError(
                    "Export button not found.",
                    step="export",
                    suggestion="Paragon changed export flow. Run `playwright codegen` to re-record.",
                    screenshot_path=ss,
                )

            # Format selection dialog — pick CSV
            csv_option = page.locator("text='CSV', input[value='CSV'], option[value='CSV']")
            if await csv_option.count() > 0:
                await csv_option.first.click()

            # Confirm button
            confirm = page.locator("button:has-text('Download'), button:has-text('OK'), button:has-text('Export')")
            if await confirm.count() > 0:
                await confirm.first.click()

        download = await download_info.value
    except PWTimeout:
        ss = await _screenshot_on_error(page, "export")
        raise SyncError(
            "CSV download timed out (60s).",
            step="export",
            suggestion="MLS may be slow. Try again: python sync.py",
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

    # Normalize headers (lowercase, strip)
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

    # Upsert to staging table (mls_number is the dedup key)
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
    """Full sync: login → search → export CSV → upload to Supabase."""
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
            await _run_search(page)
            csv_path = await _export_csv(page)

            await browser.close()

        # Upload to Supabase staging
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

    # 1. Paragon credentials
    print("Step 1: Paragon MLS credentials")
    print("  (Your CARMLS login for ims.paragonrels.com)")
    print()
    username = input("  Paragon username: ").strip()
    password = getpass.getpass("  Paragon password: ").strip()

    if not username or not password:
        print("\n  ERROR: Username and password are required.")
        return

    # 2. Supabase credentials
    print()
    print("Step 2: Dominion Supabase connection")
    print("  (Get these from Damon)")
    print()
    supabase_url = input("  Supabase URL: ").strip()
    supabase_key = input("  Supabase service role key: ").strip()

    if not supabase_url or not supabase_key:
        print("\n  ERROR: Supabase credentials are required.")
        return

    # 3. Write .env
    _write_env(username, password, supabase_url, supabase_key)
    print("\n  Credentials saved to .env")

    # Set for current session
    os.environ["PARAGON_USERNAME"] = username
    os.environ["PARAGON_PASSWORD"] = password
    os.environ["SUPABASE_URL"] = supabase_url
    os.environ["SUPABASE_SERVICE_ROLE_KEY"] = supabase_key

    # 4. Install Chromium
    print("\nStep 3: Installing browser...")
    subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
    print("  Chromium installed.")

    # 5. Create directories
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    # 6. Test login
    print("\nStep 4: Testing Paragon login...")
    if await _test_login(username, password):
        print("  Login successful!")
    else:
        print("  WARNING: Login failed. Check credentials.")
        print("  Re-run: python sync.py --setup")
        return

    # 7. Cron
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
