import os
import re
import time
from datetime import datetime, timezone

import undetected_chromedriver as uc
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import PyMongoError
from selenium.common.exceptions import (
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait


# ============================================================
# ENVIRONMENT
# ============================================================

load_dotenv()


# ============================================================
# CONFIGURATION
# ============================================================

BASE_URL = os.getenv(
    "BASE_URL",
    "https://shoob.gg/cards?page="
)

# First page if no MongoDB progress exists.
START_PAGE = int(
    os.getenv("START_PAGE", "2")
)

# Absolute final page.
MAX_PAGE = int(
    os.getenv("MAX_PAGE", "2403")
)

# Number of gallery pages handled by ONE GitHub Actions run.
BATCH_SIZE = int(
    os.getenv("BATCH_SIZE", "50")
)

PAGE_LOAD_TIMEOUT = int(
    os.getenv("PAGE_LOAD_TIMEOUT", "20")
)

GALLERY_WAIT = float(
    os.getenv("GALLERY_WAIT", "4")
)

DETAIL_WAIT = int(
    os.getenv("DETAIL_WAIT", "15")
)

DETAIL_EXTRA_WAIT = float(
    os.getenv("DETAIL_EXTRA_WAIT", "1")
)

HEADLESS = (
    os.getenv(
        "HEADLESS",
        "true"
    ).lower()
    == "true"
)

SKIP_EXISTING = (
    os.getenv(
        "SKIP_EXISTING",
        "true"
    ).lower()
    == "true"
)


# ============================================================
# MONGODB CONFIGURATION
# ============================================================

MONGODB_URI = os.getenv(
    "MONGODB_URI"
)

MONGODB_DATABASE = os.getenv(
    "MONGODB_DATABASE",
    "shoob"
)

MONGODB_COLLECTION = os.getenv(
    "MONGODB_COLLECTION",
    "cards"
)

MONGODB_STATE_COLLECTION = os.getenv(
    "MONGODB_STATE_COLLECTION",
    "scraper_state"
)


if not MONGODB_URI:
    raise RuntimeError(
        "MONGODB_URI is missing."
    )


# ============================================================
# GLOBAL CONNECTIONS
# ============================================================

mongo_client = None
cards_collection = None
state_collection = None


# ============================================================
# TIME
# ============================================================

def now_utc():
    return datetime.now(
        timezone.utc
    )


# ============================================================
# TEXT HELPERS
# ============================================================

def clean_text(value):

    return " ".join(
        str(value or "").split()
    ).strip()


def normalize_url(url):

    if not url:
        return None

    url = url.strip()

    if url.startswith("//"):
        return "https:" + url

    if url.startswith("/"):
        return (
            "https://shoob.gg"
            + url
        )

    return url


# ============================================================
# MONGODB
# ============================================================

def connect_mongodb():

    global mongo_client
    global cards_collection
    global state_collection

    print(
        "\n[!] Connecting to MongoDB..."
    )

    mongo_client = MongoClient(
        MONGODB_URI,
        serverSelectionTimeoutMS=15000
    )

    # Force connection test.
    mongo_client.admin.command(
        "ping"
    )

    db = mongo_client[
        MONGODB_DATABASE
    ]

    cards_collection = db[
        MONGODB_COLLECTION
    ]

    state_collection = db[
        MONGODB_STATE_COLLECTION
    ]

    # --------------------------------------------------------
    # Card indexes
    # --------------------------------------------------------

    cards_collection.create_index(
        "source_url",
        unique=True
    )

    cards_collection.create_index(
        "name"
    )

    cards_collection.create_index(
        "series"
    )

    cards_collection.create_index(
        "tier"
    )

    # DO NOT create an explicit _id index.
    # MongoDB automatically creates the unique _id index.

    print(
        f"[+] MongoDB connected: "
        f"{MONGODB_DATABASE}."
        f"{MONGODB_COLLECTION}"
    )


# ============================================================
# SCRAPER STATE
# ============================================================

STATE_ID = "shoob_scraper"


def get_state():

    state = state_collection.find_one(
        {
            "_id": STATE_ID
        }
    )

    if state:
        return state

    initial_state = {
        "_id": STATE_ID,
        "next_page": START_PAGE,
        "last_completed_page": START_PAGE - 1,
        "max_page": MAX_PAGE,
        "status": "not_started",
        "updated_at": now_utc(),
    }

    state_collection.insert_one(
        initial_state
    )

    return initial_state


def save_state(
    next_page,
    last_completed_page,
    status="running"
):

    state_collection.update_one(
        {
            "_id": STATE_ID
        },
        {
            "$set": {
                "next_page": next_page,
                "last_completed_page":
                    last_completed_page,
                "max_page": MAX_PAGE,
                "status": status,
                "updated_at": now_utc(),
            }
        },
        upsert=True
    )


# ============================================================
# CALCULATE CURRENT BATCH
# ============================================================

def calculate_batch():

    state = get_state()

    next_page = int(
        state.get(
            "next_page",
            START_PAGE
        )
    )

    # Everything is already finished.
    if next_page > MAX_PAGE:

        return None, None

    batch_start = next_page

    batch_end = min(
        batch_start
        + BATCH_SIZE
        - 1,
        MAX_PAGE
    )

    return (
        batch_start,
        batch_end
    )


# ============================================================
# TIER
# ============================================================

def extract_tier(text):

    text = clean_text(text)

    if not text:
        return None

    match = re.search(
        r"\bT(\d+)\b",
        text,
        re.I
    )

    if match:
        return (
            f"T{match.group(1)}"
        )

    match = re.search(
        r"\bTier\s*(\d+)\b",
        text,
        re.I
    )

    if match:
        return (
            f"T{match.group(1)}"
        )

    return None


# ============================================================
# CARD TITLE
# ============================================================

def get_title(soup):

    selectors = [
        "div.text-xl.font-bold.text-center.mt-4",
        "div.text-xl.font-bold.text-center",
        "div.text-xl.font-bold",
    ]

    for selector in selectors:

        for element in soup.select(
            selector
        ):

            title = clean_text(
                element.get_text(
                    " ",
                    strip=True
                )
            )

            if (
                title
                and title.lower()
                != "creators"
            ):
                return title

    # Heading fallback.
    for element in soup.find_all(
        [
            "h1",
            "h2",
            "h3"
        ]
    ):

        title = clean_text(
            element.get_text(
                " ",
                strip=True
            )
        )

        if (
            title
            and title.lower()
            != "creators"
            and extract_tier(title)
        ):
            return title

    # Final tier-based fallback.
    for element in soup.find_all(
        [
            "div",
            "span"
        ]
    ):

        text = clean_text(
            element.get_text(
                " ",
                strip=True
            )
        )

        if (
            text
            and len(text) < 200
            and extract_tier(text)
            and text.lower()
            not in {
                "creators",
                "cards",
            }
        ):
            return text

    return None


# ============================================================
# NAME + TIER
# ============================================================

def parse_name_and_tier(title):

    title = clean_text(title)

    if not title:
        return None, None

    tier = extract_tier(
        title
    )

    name = title

    if tier:

        name = re.sub(
            rf"\s*(?:-|\||:)\s*"
            rf"{re.escape(tier)}\s*$",
            "",
            name,
            flags=re.I
        ).strip()

        name = re.sub(
            rf"\s+{re.escape(tier)}\s*$",
            "",
            name,
            flags=re.I
        ).strip()

    return (
        clean_text(name),
        tier
    )


# ============================================================
# BREADCRUMB
# ============================================================

def get_breadcrumb_items(soup):

    selectors = [
        "ol.breadcrumb-new",
        ".breadcrumb-new",
        ".breadcrumb",
        "nav[aria-label='breadcrumb']",
    ]

    breadcrumb = None

    for selector in selectors:

        breadcrumb = soup.select_one(
            selector
        )

        if breadcrumb:
            break

    if not breadcrumb:
        return []

    items = breadcrumb.select(
        "li"
    )

    if items:

        result = [
            clean_text(
                item.get_text(
                    " ",
                    strip=True
                )
            )
            for item in items
        ]

        return [
            item
            for item in result
            if item
        ]

    result = [
        clean_text(
            item.get_text(
                " ",
                strip=True
            )
        )
        for item in breadcrumb.find_all(
            "a"
        )
    ]

    return [
        item
        for item in result
        if item
    ]


# ============================================================
# SERIES + TIER
# ============================================================

def extract_series_and_tier(soup):

    items = get_breadcrumb_items(
        soup
    )

    if not items:
        return None, None

    tier = None
    tier_index = None

    for index, item in enumerate(
        items
    ):

        detected = extract_tier(
            item
        )

        if detected:

            tier = detected
            tier_index = index

            break

    series = None

    if (
        tier_index is not None
        and tier_index + 1
        < len(items)
    ):

        candidate = items[
            tier_index + 1
        ]

        if (
            candidate.lower()
            not in {
                "cards",
                "card"
            }
            and not extract_tier(
                candidate
            )
        ):

            series = candidate

    if (
        not series
        and len(items) >= 2
    ):

        candidate = items[-2]

        if (
            candidate.lower()
            not in {
                "cards",
                "card"
            }
            and not extract_tier(
                candidate
            )
        ):

            series = candidate

    return (
        clean_text(series)
        if series
        else None,
        tier
    )


# ============================================================
# CARD IMAGE
# ============================================================

def get_card_image(soup):

    selectors = [
        "img[src*='/images/cards/']",
        "img[data-src*='/images/cards/']",
        "img[data-lazy-src*='/images/cards/']",
        "img[data-original*='/images/cards/']",
    ]

    for selector in selectors:

        for img in soup.select(
            selector
        ):

            for attr in (
                "src",
                "data-src",
                "data-lazy-src",
                "data-original",
            ):

                url = normalize_url(
                    img.get(attr)
                )

                if not url:
                    continue

                lower = url.lower()

                if "shoob_logo" in lower:
                    continue

                if (
                    "/images/cards/"
                    in lower
                ):
                    return url

    return None


# ============================================================
# VALIDATE CARD
# ============================================================

def validate_card(card):

    name = clean_text(
        card.get("name")
    )

    image = clean_text(
        card.get("image_url")
    )

    series = clean_text(
        card.get("series")
    )

    tier = clean_text(
        card.get("tier")
    )

    if (
        not name
        or name.lower()
        in {
            "creators",
            "unknown",
            "unknown card",
        }
    ):
        return False

    if (
        not image
        or "/images/cards/"
        not in image.lower()
    ):
        return False

    if "shoob_logo" in image.lower():
        return False

    if (
        not series
        or series.lower()
        == "unknown series"
    ):
        return False

    if (
        not tier
        or not extract_tier(tier)
    ):
        return False

    return True


# ============================================================
# WAIT FOR DETAIL PAGE
# ============================================================

def wait_for_detail_page(driver):

    wait = WebDriverWait(
        driver,
        DETAIL_WAIT
    )

    try:

        wait.until(
            lambda d:
            d.find_elements(
                By.CSS_SELECTOR,
                "img[src*='/images/cards/']"
            )
            or
            d.find_elements(
                By.CSS_SELECTOR,
                "img[data-src*='/images/cards/']"
            )
        )

    except TimeoutException:

        pass

    time.sleep(
        DETAIL_EXTRA_WAIT
    )


# ============================================================
# SCRAPE DETAIL PAGE
# ============================================================

def scrape_card_detail(
    driver,
    url
):

    driver.get(url)

    if (
        "/cards/info/"
        not in driver.current_url
    ):

        raise ValueError(
            "Unexpected detail URL: "
            f"{driver.current_url}"
        )

    wait_for_detail_page(
        driver
    )

    soup = BeautifulSoup(
        driver.page_source,
        "html.parser"
    )

    title = get_title(
        soup
    )

    if not title:

        raise ValueError(
            "Could not find real card "
            f"title at {driver.current_url}"
        )

    print(
        f"   Found title: {title}"
    )

    name, title_tier = (
        parse_name_and_tier(
            title
        )
    )

    series, breadcrumb_tier = (
        extract_series_and_tier(
            soup
        )
    )

    tier = (
        title_tier
        or breadcrumb_tier
    )

    image_url = get_card_image(
        soup
    )

    card = {
        "name": name,
        "series": series,
        "tier": tier,
        "image_url": image_url,
        "source_url":
            driver.current_url,
    }

    if not validate_card(
        card
    ):

        raise ValueError(
            "Invalid card data extracted: "
            f"{card}"
        )

    return card


# ============================================================
# CHECK EXISTING CARD
# ============================================================

def card_exists(source_url):

    if not SKIP_EXISTING:
        return False

    return (
        cards_collection.find_one(
            {
                "source_url":
                    source_url
            },
            {
                "_id": 1
            }
        )
        is not None
    )


# ============================================================
# SAVE CARD
# ============================================================

def save_card(card):

    result = cards_collection.update_one(
        {
            "source_url":
                card["source_url"]
        },
        {
            "$set": {
                "name":
                    card["name"],
                "series":
                    card["series"],
                "tier":
                    card["tier"],
                "image_url":
                    card["image_url"],
                "updated_at":
                    now_utc(),
            },
            "$setOnInsert": {
                "source_url":
                    card["source_url"],
                "created_at":
                    now_utc(),
            },
        },
        upsert=True
    )

    if result.upserted_id:
        return "inserted"

    if result.modified_count:
        return "updated"

    return "unchanged"


# ============================================================
# COLLECT GALLERY URLS
# ============================================================

def collect_gallery_urls(
    driver,
    page_num
):

    gallery_url = (
        f"{BASE_URL}{page_num}"
    )

    driver.get(
        gallery_url
    )

    time.sleep(
        GALLERY_WAIT
    )

    links = driver.find_elements(
        By.XPATH,
        "//a[contains(@href, '/cards/info/')]"
    )

    urls = []
    seen = set()

    for link in links:

        try:

            href = link.get_attribute(
                "href"
            )

        except Exception:

            continue

        if not href:
            continue

        href = normalize_url(
            href
        )

        if (
            href
            and "/cards/info/"
            in href
            and href not in seen
        ):

            seen.add(href)

            urls.append(
                href
            )

    return urls


# ============================================================
# CHROME DRIVER
# ============================================================

def create_driver():

    options = uc.ChromeOptions()

    if HEADLESS:

        options.add_argument(
            "--headless=new"
        )

    options.add_argument(
        "--no-sandbox"
    )

    options.add_argument(
        "--disable-dev-shm-usage"
    )

    options.add_argument(
        "--disable-gpu"
    )

    options.add_argument(
        "--window-size=1920,1080"
    )

    options.add_argument(
        "--disable-blink-features=AutomationControlled"
    )

    # GitHub Actions currently has Chrome 150.
    # This prevents undetected_chromedriver
    # from selecting ChromeDriver 151.
    return uc.Chrome(
        options=options,
        version_main=150
    )


# ============================================================
# MAIN SCRAPER
# ============================================================

def scrape_shoob_two_step():

    connect_mongodb()

    # --------------------------------------------------------
    # Determine automatic batch.
    # --------------------------------------------------------

    start_page, end_page = (
        calculate_batch()
    )

    if start_page is None:

        print(
            "\n[+] ALL PAGES HAVE BEEN SCRAPED."
        )

        print(
            f"[+] Maximum page: "
            f"{MAX_PAGE}"
        )

        return

    print(
        "\n"
        + "=" * 60
    )

    print(
        "          AUTOMATIC SCRAPE BATCH"
    )

    print(
        "=" * 60
    )

    print(
        f"Starting page : {start_page}"
    )

    print(
        f"Ending page   : {end_page}"
    )

    print(
        f"Batch size    : "
        f"{end_page - start_page + 1}"
    )

    print(
        f"Maximum page  : {MAX_PAGE}"
    )

    print(
        "=" * 60
    )

    driver = None

    successful_pages = []
    empty_pages = []
    timeout_pages = []

    inserted = 0
    updated = 0
    unchanged = 0
    failed = 0
    skipped = 0

    batch_completed = True

    try:

        driver = create_driver()

        driver.set_page_load_timeout(
            PAGE_LOAD_TIMEOUT
        )

        # ====================================================
        # STEP 1
        # GALLERY PAGES
        # ====================================================

        for page_num in range(
            start_page,
            end_page + 1
        ):

            print(
                f"\n[!] Scanning Gallery "
                f"Page {page_num}..."
            )

            try:

                card_urls = (
                    collect_gallery_urls(
                        driver,
                        page_num
                    )
                )

            except TimeoutException:

                print(
                    f"--> [TIMEOUT] Gallery "
                    f"page {page_num} "
                    f"timed out."
                )

                timeout_pages.append(
                    page_num
                )

                # Do NOT advance state.
                # This page will be retried
                # on the next GitHub run.

                batch_completed = False

                break

            except WebDriverException as error:

                print(
                    f"--> [ERROR] Gallery "
                    f"page {page_num}: "
                    f"{error}"
                )

                timeout_pages.append(
                    page_num
                )

                batch_completed = False

                break

            # =================================================
            # EMPTY PAGE
            # =================================================

            if not card_urls:

                print(
                    f"--> Gallery page "
                    f"{page_num} returned "
                    f"0 cards."
                )

                empty_pages.append(
                    page_num
                )

                # Treat empty page as completed.
                # Otherwise scraper could get
                # permanently stuck on it.

                save_state(
                    page_num + 1,
                    page_num,
                    "running"
                )

                print(
                    f"[+] Progress saved. "
                    f"Next page: "
                    f"{page_num + 1}"
                )

                continue

            successful_pages.append(
                page_num
            )

            print(
                f"Found "
                f"{len(card_urls)} "
                f"card pages to inspect "
                f"on Page {page_num}."
            )

            # =================================================
            # STEP 2
            # DETAIL PAGES
            # =================================================

            for idx, url in enumerate(
                card_urls,
                1
            ):

                try:

                    print(
                        f"\n   Navigating "
                        f"to details "
                        f"({idx}/"
                        f"{len(card_urls)})..."
                    )

                    print(
                        f"   URL: {url}"
                    )

                    # -----------------------------------------
                    # Existing card
                    # -----------------------------------------

                    if card_exists(
                        url
                    ):

                        skipped += 1

                        print(
                            "   [MongoDB] "
                            "Already exists; "
                            "skipped."
                        )

                        continue

                    # -----------------------------------------
                    # Scrape
                    # -----------------------------------------

                    card = (
                        scrape_card_detail(
                            driver,
                            url
                        )
                    )

                    print(
                        f"   Extracted - "
                        f"Name: "
                        f"{card['name']}, "
                        f"Series: "
                        f"{card['series']}, "
                        f"Tier: "
                        f"{card['tier']}"
                    )

                    print(
                        f"   Image: "
                        f"{card['image_url']}"
                    )

                    # -----------------------------------------
                    # Save immediately
                    # -----------------------------------------

                    result = save_card(
                        card
                    )

                    if result == "inserted":

                        inserted += 1

                    elif result == "updated":

                        updated += 1

                    else:

                        unchanged += 1

                    print(
                        f"   [MongoDB] "
                        f"{result}."
                    )

                except TimeoutException:

                    failed += 1

                    print(
                        "   [TIMEOUT] "
                        "Detail page timed out."
                    )

                except PyMongoError as error:

                    failed += 1

                    print(
                        f"   [MongoDB ERROR] "
                        f"{error}"
                    )

                except Exception as error:

                    failed += 1

                    print(
                        f"   [Error] "
                        f"Skipping detail "
                        f"entry: {error}"
                    )

            # =================================================
            # PAGE COMPLETED
            # =================================================

            save_state(
                page_num + 1,
                page_num,
                "running"
            )

            print(
                f"[+] Page {page_num} "
                f"completed."
            )

            print(
                f"[+] Next page: "
                f"{page_num + 1}"
            )

            total = (
                cards_collection.count_documents(
                    {}
                )
            )

            print(
                f"Current MongoDB "
                f"database size: "
                f"{total} records."
            )

        # ====================================================
        # BATCH COMPLETED
        # ====================================================

        if batch_completed:

            state = get_state()

            next_page = int(
                state.get(
                    "next_page",
                    MAX_PAGE + 1
                )
            )

            if next_page > MAX_PAGE:

                save_state(
                    MAX_PAGE + 1,
                    MAX_PAGE,
                    "completed"
                )

                print(
                    "\n"
                    + "=" * 60
                )

                print(
                    "       ALL SHOOB PAGES COMPLETED"
                )

                print(
                    "=" * 60
                )

            else:

                save_state(
                    next_page,
                    next_page - 1,
                    "waiting_for_next_run"
                )

                print(
                    "\n[+] Batch completed."
                )

                print(
                    f"[+] Next GitHub Actions "
                    f"run will start at "
                    f"page {next_page}."
                )

    finally:

        # ====================================================
        # CLOSE CHROME
        # ====================================================

        try:

            if driver:

                driver.quit()

        except Exception:

            pass

        # ====================================================
        # SUMMARY
        # ====================================================

        print(
            "\n"
            + "=" * 60
        )

        print(
            "             SHOOB SCRAPER SUMMARY"
        )

        print(
            "=" * 60
        )

        print(
            f"New Cards Inserted : "
            f"{inserted}"
        )

        print(
            f"Cards Updated      : "
            f"{updated}"
        )

        print(
            f"Cards Unchanged    : "
            f"{unchanged}"
        )

        print(
            f"Existing Skipped   : "
            f"{skipped}"
        )

        print(
            f"Failed Cards       : "
            f"{failed}"
        )

        print(
            f"Successful Pages   : "
            f"{successful_pages}"
        )

        print(
            f"Empty Pages        : "
            f"{empty_pages}"
        )

        print(
            f"Timed-Out Pages    : "
            f"{timeout_pages}"
        )

        if cards_collection is not None:

            print(
                f"MongoDB Total      : "
                f"{cards_collection.count_documents({})}"
            )

        print(
            "=" * 60
        )

        # ====================================================
        # CLOSE MONGODB
        # ====================================================

        try:

            if mongo_client:

                mongo_client.close()

        except Exception:

            pass


# ============================================================
# RESET PROGRESS
# ============================================================

def reset_progress():

    connect_mongodb()

    state_collection.delete_one(
        {
            "_id": STATE_ID
        }
    )

    print(
        "[+] Scraper progress reset."
    )

    print(
        f"[+] Next scrape will begin "
        f"at page {START_PAGE}."
    )

    try:

        if mongo_client:

            mongo_client.close()

    except Exception:

        pass


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    if (
        os.getenv(
            "RESET_PROGRESS",
            "false"
        ).lower()
        == "true"
    ):

        reset_progress()

    scrape_shoob_two_step()