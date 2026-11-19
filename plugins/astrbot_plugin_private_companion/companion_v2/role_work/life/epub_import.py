"""Parse an EPUB for the existing private role-reading queue."""

import base64
import binascii
from pathlib import PurePosixPath
from types import SimpleNamespace

from companion_v2.execution.models import TaskError
from companion_v2.together.novel_library import NovelLibrary


MAX_EPUB_BYTES = 20 * 1024 * 1024
MAX_EPUB_BASE64 = ((MAX_EPUB_BYTES + 2) // 3) * 4
MAX_EPUB_TEXT = 10_000_000


def parse_epub_upload(filename, encoded):
    if (not isinstance(filename, str) or len(filename) > 255
            or not filename.lower().endswith(".epub") or not isinstance(encoded, str)):
        raise TaskError("life_epub_invalid", "select an EPUB file")
    if len(encoded) > MAX_EPUB_BASE64:
        raise TaskError("life_epub_too_large", "EPUB exceeds 20 MB")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise TaskError("life_epub_invalid", "invalid EPUB encoding") from None
    if not content:
        raise TaskError("life_epub_invalid", "empty EPUB")
    if len(content) > MAX_EPUB_BYTES:
        raise TaskError("life_epub_too_large", "EPUB exceeds 20 MB")
    fallback = PurePosixPath(filename.replace("\\", "/")).stem[:160] or "未命名书籍"
    # Reuse the existing EPUB parser without creating a separate Together library.
    parser = SimpleNamespace(max_upload_bytes=5 * 1024 * 1024)
    try:
        title, author, chapters = NovelLibrary._parse_epub(parser, content, fallback_title=fallback, strict=True)
    except Exception as error:
        raise TaskError("life_epub_invalid", "EPUB could not be parsed") from error
    text = "\n\n".join(body for _title, body in chapters if body.strip())
    if not text:
        raise TaskError("life_epub_invalid", "EPUB contains no readable text")
    if len(text) > MAX_EPUB_TEXT:
        raise TaskError("life_epub_too_large", "EPUB text exceeds 10 million characters")
    return {"title": title[:160], "author": author[:120], "text": text}
