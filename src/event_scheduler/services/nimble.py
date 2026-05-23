import hashlib
import time

import httpx

from event_scheduler.config import settings


class NimbleClient:
    def __init__(self) -> None:
        self._base = settings.nimble_base_url
        self._headers = {
            "Authorization": f"Bearer {settings.nimble_api_key}",
            "Content-Type": "application/json",
        }
        self._http = httpx.Client(headers=self._headers, timeout=60)

    def extract(self, url: str, *, render: bool = True) -> dict:
        """Extract a page. Returns dict with 'html' and optional 'parsing' keys."""
        resp = self._http.post(
            f"{self._base}/extract",
            json={"url": url, "render": render, "format": "json", "parse": True},
        )
        resp.raise_for_status()
        return resp.json().get("data", {})

    def extract_batch(self, urls: list[str], *, render: bool = True) -> str:
        """Start an async batch extraction. Returns task_id."""
        resp = self._http.post(
            f"{self._base}/extract/async",
            json={
                "urls": [{"url": u, "render": render, "format": "json"} for u in urls],
            },
        )
        resp.raise_for_status()
        return resp.json()["task_id"]

    def extract_sync_multiple(self, urls: list[str], *, render: bool = True) -> list[dict]:
        """Extract multiple URLs synchronously one at a time. Returns list of {url, html}."""
        results = []
        for url in urls:
            try:
                data = self.extract(url, render=render)
                results.append({"url": url, "html": data.get("html", "")})
            except Exception:
                results.append({"url": url, "html": "", "error": True})
        return results

    def poll_task(self, task_id: str, *, max_wait: int = 300, interval: int = 5) -> list[dict]:
        deadline = time.monotonic() + max_wait
        while time.monotonic() < deadline:
            resp = self._http.get(f"{self._base}/tasks/{task_id}")
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") == "completed":
                return data.get("results", [])
            if data.get("status") == "failed":
                raise RuntimeError(f"Nimble task {task_id} failed: {data.get('error')}")
            time.sleep(interval)
        raise TimeoutError(f"Nimble task {task_id} did not complete within {max_wait}s")


def url_hash(url: str) -> int:
    normalized = url.rstrip("/").split("?")[0].lower()
    h = hashlib.blake2b(normalized.encode(), digest_size=8)
    return int.from_bytes(h.digest(), "big")
