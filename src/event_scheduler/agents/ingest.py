import hashlib
import json
import logging
import re
from datetime import datetime
from uuid import uuid4

from clickhouse_connect.driver import Client

from event_scheduler.agents.base import BaseAgent
from event_scheduler.config import settings
from event_scheduler.services.nimble import NimbleClient, url_hash

logger = logging.getLogger(__name__)

SOURCES_SECRETNYC = [
    "https://secretnyc.co/category/things-to-do/",
    "https://secretnyc.co/category/things-to-do/page/2/",
    "https://secretnyc.co/category/things-to-do/page/3/",
]

SOURCES_LUMA = [
    "https://lu.ma/nyc",
]

SECRETNYC_NAV = frozenset((
    "things-to-do", "top-news", "food-drink", "culture", "escapes",
    "wellness-nature", "secret-guides", "cinema", "sports", "visit-nyc",
    "music", "about-us", "profile", "contact", "advertise",
))


def _content_hash(title: str, start_time: str, location: str) -> int:
    raw = f"{title.lower().strip()}|{start_time}|{location.lower().strip()}"
    h = hashlib.blake2b(raw.encode(), digest_size=8)
    return int.from_bytes(h.digest(), "big")


class IngestAgent(BaseAgent):
    name = "ingest"

    def _execute(self, client: Client) -> tuple[int, int]:
        nimble = NimbleClient()
        total = 0
        failed = 0

        # --- Luma: extract structured events directly from JSON-LD ---
        luma_count, luma_failed = self._ingest_luma(nimble, client)
        total += luma_count
        failed += luma_failed

        # --- SecretNYC: discover URLs, scrape, then parse later ---
        snyc_count, snyc_failed = self._ingest_secretnyc(nimble, client)
        total += snyc_count
        failed += snyc_failed

        return total, failed

    # ── Luma ──────────────────────────────────────────────────────────

    def _ingest_luma(self, nimble: NimbleClient, client: Client) -> tuple[int, int]:
        inserted = 0
        for listing_url in SOURCES_LUMA:
            try:
                data = nimble.extract(listing_url)
                html = data.get("html", "")
                events = self._parse_luma_jsonld(html)
                logger.info("luma: extracted %d events from JSON-LD", len(events))
                now = datetime.now()
                for ev in events:
                    c_hash = _content_hash(ev["title"], ev["start_time"], ev["location_name"])
                    client.insert(
                        "events",
                        [[
                            str(uuid4()), url_hash(ev["source_url"]), c_hash,
                            ev["title"], ev.get("description", ""),
                            ev["start_time"], ev.get("end_time"),
                            ev["location_name"], ev.get("location_address", "New York, NY"),
                            None, None, ev.get("category", "other"),
                            ev.get("tags", []), ev.get("price_cents", 0),
                            "luma", ev["source_url"], None, now,
                        ]],
                        column_names=[
                            "event_id", "url_hash", "content_hash", "title", "description",
                            "start_time", "end_time", "location_name", "location_address",
                            "lat", "lon", "category", "tags", "price_cents", "source",
                            "source_url", "image_url", "parsed_at",
                        ],
                    )
                    inserted += 1
            except Exception:
                logger.exception("Failed to ingest luma listing: %s", listing_url)
                return inserted, 1
        return inserted, 0

    def _parse_luma_jsonld(self, html: str) -> list[dict]:
        ld_blocks = re.findall(
            r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html, re.DOTALL,
        )
        events: list[dict] = []
        for block in ld_blocks:
            try:
                data = json.loads(block)
            except json.JSONDecodeError:
                continue
            if data.get("@type") != "ItemList":
                continue
            for item in data.get("itemListElement", []):
                ev = item.get("item", {})
                if ev.get("@type") != "Event":
                    continue
                loc = ev.get("location", {})
                loc_name = loc.get("name", "New York, NY") if isinstance(loc, dict) else "New York, NY"
                addr = loc.get("address", {}) if isinstance(loc, dict) else {}
                loc_addr = addr.get("streetAddress", loc_name) if isinstance(addr, dict) else str(addr)

                events.append({
                    "title": ev.get("name", ""),
                    "description": ev.get("description", ""),
                    "start_time": ev.get("startDate", ""),
                    "end_time": ev.get("endDate"),
                    "location_name": loc_name,
                    "location_address": loc_addr or "New York, NY",
                    "source_url": ev.get("url", ""),
                    "category": "other",
                    "tags": [],
                    "price_cents": 0,
                })
        return events

    # ── SecretNYC ─────────────────────────────────────────────────────

    def _ingest_secretnyc(self, nimble: NimbleClient, client: Client) -> tuple[int, int]:
        event_urls = self._discover_secretnyc_urls(nimble)
        new_urls = self._filter_already_scraped(client, event_urls)
        logger.info("secretnyc: found %d URLs, %d new", len(event_urls), len(new_urls))

        self._insert_discovered(client, "secretnyc", new_urls)
        if not new_urls:
            return len(event_urls), 0

        scraped, failed = self._scrape_urls(nimble, client, "secretnyc", new_urls[:settings.ingest_batch_size])
        return len(event_urls) + scraped, failed

    def _discover_secretnyc_urls(self, nimble: NimbleClient) -> list[str]:
        urls: list[str] = []
        for listing_url in SOURCES_SECRETNYC:
            try:
                data = nimble.extract(listing_url)
                html = data.get("html", "")
                all_links = re.findall(r'href=["\']([^"\'> ]+)["\']', html)
                for link in all_links:
                    if self._is_secretnyc_event(link):
                        urls.append(link)
            except Exception:
                logger.exception("Failed to extract listing: %s", listing_url)
        return list(dict.fromkeys(urls))

    def _is_secretnyc_event(self, url: str) -> bool:
        if "secretnyc.co/" not in url:
            return False
        slug_match = re.search(r'secretnyc\.co/([^/?#]+)/?$', url)
        if not slug_match:
            return False
        slug = slug_match.group(1)
        if slug in SECRETNYC_NAV:
            return False
        skip = ("/category/", "/tag/", "/author/", "/page/", "/wp-", "/feed/",
                "/nl/", "/de/", "/es/", "/it/", "/fr/", "/pt/", "offloadmedia.")
        if any(s in url for s in skip):
            return False
        return len(slug) > 10

    # ── Shared helpers ────────────────────────────────────────────────

    def _filter_already_scraped(self, client: Client, urls: list[str]) -> list[str]:
        if not urls:
            return []
        hashes = [url_hash(u) for u in urls]
        existing = client.query(
            "SELECT url_hash FROM "
            "(SELECT * FROM discovered_urls ORDER BY discovered_at DESC "
            " LIMIT 1 BY source, url_hash) "
            "WHERE url_hash IN {hashes:Array(UInt64)} "
            "AND last_scraped_at IS NOT NULL "
            "AND last_scraped_at > now() - INTERVAL 24 HOUR",
            parameters={"hashes": hashes},
        )
        existing_set = {row[0] for row in existing.result_rows}
        return [u for u, h in zip(urls, hashes) if h not in existing_set]

    def _insert_discovered(self, client: Client, source: str, urls: list[str]) -> None:
        if not urls:
            return
        now = datetime.now()
        rows = [[url_hash(u), u, source, now, None, "pending"] for u in urls]
        client.insert(
            "discovered_urls",
            rows,
            column_names=["url_hash", "url", "source", "discovered_at", "last_scraped_at", "scrape_status"],
        )

    def _scrape_urls(
        self, nimble: NimbleClient, client: Client, source: str, urls: list[str]
    ) -> tuple[int, int]:
        now = datetime.now()
        scraped = 0
        failed = 0
        results = nimble.extract_sync_multiple(urls)
        for result in results:
            url = result["url"]
            html = result.get("html", "")
            if html and not result.get("error"):
                client.insert(
                    "raw_pages",
                    [[url_hash(url), source, html, now, None, "pending"]],
                    column_names=["url_hash", "source", "content", "scraped_at", "nimble_task_id", "parse_status"],
                )
                client.insert(
                    "discovered_urls",
                    [[url_hash(url), url, source, now, now, "scraped"]],
                    column_names=["url_hash", "url", "source", "discovered_at", "last_scraped_at", "scrape_status"],
                )
                scraped += 1
            else:
                failed += 1
        return scraped, failed
