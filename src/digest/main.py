import argparse
import logging
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from digest import config, library, store
from digest.reader import Reader

_PKG_DIR = Path(__file__).parent
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

log = logging.getLogger("digest")


def _setup_global_logging(level: str) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter(_LOG_FORMAT))
    root.addHandler(sh)


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
    conn,
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = config.load(require_smtp=False)
    _setup_global_logging(cfg.log_level)
    _scaffold(cfg)

    def _epub_sync_job() -> None:
        conn = store.connect(cfg.data_dir)
        reader = Reader(cfg.reader_token)
        try:
            queue = reader.list_queue()
            epub_items = [a for a in queue if a.get("category") == "epub"]
            _sync_reader_epubs(cfg, conn, reader, epub_items)
            moved = store.migrate_root_epubs(cfg.data_dir)
            if moved:
                log.info(f"epub migration: {moved} file(s) moved")
            pruned = store.prune_old_epubs(cfg.data_dir)
            if pruned:
                log.info(f"epub prune: {pruned} file(s) removed")
            store.prune_old_runs(conn)
        except Exception as e:
            log.exception(f"epub sync failed: {e}")
        finally:
            reader.close()
            conn.close()

    scheduler = BackgroundScheduler(timezone=ZoneInfo(cfg.tz))
    scheduler.add_job(
        _epub_sync_job,
        CronTrigger(minute=0),
        id="epub-sync",
    )
    scheduler.start()
    next_run = scheduler.get_job("epub-sync").next_run_time
    log.info(f"scheduler started, next epub sync: {next_run.isoformat()} ({cfg.tz})")

    threading.Thread(target=_epub_sync_job, daemon=True).start()

    app.state.cfg = cfg
    app.state.scheduler = scheduler
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
    parser.add_argument("--once", action="store_true", help="run epub sync once and exit")
    args = parser.parse_args()

    cfg = config.load(require_smtp=False)
    _setup_global_logging(cfg.log_level)

    if args.once:
        conn = store.connect(cfg.data_dir)
        reader = Reader(cfg.reader_token)
        try:
            queue = reader.list_queue()
            epub_items = [a for a in queue if a.get("category") == "epub"]
            _sync_reader_epubs(cfg, conn, reader, epub_items)
        finally:
            reader.close()
            conn.close()
        return 0

    print(
        "Server mode: run via `uvicorn digest.main:app --host 0.0.0.0 --port 8080`.\n"
        "CLI mode: pass --once to run epub sync and exit.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
