# Dominion MLS Sync

Automated daily export of expired/withdrawn listings from CARMLS Paragon MLS.

Runs on your Mac Mini, pulls listings every morning at 6 AM, and pushes them to Dominion's database automatically.

---

## Quick Setup (5 minutes)

### 1. Open Terminal

Press `Cmd + Space`, type "Terminal", hit Enter.

### 2. Clone this repo

```bash
git clone https://github.com/YOUR_ORG/dominion-mls-sync.git
cd dominion-mls-sync
```

### 3. Set up Python environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 4. Run setup wizard

```bash
python sync.py --setup
```

This will ask you for:
- **Paragon username/password** — your CARMLS login (same one you use at ims.paragonrels.com)
- **Supabase URL and key** — Damon will send you these

It will then:
- Install the browser engine (one-time, ~200MB)
- Test your Paragon login
- Offer to set up the daily cron job (6 AM)

### 5. Test it

```bash
python sync.py
```

You should see it log in, search, export, and upload. If it works, you're done!

---

## Daily Operation

Once set up, the cron job runs at **6 AM daily** — no action needed.

Leads appear in Dominion's pipeline automatically.

### Check if it's running

```bash
crontab -l
```

You should see a line with `sync.py` in it.

### Run manually

```bash
cd ~/dominion-mls-sync
source .venv/bin/activate
python sync.py
```

### Run with visible browser (debugging)

```bash
python sync.py --visible
```

This opens a real browser window so you can watch the automation.

---

## If Something Breaks

Errors are logged to `data/logs/sync.log` with screenshots.

**Easiest fix:** Open Claude and say:

> "Read `data/logs/sync.log` in my dominion-mls-sync folder and fix the issue."

### Common issues

| Problem | Fix |
|---|---|
| Login failed | Check username/password: `python sync.py --setup` |
| Browser not found | Run: `source .venv/bin/activate && playwright install chromium` |
| Paragon changed their UI | Run: `playwright codegen https://ims.paragonrels.com` and update selectors |
| Supabase error | Check credentials with Damon |
| Cron not running | Re-run setup: `python sync.py --setup` |

---

## Updating

When Damon pushes updates:

```bash
cd ~/dominion-mls-sync
git pull
source .venv/bin/activate
pip install -r requirements.txt
```

No need to re-run setup unless told otherwise.
