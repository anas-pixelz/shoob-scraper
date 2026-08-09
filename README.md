# Shoob Scraper

Two-step Shoob card scraper using Selenium/undetected-chromedriver and MongoDB Atlas.

## Local

```bash
pip install -r requirements.txt
cp .env.example .env
python shoob_scraper.py
```

Set `HEADLESS=false` locally if you want to see Chrome.

## MongoDB

Create a MongoDB Atlas database and add the connection string as `MONGODB_URI`.

Collections used:

- `cards` — scraped cards
- `scraper_state` — resumable page progress

Cards are upserted by `source_url`, so reruns do not create duplicates.

## GitHub Actions

Add this repository secret:

`MONGODB_URI`

The workflow runs every 6 hours and can also be started manually.
