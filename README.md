# Job Monitor — ATS JSON API Scraper

Monitors career pages at top tech/AI companies via **hidden JSON APIs** (Greenhouse, Ashby, Workday, etc.), filters jobs against your **candidate profile**, deduplicates with SQLite, and sends **Telegram/Email** alerts.

Runs locally or on a free **GitHub Actions** cron (every 6 hours).

## Folder Structure

```
scrap-jobs/
├── .github/workflows/job_scraper.yml
├── config.json              # Profile filter + company list (ATS config)
├── filters.py               # Profile-based include/exclude logic
├── scraper.py               # Greenhouse / Ashby / Workday / Amazon / Microsoft
├── database.py              # SQLite duplicate prevention
├── notifier.py              # Telegram + Email alerts
├── main.py                  # Entry point
├── requirements.txt
└── data/seen_jobs.db        # Auto-created
```

## Scraping Strategy (JSON APIs only — no HTML parsing)

| ATS | Scraper key | API endpoint | Companies in config |
|-----|-------------|--------------|---------------------|
| **Greenhouse** | `greenhouse` | `boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true` | Datadog, Stripe, Anthropic, Glean, Databricks, Scale AI, MongoDB, Cloudflare |
| **Ashby** | `ashby` | `api.ashbyhq.com/posting-api/job-board/{slug}` | OpenAI, Snowflake, Cohere, Perplexity, Cursor |
| **Workday** | `workday` | `{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs` | Salesforce |
| **Custom** | `amazon` / `microsoft` | Internal JSON APIs | Amazon, Microsoft |

> **Intuit** is included but disabled — it uses Phenom People with no public JSON API (requires OAuth from Phenom).

## Profile Filtering

Edit `profile_filter` in `config.json`:

```json
{
  "profile_filter": {
    "must_include_keywords": ["Python", "FastAPI", "Machine Learning", "Backend", "..."],
    "target_roles": ["New Grad", "Software Engineer", "AI Engineer", "2026", "..."],
    "exclude_keywords": ["Staff", "Principal", "Director", "Manager", "Senior"]
  }
}
```

**Logic:**
1. **Exclude** — if any exclude term appears in the **title**, the job is rejected (word-boundary match).
2. **Target roles** — at least one role keyword must appear in the title (falls back to full text).
3. **Must include** — at least one skill keyword must appear in title + description + location.
4. **Posting date** — when `posting_date_filter.enabled` is true, only jobs inside the configured posting-date window pass.

Example posting-date filter:

```json
{
  "posting_date_filter": {
    "enabled": true,
    "posted_within_days": 14,
    "posted_after": "",
    "posted_before": "",
    "require_posted_at": true
  }
}
```

---

## How to Add a New Company

### Step 1 — Find the ATS type

Open the company's careers page → DevTools → **Network** tab → filter by `Fetch/XHR`. Look for:

| If you see requests to… | ATS type | Config key |
|-------------------------|----------|------------|
| `boards-api.greenhouse.io` | Greenhouse | `greenhouse` |
| `api.ashbyhq.com/posting-api` | Ashby | `ashby` |
| `*.myworkdayjobs.com/wday/cxs` | Workday | `workday` |
| `api.smartrecruiters.com` | SmartRecruiters | *(not yet supported — open an issue)* |
| `api.lever.co` | Lever | *(not yet supported)* |

### Step 2 — Find the board slug / Workday tenant

**Greenhouse:** slug is in the URL or API path:
```
https://boards.greenhouse.io/datadog  →  board_slug: "datadog"
```

**Ashby:** slug is in the API URL:
```
https://api.ashbyhq.com/posting-api/job-board/openai  →  board_slug: "openai"
```

**Workday:** parse from the careers URL:
```
https://salesforce.wd12.myworkdayjobs.com/en-US/External_Career_Site
  → tenant: "salesforce", wd_server: "wd12", site: "External_Career_Site"
```

**Quick test (Greenhouse):**
```bash
curl "https://boards-api.greenhouse.io/v1/boards/YOUR_SLUG/jobs?content=true" | head
```

### Step 3 — Add to `config.json`

**Greenhouse example:**
```json
{
  "name": "Figma",
  "enabled": true,
  "scraper": "greenhouse",
  "board_slug": "figma",
  "careers_url": "https://www.figma.com/careers/"
}
```

**Ashby example:**
```json
{
  "name": "Replit",
  "enabled": true,
  "scraper": "ashby",
  "board_slug": "replit",
  "careers_url": "https://replit.com/careers"
}
```

**Workday example:**
```json
{
  "name": "NVIDIA",
  "enabled": true,
  "scraper": "workday",
  "tenant": "nvidia",
  "wd_server": "wd5",
  "site": "NVIDIAExternalCareerSite",
  "careers_url": "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite"
}
```

### Step 4 — Test locally

```bash
python main.py
```

Check logs for `[CompanyName] Done — scraped: X, profile matches: Y`.

To send one email containing every matching job already saved in `data/seen_jobs.db`:

```bash
python main.py --email-all-seen
```

Normal runs send at most one email digest containing the new jobs found during that run.

To scrape normally and send a final email even for matching jobs that were already seen:

```bash
python main.py --email-sent yes
```

---

## Quick Start (Local)

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
cp .env.example .env            # configure Telegram/Email
python main.py
```

## Application Tracker from Gmail

The application tracker scans Gmail confirmation emails such as "thank you for applying" and writes a deduplicated spreadsheet-style CSV to `data/applications.csv`.

1. Create a Gmail App Password for `sakshijs1211@gmail.com`.
2. Add these values to `.env`:

```bash
APPLICATION_EMAIL_USERNAME=sakshijs1211@gmail.com
APPLICATION_EMAIL_APP_PASSWORD=your_gmail_app_password
APPLICATION_IMAP_HOST=imap.gmail.com
APPLICATION_IMAP_MAILBOX=INBOX
```

Run:

```bash
python application_tracker.py --days-back 30 --max-emails 200
```

The CSV columns include `company`, `role`, `status`, `recruiter_name`, `recruiter_email`, `hiring_manager_name`, and `hiring_manager_email`.

For cleaner extraction, enable the optional LLM layer:

```bash
APPLICATION_LLM_ENABLED=true
APPLICATION_LLM_MODEL=gemini-flash-latest
GEMINI_API_KEY=your_gemini_api_key
```

Clean rows already saved in `data/applications.csv`:

```bash
python application_tracker.py --enrich-existing --drop-irrelevant
```

Use the LLM while scanning new Gmail confirmations:

```bash
python application_tracker.py --days-back 30 --max-emails 200 --use-llm
```

Scan or enrich only a specific received-date range:

```powershell
python application_tracker.py --since-date 2026-06-01 --until-date 2026-06-11 --max-emails 200 --use-llm
python application_tracker.py --enrich-existing --since-date 2026-06-01 --until-date 2026-06-11 --drop-irrelevant
```

Register a local Windows daily scan at 9 AM:

```powershell
.\scripts\register_daily_application_tracker.ps1
```

Use a different run time:

```powershell
.\scripts\register_daily_application_tracker.ps1 -At "18:30"
```

The daily task runs `scripts\run_application_tracker.ps1`, scans the last 2 days of Gmail, and logs to `logs\application_tracker.log`. New status emails for an existing `company + role` update the existing CSV row instead of creating a duplicate.

### Telegram setup
1. Create bot via **@BotFather** → copy token
2. Get chat ID via **@userinfobot**
3. Send a message to your bot first

### Gmail setup
1. Enable 2-Step Verification
2. Create an [App Password](https://myaccount.google.com/apppasswords)
3. Set `EMAIL_PASSWORD` to the 16-character app password

---

## GitHub Actions Deployment

Push to GitHub, then add these **repository secrets** (Settings → Secrets → Actions):

| Secret | Value |
|--------|-------|
| `ENABLE_TELEGRAM` | `true` or `false` |
| `ENABLE_EMAIL` | `true` or `false` |
| `TELEGRAM_BOT_TOKEN` | Bot token |
| `TELEGRAM_CHAT_ID` | Your chat ID |
| `EMAIL_SENDER` | Gmail address |
| `EMAIL_PASSWORD` | Gmail App Password |
| `EMAIL_RECIPIENT` | Alert recipient |

The workflow runs every 6 hours and restores the latest cached `data/seen_jobs.db` before scraping, then saves the updated DB under a new cache key for the next run.

---

## Currently Configured Companies

| Company | ATS | Status |
|---------|-----|--------|
| Salesforce | Workday | enabled |
| Snowflake | Ashby | enabled |
| Datadog | Greenhouse | enabled |
| Stripe | Greenhouse | enabled |
| Anthropic | Greenhouse | enabled |
| OpenAI | Ashby | enabled |
| Glean | Greenhouse | enabled |
| Databricks | Greenhouse | enabled |
| Scale AI | Greenhouse | enabled |
| Cohere | Ashby | enabled |
| Perplexity | Ashby | enabled |
| Cursor | Ashby | enabled |
| MongoDB | Greenhouse | enabled |
| Cloudflare | Greenhouse | enabled |
| Microsoft | Custom API | enabled |
| Amazon | Custom API | enabled |
| Intuit | Phenom | **disabled** (no public API) |

## License

MIT
