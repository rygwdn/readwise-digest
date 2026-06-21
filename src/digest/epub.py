import hashlib
import html as html_lib
import io
import logging
import mimetypes
import re
from datetime import date
from urllib.parse import urljoin, urlparse

import httpx
from ebooklib import epub
from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)

CSS = """
body { font-family: serif; line-height: 1.5; text-align: justify; margin: 1em; }
h1 { font-size: 1.5em; margin-top: 1em; }
p.meta { font-size: 0.9em; font-style: italic; margin-bottom: 0.5em; }
p.source { font-size: 0.85em; word-break: break-all; }
img { max-width: 100%; height: auto; }
blockquote { border-left: 3px solid black; margin-left: 0; padding-left: 1em; }
pre, code { font-family: monospace; font-size: 0.9em; }
ol.toc { padding-left: 1.2em; }
ol.toc li { margin-bottom: 0.8em; }
""".strip()

_IMG_TAG_RE = re.compile(r'<img\b[^>]*\bsrc=["\']([^"\']+)["\'][^>]*>', re.IGNORECASE)
_LINK_RE = re.compile(r'<a\b[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)


def _source_domain(url: str | None) -> str:
    if not url:
        return ""
    try:
        return urlparse(url).netloc.lstrip("www.")
    except Exception:
        return ""


def _safe_text(value) -> str:
    return str(value) if value else ""


def _strip_links(html: str) -> str:
    return _LINK_RE.sub(r'\1', html)


def make_cover(today: date, article_count: int, volume: int = 1) -> bytes:
    img = Image.new("L", (600, 900), 255)
    draw = ImageDraw.Draw(img)
    try:
        title_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf", 56)
        meta_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf", 32)
    except OSError:
        title_font = ImageFont.load_default()
        meta_font = ImageFont.load_default()
    draw.text((50, 220), f"Paper {today.isoformat()} v{volume}", fill=0, font=title_font)
    draw.text(
        (50, 380),
        f"{article_count} article{'s' if article_count != 1 else ''}",
        fill=64,
        font=meta_font,
    )
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def _fetch_image(client: httpx.Client, url: str) -> tuple[bytes, str] | None:
    try:
        r = client.get(url, timeout=30.0, follow_redirects=True)
        r.raise_for_status()
    except (httpx.HTTPError, httpx.InvalidURL) as e:
        log.warning(f"image fetch failed: {url}: {e}")
        return None
    content_type = r.headers.get("content-type", "").split(";")[0].strip()
    ext = mimetypes.guess_extension(content_type) or ".bin"
    return r.content, ext


def _to_eink_image(content: bytes, ext: str) -> tuple[bytes, str]:
    try:
        img = Image.open(io.BytesIO(content))
        img = img.convert("L")
        w, h = img.size
        if w > 600:
            img = img.resize((600, int(h * 600 / w)), Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=70)
        return out.getvalue(), ".jpeg"
    except Exception as e:
        log.warning(f"e-ink image convert failed: {e}")
        return content, ext


def _process_article_html(
    client: httpx.Client,
    book: epub.EpubBook,
    html: str,
    base_url: str | None,
    article_id: str,
) -> tuple[str, int]:
    """Replace <img> srcs with grayscale internal refs. Returns (new_html, total_bytes)."""
    cache: dict[str, str] = {}
    total_bytes = 0

    def repl(match: re.Match[str]) -> str:
        nonlocal total_bytes
        whole = match.group(0)
        raw_src = match.group(1)
        src = html_lib.unescape(raw_src)
        if src.startswith("data:"):
            return whole
        absolute = urljoin(base_url, src) if base_url else src
        if absolute in cache:
            return whole.replace(raw_src, cache[absolute])
        fetched = _fetch_image(client, absolute)
        if fetched is None:
            return whole
        content, ext = _to_eink_image(*fetched)
        digest = hashlib.sha1(absolute.encode()).hexdigest()[:12]
        internal = f"images/{article_id}_{digest}{ext}"
        item = epub.EpubImage(
            uid=f"img_{article_id}_{digest}",
            file_name=internal,
            media_type=mimetypes.guess_type(internal)[0] or "application/octet-stream",
            content=content,
        )
        book.add_item(item)
        cache[absolute] = internal
        total_bytes += len(content)
        return whole.replace(raw_src, internal)

    new_html = _IMG_TAG_RE.sub(repl, html or "")
    return new_html, total_bytes


def _build_toc_chapter(articles: list[dict], style: epub.EpubItem) -> epub.EpubHtml:
    rows = []
    for i, article in enumerate(articles):
        title = _safe_text(article.get("title")) or "(untitled)"
        author = _safe_text(article.get("author"))
        wc = article.get("word_count")
        meta = " · ".join(b for b in (author, f"{wc} words" if wc else "") if b)
        rows.append(
            f'<li><a href="chap_{i}.xhtml">{_html_escape(title)}</a>'
            + (f"<br/><small>{_html_escape(meta)}</small>" if meta else "")
            + "</li>"
        )
    chap = epub.EpubHtml(uid="toc", title="Contents", file_name="toc.xhtml", lang="en")
    chap.content = (
        "<html><head><title>Contents</title>"
        '<link rel="stylesheet" href="style/style.css" type="text/css"/></head>'
        '<body><h1>Contents</h1><ol class="toc">'
        + "\n".join(rows)
        + "</ol></body></html>"
    )
    chap.add_item(style)
    return chap


def _build_chapter_xhtml(item: dict, body_html: str) -> str:
    title = _safe_text(item.get("title")) or "(untitled)"
    author = _safe_text(item.get("author"))
    domain = _source_domain(item.get("source_url") or item.get("url"))
    pub_date = _safe_text(item.get("published_date"))
    word_count = item.get("word_count")
    source_url = item.get("source_url") or item.get("url") or ""

    meta_bits = [b for b in (author, domain, pub_date,
                             f"{word_count} words" if word_count else "") if b]
    meta = " · ".join(meta_bits)

    return (
        f"<html><head><title>{_html_escape(title)}</title>"
        f'<link rel="stylesheet" href="style/style.css" type="text/css"/></head>'
        f"<body>"
        f"<h1>{_html_escape(title)}</h1>"
        f'<p class="meta">{_html_escape(meta)}</p>'
        f'<p class="source">{_html_escape(source_url)}</p>'
        f"{_strip_links(body_html)}"
        f"</body></html>"
    )


def _html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def build_epub(
    today: date,
    articles: list[dict],
    out_path,
    image_soft_cap_mb: int,
    volume: int = 1,
) -> None:
    book = epub.EpubBook()
    suffix = "" if volume == 1 else f"-vol-{volume}"
    title_suffix = "" if volume == 1 else f" (Vol. {volume})"
    book.set_identifier(f"morning-paper-{today.isoformat()}{suffix}")
    book.set_title(f"Paper {today.isoformat()} v{volume}")
    book.set_language("en")

    cover_bytes = make_cover(today, len(articles), volume=volume)
    book.set_cover("cover.png", cover_bytes)

    style = epub.EpubItem(
        uid="style", file_name="style/style.css",
        media_type="text/css", content=CSS,
    )
    book.add_item(style)

    toc_chap = _build_toc_chapter(articles, style)
    book.add_item(toc_chap)

    chapters: list[epub.EpubHtml] = []
    soft_cap_bytes = image_soft_cap_mb * 1024 * 1024

    with httpx.Client(headers={"User-Agent": "inkbook-digest/0.1"}) as img_client:
        for idx, article in enumerate(articles):
            article_id = article.get("id", f"a{idx}")
            html = article.get("html_content") or article.get("content") or ""
            base_url = article.get("source_url") or article.get("url")
            processed_html, img_bytes = _process_article_html(
                img_client, book, html, base_url, article_id
            )
            if img_bytes > soft_cap_bytes:
                log.warning(
                    f"article '{article.get('title')}' images = {img_bytes/1024/1024:.1f}MB "
                    f"(soft cap {image_soft_cap_mb}MB) — included anyway"
                )

            title = _safe_text(article.get("title")) or "(untitled)"
            domain = _source_domain(base_url)
            chap = epub.EpubHtml(
                uid=f"chap_{idx}",
                title=f"{title} — {domain}" if domain else title,
                file_name=f"chap_{idx}.xhtml",
                lang="en",
            )
            chap.content = _build_chapter_xhtml(article, processed_html)
            chap.add_item(style)
            book.add_item(chap)
            chapters.append(chap)

    book.toc = tuple(chapters)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = [toc_chap, *chapters]

    epub.write_epub(str(out_path), book)
