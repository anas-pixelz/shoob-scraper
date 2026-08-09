import os
import re
import time
from datetime import datetime, timezone

import undetected_chromedriver as uc
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import PyMongoError
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

load_dotenv()

BASE_URL = os.getenv("BASE_URL", "https://shoob.gg/cards?page=")
START_PAGE = int(os.getenv("START_PAGE", "2"))
END_PAGE = int(os.getenv("END_PAGE", "10"))
PAGE_LOAD_TIMEOUT = int(os.getenv("PAGE_LOAD_TIMEOUT", "20"))
GALLERY_WAIT = float(os.getenv("GALLERY_WAIT", "4"))
DETAIL_WAIT = int(os.getenv("DETAIL_WAIT", "15"))
DETAIL_EXTRA_WAIT = float(os.getenv("DETAIL_EXTRA_WAIT", "1"))
HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"
SKIP_EXISTING = os.getenv("SKIP_EXISTING", "true").lower() == "true"

MONGODB_URI = os.getenv("MONGODB_URI")
MONGODB_DATABASE = os.getenv("MONGODB_DATABASE", "shoob")
MONGODB_COLLECTION = os.getenv("MONGODB_COLLECTION", "cards")
MONGODB_STATE_COLLECTION = os.getenv("MONGODB_STATE_COLLECTION", "scraper_state")

if not MONGODB_URI:
    raise RuntimeError("MONGODB_URI is missing.")

mongo_client = None
cards_collection = None
state_collection = None


def now_utc():
    return datetime.now(timezone.utc)


def clean_text(value):
    return " ".join(str(value or "").split()).strip()


def normalize_url(url):
    if not url:
        return None
    url = url.strip()
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return "https://shoob.gg" + url
    return url


def connect_mongodb():
    global mongo_client, cards_collection, state_collection

    print("\n[!] Connecting to MongoDB...")
    mongo_client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=15000)
    mongo_client.admin.command("ping")

    db = mongo_client[MONGODB_DATABASE]
    cards_collection = db[MONGODB_COLLECTION]
    state_collection = db[MONGODB_STATE_COLLECTION]

    cards_collection.create_index("source_url", unique=True)
    cards_collection.create_index("name")
    cards_collection.create_index("series")
    cards_collection.create_index("tier")

    print(f"[+] MongoDB connected: {MONGODB_DATABASE}.{MONGODB_COLLECTION}")


def get_state():
    state = state_collection.find_one({"_id": "shoob_scraper"})
    return state or {
        "next_page": START_PAGE,
        "last_completed_page": START_PAGE - 1,
    }


def save_state(next_page, last_completed_page, status="running"):
    state_collection.update_one(
        {"_id": "shoob_scraper"},
        {
            "$set": {
                "next_page": next_page,
                "last_completed_page": last_completed_page,
                "status": status,
                "updated_at": now_utc(),
            }
        },
        upsert=True,
    )


def extract_tier(text):
    text = clean_text(text)
    if not text:
        return None

    match = re.search(r"\bT(\d+)\b", text, re.I)
    if match:
        return f"T{match.group(1)}"

    match = re.search(r"\bTier\s*(\d+)\b", text, re.I)
    if match:
        return f"T{match.group(1)}"

    return None


def get_title(soup):
    # The generic page heading can be "Creators". Prefer the actual
    # card-name element and reject the site-wide "Creators" heading.
    selectors = [
        "div.text-xl.font-bold.text-center.mt-4",
        "div.text-xl.font-bold.text-center",
        "div.text-xl.font-bold",
    ]

    for selector in selectors:
        for element in soup.select(selector):
            title = clean_text(element.get_text(" ", strip=True))
            if title and title.lower() != "creators":
                return title

    for element in soup.find_all(["h1", "h2", "h3"]):
        title = clean_text(element.get_text(" ", strip=True))
        if title and title.lower() != "creators" and extract_tier(title):
            return title

    # Final fallback: find visible text containing a tier, excluding
    # navigation/site headings.
    for element in soup.find_all(["div", "span"]):
        text = clean_text(element.get_text(" ", strip=True))
        if text and len(text) < 200 and extract_tier(text):
            if text.lower() not in {"creators", "cards"}:
                return text

    return None


def parse_name_and_tier(title):
    title = clean_text(title)
    if not title:
        return None, None

    tier = extract_tier(title)
    name = title

    if tier:
        name = re.sub(
            rf"\s*(?:-|\||:)\s*{re.escape(tier)}\s*$",
            "",
            name,
            flags=re.I,
        ).strip()
        name = re.sub(
            rf"\s+{re.escape(tier)}\s*$",
            "",
            name,
            flags=re.I,
        ).strip()

    return clean_text(name), tier


def get_breadcrumb_items(soup):
    selectors = [
        "ol.breadcrumb-new",
        ".breadcrumb-new",
        ".breadcrumb",
        "nav[aria-label='breadcrumb']",
    ]

    breadcrumb = None
    for selector in selectors:
        breadcrumb = soup.select_one(selector)
        if breadcrumb:
            break

    if not breadcrumb:
        return []

    items = breadcrumb.select("li")
    if items:
        result = [clean_text(x.get_text(" ", strip=True)) for x in items]
        return [x for x in result if x]

    result = [clean_text(x.get_text(" ", strip=True)) for x in breadcrumb.find_all("a")]
    return [x for x in result if x]


def extract_series_and_tier(soup):
    items = get_breadcrumb_items(soup)
    if not items:
        return None, None

    tier = None
    tier_index = None
    for i, item in enumerate(items):
        detected = extract_tier(item)
        if detected:
            tier = detected
            tier_index = i
            break

    series = None
    if tier_index is not None and tier_index + 1 < len(items):
        candidate = items[tier_index + 1]
        if candidate.lower() not in {"cards", "card"} and not extract_tier(candidate):
            series = candidate

    if not series and len(items) >= 2:
        candidate = items[-2]
        if candidate.lower() not in {"cards", "card"} and not extract_tier(candidate):
            series = candidate

    return clean_text(series) if series else None, tier


def get_card_image(soup):
    selectors = [
        "img[src*='/images/cards/']",
        "img[data-src*='/images/cards/']",
        "img[data-lazy-src*='/images/cards/']",
        "img[data-original*='/images/cards/']",
    ]

    for selector in selectors:
        for img in soup.select(selector):
            for attr in ("src", "data-src", "data-lazy-src", "data-original"):
                url = normalize_url(img.get(attr))
                if not url:
                    continue
                lower = url.lower()
                if "shoob_logo" in lower:
                    continue
                if "/images/cards/" in lower:
                    return url

    return None


def validate_card(card):
    name = clean_text(card.get("name"))
    image = clean_text(card.get("image_url"))
    series = clean_text(card.get("series"))
    tier = clean_text(card.get("tier"))

    if not name or name.lower() in {"creators", "unknown", "unknown card"}:
        return False
    if not image or "/images/cards/" not in image.lower():
        return False
    if "shoob_logo" in image.lower():
        return False
    if not series or series.lower() == "unknown series":
        return False
    if not tier or not extract_tier(tier):
        return False
    return True


def wait_for_detail_page(driver):
    wait = WebDriverWait(driver, DETAIL_WAIT)
    try:
        wait.until(
            lambda d: d.find_elements(By.CSS_SELECTOR, "img[src*='/images/cards/']")
            or d.find_elements(By.CSS_SELECTOR, "img[data-src*='/images/cards/']")
        )
    except TimeoutException:
        pass
    time.sleep(DETAIL_EXTRA_WAIT)


def scrape_card_detail(driver, url):
    driver.get(url)
    if "/cards/info/" not in driver.current_url:
        raise ValueError(f"Unexpected detail URL: {driver.current_url}")

    wait_for_detail_page(driver)
    soup = BeautifulSoup(driver.page_source, "html.parser")

    title = get_title(soup)
    if not title:
        raise ValueError(f"Could not find real card title at {driver.current_url}")

    print(f"   Found title: {title}")

    name, title_tier = parse_name_and_tier(title)
    series, breadcrumb_tier = extract_series_and_tier(soup)
    tier = title_tier or breadcrumb_tier
    image_url = get_card_image(soup)

    card = {
        "name": name,
        "series": series,
        "tier": tier,
        "image_url": image_url,
        "source_url": driver.current_url,
    }

    if not validate_card(card):
        raise ValueError(f"Invalid card data extracted: {card}")

    return card


def card_exists(source_url):
    if not SKIP_EXISTING:
        return False
    return cards_collection.find_one({"source_url": source_url}, {"_id": 1}) is not None


def save_card(card):
    result = cards_collection.update_one(
        {"source_url": card["source_url"]},
        {
            "$set": {
                "name": card["name"],
                "series": card["series"],
                "tier": card["tier"],
                "image_url": card["image_url"],
                "updated_at": now_utc(),
            },
            "$setOnInsert": {
                "source_url": card["source_url"],
                "created_at": now_utc(),
            },
        },
        upsert=True,
    )
    if result.upserted_id:
        return "inserted"
    if result.modified_count:
        return "updated"
    return "unchanged"


def collect_gallery_urls(driver, page_num):
    driver.get(f"{BASE_URL}{page_num}")
    time.sleep(GALLERY_WAIT)

    links = driver.find_elements(By.XPATH, "//a[contains(@href, '/cards/info/')]")
    urls = []
    seen = set()

    for link in links:
        try:
            href = link.get_attribute("href")
        except Exception:
            continue
        if not href:
            continue
        href = normalize_url(href)
        if href and "/cards/info/" in href and href not in seen:
            seen.add(href)
            urls.append(href)

    return urls


def create_driver():
    options = uc.ChromeOptions()

    if HEADLESS:
        options.add_argument("--headless=new")

    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")

    return uc.Chrome(
    options=options,
    version_main=150
)


def scrape_shoob_two_step():
    connect_mongodb()

    state = get_state()
    requested_start = START_PAGE
    start_page = max(requested_start, int(state.get("next_page", requested_start)))

    if start_page > END_PAGE:
        print(f"[+] No pending pages. Resume state says next_page={start_page}.")
        print("[+] Set RESET_PROGRESS=true for a fresh full scan.")
        return

    driver = None
    successful_pages = []
    empty_pages = []
    timeout_pages = []
    inserted = updated = unchanged = failed = skipped = 0

    try:
        driver = create_driver()
        driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)

        for page_num in range(start_page, END_PAGE + 1):
            print(f"\n[!] Scanning Gallery Page {page_num}...")
            save_state(page_num, page_num - 1, "running")

            try:
                card_urls = collect_gallery_urls(driver, page_num)
            except TimeoutException:
                print(f"--> [TIMEOUT] Gallery page {page_num} timed out.")
                timeout_pages.append(page_num)
                continue
            except WebDriverException as error:
                print(f"--> [ERROR] Gallery page {page_num}: {error}")
                continue

            if not card_urls:
                print(f"--> Gallery page {page_num} returned 0 cards. Skipping...")
                empty_pages.append(page_num)
                continue

            successful_pages.append(page_num)
            print(f"Found {len(card_urls)} card pages to inspect on Page {page_num}.")

            for idx, url in enumerate(card_urls, 1):
                try:
                    print(f"\n   Navigating to details ({idx}/{len(card_urls)})...")
                    print(f"   URL: {url}")

                    if card_exists(url):
                        skipped += 1
                        print("   [MongoDB] Already exists; skipped.")
                        continue

                    card = scrape_card_detail(driver, url)
                    print(
                        f"   Extracted - Name: {card['name']}, "
                        f"Series: {card['series']}, Tier: {card['tier']}"
                    )
                    print(f"   Image: {card['image_url']}")

                    result = save_card(card)
                    if result == "inserted":
                        inserted += 1
                    elif result == "updated":
                        updated += 1
                    else:
                        unchanged += 1
                    print(f"   [MongoDB] {result}.")

                except TimeoutException:
                    failed += 1
                    print("   [TIMEOUT] Detail page timed out.")
                except PyMongoError as error:
                    failed += 1
                    print(f"   [MongoDB ERROR] {error}")
                except Exception as error:
                    failed += 1
                    print(f"   [Error] Skipping detail entry: {error}")

            # Only advance after the entire gallery page has been processed.
            save_state(page_num + 1, page_num, "running")
            total = cards_collection.count_documents({})
            print(f"Current MongoDB database size: {total} records.")

        save_state(END_PAGE + 1, END_PAGE, "completed")

    finally:
        try:
            if driver:
                driver.quit()
        except Exception:
            pass

        print("\n" + "=" * 50)
        print("          SHOOB SCRAPER SUMMARY")
        print("=" * 50)
        print(f"New Cards Inserted : {inserted}")
        print(f"Cards Updated      : {updated}")
        print(f"Cards Unchanged    : {unchanged}")
        print(f"Existing Skipped   : {skipped}")
        print(f"Failed Cards       : {failed}")
        print(f"Successful Pages   : {successful_pages}")
        print(f"Empty Pages        : {empty_pages}")
        print(f"Timed-Out Pages    : {timeout_pages}")
        if cards_collection is not None:
            print(f"MongoDB Total      : {cards_collection.count_documents({})}")
        print("=" * 50)

        try:
            if mongo_client:
                mongo_client.close()
        except Exception:
            pass


if __name__ == "__main__":
    if os.getenv("RESET_PROGRESS", "false").lower() == "true":
        connect_mongodb()
        state_collection.delete_one({"_id": "shoob_scraper"})
        mongo_client.close()
        mongo_client = None
        cards_collection = None
        state_collection = None
        print("[+] Scraper progress reset.")

    scrape_shoob_two_step()