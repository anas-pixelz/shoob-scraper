import os
import re
import time
import html
import subprocess
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
import undetected_chromedriver as uc
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import PyMongoError
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait


# ============================================================
# ENVIRONMENT
# ============================================================

load_dotenv()

BASE_URL = os.getenv(
    "BASE_URL",
    "https://shoob.gg/cards?page="
)

START_PAGE = int(
    os.getenv("START_PAGE", "1")
)

END_PAGE = int(
    os.getenv("END_PAGE", "2403")
)

# Number of gallery pages processed per run
BATCH_SIZE = int(
    os.getenv("BATCH_SIZE", "30")
)

PAGE_LOAD_TIMEOUT = int(
    os.getenv("PAGE_LOAD_TIMEOUT", "30")
)

GALLERY_WAIT = float(
    os.getenv("GALLERY_WAIT", "4")
)

DETAIL_WAIT = int(
    os.getenv("DETAIL_WAIT", "20")
)

DETAIL_EXTRA_WAIT = float(
    os.getenv("DETAIL_EXTRA_WAIT", "1")
)

HEADLESS = (
    os.getenv(
        "HEADLESS",
        "true"
    ).lower() == "true"
)

SKIP_EXISTING = (
    os.getenv(
        "SKIP_EXISTING",
        "true"
    ).lower() == "true"
)

RESET_PROGRESS = (
    os.getenv(
        "RESET_PROGRESS",
        "false"
    ).lower() == "true"
)


# ============================================================
# MONGODB
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


# ============================================================
# TELEGRAM
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN"
)

TELEGRAM_CHAT_ID = os.getenv(
    "TELEGRAM_CHAT_ID"
)

TELEGRAM_TIMEOUT = int(
    os.getenv(
        "TELEGRAM_TIMEOUT",
        "60"
    )
)

TELEGRAM_RETRIES = int(
    os.getenv(
        "TELEGRAM_RETRIES",
        "3"
    )
)

TELEGRAM_RETRY_DELAY = float(
    os.getenv(
        "TELEGRAM_RETRY_DELAY",
        "3"
    )
)


if not MONGODB_URI:
    raise RuntimeError(
        "MONGODB_URI is missing."
    )

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError(
        "TELEGRAM_BOT_TOKEN is missing."
    )

if not TELEGRAM_CHAT_ID:
    raise RuntimeError(
        "TELEGRAM_CHAT_ID is missing."
    )


# ============================================================
# GLOBALS
# ============================================================

mongo_client = None
cards_collection = None
state_collection = None


# ============================================================
# HELPERS
# ============================================================

def now_utc():
    return datetime.now(
        timezone.utc
    )


def clean_text(value):

    return " ".join(
        str(value or "").split()
    ).strip()


def normalize_url(url):

    if not url:
        return None

    url = str(url).strip()

    if not url:
        return None

    if url.startswith("//"):
        return "https:" + url

    if url.startswith("/"):
        return "https://shoob.gg" + url

    return url


def is_card_media_url(url):

    if not url:
        return False

    url = normalize_url(
        url
    )

    if not url:
        return False

    lower = url.lower()

    if "shoob_logo" in lower:
        return False

    if "/images/cards/" not in lower:
        return False

    return True


def is_invalid_card_name(name):

    if not name:
        return True

    value = clean_text(
        name
    ).lower()

    invalid_names = {
        "",
        "creators",
        "creator",
        "cards",
        "card",
        "unknown",
        "unknown card",
        "navigation menu",
        "menu",
    }

    return value in invalid_names


# ============================================================
# MONGODB CONNECTION
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
        serverSelectionTimeoutMS=15000,
        connectTimeoutMS=15000,
        socketTimeoutMS=30000,
    )

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

    # Unique card identifier
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

    cards_collection.create_index(
        "telegram_message_id"
    )

    print(
        f"[+] MongoDB connected: "
        f"{MONGODB_DATABASE}.{MONGODB_COLLECTION}"
    )


# ============================================================
# SCRAPER STATE
# ============================================================

def get_state():

    state = state_collection.find_one(
        {
            "_id":
                "shoob_scraper"
        }
    )

    if state:
        return state

    return {
        "next_page":
            START_PAGE,

        "last_completed_page":
            START_PAGE - 1,

        "status":
            "new",
    }


def save_state(
    next_page,
    last_completed_page,
    status="running"
):

    state_collection.update_one(
        {
            "_id":
                "shoob_scraper"
        },
        {
            "$set": {

                "next_page":
                    next_page,

                "last_completed_page":
                    last_completed_page,

                "status":
                    status,

                "updated_at":
                    now_utc(),

            }
        },
        upsert=True,
    )


# ============================================================
# TIER EXTRACTION
# ============================================================

def extract_tier(text):

    text = clean_text(
        text
    )

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
# TITLE EXTRACTION
# ============================================================

def get_title(soup):

    selectors = [

        "div.text-xl.font-bold.text-center.mt-4",

        "div.text-xl.font-bold.text-center",

        "div.text-xl.font-bold",

        "h1.text-xl.font-bold",

        "h1.text-center",

        "h1",

        "h2",

        "h3",

    ]

    for selector in selectors:

        elements = soup.select(
            selector
        )

        for element in elements:

            title = clean_text(
                element.get_text(
                    " ",
                    strip=True
                )
            )

            if not title:
                continue

            if is_invalid_card_name(
                title
            ):
                continue

            if extract_tier(
                title
            ):
                return title

    for element in soup.find_all(
        ["div", "span"]
    ):

        text = clean_text(
            element.get_text(
                " ",
                strip=True
            )
        )

        if not text:
            continue

        if len(text) > 200:
            continue

        if is_invalid_card_name(
            text
        ):
            continue

        if extract_tier(
            text
        ):

            if text.lower() in {
                "cards",
                "tier",
                "navigation menu",
                "creators",
            }:
                continue

            return text

    return None


# ============================================================
# NAME + TIER
# ============================================================

def parse_name_and_tier(title):

    title = clean_text(
        title
    )

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
            flags=re.I,
        ).strip()

        name = re.sub(
            rf"\s+{re.escape(tier)}\s*$",
            "",
            name,
            flags=re.I,
        ).strip()

    return (
        clean_text(name),
        tier,
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

    result = []

    if items:

        for item in items:

            text = clean_text(
                item.get_text(
                    " ",
                    strip=True
                )
            )

            if text:
                result.append(
                    text
                )

        return result

    for item in breadcrumb.find_all(
        "a"
    ):

        text = clean_text(
            item.get_text(
                " ",
                strip=True
            )
        )

        if text:
            result.append(
                text
            )

    return result


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
        and tier_index + 1 < len(items)
    ):

        candidate = clean_text(
            items[tier_index + 1]
        )

        if (
            candidate.lower()
            not in {
                "cards",
                "card",
            }
            and not extract_tier(
                candidate
            )
        ):

            series = candidate

    if not series and len(items) >= 2:

        candidate = clean_text(
            items[-2]
        )

        if (
            candidate.lower()
            not in {
                "cards",
                "card",
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
        tier,
    )


# ============================================================
# MEDIA EXTRACTION
# ============================================================

def extract_from_element(
    element,
    attributes
):

    for attr in attributes:

        value = element.get(
            attr
        )

        if not value:
            continue

        url = normalize_url(
            value
        )

        if not url:
            continue

        if is_card_media_url(
            url
        ):
            return url

    return None


def get_card_media(soup):

    video_selectors = [

        "video[src*='/images/cards/']",

        "video source[src*='/images/cards/']",

        "video",

        "source",

    ]

    for selector in video_selectors:

        elements = soup.select(
            selector
        )

        for element in elements:

            url = extract_from_element(
                element,
                (
                    "src",
                    "data-src",
                    "data-video",
                    "data-url",
                ),
            )

            if url:

                return {
                    "media_url":
                        url,

                    "media_type":
                        "video",
                }

    image_selectors = [

        "img[src*='/images/cards/']",

        "img[data-src*='/images/cards/']",

        "img[data-lazy-src*='/images/cards/']",

        "img[data-original*='/images/cards/']",

        "img",

    ]

    for selector in image_selectors:

        elements = soup.select(
            selector
        )

        for element in elements:

            url = extract_from_element(
                element,
                (
                    "src",
                    "data-src",
                    "data-lazy-src",
                    "data-original",
                ),
            )

            if url:

                return {
                    "media_url":
                        url,

                    "media_type":
                        "image",
                }

    return None


# ============================================================
# CARD VALIDATION
# ============================================================

def validate_card(card):

    name = clean_text(
        card.get("name")
    )

    series = clean_text(
        card.get("series")
    )

    tier = clean_text(
        card.get("tier")
    )

    media_url = clean_text(
        card.get("media_url")
    )

    media_type = clean_text(
        card.get("media_type")
    )

    source_url = clean_text(
        card.get("source_url")
    )

    if is_invalid_card_name(
        name
    ):
        return False

    if (
        not series
        or series.lower()
        == "unknown series"
    ):
        return False

    if (
        not tier
        or not extract_tier(
            tier
        )
    ):
        return False

    if not media_url:
        return False

    if not is_card_media_url(
        media_url
    ):
        return False

    if media_type not in {
        "image",
        "video",
    }:
        return False

    if (
        not source_url
        or "/cards/info/"
        not in source_url
    ):
        return False

    return True


# ============================================================
# DETAIL PAGE WAIT
# ============================================================

def wait_for_detail_page(driver):

    wait = WebDriverWait(
        driver,
        DETAIL_WAIT
    )

    selectors = [

        "img[src*='/images/cards/']",

        "img[data-src*='/images/cards/']",

        "img[data-lazy-src*='/images/cards/']",

        "video[src*='/images/cards/']",

        "video",

        "video source[src*='/images/cards/']",

    ]

    try:

        wait.until(
            lambda d:
                any(
                    d.find_elements(
                        By.CSS_SELECTOR,
                        selector
                    )
                    for selector
                    in selectors
                )
        )

    except TimeoutException:
        pass

    time.sleep(
        DETAIL_EXTRA_WAIT
    )


# ============================================================
# SCRAPE ONE CARD
# ============================================================

def scrape_card_detail(
    driver,
    url
):

    driver.get(
        url
    )

    current_url = (
        driver.current_url
    )

    if "/cards/info/" not in current_url:

        raise ValueError(
            "Unexpected detail URL: "
            f"{current_url}"
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
            "Could not find real card title at "
            f"{current_url}"
        )

    print(
        f"   Found title: {title}"
    )

    name, title_tier = (
        parse_name_and_tier(
            title
        )
    )

    (
        series,
        breadcrumb_tier,
    ) = extract_series_and_tier(
        soup
    )

    tier = (
        title_tier
        or breadcrumb_tier
    )

    media = get_card_media(
        soup
    )

    if not media:

        raise ValueError(
            "No valid card media found at "
            f"{current_url}"
        )

    card = {

        "name":
            name,

        "series":
            series,

        "tier":
            tier,

        "media_url":
            media["media_url"],

        "media_type":
            media["media_type"],

        "source_url":
            current_url,

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
# TELEGRAM
# ============================================================

def get_telegram_api_url(method):

    return (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/"
        f"{method}"
    )


def build_caption(card):

    name = html.escape(
        str(card["name"])
    )

    series = html.escape(
        str(card["series"])
    )

    tier = html.escape(
        str(card["tier"])
    )

    source_url = html.escape(
        str(card["source_url"])
    )

    caption = (
        f"<b>🎴 {name}</b>\n\n"
        f"<b>Series:</b> {series}\n"
        f"<b>Tier:</b> {tier}\n\n"
        f"<b>Source:</b>\n"
        f"<a href=\"{source_url}\">View on Shoob</a>"
    )

    return caption


def get_file_extension(url):

    try:

        path = urlparse(
            url
        ).path.lower()

        return os.path.splitext(
            path
        )[1]

    except Exception:

        return ""


def send_telegram_request(
    method,
    payload
):

    url = get_telegram_api_url(
        method
    )

    last_error = None

    for attempt in range(
        1,
        TELEGRAM_RETRIES + 1
    ):

        try:

            response = requests.post(
                url,
                data=payload,
                timeout=TELEGRAM_TIMEOUT,
            )

            data = response.json()

            if (
                response.ok
                and data.get("ok")
            ):

                return data[
                    "result"
                ]

            last_error = (
                data.get(
                    "description"
                )
                or response.text
            )

            retry_after = None

            parameters = data.get(
                "parameters",
                {}
            )

            if parameters:

                retry_after = parameters.get(
                    "retry_after"
                )

            print(
                f"   [Telegram Error] "
                f"Attempt {attempt}/"
                f"{TELEGRAM_RETRIES}: "
                f"{last_error}"
            )

            if retry_after:

                time.sleep(
                    int(retry_after) + 1
                )

            elif attempt < TELEGRAM_RETRIES:

                time.sleep(
                    TELEGRAM_RETRY_DELAY
                )

        except Exception as error:

            last_error = str(
                error
            )

            print(
                f"   [Telegram Error] "
                f"Attempt {attempt}/"
                f"{TELEGRAM_RETRIES}: "
                f"{last_error}"
            )

            if attempt < TELEGRAM_RETRIES:

                time.sleep(
                    TELEGRAM_RETRY_DELAY
                )

    raise RuntimeError(
        "Telegram delivery failed: "
        f"{last_error}"
    )


def extract_telegram_file_id(
    message,
    media_type
):

    if media_type == "image":

        photos = message.get(
            "photo",
            []
        )

        if photos:

            return photos[-1].get(
                "file_id"
            )

    if media_type == "video":

        video = message.get(
            "video"
        )

        if video:

            return video.get(
                "file_id"
            )

        animation = message.get(
            "animation"
        )

        if animation:

            return animation.get(
                "file_id"
            )

        document = message.get(
            "document"
        )

        if document:

            return document.get(
                "file_id"
            )

    return None


def send_card_to_telegram(card):

    caption = build_caption(
        card
    )

    extension = get_file_extension(
        card["media_url"]
    )

    common_payload = {

        "chat_id":
            TELEGRAM_CHAT_ID,

        "caption":
            caption,

        "parse_mode":
            "HTML",

    }

    if card["media_type"] == "image":

        payload = dict(
            common_payload
        )

        payload["photo"] = (
            card["media_url"]
        )

        result = send_telegram_request(
            "sendPhoto",
            payload
        )

        return {

            "telegram_message_id":
                result.get(
                    "message_id"
                ),

            "telegram_file_id":
                extract_telegram_file_id(
                    result,
                    "image"
                ),

            "telegram_media_type":
                "photo",

        }

    # GIF animation
    if extension == ".gif":

        payload = dict(
            common_payload
        )

        payload["animation"] = (
            card["media_url"]
        )

        try:

            result = send_telegram_request(
                "sendAnimation",
                payload
            )

            return {

                "telegram_message_id":
                    result.get(
                        "message_id"
                    ),

                "telegram_file_id":
                    extract_telegram_file_id(
                        result,
                        "video"
                    ),

                "telegram_media_type":
                    "animation",

            }

        except Exception as error:

            print(
                "   [Telegram] "
                "sendAnimation failed. "
                "Trying sendDocument..."
            )

            print(
                f"   Reason: {error}"
            )

    # Normal video
    payload = dict(
        common_payload
    )

    payload["video"] = (
        card["media_url"]
    )

    try:

        result = send_telegram_request(
            "sendVideo",
            payload
        )

        return {

            "telegram_message_id":
                result.get(
                    "message_id"
                ),

            "telegram_file_id":
                extract_telegram_file_id(
                    result,
                    "video"
                ),

            "telegram_media_type":
                "video",

        }

    except Exception as error:

        print(
            "   [Telegram] "
            "sendVideo failed. "
            "Trying sendDocument..."
        )

        print(
            f"   Reason: {error}"
        )

    # Fallback for unsupported animated/video formats
    payload = dict(
        common_payload
    )

    payload["document"] = (
        card["media_url"]
    )

    result = send_telegram_request(
        "sendDocument",
        payload
    )

    return {

        "telegram_message_id":
            result.get(
                "message_id"
            ),

        "telegram_file_id":
            extract_telegram_file_id(
                result,
                "video"
            ),

        "telegram_media_type":
            "document",

    }


# ============================================================
# CHECK EXISTING CARD
# ============================================================

def card_exists(source_url):

    if not SKIP_EXISTING:
        return False

    existing = cards_collection.find_one(
        {
            "source_url":
                source_url
        },
        {
            "_id": 1,
            "telegram_message_id": 1,
        },
    )

    if not existing:
        return False

    # Only skip cards successfully delivered
    return bool(
        existing.get(
            "telegram_message_id"
        )
    )


# ============================================================
# SAVE CARD
# ============================================================

def save_card(
    card,
    telegram_data
):

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

                "telegram_message_id":
                    telegram_data[
                        "telegram_message_id"
                    ],

                "telegram_file_id":
                    telegram_data[
                        "telegram_file_id"
                    ],

                "telegram_media_type":
                    telegram_data[
                        "telegram_media_type"
                    ],

                "telegram_chat_id":
                    str(
                        TELEGRAM_CHAT_ID
                    ),

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

        upsert=True,
    )

    if result.upserted_id:
        return "inserted"

    if result.modified_count:
        return "updated"

    return "unchanged"


# ============================================================
# COLLECT CARD URLS
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
            and "/cards/info/" in href
            and href not in seen
        ):

            seen.add(
                href
            )

            urls.append(
                href
            )

    return urls


# ============================================================
# CHROME VERSION DETECTION
# ============================================================

def get_chrome_major_version():

    commands = [

        [
            "google-chrome",
            "--version",
        ],

        [
            "google-chrome-stable",
            "--version",
        ],

        [
            "chromium-browser",
            "--version",
        ],

        [
            "chromium",
            "--version",
        ],

    ]

    for command in commands:

        try:

            output = subprocess.check_output(
                command,
                stderr=subprocess.STDOUT,
                text=True,
            )

            match = re.search(
                r"(\d+)\.",
                output
            )

            if match:

                return int(
                    match.group(1)
                )

        except Exception:

            continue

    return None


# ============================================================
# CREATE CHROME DRIVER
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

    options.add_argument(
        "--disable-extensions"
    )

    options.add_argument(
        "--disable-popup-blocking"
    )

    options.add_argument(
        "--disable-notifications"
    )

    chrome_version = (
        get_chrome_major_version()
    )

    if chrome_version:

        print(
            f"[+] Detected Chrome major version: "
            f"{chrome_version}"
        )

        return uc.Chrome(
            options=options,
            version_main=chrome_version,
        )

    print(
        "[!] Could not detect Chrome version. "
        "Using undetected_chromedriver auto detection."
    )

    return uc.Chrome(
        options=options
    )


# ============================================================
# MAIN TWO-STEP SCRAPER
# ============================================================

def scrape_shoob_two_step():

    connect_mongodb()

    state = get_state()

    requested_start = START_PAGE

    saved_next_page = int(
        state.get(
            "next_page",
            requested_start
        )
    )

    start_page = max(
        requested_start,
        saved_next_page
    )

    if start_page > END_PAGE:

        print(
            "[+] No pending pages."
        )

        print(
            "[+] Scraping is already complete."
        )

        return

    # ========================================================
    # 30 PAGE BATCH
    # ========================================================

    batch_end = min(
        start_page + BATCH_SIZE - 1,
        END_PAGE
    )

    print(
        "\n"
        + "=" * 60
    )

    print(
        "           SHOOB SCRAPER BATCH"
    )

    print(
        "=" * 60
    )

    print(
        f"Start Page    : {start_page}"
    )

    print(
        f"End Page      : {batch_end}"
    )

    print(
        f"Batch Size    : "
        f"{batch_end - start_page + 1}"
    )

    print(
        f"Overall Limit : {END_PAGE}"
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
    telegram_sent = 0

    try:

        driver = create_driver()

        driver.set_page_load_timeout(
            PAGE_LOAD_TIMEOUT
        )

        # ====================================================
        # GALLERY PAGINATION
        # ====================================================

        for page_num in range(
            start_page,
            batch_end + 1
        ):

            print(
                "\n"
                + "=" * 60
            )

            print(
                f"[!] Scanning Gallery Page "
                f"{page_num}/{END_PAGE}"
            )

            print(
                "=" * 60
            )

            # Save current position before processing.
            # If the workflow crashes, this page is retried.
            save_state(
                page_num,
                page_num - 1,
                "running"
            )

            # ================================================
            # STEP 1: GALLERY
            # ================================================

            try:

                card_urls = (
                    collect_gallery_urls(
                        driver,
                        page_num
                    )
                )

            except TimeoutException:

                print(
                    f"--> [TIMEOUT] Gallery page "
                    f"{page_num} timed out."
                )

                timeout_pages.append(
                    page_num
                )

                continue

            except WebDriverException as error:

                print(
                    f"--> [ERROR] Gallery page "
                    f"{page_num}: {error}"
                )

                continue

            except Exception as error:

                print(
                    f"--> [ERROR] Could not scan "
                    f"gallery page {page_num}: "
                    f"{error}"
                )

                continue

            # ================================================
            # EMPTY PAGE
            # ================================================

            if not card_urls:

                print(
                    f"--> Gallery page "
                    f"{page_num} returned 0 cards."
                )

                empty_pages.append(
                    page_num
                )

                save_state(
                    page_num + 1,
                    page_num,
                    "running"
                )

                continue

            successful_pages.append(
                page_num
            )

            print(
                f"Found {len(card_urls)} "
                f"card pages to inspect."
            )

            # ================================================
            # STEP 2: DETAIL PAGES
            # ================================================

            for idx, url in enumerate(
                card_urls,
                1
            ):

                try:

                    print(
                        "\n"
                        f"   Navigating to details "
                        f"({idx}/{len(card_urls)})..."
                    )

                    print(
                        f"   URL: {url}"
                    )

                    # ----------------------------------------
                    # DUPLICATE CHECK
                    # ----------------------------------------

                    if card_exists(
                        url
                    ):

                        skipped += 1

                        print(
                            "   [MongoDB] "
                            "Already sent to Telegram; "
                            "skipped."
                        )

                        continue

                    # ----------------------------------------
                    # SCRAPE
                    # ----------------------------------------

                    card = (
                        scrape_card_detail(
                            driver,
                            url
                        )
                    )

                    print(
                        f"   Extracted - "
                        f"Name: {card['name']}, "
                        f"Series: {card['series']}, "
                        f"Tier: {card['tier']}"
                    )

                    print(
                        f"   Media Type: "
                        f"{card['media_type']}"
                    )

                    # ----------------------------------------
                    # SEND TO TELEGRAM
                    # ----------------------------------------

                    telegram_data = (
                        send_card_to_telegram(
                            card
                        )
                    )

                    telegram_sent += 1

                    print(
                        "   [Telegram] "
                        "Media sent successfully."
                    )

                    print(
                        f"   Message ID: "
                        f"{telegram_data['telegram_message_id']}"
                    )

                    # ----------------------------------------
                    # SAVE METADATA
                    # ----------------------------------------

                    result = save_card(
                        card,
                        telegram_data
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
                        "   [MongoDB ERROR] "
                        f"{error}"
                    )

                except Exception as error:

                    failed += 1

                    print(
                        "   [ERROR] "
                        "Skipping detail entry: "
                        f"{error}"
                    )

            # ================================================
            # PAGE COMPLETE
            # ================================================

            save_state(
                page_num + 1,
                page_num,
                "running"
            )

            total = (
                cards_collection.count_documents(
                    {}
                )
            )

            print(
                f"\n[+] Page {page_num} completed."
            )

            print(
                f"[+] Current MongoDB size: "
                f"{total} records."
            )

        # ====================================================
        # BATCH COMPLETE
        # ====================================================

        next_page = (
            batch_end + 1
        )

        if batch_end >= END_PAGE:

            save_state(
                END_PAGE + 1,
                END_PAGE,
                "completed"
            )

            print(
                "\n[+] ALL REQUESTED "
                "PAGES COMPLETED."
            )

        else:

            save_state(
                next_page,
                batch_end,
                "waiting_for_next_batch"
            )

            print(
                "\n[+] BATCH COMPLETED."
            )

            print(
                f"[+] Processed pages: "
                f"{start_page} -> {batch_end}"
            )

            print(
                f"[+] Next run starts at: "
                f"{next_page}"
            )

    finally:

        try:

            if driver:

                driver.quit()

        except Exception:

            pass

        print(
            "\n"
            + "=" * 60
        )

        print(
            "          SHOOB SCRAPER SUMMARY"
        )

        print(
            "=" * 60
        )

        print(
            f"Batch Range       : "
            f"{start_page} -> {batch_end}"
        )

        print(
            f"New Cards         : "
            f"{inserted}"
        )

        print(
            f"Cards Updated     : "
            f"{updated}"
        )

        print(
            f"Cards Unchanged   : "
            f"{unchanged}"
        )

        print(
            f"Existing Skipped  : "
            f"{skipped}"
        )

        print(
            f"Telegram Sent     : "
            f"{telegram_sent}"
        )

        print(
            f"Failed Cards      : "
            f"{failed}"
        )

        print(
            f"Successful Pages  : "
            f"{successful_pages}"
        )

        print(
            f"Empty Pages       : "
            f"{empty_pages}"
        )

        print(
            f"Timed-Out Pages   : "
            f"{timeout_pages}"
        )

        if cards_collection is not None:

            try:

                total = (
                    cards_collection.count_documents(
                        {}
                    )
                )

                print(
                    f"MongoDB Total     : "
                    f"{total}"
                )

            except Exception:

                pass

        print(
            "=" * 60
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

    if RESET_PROGRESS:

        connect_mongodb()

        state_collection.delete_one(
            {
                "_id":
                    "shoob_scraper"
            }
        )

        print(
            "[+] Scraper progress reset."
        )

        try:

            mongo_client.close()

        except Exception:

            pass

        mongo_client = None
        cards_collection = None
        state_collection = None

    scrape_shoob_two_step()
