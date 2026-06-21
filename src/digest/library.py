import io
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from ebooklib import ITEM_COVER, ITEM_IMAGE, epub
from PIL import Image

log = logging.getLogger(__name__)

ALLOWED_EXTS = {".epub", ".pdf"}
COVER_MAX_EDGE = 600
COVER_QUALITY = 80


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _files_dir(data_dir: Path) -> Path:
    return data_dir / "library" / "files"


def _covers_dir(data_dir: Path) -> Path:
    return data_dir / "library" / "covers"


def cover_path(data_dir: Path, book_id: int) -> Path:
    return _covers_dir(data_dir) / f"{book_id}.jpg"


def file_path(data_dir: Path, filename: str) -> Path:
    return _files_dir(data_dir) / filename


def extract_epub_metadata(path: Path) -> dict:
    """Best-effort extract title/author/language from an EPUB. Never raises."""
    out: dict[str, str | None] = {"title": None, "author": None, "language": None}
    try:
        book = epub.read_epub(str(path), options={"ignore_ncx": True})
        title = book.get_metadata("DC", "title")
        if title:
            out["title"] = title[0][0] or None
        creator = book.get_metadata("DC", "creator")
        if creator:
            out["author"] = creator[0][0] or None
        language = book.get_metadata("DC", "language")
        if language:
            out["language"] = language[0][0] or None
    except Exception as e:
        log.warning(f"epub metadata extract failed for {path}: {e}")
    return out


def _find_cover_item(book: epub.EpubBook):
    for item in book.get_items_of_type(ITEM_COVER):
        return item
    for item in book.get_items_of_type(ITEM_IMAGE):
        name = (item.get_name() or "").lower()
        if "cover" in name:
            return item
    for item in book.get_items_of_type(ITEM_IMAGE):
        return item
    return None


def extract_epub_cover(path: Path, dest_path: Path) -> bool:
    """Extract first usable cover image, resize to <=600px long edge, save as JPEG q80."""
    try:
        book = epub.read_epub(str(path), options={"ignore_ncx": True})
        item = _find_cover_item(book)
        if item is None:
            return False
        img = Image.open(io.BytesIO(item.get_content()))
        img = img.convert("RGB")
        w, h = img.size
        long_edge = max(w, h)
        if long_edge > COVER_MAX_EDGE:
            scale = COVER_MAX_EDGE / long_edge
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(dest_path, format="JPEG", quality=COVER_QUALITY)
        return True
    except Exception as e:
        log.warning(f"epub cover extract failed for {path}: {e}")
        return False


def validate_upload(
    conn: sqlite3.Connection, filename: str, size_bytes: int, max_mb: int
) -> tuple[bool, str]:
    if not filename:
        return False, "missing filename"
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTS:
        return False, f"unsupported extension {ext or '(none)'}"
    if size_bytes <= 0:
        return False, "empty file"
    if size_bytes > max_mb * 1024 * 1024:
        return False, f"exceeds {max_mb}MB cap"
    row = conn.execute(
        "SELECT 1 FROM library_books WHERE filename = ?", (filename,)
    ).fetchone()
    if row:
        return False, "filename already exists"
    return True, ""


def store_upload(
    conn: sqlite3.Connection,
    data_dir: Path,
    file_stream: BinaryIO,
    filename: str,
) -> int:
    dest = file_path(data_dir, filename)
    dest.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    with dest.open("wb") as f:
        while True:
            chunk = file_stream.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)
            size += len(chunk)

    ext = Path(filename).suffix.lower().lstrip(".")
    title: str | None = None
    author: str | None = None
    language: str | None = None
    has_cover = 0

    if ext == "epub":
        meta = extract_epub_metadata(dest)
        title, author, language = meta["title"], meta["author"], meta["language"]

    cur = conn.execute(
        "INSERT INTO library_books "
        "(filename, title, author, language, format, size_bytes, has_cover, added_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (filename, title, author or "Unknown", language, ext, size, 0, _now()),
    )
    book_id = cur.lastrowid

    if ext == "epub":
        if extract_epub_cover(dest, cover_path(data_dir, book_id)):
            has_cover = 1
            conn.execute(
                "UPDATE library_books SET has_cover = 1 WHERE id = ?", (book_id,)
            )

    log.info(
        f"library upload stored: id={book_id} filename={filename!r} "
        f"size={size} format={ext} has_cover={has_cover}"
    )
    return book_id


def store_reader_epub(
    conn: sqlite3.Connection,
    data_dir: Path,
    doc_id: str,
    epub_bytes: bytes,
    title: str | None,
    author: str | None,
) -> int:
    filename = f"readwise-{doc_id}.epub"
    dest = file_path(data_dir, filename)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(epub_bytes)

    meta = extract_epub_metadata(dest)
    resolved_title = meta["title"] or title
    resolved_author = meta["author"] or author
    language = meta.get("language")

    cur = conn.execute(
        "INSERT INTO library_books "
        "(filename, title, author, language, format, size_bytes, has_cover, added_at, readwise_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (filename, resolved_title, resolved_author or "Unknown", language,
         "epub", len(epub_bytes), 0, _now(), doc_id),
    )
    book_id = cur.lastrowid

    if extract_epub_cover(dest, cover_path(data_dir, book_id)):
        conn.execute("UPDATE library_books SET has_cover = 1 WHERE id = ?", (book_id,))

    log.info(f"reader epub synced: id={book_id} doc_id={doc_id!r} title={resolved_title!r}")
    return book_id


def delete_book(conn: sqlite3.Connection, data_dir: Path, book_id: int) -> bool:
    row = conn.execute(
        "SELECT filename FROM library_books WHERE id = ?", (book_id,)
    ).fetchone()
    if not row:
        return False
    filename = row[0]
    fp = file_path(data_dir, filename)
    cp = cover_path(data_dir, book_id)
    try:
        fp.unlink()
    except FileNotFoundError:
        log.warning(f"library delete: file already missing: {fp}")
    except OSError as e:
        log.warning(f"library delete: failed to remove file {fp}: {e}")
    try:
        cp.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        log.warning(f"library delete: failed to remove cover {cp}: {e}")
    conn.execute("DELETE FROM library_books WHERE id = ?", (book_id,))
    log.info(f"library book deleted: id={book_id} filename={filename!r}")
    return True


def list_books(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, filename, title, author, language, format, size_bytes, "
        "has_cover, added_at FROM library_books ORDER BY added_at DESC"
    ).fetchall()
    return [
        {
            "id": r[0], "filename": r[1], "title": r[2], "author": r[3],
            "language": r[4], "format": r[5], "size_bytes": r[6],
            "has_cover": bool(r[7]), "added_at": r[8],
        }
        for r in rows
    ]
