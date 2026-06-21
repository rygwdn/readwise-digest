import json
import logging
import random
import re
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, Response
from fastapi.templating import Jinja2Templates

from digest import epub as epub_module, library, store
from digest.reader import Reader, word_count as reader_word_count

log = logging.getLogger(__name__)

_PKG_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(_PKG_DIR / "templates"))

NAV_TYPE = "application/atom+xml;profile=opds-catalog;kind=navigation"
ACQ_TYPE = "application/atom+xml;profile=opds-catalog;kind=acquisition"

_IMG_COUNT_RE = re.compile(r"<img\b", re.IGNORECASE)
_MIN_WORDS = 150
_MAX_IMGS_PER_100_WORDS = 2.0

router = APIRouter(prefix="/opds")


def _base(cfg, request: Request) -> str:
    return cfg.base_url or str(request.base_url).rstrip("/")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_eligible(item: dict) -> tuple[bool, str]:
    wc = item.get("word_count") or 0
    if wc < _MIN_WORDS:
        return False, f"stub ({wc} words)"
    html = item.get("html_content") or item.get("content") or ""
    img_count = len(_IMG_COUNT_RE.findall(html))
    if img_count > 0:
        ratio = img_count / wc * 100
        if ratio > _MAX_IMGS_PER_100_WORDS:
            return False, f"image-heavy ({img_count} imgs / {wc} words)"
    return True, ""


def _partition_into_volumes(articles: list[dict], budget: int) -> list[list[dict]]:
    volumes: list[list[dict]] = []
    current: list[dict] = []
    current_words = 0
    for a in articles:
        wc = reader_word_count(a)
        if current_words > 0 and current_words + wc > budget:
            volumes.append(current)
            current = [a]
            current_words = wc
        else:
            current.append(a)
            current_words += wc
    if current:
        volumes.append(current)
    return volumes


def _pending_entry(p: dict, vol_num: int, base: str) -> dict:
    count = p["article_count"]
    return {
        "id": f"urn:inkbook-digest:pending:{p['id']}",
        "title": f"Morning Paper (Vol. {vol_num})",
        "author": "Readwise Reader Digest",
        "updated": p["sent_at"],
        "published": p["sent_at"],
        "summary": f"{count} article{'s' if count != 1 else ''} queued",
        "language": "en",
        "cover_url": f"{base}/opds/cover/digest/{p['id']}",
        "acquisition_url": f"{base}/opds/file/digest/{p['id']}",
        "media_type": "application/epub+zip",
    }


def _digest_entry(d: dict, base: str) -> dict:
    sent_at = d["sent_at"]
    day = sent_at[:10]
    vol = d["volume"]
    title = f"Morning Paper {day}" + (f" (Vol. {vol})" if vol > 1 else "")
    summary = (
        f"{d['article_count']} article" + ("s" if d["article_count"] != 1 else "")
        + f", {d['total_words']:,} words"
    )
    return {
        "id": f"urn:inkbook-digest:digest:{d['id']}",
        "title": title,
        "author": "Readwise Reader Digest",
        "updated": sent_at,
        "published": sent_at,
        "summary": summary,
        "language": "en",
        "cover_url": f"{base}/opds/cover/digest/{d['id']}",
        "acquisition_url": f"{base}/opds/file/digest/{d['id']}",
        "media_type": "application/epub+zip",
    }


def _library_entry(b: dict, base: str) -> dict:
    media_type = "application/epub+zip" if b["format"] == "epub" else "application/pdf"
    title = b["title"] or b["filename"]
    return {
        "id": f"urn:inkbook-digest:library:{b['id']}",
        "title": title,
        "author": b["author"] or "Unknown",
        "updated": b["added_at"],
        "published": b["added_at"],
        "summary": b["filename"],
        "language": b["language"] or "",
        "cover_url": f"{base}/opds/cover/library/{b['id']}",
        "acquisition_url": f"{base}/opds/file/library/{b['id']}",
        "media_type": media_type,
    }


@router.get("/")
def root(request: Request) -> Response:
    cfg = request.app.state.cfg
    base = _base(cfg, request)
    entries = [
        {
            "id": "urn:inkbook-digest:catalog:digests",
            "title": "Morning Papers",
            "summary": "On-demand digests built from Readwise Reader articles.",
            "href": f"{base}/opds/digests/",
        },
        {
            "id": "urn:inkbook-digest:catalog:library",
            "title": "Library",
            "summary": "Manually uploaded EPUBs and PDFs.",
            "href": f"{base}/opds/library/",
        },
    ]
    body = templates.get_template("opds_navigation.xml").render(
        feed_id="urn:inkbook-digest:root",
        feed_title="E-books & Morning Paper",
        updated=_now_iso(),
        self_url=f"{base}/opds/",
        entries=entries,
    )
    return Response(content=body, media_type=NAV_TYPE)


@router.get("/digests/")
def digests_feed(request: Request) -> Response:
    cfg = request.app.state.cfg
    base = _base(cfg, request)
    conn = store.connect(cfg.data_dir)
    try:
        store.prune_stale_pending(conn)

        reader = Reader(cfg.reader_token)
        try:
            queue = reader.list_queue()
        finally:
            reader.close()

        sent_ids = store.already_sent_ids(conn)
        pending_ids = store.already_pending_ids(conn)
        already_done = sent_ids | pending_ids

        new_eligible = []
        for a in queue:
            if a["id"] in already_done:
                continue
            ok, reason = is_eligible(a)
            if not ok:
                log.info(f"filtered from queue: {a.get('title')!r}: {reason}")
                continue
            new_eligible.append(a)

        if new_eligible:
            random.shuffle(new_eligible)
            budget = store.get_word_budget(conn)
            for vol_articles in _partition_into_volumes(new_eligible, budget):
                store.create_pending_digest(conn, [a["id"] for a in vol_articles])

        pending = store.get_pending_digests(conn)
        rows = conn.execute(
            "SELECT id, sent_at, volume, article_count, total_words FROM digests "
            "WHERE status = 'sent' AND sent_at >= datetime('now', '-30 days') "
            "ORDER BY sent_at DESC"
        ).fetchall()
        sent_data = [
            {"id": r[0], "sent_at": r[1], "volume": r[2], "article_count": r[3], "total_words": r[4]}
            for r in rows
        ]

        entries = [_pending_entry(p, i + 1, base) for i, p in enumerate(pending)]
        entries += [_digest_entry(d, base) for d in sent_data]

        body = templates.get_template("opds_acquisition.xml").render(
            feed_id="urn:inkbook-digest:catalog:digests",
            feed_title="Morning Papers",
            updated=_now_iso(),
            self_url=f"{base}/opds/digests/",
            root_url=f"{base}/opds/",
            entries=entries,
        )
        return Response(content=body, media_type=ACQ_TYPE)
    finally:
        conn.close()


@router.get("/library/")
def library_feed(request: Request) -> Response:
    cfg = request.app.state.cfg
    base = _base(cfg, request)
    conn = store.connect(cfg.data_dir)
    try:
        books = library.list_books(conn)
    finally:
        conn.close()
    entries = [_library_entry(b, base) for b in books]
    body = templates.get_template("opds_acquisition.xml").render(
        feed_id="urn:inkbook-digest:catalog:library",
        feed_title="Library",
        updated=_now_iso(),
        self_url=f"{base}/opds/library/",
        root_url=f"{base}/opds/",
        entries=entries,
    )
    return Response(content=body, media_type=ACQ_TYPE)


@router.get("/file/digest/{digest_id}")
def file_digest(digest_id: int, request: Request):
    cfg = request.app.state.cfg
    conn = store.connect(cfg.data_dir)
    try:
        row = conn.execute(
            "SELECT sent_at, volume, status, article_ids FROM digests WHERE id = ?",
            (digest_id,),
        ).fetchone()
    finally:
        conn.close()

    if not row:
        return Response("not found", status_code=404, media_type="text/plain")

    sent_at, vol, status, article_ids_json = row

    if status == "sent":
        path = store.build_epub_path(cfg.data_dir, sent_at, vol)
        if not path.exists():
            return Response("EPUB no longer available", status_code=404, media_type="text/plain")
        return FileResponse(path, media_type="application/epub+zip", filename=path.name)

    if status != "pending":
        return Response("not available", status_code=404, media_type="text/plain")

    article_ids = json.loads(article_ids_json or "[]")
    id_order = {aid: i for i, aid in enumerate(article_ids)}
    id_set = set(article_ids)

    reader = Reader(cfg.reader_token)
    try:
        queue = reader.list_queue()
    finally:
        reader.close()

    articles = [
        a for a in queue
        if a["id"] in id_set and (a.get("html_content") or a.get("content"))
    ]
    articles.sort(key=lambda a: id_order.get(a["id"], 999))

    if not articles:
        return Response("no content available", status_code=404, media_type="text/plain")

    today = datetime.now(ZoneInfo(cfg.tz)).date()
    conn = store.connect(cfg.data_dir)
    try:
        volume_num = store.get_today_volume_number(conn)
        (cfg.data_dir / "epubs").mkdir(exist_ok=True)
        out_path = store.build_epub_path(cfg.data_dir, today.isoformat(), volume_num)

        epub_module.build_epub(today, articles, out_path, cfg.image_soft_cap_mb, volume=volume_num)

        total_words = sum(reader_word_count(a) for a in articles)
        store.activate_pending_digest(conn, digest_id, volume_num, total_words)
        for a in articles:
            store.record_sent_article(
                conn, digest_id, a["id"],
                a.get("title"), a.get("source_url") or a.get("url"),
                word_count=reader_word_count(a),
            )
        log.info(
            f"on-demand epub generated: {out_path.name} "
            f"({len(articles)} articles, {total_words} words)"
        )
    finally:
        conn.close()

    return FileResponse(out_path, media_type="application/epub+zip", filename=out_path.name)


@router.get("/file/library/{book_id}")
def file_library(book_id: int, request: Request):
    cfg = request.app.state.cfg
    conn = store.connect(cfg.data_dir)
    try:
        row = conn.execute(
            "SELECT filename, format FROM library_books WHERE id = ?", (book_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return Response("not found", status_code=404, media_type="text/plain")
    filename, fmt = row
    path = library.file_path(cfg.data_dir, filename)
    if not path.exists():
        return Response("file missing", status_code=404, media_type="text/plain")
    media = "application/epub+zip" if fmt == "epub" else "application/pdf"
    return FileResponse(path, media_type=media, filename=filename)


@router.get("/cover/digest/{digest_id}")
def cover_digest(digest_id: int, request: Request):
    placeholder = _PKG_DIR / "static" / "placeholder-epub.png"
    return FileResponse(placeholder, media_type="image/png")


@router.get("/cover/library/{book_id}")
def cover_library(book_id: int, request: Request):
    cfg = request.app.state.cfg
    conn = store.connect(cfg.data_dir)
    try:
        row = conn.execute(
            "SELECT format, has_cover FROM library_books WHERE id = ?", (book_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return Response("not found", status_code=404, media_type="text/plain")
    fmt, has_cover = row
    if has_cover:
        cp = library.cover_path(cfg.data_dir, book_id)
        if cp.exists():
            return FileResponse(cp, media_type="image/jpeg")
    placeholder_name = "placeholder-pdf.png" if fmt == "pdf" else "placeholder-epub.png"
    return FileResponse(_PKG_DIR / "static" / placeholder_name, media_type="image/png")
