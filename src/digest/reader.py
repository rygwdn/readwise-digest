import logging
import re
from typing import Iterator

import httpx

BASE = "https://readwise.io/api/v3"
log = logging.getLogger(__name__)

_TAG_STRIP_RE = re.compile(r"<[^>]+>")


class Reader:
    def __init__(self, token: str):
        self._client = httpx.Client(
            headers={"Authorization": f"Token {token}"},
            timeout=60.0,
        )

    def close(self) -> None:
        self._client.close()

    def list_later(self) -> Iterator[dict]:
        cursor: str | None = None
        while True:
            params: dict[str, str] = {
                "location": "later",
                "withHtmlContent": "true",
                "withRawSourceUrl": "true",
            }
            if cursor:
                params["pageCursor"] = cursor
            r = self._client.get(f"{BASE}/list/", params=params)
            r.raise_for_status()
            data = r.json()
            for item in data.get("results", []):
                yield item
            cursor = data.get("nextPageCursor")
            if not cursor:
                break

    def list_queue(self) -> list[dict]:
        return list(self.list_later())

    def download_epub(self, raw_source_url: str) -> bytes:
        r = self._client.get(raw_source_url, follow_redirects=True)
        r.raise_for_status()
        return r.content

    def add_tag(self, doc_id: str, current_tag_names: list[str], new_tag: str) -> None:
        """PATCH replaces the full tag list. Send existing names + new tag."""
        if new_tag in current_tag_names:
            return
        names = current_tag_names + [new_tag]
        r = self._client.patch(f"{BASE}/update/{doc_id}/", json={"tags": names})
        if r.status_code not in (200, 204):
            raise RuntimeError(
                f"PATCH /update/{doc_id}/ failed: {r.status_code} {r.text[:200]}"
            )


def tag_names(item: dict) -> list[str]:
    tags = item.get("tags") or {}
    if isinstance(tags, dict):
        return list(tags.keys())
    if isinstance(tags, list):
        return [t.get("name") if isinstance(t, dict) else t for t in tags]
    return []


def word_count(item: dict) -> int:
    """Word count for an article. Reader's value if present and >0; else fallback to body length."""
    raw = item.get("word_count")
    try:
        if raw is not None and int(raw) > 0:
            return int(raw)
    except (TypeError, ValueError):
        pass
    html = item.get("html_content") or item.get("content") or ""
    if not html:
        return 0
    text = _TAG_STRIP_RE.sub(" ", html)
    fallback = len(text.split())
    log.info(
        f"word_count fallback for {item.get('id')!r}: Reader missing/zero, computed {fallback}"
    )
    return fallback
