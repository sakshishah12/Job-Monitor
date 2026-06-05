# Job Monitor — FAANG Career Page Scraper

Monitors career pages for **Microsoft, Apple, Google, and Amazon**, filters jobs by your keywords, deduplicates via SQLite, and sends real-time alerts via **Telegram** and/or **Email**.

Runs locally or on a free **GitHub Actions** cron schedule (every 6 hours).

## Folder Structure

```
scrap-jobs/
├── .github/
│   └── workflows/
│       └── job_scraper.yml    # GitHub Actions cron workflow
├── .env.example               # Environment variable template
├── .gitignore
├── config.json                # Keywords, companies, scrape settings
├── database.py                # SQLite duplicate prevention
├── main.py                    # Entry point
├── notifier.py                # Telegram + Email alerts
├── requirements.txt
├── scraper.py                 # Company-specific scrapers
├── data/
│   └── seen_jobs.db           # Auto-created SQLite database
└── README.md
```

## Scraping Strategy

| Company   | Method | Why |
|-----------|--------|-----|
| Amazon    | `GET /en/search.json` REST API | Returns full JSON; no browser needed |
| Microsoft | `GET /api/pcsx/search` PCSX API | Hidden API used by apply.careers.microsoft.com |
| Google    | Parse `AF_initDataCallback` JSON embedded in HTML | Server-rendered job data in page source |
| Apple     | BeautifulSoup HTML parsing | Server-rendered listings (20/page); REST API deprecated |

**JobSpy** is not used — it targets job boards (LinkedIn, Indeed, etc.), not direct company career pages.

**Playwright** is not required — all four companies expose data via HTTP requests.

## Quick Start (Local)

### 1. Clone and install

```bash
git clone https://github.com/YOUR_USERNAME/scrap-jobs.git
cd scrap-jobs
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Configure keywords and companies

Edit `config.json`:

```json
{
  "keywords": ["AI", "Machine Learning", "FastAPI", "New Grad", "2026"],
  "scrape_settings": {
    "max_pages_per_company": 5,
    "results_per_page": 20,
    "delay_min_seconds": 2,
    "delay_max_seconds": 6
  }
}
```

- `max_pages_per_company` — pages fetched per company per run (20 jobs/page for most sites)
- `delay_min/max_seconds` — randomized pause between requests (anti-bot)

### 3. Set up notifications

```bash
cp .env.example .env
```

Edit `.env`:

```env
ENABLE_TELEGRAM=true
ENABLE_EMAIL=false

TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
TELEGRAM_CHAT_ID=987654321
```

#### Telegram setup

1. Open Telegram, search for **@BotFather**
2. Send `/newbot`, follow prompts, copy the **bot token**
3. Search for **@userinfobot**, send `/start`, copy your **chat ID**
4. Send any message to your new bot first (required before it can message you)

#### Gmail setup

1. Enable [2-Step Verification](https://myaccount.google.com/security) on your Google account
2. Go to [App Passwords](https://myaccount.google.com/apppasswords)
3. Create an app password for "Mail"
4. Set in `.env`:

```env
ENABLE_EMAIL=true
EMAIL_SENDER=you@gmail.com
EMAIL_PASSWORD=your_16_char_app_password
EMAIL_RECIPIENT=you@gmail.com
```

### 4. Run locally

```bash
python main.py
```

Expected output:

```
2026-06-05 10:00:00 [INFO] job_monitor: Starting job monitor — 4 companies, 5 keywords
2026-06-05 10:00:01 [INFO] scraper: [Amazon] Scraped 100 jobs
2026-06-05 10:00:05 [INFO] job_monitor: NEW: [Amazon] ML Engineer — keywords: AI, Machine Learning
...
```

On first run, matching jobs trigger alerts. Subsequent runs only alert on **new** listings.

---

## GitHub Actions Deployment (Free)

### 1. Push to GitHub

```bash
git init
git add .
git commit -m "Add FAANG job monitor"
git remote add origin https://github.com/YOUR_USERNAME/scrap-jobs.git
git push -u origin main
```

### 2. Add repository secrets

Go to your repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

Add these secrets:

| Secret Name | Required When | Value |
|-------------|---------------|-------|
| `ENABLE_TELEGRAM` | Telegram alerts | `true` or `false` |
| `ENABLE_EMAIL` | Email alerts | `true` or `false` |
| `TELEGRAM_BOT_TOKEN` | Telegram enabled | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | Telegram enabled | Your chat ID from @userinfobot |
| `EMAIL_SMTP_HOST` | Email enabled | `smtp.gmail.com` (optional, this is the default) |
| `EMAIL_SMTP_PORT` | Email enabled | `587` (optional) |
| `EMAIL_SENDER` | Email enabled | Your Gmail address |
| `EMAIL_PASSWORD` | Email enabled | Gmail App Password (not your login password) |
| `EMAIL_RECIPIENT` | Email enabled | Where to send alerts |

> **Security:** Never commit `.env` to git. Secrets are encrypted by GitHub and only exposed to the workflow at runtime.

### 3. Enable the workflow

The workflow at `.github/workflows/job_scraper.yml` runs automatically every 6 hours.

To trigger manually: **Actions** tab → **Job Scraper** → **Run workflow**.

### 4. How duplicate prevention works in CI

- The SQLite database (`data/seen_jobs.db`) is cached between runs via `actions/cache`
- A backup artifact is uploaded after each run
- Jobs are keyed by `SHA256(company + job_id)` — you won't get repeat alerts

---

## Customization

### Add more keywords

```json
"keywords": ["AI", "LLM", "Python", "Backend", "2026", "Intern"]
```

Matching is case-insensitive across **title**, **location**, and **description**.

### Disable a company

```json
{
  "name": "Apple",
  "enabled": false,
  ...
}
```

### Change scrape frequency

Edit the cron in `.github/workflows/job_scraper.yml`:

```yaml
# Every 4 hours
- cron: "0 */4 * * *"

# Every day at 8 AM UTC
- cron: "0 8 * * *"
```

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| No alerts on first run | Check keywords match actual job titles; try broader terms like "Software" |
| Telegram "chat not found" | Send a message to your bot first, verify `TELEGRAM_CHAT_ID` |
| Gmail auth failed | Use an App Password, not your regular Gmail password |
| Scraper returns 0 jobs | Company may have changed their site; check logs for that company |
| GitHub Action repeats alerts | Ensure cache step is present; check `data/seen_jobs.db` artifact |

---

## License

MIT
