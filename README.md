# Shoob Scraper

Two-step Shoob card scraper using Selenium/undetected-chromedriver and MongoDB Atlas.

> [!IMPORTANT]
> Star and fork repo before using

> [!NOTE]
> Please Follow

## Local

```bash
pip install -r requirements.txt
cp .env.example .env
python shoob_scraper.py
```

Set `HEADLESS=false` locally if you want to see Chrome.

### Creating a GitHub Repository Secret for GitHub Actions

GitHub Secrets are encrypted environment variables that you create in an organization, repository, or repository environment. They allow you to store sensitive information—such as API keys, passwords, or certificates—safely so they can be used in your GitHub Actions workflows without being exposed in your public code. 

### Step-by-Step Instructions

1. On GitHub.com, navigate to the **main page of the repository**.
2. Under your repository name, click on the **Settings** tab (represented by a gear icon).
3. In the left sidebar, click the **Secrets and variables** dropdown menu to expand it.
4. Click on **Actions**.
5. Click the **Variables and secrets** tab if it isn't already selected, then look for the "Repository secrets" section.
6. Click the **New repository secret** button.
7. In the **Name** field, type a name for your secret (e.g., API_KEY or DISCORD_WEBHOOK).
8. In the **Secret** field, enter the sensitive value you want to protect.
9. Click **Add secret** to save it.


# For MongoDB Atlas (Cloud)
To allow global access, you must add the universal CIDR block `0.0.0.0/0` to your project's network settings.
- Log in to your MongoDB Atlas Account.In the left-hand navigation sidebar, click on Network Access under the Security section.
- Click the Add IP Address button on the right side of the screen.
- In the dialog box that appears, click the button labeled Allow Access From Anywhere.
- This will automatically fill the Access List Entry field with 0.0.0.0/0.Optional: Add a text description (e.g., "Global Access").
- Click Confirm. It will take about a minute for the status to change from Pending to Active


# MongoDB

Create a MongoDB Atlas database and add the connection string as `MONGODB_URI`.

Collections used:

- `cards` — scraped cards
- `scraper_state` — resumable page progress

Cards are upserted by `source_url`, so reruns do not create duplicates.

## GitHub Actions

Add this repository secret:

`MONGODB_URI`

The workflow runs every 2 minutes and can also be started manually.


# Telegram

- Create a Telegram Bot with [@BotFatherr](https://t.me/BotFather) and create a private Group.
- Get the group chat id with [User Info • Get ID • idbot](https://t.me/userinfobot)
- Add bot to the group and make admin will all rights.

## GitHub Actions

Add this repository secret:

`TELEGRAM_BOT_TOKEN`

`TELEGRAM_CHAT_ID`

The workflow runs every 1 hour and can also be started manually.

