"""Write color tags into a generated video's container without touching its frames or timing.

Draw Things' H.264 (and older ProRes) files carry no ``colr`` box, so players guess the color matrix. Remuxing with
ffmpeg would add one, but it rewrites the timestamps of these files (they have ``pts < dts``), so this edits the
MP4/QuickTime boxes directly: one ``colr`` box is added to the video sample entry, the sizes of its parent boxes are
raised by that many bytes, and, when the ``moov`` box comes before the media data, the chunk offsets are shifted. The
media data and every timestamp stay byte-identical.
"""

from __future__ import annotations

import os
import shutil
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

# ITU-T H.273 codes: BT.709 primaries, the sRGB transfer (the PNG label uses the same), and the BT.709 matrix.
PRIMARIES_BT709 = 1
TRANSFER_SRGB = 13
MATRIX_BT709 = 1
# The visual sample entry's fixed fields (reserved, data reference, size, resolution, compressor name, depth, and so on).
VISUAL_ENTRY_FIELDS = 78
# Boxes that hold the path to the video sample entry.
CONTAINERS = (b"moov", b"trak", b"mdia", b"minf", b"stbl")
COPY_BLOCK = 1 << 20


@dataclass(frozen=True)
class Box:
    """A box's place in the file."""

    kind: bytes
    start: int
    header: int
    end: int

    @property
    def body(self) -> int:
        return self.start + self.header


def tag_video_colors(video: Path) -> bool:
    """Add BT.709 / sRGB / BT.709 color tags to ``video`` if it has none; return whether the file was changed.

    A video that already has a ``colr`` box is left alone, since its tags are the writer's statement. The file is
    replaced atomically by a copy that differs only by the new box. Raises ValueError, leaving the file untouched,
    when it is not a plain (non-fragmented) MP4 or QuickTime file with a video track this can edit.
    """
    with video.open("rb") as file:
        size = os.fstat(file.fileno()).st_size
        top = _boxes(file, 0, size)
        if any(box.kind == b"moof" for box in top):
            raise ValueError(f"{video} is a fragmented MP4, which this cannot tag")
        moov = next((box for box in top if box.kind == b"moov"), None)
        if moov is None:
            raise ValueError(f"{video} has no moov box, so it is not an MP4 or QuickTime file this can tag")
        brand = _major_brand(file, top)
        file.seek(moov.start)
        old = file.read(moov.end - moov.start)
        patched = _patch_moov(old, moov, brand, media_after_moov=any(box.kind == b"mdat" and box.start > moov.start for box in top))
    if patched is None:
        return False
    added = len(patched) - len(old)
    _replace(video, moov, patched, size)
    logger.info("Tagged {} as BT.709 primaries, sRGB transfer, BT.709 matrix ({} bytes added)", video.name, added)
    return True


def _boxes(file, start: int, end: int) -> list[Box]:
    """The boxes laid out one after another in ``[start, end)``."""
    boxes: list[Box] = []
    position = start
    while position < end:
        file.seek(position)
        head = file.read(16)
        if len(head) < 8:
            raise ValueError("truncated box header")
        size, kind = struct.unpack(">I4s", head[:8])
        header = 8
        if size == 1:
            if len(head) < 16:
                raise ValueError("truncated box header")
            size, header = struct.unpack(">Q", head[8:16])[0], 16
        elif size == 0:
            size = end - position
        if size < header or position + size > end:
            raise ValueError(f"invalid box '{kind.decode('latin1')}' at offset {position}")
        boxes.append(Box(kind, position, header, position + size))
        position += size
    return boxes


def _major_brand(file, top: list[Box]) -> bytes:
    ftyp = next((box for box in top if box.kind == b"ftyp"), None)
    if ftyp is None:
        return b""
    file.seek(ftyp.body)
    return file.read(4)


def _patch_moov(moov: bytes, box: Box, brand: bytes, *, media_after_moov: bool) -> bytes | None:
    """``moov`` with a colr box added to the video sample entry, or None if it already has one."""
    data = bytearray(moov)
    tree = _video_path(data, Box(b"moov", 0, box.header, len(data)))
    if tree is None:
        raise ValueError("no video track with a sample entry this can tag")
    if any(_child(data, entry, b"colr") is not None for entry in tree[-1:]):
        return None
    if _child(data, tree[0], b"mvex") is not None:
        raise ValueError("fragmented MP4 files are not supported")
    colr = _colr_box(brand)
    entry = tree[-1]
    # Insert after the entry's last child box, which is before the 4 zero bytes QuickTime leaves at its end, then
    # raise every enclosing box's size.
    children = _boxes_in(data, entry.body + VISUAL_ENTRY_FIELDS, entry.end)
    insertion = children[-1].end if children else entry.body + VISUAL_ENTRY_FIELDS
    data[insertion:insertion] = colr
    for enclosing in tree[:-1] + [entry]:
        _grow(data, enclosing, len(colr))
    if media_after_moov:
        _shift_chunk_offsets(data, Box(b"moov", 0, box.header, len(data)), len(colr), moov_end=box.end)
    return bytes(data)


def _colr_box(brand: bytes) -> bytes:
    """A QuickTime ``nclc`` box for ``qt`` files, an ISO ``nclx`` box (limited range) for the rest."""
    codes = struct.pack(">HHH", PRIMARIES_BT709, TRANSFER_SRGB, MATRIX_BT709)
    if brand == b"qt  ":
        return struct.pack(">I4s4s", 8 + 4 + len(codes), b"colr", b"nclc") + codes
    return struct.pack(">I4s4s", 8 + 4 + len(codes) + 1, b"colr", b"nclx") + codes + b"\x00"


def _video_path(data: bytearray, moov: Box) -> list[Box] | None:
    """The boxes from moov down to the first video track's first sample entry, or None."""
    for trak in _children(data, moov):
        if trak.kind != b"trak":
            continue
        mdia = _child(data, trak, b"mdia")
        if mdia is None:
            continue
        hdlr = _child(data, mdia, b"hdlr")
        # hdlr: version and flags (4), predefined (4), handler type (4).
        if hdlr is None or bytes(data[hdlr.body + 8 : hdlr.body + 12]) != b"vide":
            continue
        minf = _child(data, mdia, b"minf")
        stbl = _child(data, minf, b"stbl") if minf else None
        stsd = _child(data, stbl, b"stsd") if stbl else None
        if stsd is None:
            continue
        # stsd: version and flags (4), entry count (4), then the entries.
        entries = _boxes_in(data, stsd.body + 8, stsd.end)
        if entries:
            return [moov, trak, mdia, minf, stbl, stsd, entries[0]]
    return None


def _boxes_in(data: bytearray, start: int, end: int) -> list[Box]:
    boxes: list[Box] = []
    position = start
    while position + 8 <= end:
        size, kind = struct.unpack(">I4s", data[position : position + 8])
        header = 8
        if size == 1:
            size, header = struct.unpack(">Q", data[position + 8 : position + 16])[0], 16
        elif size == 0:
            size = end - position
        if size < header or position + size > end:
            raise ValueError("invalid box inside moov")
        boxes.append(Box(bytes(kind), position, header, position + size))
        position += size
    return boxes


def _children(data: bytearray, parent: Box) -> list[Box]:
    return _boxes_in(data, parent.body, parent.end)


def _child(data: bytearray, parent: Box | None, kind: bytes) -> Box | None:
    if parent is None:
        return None
    if kind == b"colr":
        # A visual sample entry's children start after its fixed fields.
        return next((box for box in _boxes_in(data, parent.body + VISUAL_ENTRY_FIELDS, parent.end) if box.kind == kind), None)
    return next((box for box in _children(data, parent) if box.kind == kind), None)


def _grow(data: bytearray, box: Box, by: int) -> None:
    """Add ``by`` to the size field of ``box`` (32- or 64-bit); the box's own bytes have not moved."""
    if box.header == 16:
        struct.pack_into(">Q", data, box.start + 8, box.end - box.start + by)
    else:
        size = box.end - box.start + by
        if size > 0xFFFFFFFF:
            raise ValueError("box too large to grow")
        struct.pack_into(">I", data, box.start, size)


def _shift_chunk_offsets(data: bytearray, moov: Box, by: int, *, moov_end: int) -> None:
    """Add ``by`` to every chunk offset that points past the old moov (media data that follows it)."""
    for kind, fmt, width in ((b"stco", ">I", 4), (b"co64", ">Q", 8)):
        for table in _find_all(data, moov, kind):
            count = struct.unpack_from(">I", data, table.body + 4)[0]
            position = table.body + 8
            if position + count * width > table.end:
                raise ValueError(f"invalid {kind.decode()} table")
            for index in range(count):
                offset = struct.unpack_from(fmt, data, position + index * width)[0]
                if offset >= moov_end:
                    struct.pack_into(fmt, data, position + index * width, offset + by)


def _find_all(data: bytearray, moov: Box, kind: bytes) -> list[Box]:
    """Every box of ``kind`` inside ``moov``, at any depth through the container boxes."""
    found: list[Box] = []

    def walk(parent_start: int, parent_end: int) -> None:
        for box in _boxes_in(data, parent_start, parent_end):
            if box.kind == kind:
                found.append(box)
            elif box.kind in CONTAINERS:
                walk(box.body, box.end)

    walk(moov.body, moov.end)
    return found


def _replace(video: Path, moov: Box, patched: bytes, size: int) -> None:
    """Write the file with ``patched`` in place of its moov box, and swap it in atomically."""
    descriptor, temporary = tempfile.mkstemp(prefix=f".{video.name}.", dir=video.parent)
    try:
        with os.fdopen(descriptor, "wb") as out, video.open("rb") as source:
            _copy(source, out, 0, moov.start)
            out.write(patched)
            _copy(source, out, moov.end, size)
        shutil.copymode(video, temporary)
        os.replace(temporary, video)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _copy(source, out, start: int, end: int) -> None:
    source.seek(start)
    remaining = end - start
    while remaining > 0:
        block = source.read(min(COPY_BLOCK, remaining))
        if not block:
            raise ValueError("file shrank while it was being tagged")
        out.write(block)
        remaining -= len(block)
