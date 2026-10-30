"""Size limits for ZIP-based documents (DOCX, EPUB).

A DOCX or EPUB is a ZIP archive, and the parsers read its members fully
into memory. A small, highly compressed member can expand to gigabytes
(measured: a 200 KB DOCX expanded to 200 MB and took 1.4 GB of RAM to
parse). The limits below are far above any real book and are checked from
the archive's directory, before anything is decompressed.
"""

from __future__ import annotations

import zipfile

from atom_brain.exceptions import ExtractionError

MAX_TOTAL_UNCOMPRESSED = 512 * 1024 * 1024   # whole archive
MAX_MEMBER_UNCOMPRESSED = 128 * 1024 * 1024  # one XML/HTML part
MAX_RATIO = 100                               # uncompressed / compressed, per member
RATIO_CHECK_FLOOR = 8 * 1024 * 1024           # ratio only matters for large members


def check_zip_limits(zf: zipfile.ZipFile, label: str) -> None:
    """Raise ExtractionError when the archive would expand past the limits."""
    total = 0
    for info in zf.infolist():
        total += info.file_size
        if info.file_size > MAX_MEMBER_UNCOMPRESSED:
            raise ExtractionError(
                f"{label}: member '{info.filename}' expands to "
                f"{info.file_size // (1024 * 1024)} MB, over the "
                f"{MAX_MEMBER_UNCOMPRESSED // (1024 * 1024)} MB limit")
        if (info.file_size > RATIO_CHECK_FLOOR and info.compress_size
                and info.file_size / info.compress_size > MAX_RATIO):
            raise ExtractionError(
                f"{label}: member '{info.filename}' is compressed "
                f"{info.file_size // max(info.compress_size, 1)}:1, which looks "
                "like a decompression bomb")
    if total > MAX_TOTAL_UNCOMPRESSED:
        raise ExtractionError(
            f"{label}: archive expands to {total // (1024 * 1024)} MB, over the "
            f"{MAX_TOTAL_UNCOMPRESSED // (1024 * 1024)} MB limit")
