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
    os.getenv("END_PAGE", "2404")
)

# Number of gallery pages processed per GitHub Actions run
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


if not MONGODB_URI:
    raise RuntimeError(
        "MONGODB_URI is missing."
    )


mongo_client = None
cards_collection = None
state_collection = None


# ============================================================
# HELPERS
# ============================================================

def now_utc():
    return datetime.now(timezone.utc)


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

    url = normalize_url(url)

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

    # DO NOT create a unique _id index.
    # MongoDB already manages _id automatically.

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
        "media_type"
    )

    print(
        f"[+] MongoDB connected: "
        f"{MONGODB_DATABASE}."
        f"{MONGODB_COLLECTION}"
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
        "next_page": START_PAGE,
        "last_completed_page":
            START_PAGE - 1,
        "status": "new",
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

    text = clean_text(text)

    if not text:
        return None

    # T4
    match = re.search(
        r"\bT(\d+)\b",
        text,
        re.I
    )

    if match:
        return (
            f"T{match.group(1)}"
        )

    # Tier 4
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

    """
    Shoob contains a generic "Creators"
    heading, so it must be ignored.
    """

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

    # --------------------------------------------------------
    # SECONDARY SEARCH
    # --------------------------------------------------------

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

            lower = text.lower()

            if lower in {
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

        # Example:
        # Kakashi & Obito - T4

        name = re.sub(
            rf"\s*(?:-|\||:)\s*"
            rf"{re.escape(tier)}\s*$",
            "",
            name,
            flags=re.I,
        ).strip()

        # Example:
        # Kakashi & Obito T4

        name = re.sub(
            rf"\s+{re.escape(tier)}\s*$",
            "",
            name,
            flags=re.I,
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

        result = []

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

    result = []

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


def extract_series_and_tier(
    soup
):

    items = get_breadcrumb_items(
        soup
    )

    if not items:
        return None, None

    tier = None
    tier_index = None

    # --------------------------------------------------------
    # FIND TIER
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # FIND SERIES
    # --------------------------------------------------------

    series = None

    if (
        tier_index is not None
        and tier_index + 1 < len(items)
    ):

        candidate = clean_text(
            items[
                tier_index + 1
            ]
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

    # --------------------------------------------------------
    # FALLBACK
    # --------------------------------------------------------

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

    """
    Supports:

    PNG/JPG/WebP/etc:
        media_type = image

    WebM/MP4/etc:
        media_type = video
    """

    # ========================================================
    # VIDEO
    # ========================================================

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

    # ========================================================
    # IMAGE
    # ========================================================

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
# MEDIA VALIDATION
# ============================================================

def validate_media(
    media_url,
    media_type
):

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

    return True


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

    if not validate_media(
        media_url,
        media_type
    ):
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

def wait_for_detail_page(
    driver
):

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

    # --------------------------------------------------------
    # TITLE
    # --------------------------------------------------------

    title = get_title(
        soup
    )

    if not title:

        raise ValueError(
            "Could not find real card "
            f"title at {current_url}"
        )

    print(
        f"   Found title: {title}"
    )

    # --------------------------------------------------------
    # NAME + TITLE TIER
    # --------------------------------------------------------

    name, title_tier = (
        parse_name_and_tier(
            title
        )
    )

    # --------------------------------------------------------
    # SERIES + BREADCRUMB TIER
    # --------------------------------------------------------

    (
        series,
        breadcrumb_tier
    ) = extract_series_and_tier(
        soup
    )

    tier = (
        title_tier
        or breadcrumb_tier
    )

    # --------------------------------------------------------
    # MEDIA
    # --------------------------------------------------------

    media = get_card_media(
        soup
    )

    if not media:

        raise ValueError(
            "No valid card media found "
            f"at {current_url}"
        )

    media_url = media[
        "media_url"
    ]

    media_type = media[
        "media_type"
    ]

    # --------------------------------------------------------
    # CARD
    # --------------------------------------------------------

    card = {

        "name":
            name,

        "series":
            series,

        "tier":
            tier,

        "media_url":
            media_url,

        "media_type":
            media_type,

        "source_url":
            current_url,

    }

    # --------------------------------------------------------
    # VALIDATE
    # --------------------------------------------------------

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

def card_exists(
    source_url
):

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
            },
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
                card[
                    "source_url"
                ]
        },

        {
            "$set": {

                "name":
                    card["name"],

                "series":
                    card["series"],

                "tier":
                    card["tier"],

                "media_url":
                    card["media_url"],

                "media_type":
                    card["media_type"],

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

        if not href:
            continue

        if (
            "/cards/info/" in href
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
# CREATE CHROME DRIVER
# ============================================================

def create_driver():

    options = uc.ChromeOptions()

    options.headless = HEADLESS

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

    return uc.Chrome(
        options=options
    )

    options = uc.ChromeOptions()

    options.headless = HEADLESS

            if HEADLESS:
                    options.add_argument("--headless=new")

                        options.add_argument("--no-sandbox")
                            options.add_argument("--disable-dev-shm-usage")
                                options.add_argument("--disable-gpu")
                                    options.add_argument("--window-size=1920,1080")
                                        options.add_argument("--disable-blink-features=AutomationControlled")
                                            options.add_argument("--disable-extensions")
                                                options.add_argument("--disable-popup-blocking")
                                                    options.add_argument("--disable-notifications")

                                                        # Let undetected_chromedriver auto-detect the installed Chrome version
                                                            return uc.Chrome(options=options)
                                                            

        options = uc.ChromeOptions()

            options.headless = HEADLESS

                if HEADLESS:
                        options.add_argument("--headless=new")

                            options.add_argument("--no-sandbox")
                                options.add_argument("--disable-dev-shm-usage")
                                    options.add_argument("--disable-gpu")
                                        options.add_argument("--window-size=1920,1080")
                                            options.add_argument("--disable-blink-features=AutomationControlled")
                                                options.add_argument("--disable-extensions")
                                                    options.add_argument("--disable-popup-blocking")
                                                        options.add_argument("--disable-notifications")

                                                            # Let undetected_chromedriver automatically detect the browser version
                                                                return uc.Chrome(options=options)


    options = uc.ChromeOptions()

    options.headless = HEADLESS

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

    # GitHub Actions currently using Chrome 150
    return uc.Chrome(
        options=options,
        version_main=152
    )


# ============================================================
# MAIN TWO-STEP SCRAPER
# ============================================================

def scrape_shoob_two_step():

    connect_mongodb()

    # ========================================================
    # RESUME STATE
    # ========================================================

    state = get_state()

    requested_start = (
        START_PAGE
    )

    saved_next_page = int(
        state.get(
            "next_page",
            requested_start
        )
    )

    # Never go backwards
    start_page = max(
        requested_start,
        saved_next_page
    )

    # ========================================================
    # ALREADY FINISHED
    # ========================================================

    if start_page > END_PAGE:

        print(
            f"[+] No pending pages. "
            f"Resume state says "
            f"next_page={start_page}."
        )

        print(
            "[+] Scraping is already "
            "complete."
        )

        return

    # ========================================================
    # CALCULATE THIS RUN'S BATCH
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
        f"Starting page : {start_page}"
    )

    print(
        f"Ending page   : {batch_end}"
    )

    print(
        f"Batch size    : "
        f"{batch_end - start_page + 1}"
    )

    print(
        f"Overall target: "
        f"{END_PAGE}"
    )

    print(
        "=" * 60
    )

    # ========================================================
    # COUNTERS
    # ========================================================

    driver = None

    successful_pages = []

    empty_pages = []

    timeout_pages = []

    inserted = 0

    updated = 0

    unchanged = 0

    failed = 0

    skipped = 0

    # ========================================================
    # RUN
    # ========================================================

    try:

        driver = create_driver()

        driver.set_page_load_timeout(
            PAGE_LOAD_TIMEOUT
        )

        # ====================================================
        # TWO-STEP ARCHITECTURE
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
                f"[!] Scanning Gallery "
                f"Page {page_num}/{END_PAGE}"
            )

            print(
                "=" * 60
            )

            # ------------------------------------------------
            # Save current page BEFORE processing.
            #
            # If GitHub dies here, this same page will
            # be retried on the next run.
            # ------------------------------------------------

            save_state(
                page_num,
                page_num - 1,
                "running"
            )

            # =================================================
            # STEP 1 — GALLERY
            # =================================================

            try:

                card_urls = (
                    collect_gallery_urls(
                        driver,
                        page_num
                    )
                )

            except TimeoutException:

                print(
                    f"--> [TIMEOUT] "
                    f"Gallery page "
                    f"{page_num} timed out."
                )

                timeout_pages.append(
                    page_num
                )

                continue

            except WebDriverException as error:

                print(
                    f"--> [ERROR] "
                    f"Gallery page "
                    f"{page_num}: "
                    f"{error}"
                )

                continue

            except Exception as error:

                print(
                    f"--> [ERROR] "
                    f"Could not scan gallery "
                    f"page {page_num}: "
                    f"{error}"
                )

                continue

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

                # This page is considered complete.
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
                f"card pages to inspect "
                f"on Page {page_num}."
            )

            # =================================================
            # STEP 2 — DETAILS
            # =================================================

            for idx, url in enumerate(
                card_urls,
                1
            ):

                try:

                    print(
                        "\n"
                        f"   Navigating to "
                        f"details "
                        f"({idx}/"
                        f"{len(card_urls)})..."
                    )

                    print(
                        f"   URL: {url}"
                    )

                    # -----------------------------------------
                    # SKIP EXISTING
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
                    # SCRAPE DETAIL
                    # -----------------------------------------

                    card = (
                        scrape_card_detail(
                            driver,
                            url
                        )
                    )

                    # -----------------------------------------
                    # LOG
                    # -----------------------------------------

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
                        f"   Media Type: "
                        f"{card['media_type']}"
                    )

                    print(
                        f"   Media URL: "
                        f"{card['media_url']}"
                    )

                    # -----------------------------------------
                    # SAVE
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
                        "   [MongoDB ERROR] "
                        f"{error}"
                    )

                except Exception as error:

                    failed += 1

                    print(
                        "   [Error] "
                        "Skipping detail entry: "
                        f"{error}"
                    )

            # =================================================
            # PAGE COMPLETE
            # =================================================

            save_state(
                page_num + 1,
                page_num,
                "running"
            )

            try:

                total = (
                    cards_collection
                    .count_documents({})
                )

            except Exception:

                total = "unknown"

            print(
                "\n"
                f"[+] Page {page_num} "
                f"completed."
            )

            print(
                f"[+] Current MongoDB "
                f"database size: "
                f"{total} records."
            )

        # ====================================================
        # BATCH COMPLETE
        # ====================================================

        next_page = (
            batch_end + 1
        )

        if batch_end >= END_PAGE:

            # ----------------------------------------------
            # ENTIRE SCRAPE FINISHED
            # ----------------------------------------------

            save_state(
                END_PAGE + 1,
                END_PAGE,
                "completed"
            )

            print(
                "\n"
                + "=" * 60
            )

            print(
                "[+] ALL REQUESTED "
                "PAGES COMPLETED."
            )

            print(
                "=" * 60
            )

        else:

            # ----------------------------------------------
            # BATCH FINISHED
            # ----------------------------------------------

            save_state(
                next_page,
                batch_end,
                "waiting_for_next_batch"
            )

            print(
                "\n"
                + "=" * 60
            )

            print(
                "[+] BATCH COMPLETED."
            )

            print(
                f"[+] Pages processed: "
                f"{start_page} → "
                f"{batch_end}"
            )

            print(
                f"[+] Next run starts at: "
                f"{next_page}"
            )

            print(
                "[+] Waiting for the "
                "next GitHub Actions run."
            )

            print(
                "=" * 60
            )

    # ========================================================
    # CLEANUP
    # ========================================================

    finally:

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
            f"Batch Range       : "
            f"{start_page} → {batch_end}"
        )

        print(
            f"Batch Size        : "
            f"{batch_end - start_page + 1}"
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
                    cards_collection
                    .count_documents({})
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

        # ====================================================
        # CLOSE MONGODB
        # ====================================================

        try:

            if mongo_client:

                mongo_client.close()

        except Exception:

            pass


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    # ========================================================
    # RESET PROGRESS
    # ========================================================

    if (
        os.getenv(
            "RESET_PROGRESS",
            "false"
        ).lower()
        == "true"
    ):

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

            if mongo_client:

                mongo_client.close()

        except Exception:

            pass

        mongo_client = None
        cards_collection = None
        state_collection = None

    # ========================================================
    # START
    # ========================================================

    scrape_shoob_two_step()
