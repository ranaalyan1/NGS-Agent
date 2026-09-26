"""Small helpers shared by core modules (hashing, safe IO)."""

from __future__ import annotations

import hashlib
from pathlib import Path


def sha256_file(path: str | Path, limit: int | None = None) -> str:
    """SHA-256 of a file's bytes.

    Every report footer carries this so a forwarded HTML can be tied back to
    the exact bytes it was made from. Never computed from the filename.
    """
    h = hashlib.sha256()
    remaining = limit
    try:
        fh = Path(path).open("rb")
    except OSError:
        # A directory, a broken symlink, no permission: no hash, no crash.
        return ""
    with fh:
        while True:
            size = 1024 * 256 if remaining is None else min(1024 * 256, remaining)
            chunk = fh.read(size)
            if not chunk:
                break
            h.update(chunk)
            if remaining is not None:
                remaining -= len(chunk)
                if remaining <= 0:
                    break
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def short_sha(path: str | Path) -> str:
    """First 12 hex chars — enough to identify a file in a receipt."""
    return sha256_file(path)[:12]


def read_head(path: str | Path, nbytes: int) -> bytes:
    """First ``nbytes`` of a file. Missing/unreadable -> b'' (never raises)."""
    try:
        with Path(path).open("rb") as fh:
            return fh.read(nbytes)
    except OSError:
        return b""


def read_gzip_head(path: str | Path, nbytes: int) -> tuple[bytes, bool | None]:
    """First ``nbytes`` of *decompressed* content, plus a completeness verdict.

    Returns ``(content, intact)`` where ``intact`` is:

    * ``True``  — the whole gzip stream was read and ended cleanly;
    * ``False`` — the stream is truncated or damaged (content is partial);
    * ``None``  — we stopped early because we already had enough content to
      identify the file, so completeness was not checked.

    Truncated streams must not raise: a BAM cut short by a failed transfer
    still has to be recognised as a BAM, with the caller told it is partial.
    """
    import zlib

    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = bytearray()
    intact: bool | None = None
    saw_all_input = False
    try:
        with Path(path).open("rb") as fh:
            while len(out) < nbytes:
                chunk = fh.read(65536)
                if not chunk:
                    saw_all_input = True
                    break
                # With a max_length, zlib leaves the rest of this chunk in
                # unconsumed_tail; keep feeding it until the chunk is spent.
                pending = chunk
                while pending and len(out) < nbytes:
                    try:
                        piece = decompressor.decompress(pending, nbytes - len(out))
                    except zlib.error:
                        intact = False
                        break
                    if not piece:
                        break
                    out += piece
                    pending = decompressor.unconsumed_tail
                if intact is False:
                    break
        if intact is None and saw_all_input:
            try:
                out += decompressor.flush()
                intact = decompressor.eof
            except zlib.error:
                intact = False
    except OSError:
        return b"", False
    return bytes(out[:nbytes]), intact


def gzip_stream_is_intact(path: str | Path, budget: int = 2 * 1024 * 1024) -> bool | None:
    """Is this small gzip file complete? ``None`` if it exceeds the budget.

    Only ever called for files small enough that a full scan is cheap; a 30 GB
    BAM is out of budget and we simply stay silent about its completeness
    rather than pretending to have checked.
    """
    import zlib

    p = Path(path)
    try:
        if p.stat().st_size > budget:
            return None
    except OSError:
        return False
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        with p.open("rb") as fh:
            while True:
                chunk = fh.read(262144)
                if not chunk:
                    break
                while chunk:
                    decompressor.decompress(chunk)
                    chunk = decompressor.unconsumed_tail
                    if decompressor.eof:
                        break
                if decompressor.eof:
                    break
    except (OSError, zlib.error):
        return False
    return decompressor.eof


def decode(sample: bytes) -> str:
    """Best-effort decode of a sniffed sample. Bad bytes are dropped, not fatal."""
    return sample.decode("utf-8", errors="replace")


def first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line
    return ""
