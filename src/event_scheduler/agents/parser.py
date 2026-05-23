import hashlib
import json
import logging
from datetime import datetime
from uuid import uuid4

from clickhouse_connect.driver import Client

from event_scheduler.agents.base import BaseAgent
from event_scheduler.config import settings
from event_scheduler.services.llm import _chat, _extract_json_array, html_to_text
from event_scheduler.models import EventCategory

logger = logging.getLogger(__name__)


def content_hash(title: str, start_time: str, location: str) -> int:
    raw = f"{title.lower().strip()}|{start_time}|{location.lower().strip()}"
    h = hashlib.blake2b(raw.encode(), digest_size=8)
    return int.from_bytes(h.digest(), "big")


CATEGORIZE_SYSTEM = """You receive pre-extracted article data about NYC events.
For each article, extract the specific event details as a JSON object with:
- title (string, the event name)
- description (string, 1-2 sentences)
- start_time (ISO 8601 with timezone -04:00, e.g. "2026-05-24T19:00:00-04:00")
- end_time (ISO 8601 or null)
- location_name (string, venue name)
- location_address (string, address or neighborhood in NYC)
- category (one of: music, tech, food, art, fitness, nightlife, comedy, networking, family, other)
- tags (array of lowercase strings)
- price_cents (integer, 0 if free)

Today is {today}. If the article describes MULTIPLE events, return one object per event.
If the article is NOT about a specific event (e.g. a listicle or general guide), return an empty array.
Return ONLY a JSON array."""


class ParserAgent(BaseAgent):
    name = "parser"

    def _execute(self, client: Client) -> tuple[int, int]:
        rows = client.query(
            "SELECT url_hash, source, content, scraped_at FROM raw_pages "
            "WHERE parse_status = 'pending' "
            f"ORDER BY scraped_at LIMIT {settings.parser_batch_size}"
        )
        if not rows.result_rows:
            return 0, 0

        today = datetime.now().strftime("%Y-%m-%d")
        processed = 0
        failed = 0

        for row_url_hash, source, raw_html, scraped_at in rows.result_rows:
            try:
                pre_extracted = self._pre_extract(raw_html)
                if not pre_extracted:
                    text = html_to_text(raw_html)[:4000]
                    pre_extracted = f"Article text:\n{text}"

                events = self._categorize(pre_extracted, today)
                now = datetime.now()
                for ev in events:
                    c_hash = content_hash(ev["title"], ev.get("start_time", ""), ev.get("location_name", ""))
                    cat = ev.get("category", "other")
                    if cat not in {e.value for e in EventCategory}:
                        cat = "other"
                    client.insert(
                        "events",
                        [[
                            str(uuid4()), row_url_hash, c_hash,
                            ev.get("title", ""), ev.get("description", ""),
                            ev.get("start_time", now.isoformat()),
                            ev.get("end_time"),
                            ev.get("location_name", ""), ev.get("location_address", "NYC"),
                            None, None, cat,
                            ev.get("tags", []), ev.get("price_cents", 0),
                            source, "", None, now,
                        ]],
                        column_names=[
                            "event_id", "url_hash", "content_hash", "title", "description",
                            "start_time", "end_time", "location_name", "location_address",
                            "lat", "lon", "category", "tags", "price_cents", "source",
                            "source_url", "image_url", "parsed_at",
                        ],
                    )
                self._mark_parsed(client, row_url_hash, scraped_at)
                processed += 1
                logger.info("Parsed %d events from url_hash=%d", len(events), row_url_hash)
            except Exception:
                logger.exception("Failed to parse page url_hash=%d", row_url_hash)
                self._mark_failed(client, row_url_hash, scraped_at)
                failed += 1

        return processed, failed

    def _pre_extract(self, html: str) -> str:
        """Extract structured data from Nimble's JSON-LD parsing embedded in HTML."""
        parts: list[str] = []

        import re
        ld_blocks = re.findall(
            r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html, re.DOTALL
        )
        for block in ld_blocks:
            try:
                data = json.loads(block)
                items = data if isinstance(data, list) else [data]
                for item in items:
                    graph = item.get("@graph", [item])
                    for node in graph:
                        ntype = node.get("@type", "")
                        if ntype == "NewsArticle":
                            parts.append(f"Title: {node.get('headline', '')}")
                            parts.append(f"Published: {node.get('datePublished', '')}")
                            body = node.get("articleBody", "")
                            if body:
                                parts.append(f"Body: {body[:3000]}")
                        elif ntype == "WebPage":
                            parts.append(f"Page description: {node.get('description', '')}")
                        elif ntype == "BreadcrumbList":
                            crumbs = [el.get("name", "") for el in node.get("itemListElement", [])]
                            if crumbs:
                                parts.append(f"Category path: {' > '.join(crumbs)}")
            except (json.JSONDecodeError, TypeError):
                continue

        return "\n".join(parts) if parts else ""

    def _categorize(self, pre_extracted: str, today: str) -> list[dict]:
        text = _chat(
            model=settings.llm_parse_model,
            system=CATEGORIZE_SYSTEM.format(today=today),
            user_msg=pre_extracted,
        )
        return _extract_json_array(text)

    def _mark_parsed(self, client: Client, uhash: int, scraped_at: datetime) -> None:
        client.command(
            "ALTER TABLE raw_pages UPDATE parse_status = 'parsed' "
            f"WHERE url_hash = {uhash} AND scraped_at = '{scraped_at}'"
        )

    def _mark_failed(self, client: Client, uhash: int, scraped_at: datetime) -> None:
        client.command(
            "ALTER TABLE raw_pages UPDATE parse_status = 'failed' "
            f"WHERE url_hash = {uhash} AND scraped_at = '{scraped_at}'"
        )
