import argparse
import io
import logging
import random
import sys
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from digest import config, epub, library, mailer, store
from digest.reader import Reader, tag_names, word_count as reader_word_count

_PKG_DIR = Path(__file__).parent
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

log = logging.getLogger("digest")
is_running = threading.Lock()


def _setup_global_logging(level: str) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter(_LOG_FORMAT))
    root.addHandler(sh)


def _attach_run_buffer() -> tuple[io.StringIO, logging.Handler]:
    buf = io.StringIO()
    bh = logging.StreamHandler(buf)
    bh.setFormatter(logging.Formatter(_LOG_FORMAT))
    logging.getLogger().addHandler(bh)
    return buf, bh


def _ensure_placeholder(out_path: Path, label: str) -> None:
    if out_path.exists():
        return
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (600, 900), "#f5f5f5")
    draw = ImageDraw.Draw(img)
    draw.rectangle([(80, 150), (520, 750)], outline="#888", width=4, fill="white")
    draw.line([(80, 220), (520, 220)], fill="#bbb", width=2)
    draw.line([(80, 280), (520, 280)], fill="#bbb", width=2)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf", 96)
    except OSError:
        font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), label, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((600 - tw) / 2, (900 - th) / 2 - 20), label, fill="#444", font=font)
    img.save(out_path, format="PNG")
    log.info(f"generated placeholder cover: {out_path}")


def _scaffold(cfg: config.Config) -> None:
    (cfg.data_dir / "epubs").mkdir(parents=True, exist_ok=True)
    (cfg.data_dir / "library" / "files").mkdir(parents=True, exist_ok=True)
    (cfg.data_dir / "library" / "covers").mkdir(parents=True, exist_ok=True)
    static = _PKG_DIR / "static"
    _ensure_placeholder(static / "placeholder-epub.png", "EPUB")
    _ensure_placeholder(static / "placeholder-pdf.png", "PDF")


def _sync_reader_epubs(
    cfg: config.Config,
    conn: sqlite3.Connection,
    reader: Reader,
    epub_items: list[dict],
) -> None:
    synced = store.already_synced_reader_epub_ids(conn)
    new_items = [a for a in epub_items if a["id"] not in synced]
    if not new_items:
        log.info("reader epub sync: nothing new")
        return
    log.info(f"reader epub sync: {len(new_items)} new epub(s) to download")
    for item in new_items:
        doc_id = item["id"]
        raw_url = item.get("raw_source_url")
        if not raw_url:
            log.warning(f"epub item missing raw_source_url: {doc_id!r} — skipping")
            continue
        try:
            epub_bytes = reader.download_epub(raw_url)
            library.store_reader_epub(
                conn, cfg.data_dir, doc_id, epub_bytes,
                title=item.get("title"),
                author=item.get("author"),
            )
        except Exception as e:
            log.warning(f"failed to sync reader epub {doc_id!r} ({item.get('title')!r}): {e}")


def run_once(
    cfg: config.Config, *, dry_run: bool = False, manual_trigger: bool = False
) -> int:
    log_buf, buf_handler = _attach_run_buffer()
    today = datetime.now(ZoneInfo(cfg.tz)).date()
    mode_bits: list[str] = []
    if dry_run:
        mode_bits.append("dry-run")
    if manual_trigger:
        mode_bits.append("manual")
    mode = f" ({', '.join(mode_bits)})" if mode_bits else ""
    log.info(f"=== digest run start: {today.isoformat()}{mode} ===")

    conn = store.connect(cfg.data_dir)
    store.prune_old_runs(conn)
    run_id = store.record_run_start(conn)

    if not manual_trigger and store.get_setting(conn, "paused") == "true":
        log.info("paused — skipping scheduled run (use manual trigger to override)")
        store.record_run_end(conn, run_id, "paused", log_buf.getvalue())
        return 0

    if not manual_trigger:
        interval = store.get_digest_interval_days(conn)
        last_day = store.get_last_sent_date(conn)
        if interval > 1 and last_day:
            days_since = (today - date.fromisoformat(last_day)).days
            if days_since < interval:
                log.info(
                    f"interval not elapsed — last sent {last_day} ({days_since}d ago), "
                    f"interval {interval}d; skipping"
                )
                store.record_run_end(conn, run_id, "interval-skip", log_buf.getvalue())
                return 0

    try:
        reader = Reader(cfg.reader_token)
        try:
            already = store.already_sent_ids(conn)
            queue = reader.list_queue()
            log.info(f"reader queue: {len(queue)} items, {len(already)} previously sent")

            _sync_reader_epubs(cfg, conn, reader,
                               [a for a in queue if a.get("category") == "epub"])

            _SKIP_CATEGORIES = {"epub", "video"}
            new = [a for a in queue if a["id"] not in already
                   and a.get("category") not in _SKIP_CATEGORIES]
            for a in queue:
                if a.get("category") in _SKIP_CATEGORIES and a["id"] not in already:
                    log.info(f"skipping {a.get('category')} item: {a.get('title')!r}")
            ready = [a for a in new if (a.get("html_content") or a.get("content"))]
            for a in new:
                if a not in ready:
                    log.info(f"skipping unparsed article (no html yet): {a.get('title')!r}")
            log.info(f"eligible after parse-check: {len(ready)}")

            random.shuffle(ready)
            budget = store.get_word_budget(conn)
            log.info(f"word budget: {budget}")

            selected: list[tuple[dict, int]] = []
            total_words = 0
            for a in ready:
                wc = reader_word_count(a)
                selected.append((a, wc))
                total_words += wc
                log.info(
                    f"selected: {a.get('title')!r} ({wc} words, running total {total_words})"
                )
                if total_words >= budget:
                    log.info(f"budget exceeded ({total_words} >= {budget}), stopping selection")
                    break

            if not selected:
                store.record_digest(conn, 0, "empty")
                store.record_run_end(conn, run_id, "empty", log_buf.getvalue())
                log.info("empty run — no email")
                return 0

            volume = store.get_today_volume_number(conn)
            articles_only = [a for a, _ in selected]
            (cfg.data_dir / "epubs").mkdir(exist_ok=True)
            out_path = store.build_epub_path(cfg.data_dir, today.isoformat(), volume)
            epub.build_epub(today, articles_only, out_path, cfg.image_soft_cap_mb, volume=volume)
            log.info(
                f"epub built: {out_path} ({out_path.stat().st_size} bytes, "
                f"vol {volume}, {total_words} words)"
            )

            if dry_run:
                store.record_run_end(conn, run_id, "ok", log_buf.getvalue())
                log.info(f"=== dry-run done: {len(selected)} articles, epub at {out_path} ===")
                log.info("dry-run: skipped Reader tag-add and sent_articles recording")
                return 0

            digest_id = store.record_digest(
                conn, len(selected), "sent", volume=volume, total_words=total_words
            )
            tag_errors: list[str] = []
            for a, wc in selected:
                store.record_sent_article(
                    conn, digest_id, a["id"],
                    a.get("title"), a.get("source_url") or a.get("url"),
                    word_count=wc,
                )
                try:
                    reader.add_tag(a["id"], tag_names(a), cfg.reader_tag_done)
                except Exception as e:
                    tag_errors.append(f"{a['id']}: {e}")
                    log.warning(f"failed to add done-tag to {a['id']}: {e}")

            outcome = "ok" if not tag_errors else "error"
            store.record_run_end(conn, run_id, outcome, log_buf.getvalue())

            if tag_errors:
                _try_send_alert(cfg, today, log_buf.getvalue(),
                                f"[inkbook-digest] partial failure on {today.isoformat()}")
            log.info(f"=== digest run done: {len(selected)} articles, {total_words} words, vol {volume} ===")
            return 0
        finally:
            reader.close()

    except Exception as e:
        log.exception(f"digest run failed: {e}")
        store.record_run_end(conn, run_id, "error", log_buf.getvalue())
        if not dry_run:
            _try_send_alert(cfg, today, log_buf.getvalue(),
                            f"[inkbook-digest] failure on {today.isoformat()}")
        return 1
    finally:
        try:
            conn.close()
        except Exception:
            pass
        logging.getLogger().removeHandler(buf_handler)


def run_with_lock(cfg: config.Config, *, manual_trigger: bool = False) -> None:
    """Wrapper for scheduler/trigger paths. Skips if another run is in progress."""
    if not is_running.acquire(blocking=False):
        log.warning("run skipped: another digest run is already in progress")
        return
    try:
        run_once(cfg, manual_trigger=manual_trigger)
    finally:
        is_running.release()


def _try_send_alert(cfg: config.Config, today: date, body: str, subject: str) -> None:
    try:
        mailer.send_alert(
            host=cfg.smtp_host, port=cfg.smtp_port,
            user=cfg.smtp_user, password=cfg.smtp_password,
            sender=cfg.smtp_from, recipient=cfg.alert_email,
            subject=subject, body=body,
        )
    except Exception as e:
        log.error(f"alert email failed too: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = config.load(require_smtp=True)
    _setup_global_logging(cfg.log_level)
    _scaffold(cfg)
    scheduler = BackgroundScheduler(timezone=ZoneInfo(cfg.tz))
    scheduler.add_job(
        lambda: run_with_lock(cfg),
        CronTrigger(hour=cfg.digest_hour, minute=cfg.digest_minute),
        id="daily-digest",
    )
    scheduler.start()
    next_run = scheduler.get_job("daily-digest").next_run_time
    log.info(f"scheduler started, next run: {next_run.isoformat()} ({cfg.tz})")
    moved = store.migrate_root_epubs(cfg.data_dir)
    log.info(f"startup epub migration: {moved} file(s) moved to epubs/")
    pruned = store.prune_old_epubs(cfg.data_dir)
    log.info(f"startup epub prune: {pruned} file(s) removed")
    app.state.cfg = cfg
    app.state.scheduler = scheduler
    app.state.is_running = is_running
    app.state.run_with_lock = run_with_lock
    yield
    scheduler.shutdown(wait=False)
    log.info("scheduler stopped")


app = FastAPI(lifespan=lifespan, title="inkbook-digest")
app.mount("/static", StaticFiles(directory=str(_PKG_DIR / "static")), name="static")


@app.get("/healthz")
def healthz() -> dict[str, bool]:
    return {"ok": True}


from digest.dashboard import router as _dashboard_router  # noqa: E402
from digest.opds import router as _opds_router  # noqa: E402

app.include_router(_dashboard_router)
app.include_router(_opds_router)


def main() -> int:
    parser = argparse.ArgumentParser(prog="digest")
    parser.add_argument("--once", action="store_true", help="run a single digest and exit")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="build EPUB but skip SMTP send, Reader tag-add, and sent_articles recording",
    )
    parser.add_argument(
        "--manual",
        action="store_true",
        help="manual trigger: bypass the paused setting (mirrors the dashboard's Trigger button)",
    )
    args = parser.parse_args()

    cfg = config.load(require_smtp=not args.dry_run)
    _setup_global_logging(cfg.log_level)

    if args.once or args.dry_run:
        return run_once(cfg, dry_run=args.dry_run, manual_trigger=args.manual)

    print(
        "Server mode: run via `uvicorn digest.main:app --host 0.0.0.0 --port 8080`.\n"
        "CLI mode: pass --once or --dry-run.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
