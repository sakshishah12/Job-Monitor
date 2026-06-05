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

---

## Quick Start (Local)

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
cp .env.example .env            # configure Telegram/Email
python main.py
```

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

The workflow runs every 6 hours and caches `data/seen_jobs.db` between runs.

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
