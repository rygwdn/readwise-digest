import logging
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from digest import library, store
from digest.opds import SKIP_CATEGORIES, is_eligible
from digest.reader import Reader, tag_names, word_count as reader_word_count

log = logging.getLogger(__name__)

_PKG_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(_PKG_DIR / "templates"))

LOG_DISPLAY_CAP_BYTES = 20 * 1024
TABLE_LIMIT = 30
QUEUE_TTL_OK = 60
QUEUE_TTL_ERR = 30
WB_MIN = 500
WB_MAX = 50000
INTERVAL_MIN = 1
INTERVAL_MAX = 99

_queue_cache: dict = {"data": None, "expires": 0.0, "error": None}

router = APIRouter()


def _domain(url):
    if not url:
        return ""
    try:
        netloc = urlparse(url).netloc
        return netloc[4:] if netloc.startswith("www.") else netloc
    except Exception:
        return ""


def _human_size(b):
    if b is None:
        return ""
    b = float(b)
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024 or unit == "GB":
            return f"{b:.0f} {unit}" if unit == "B" else f"{b:.1f} {unit}"
        b /= 1024


templates.env.filters["domain"] = _domain
templates.env.filters["human_size"] = _human_size


def _opds_url(cfg, request: Request) -> str:
    base = cfg.base_url or str(request.base_url).rstrip("/")
    return f"{base}/opds/"


def _upload_flashes(request: Request) -> list[dict]:
    kinds = request.query_params.getlist("library_upload")
    names = request.query_params.getlist("name")
    reasons = request.query_params.getlist("reason")
    out = []
    for i, kind in enumerate(kinds):
        out.append({
            "kind": kind,
            "name": names[i] if i < len(names) else "",
            "reason": reasons[i] if i < len(reasons) else "",
        })
    return out


def _truncate_log(s: str | None) -> str:
    if not s:
        return ""
    b = s.encode("utf-8")
    if len(b) <= LOG_DISPLAY_CAP_BYTES:
        return s
    return b[:LOG_DISPLAY_CAP_BYTES].decode("utf-8", errors="ignore") + "\n... (truncated)"


def _get_queue_stats(cfg, conn) -> tuple[dict | None, str | None]:
    now = time.time()
    if _queue_cache["expires"] > now:
        return _queue_cache["data"], _queue_cache["error"]
    try:
        reader = Reader(cfg.reader_token)
        try:
            articles = reader.list_queue()
        finally:
            reader.close()
        sent = store.already_sent_ids(conn)
        done_tag = cfg.reader_tag_done
        eligible = [
            a for a in articles
            if a["id"] not in sent
            and a.get("category") not in SKIP_CATEGORIES
            and done_tag not in tag_names(a)
            and is_eligible(a)[0]
        ]
        total_words = sum(reader_word_count(a) for a in eligible)
        stats = {"count": len(eligible), "total_words": total_words}
        _queue_cache.update(data=stats, error=None, expires=now + QUEUE_TTL_OK)
        return stats, None
    except Exception as e:
        log.warning(f"queue stats failed: {e}")
        _queue_cache.update(data=None, error=str(e), expires=now + QUEUE_TTL_ERR)
        return None, str(e)


def _chart_data(conn) -> list[dict]:
    today = date.today()
    rows = conn.execute(
        "SELECT DATE(sent_at) AS day, COUNT(*) AS n, COALESCE(SUM(word_count), 0) AS words "
        "FROM sent_articles WHERE sent_at >= DATE('now', '-29 days') "
        "GROUP BY day"
    ).fetchall()
    by_day = {r[0]: (r[1], r[2]) for r in rows}
    out = []
    for i in range(30):
        d = (today - timedelta(days=29 - i)).isoformat()
        n, w = by_day.get(d, (0, 0))
        out.append({"day": d, "n": n, "words": w})
    return out


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    cfg = request.app.state.cfg
    scheduler = request.app.state.scheduler

    conn = store.connect(cfg.data_dir)
    try:
        last = conn.execute(
            "SELECT sent_at, article_count, volume, total_words FROM digests "
            "WHERE status = 'sent' ORDER BY sent_at DESC LIMIT 1"
        ).fetchone()

        totals_row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(article_count), 0), COALESCE(SUM(total_words), 0) "
            "FROM digests WHERE status = 'sent'"
        ).fetchone()
        totals = {
            "digests": totals_row[0],
            "articles": totals_row[1],
            "words": totals_row[2],
        }

        digests = [
            {
                "id": r[0], "sent_at": r[1], "volume": r[2], "article_count": r[3],
                "total_words": r[4], "status": r[5],
            }
            for r in conn.execute(
                "SELECT id, sent_at, volume, article_count, total_words, status FROM digests "
                "ORDER BY sent_at DESC LIMIT ?", (TABLE_LIMIT,),
            )
        ]

        runs = []
        for r in conn.execute(
            "SELECT id, started_at, finished_at, outcome, log FROM runs "
            "ORDER BY started_at DESC LIMIT ?", (TABLE_LIMIT,),
        ):
            duration = None
            if r[2]:
                duration = (
                    datetime.fromisoformat(r[2]) - datetime.fromisoformat(r[1])
                ).total_seconds()
            runs.append({
                "id": r[0], "started_at": r[1], "outcome": r[3] or "running",
                "duration": duration, "log": _truncate_log(r[4]),
            })

        articles = [
            {"title": r[0] or "(untitled)", "url": r[1] or "", "word_count": r[2], "sent_at": r[3]}
            for r in conn.execute(
                "SELECT title, url, word_count, sent_at FROM sent_articles "
                "ORDER BY sent_at DESC LIMIT ?", (TABLE_LIMIT,),
            )
        ]

        paused = store.get_setting(conn, "paused") == "true"
        word_budget = store.get_word_budget(conn)
        digest_interval = store.get_digest_interval_days(conn)
        feed_mode = store.get_feed_mode(conn)
        queue_stats, queue_error = _get_queue_stats(cfg, conn)
        chart = _chart_data(conn)
        books = library.list_books(conn)
    finally:
        conn.close()

    next_run = None
    job = scheduler.get_job("epub-sync")
    if job and job.next_run_time:
        next_run = job.next_run_time.isoformat()

    upload_flashes = _upload_flashes(request)
    delete_flash = (
        request.query_params.get("name", "")
        if request.query_params.get("library_delete") == "ok"
        else None
    )

    return templates.TemplateResponse(
        request, "index.html", {
            "last_digest": last, "next_run": next_run, "paused": paused,
            "queue_stats": queue_stats, "queue_error": queue_error,
            "totals": totals, "digests": digests, "runs": runs, "articles": articles,
            "chart_data": chart, "word_budget": word_budget,
            "wb_min": WB_MIN, "wb_max": WB_MAX,
            "digest_interval": digest_interval,
            "interval_min": INTERVAL_MIN, "interval_max": INTERVAL_MAX,
            "triggered": request.query_params.get("triggered"),
            "budget_flash": request.query_params.get("budget"),
            "interval_flash": request.query_params.get("interval"),
            "is_running": False,
            "feed_mode": feed_mode,
            "feed_mode_flash": request.query_params.get("feed_mode"),
            "books": books,
            "opds_url": _opds_url(cfg, request),
            "upload_flashes": upload_flashes,
            "delete_flash": delete_flash,
        },
    )


@router.get("/digests/{digest_id}/epub")
def download_epub(digest_id: int, request: Request):
    cfg = request.app.state.cfg
    conn = store.connect(cfg.data_dir)
    try:
        row = conn.execute(
            "SELECT sent_at, volume FROM digests WHERE id = ?", (digest_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return PlainTextResponse("not found", status_code=404)
    path = store.build_epub_path(cfg.data_dir, row[0], row[1])
    if not path.exists():
        return PlainTextResponse("EPUB no longer available", status_code=404)
    return FileResponse(path, filename=path.name)


@router.get("/runs/{run_id}/log", response_class=PlainTextResponse)
def run_log(run_id: int, request: Request):
    cfg = request.app.state.cfg
    conn = store.connect(cfg.data_dir)
    try:
        row = conn.execute("SELECT log FROM runs WHERE id = ?", (run_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return PlainTextResponse("not found", status_code=404)
    return PlainTextResponse(row[0] or "")


@router.post("/trigger")
def trigger(request: Request) -> RedirectResponse:
    cfg = request.app.state.cfg
    conn = store.connect(cfg.data_dir)
    try:
        pruned = conn.execute(
            "DELETE FROM digests WHERE status = 'pending'"
        ).rowcount
        log.info(f"trigger: cleared {pruned} pending digest(s); will regenerate on next feed poll")
    finally:
        conn.close()
    return RedirectResponse(url="/?triggered=ok", status_code=303)


@router.post("/pause")
def pause(request: Request) -> RedirectResponse:
    cfg = request.app.state.cfg
    conn = store.connect(cfg.data_dir)
    try:
        current = store.get_setting(conn, "paused") or "false"
        new_value = "false" if current == "true" else "true"
        store.set_setting(conn, "paused", new_value)
        log.info(f"pause toggled: {current} -> {new_value}")
    finally:
        conn.close()
    return RedirectResponse(url="/", status_code=303)


@router.post("/word-budget")
def word_budget(request: Request, value: str = Form(...)) -> RedirectResponse:
    try:
        n = int(value.strip())
    except (ValueError, AttributeError):
        return RedirectResponse(url="/?budget=invalid", status_code=303)
    if n < WB_MIN or n > WB_MAX:
        return RedirectResponse(url="/?budget=invalid", status_code=303)
    cfg = request.app.state.cfg
    conn = store.connect(cfg.data_dir)
    try:
        store.set_setting(conn, "word_budget", str(n))
        log.info(f"word_budget updated to {n}")
    finally:
        conn.close()
    return RedirectResponse(url="/?budget=ok", status_code=303)


@router.post("/feed-mode")
def feed_mode_update(request: Request, value: str = Form(...)) -> RedirectResponse:
    if value not in ("single", "multi"):
        return RedirectResponse(url="/?feed_mode=invalid", status_code=303)
    cfg = request.app.state.cfg
    conn = store.connect(cfg.data_dir)
    try:
        store.set_setting(conn, "feed_mode", value)
        log.info(f"feed_mode updated to {value!r}")
    finally:
        conn.close()
    return RedirectResponse(url="/?feed_mode=ok", status_code=303)


@router.post("/digest-interval")
def digest_interval(request: Request, value: str = Form(...)) -> RedirectResponse:
    try:
        n = int(value.strip())
    except (ValueError, AttributeError):
        return RedirectResponse(url="/?interval=invalid", status_code=303)
    if n < INTERVAL_MIN or n > INTERVAL_MAX:
        return RedirectResponse(url="/?interval=invalid", status_code=303)
    cfg = request.app.state.cfg
    conn = store.connect(cfg.data_dir)
    try:
        store.set_setting(conn, "digest_interval_days", str(n))
        log.info(f"digest_interval_days updated to {n}")
    finally:
        conn.close()
    return RedirectResponse(url="/?interval=ok", status_code=303)


def _upload_size(upload: UploadFile) -> int:
    if upload.size is not None:
        return upload.size
    upload.file.seek(0, 2)
    n = upload.file.tell()
    upload.file.seek(0)
    return n


@router.post("/library/upload")
async def library_upload(
    request: Request, files: list[UploadFile] = File(...)
) -> RedirectResponse:
    cfg = request.app.state.cfg
    conn = store.connect(cfg.data_dir)
    pairs: list[tuple[str, str]] = []
    try:
        for f in files:
            name = Path(f.filename or "").name
            if not name:
                pairs += [("library_upload", "invalid"), ("name", ""), ("reason", "missing filename")]
                continue
            size = _upload_size(f)
            ok, reason = library.validate_upload(conn, name, size, cfg.library_max_upload_mb)
            if not ok:
                kind = "conflict" if "exists" in reason else "invalid"
                pairs += [("library_upload", kind), ("name", name), ("reason", reason if kind == "invalid" else "")]
                continue
            try:
                f.file.seek(0)
                library.store_upload(conn, cfg.data_dir, f.file, name)
                pairs += [("library_upload", "ok"), ("name", name), ("reason", "")]
            except Exception as e:
                log.exception(f"upload failed for {name}: {e}")
                pairs += [("library_upload", "invalid"), ("name", name), ("reason", "store error")]
    finally:
        conn.close()
    qs = "&".join(f"{k}={quote(v, safe='')}" for k, v in pairs)
    return RedirectResponse(url=f"/?{qs}" if qs else "/", status_code=303)


@router.post("/library/{book_id}/delete")
def library_delete(book_id: int, request: Request) -> RedirectResponse:
    cfg = request.app.state.cfg
    conn = store.connect(cfg.data_dir)
    try:
        row = conn.execute(
            "SELECT filename FROM library_books WHERE id = ?", (book_id,)
        ).fetchone()
        name = row[0] if row else ""
        library.delete_book(conn, cfg.data_dir, book_id)
    finally:
        conn.close()
    return RedirectResponse(
        url=f"/?library_delete=ok&name={quote(name, safe='')}", status_code=303
    )
