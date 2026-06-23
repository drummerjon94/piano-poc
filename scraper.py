"""
New Scientist Discovery Tours Scraper
--------------------------------------
Scrapes all tour category pages using Playwright (real browser),
extracts structured tour data, and upserts into Supabase.
Designed to run hourly as a cron job.

Requirements:
    pip3 install playwright beautifulsoup4 supabase
    playwright install chromium

Environment variables required:
    SUPABASE_URL        — your Supabase project URL
    SUPABASE_KEY        — your Supabase secret key

Optional environment variables:
    SLACK_WEBHOOK_URL   — Slack incoming webhook URL for error alerts
    ALERT_EMAIL         — email address for error alerts (requires SendGrid)
    SENDGRID_API_KEY    — SendGrid API key (if using email alerts)
    MIN_TOURS_THRESHOLD — minimum tours expected per category (default: 10)
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from supabase import create_client

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────
BASE_URL = "https://www.newscientist.com"
IMAGE_WIDTH = 500

MIN_TOURS_THRESHOLD = int(os.environ.get("MIN_TOURS_THRESHOLD", 10))

TOUR_CATEGORIES = [
    "nature",
    # Uncomment once nature is confirmed working:
    # "marine",
    # "archaeology",
    # "history",
    # "space",
    # "uk-short-breaks",
    # "geology",
]


# ── Alerting ───────────────────────────────────────────────────────────────
def send_alert(message: str) -> None:
    log.error(f"ALERT: {message}")

    slack_webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if slack_webhook:
        try:
            requests.post(
                slack_webhook,
                json={"text": f":warning: *NS Tours Scraper Alert*\n{message}"},
                timeout=10,
            )
            log.info("Slack alert sent")
        except Exception as e:
            log.error(f"Failed to send Slack alert: {e}")

    sendgrid_key = os.environ.get("SENDGRID_API_KEY")
    alert_email = os.environ.get("ALERT_EMAIL")
    if sendgrid_key and alert_email:
        try:
            requests.post(
                "https://api.sendgrid.com/v3/mail/send",
                headers={
                    "Authorization": f"Bearer {sendgrid_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "personalizations": [{"to": [{"email": alert_email}]}],
                    "from": {"email": alert_email},
                    "subject": "NS Tours Scraper — Action Required",
                    "content": [{"type": "text/plain", "value": message}],
                },
                timeout=10,
            )
            log.info("Email alert sent")
        except Exception as e:
            log.error(f"Failed to send email alert: {e}")


# ── Validation ─────────────────────────────────────────────────────────────
def validate_tour(tour: dict) -> list[str]:
    errors = []
    for field in ["title", "url", "image_url", "date", "duration", "type"]:
        if not tour.get(field):
            errors.append(f"Missing {field}")
    if tour.get("url") and not tour["url"].startswith("https://"):
        errors.append(f"Invalid URL: {tour['url']}")
    if tour.get("image_url") and "images.newscientist" not in tour["image_url"]:
        errors.append(f"Unexpected image domain: {tour['image_url']}")
    return errors


def validate_category_results(tours: list[dict], category: str) -> tuple[bool, list[str]]:
    errors = []

    if len(tours) < MIN_TOURS_THRESHOLD:
        errors.append(
            f"[{category}] Only {len(tours)} tours returned "
            f"(minimum expected: {MIN_TOURS_THRESHOLD})"
        )

    for i, tour in enumerate(tours):
        tour_errors = validate_tour(tour)
        if tour_errors:
            errors.append(
                f"[{category}] Tour {i+1} '{tour.get('title', 'unknown')}': "
                + ", ".join(tour_errors)
            )

    urls = [t["url"] for t in tours]
    duplicates = [url for url in urls if urls.count(url) > 1]
    if duplicates:
        errors.append(f"[{category}] Duplicate URLs: {list(set(duplicates))}")

    return len(errors) == 0, errors


# ── Scraping ───────────────────────────────────────────────────────────────
def fetch_page(url: str, playwright_browser) -> str | None:
    """Fetch a page using Playwright to bypass WAF."""
    try:
        page = playwright_browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        html = page.content()
        page.close()
        return html
    except Exception as e:
        log.error(f"Failed to fetch {url}: {e}")
        return None


def parse_tours(html: str, category: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")

    image_tags = soup.find_all("img", attrs={"data-image-context": "Card--SmallArticle"})
    images = [
        img.get("src", "").split("?")[0] + f"?width={IMAGE_WIDTH}&crop=3:2,smart"
        for img in image_tags
    ]

    all_datalayer = soup.find_all(attrs={"data-datalayer-events": True})
    tour_cards = [
        el for el in all_datalayer
        if "tours-panel" in el.get("data-datalayer-events", "")
    ]

    if len(tour_cards) != len(images):
        log.warning(
            f"[{category}] Mismatch — {len(tour_cards)} tours, "
            f"{len(images)} images. Will zip to shortest."
        )

    tours = []
    for card, image_url in zip(tour_cards, images):
        try:
            raw = card["data-datalayer-events"]
            event_data = json.loads(json.loads(raw))["events"][0]["eventData"]

            title = event_data["event_object_text"]
            availability = None

            # Format 1: "2 places remaining: Tour name" (prefix with colon)
            if ": " in title and "place" in title.lower().split(":")[0]:
                prefix, title = title.split(": ", 1)
                availability = prefix.strip()

            # Format 2: "Tour name - 1 place remaining" (suffix with dash)
            elif " - " in title and "place" in title.lower().split(" - ")[-1]:
                title, suffix = title.rsplit(" - ", 1)
                availability = suffix.strip()

            tours.append({
                "title": title.strip(),
                "category": category,
                "type": event_data["event_category"],
                "duration": event_data["event_label"],
                "date": event_data["event_details"],
                "url": BASE_URL + event_data["target_url"],
                "image_url": image_url,
                "availability": availability,
                "last_scraped": datetime.now(timezone.utc).isoformat(),
            })
        except Exception as e:
            log.error(f"[{category}] Failed to parse card: {e}")
            continue

    log.info(f"[{category}] Extracted {len(tours)} tours")
    return tours


def scrape_all_categories() -> tuple[list[dict], list[str]]:
    all_tours = []
    all_errors = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        for category in TOUR_CATEGORIES:
            url = f"{BASE_URL}/tours/{category}/"
            log.info(f"Fetching {url}")
            html = fetch_page(url, browser)

            if not html:
                msg = f"[{category}] Failed to fetch page — skipping"
                log.error(msg)
                all_errors.append(msg)
                continue

            tours = parse_tours(html, category)
            is_valid, errors = validate_category_results(tours, category)

            if not is_valid:
                log.error(f"[{category}] Validation failed — skipping upsert")
                all_errors.extend(errors)
                continue

            all_tours.extend(tours)

        browser.close()

    log.info(f"Total valid tours to upsert: {len(all_tours)}")
    return all_tours, all_errors


# ── Supabase ───────────────────────────────────────────────────────────────
def upsert_tours(tours: list[dict]) -> None:
    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_KEY")

    if not supabase_url or not supabase_key:
        log.error("SUPABASE_URL and SUPABASE_KEY environment variables are required.")
        sys.exit(1)

    client = create_client(supabase_url, supabase_key)

    try:
        client.table("tours").upsert(tours, on_conflict="url").execute()
        log.info(f"Upserted {len(tours)} tours into Supabase")
    except Exception as e:
        error_msg = f"Supabase upsert failed: {e}"
        log.error(error_msg)
        send_alert(error_msg)
        sys.exit(1)


# ── Entry point ────────────────────────────────────────────────────────────
def main():
    dry_run = "--dry-run" in sys.argv

    log.info("=== NS Tours Scraper starting ===")
    if dry_run:
        log.info("DRY RUN — Supabase upsert will be skipped")

    tours, errors = scrape_all_categories()

    if errors:
        send_alert(
            f"{len(errors)} issue(s) found during scrape — "
            f"affected categories were skipped to preserve existing data.\n\n"
            + "\n".join(errors)
        )

    if not tours:
        log.warning("No valid tours to upsert — existing Supabase data preserved")
        return

    if dry_run:
        print(json.dumps(tours, indent=2))
        log.info(f"DRY RUN complete — {len(tours)} tours found, nothing written")
    else:
        upsert_tours(tours)
        log.info("=== Done ===")


if __name__ == "__main__":
    main()
