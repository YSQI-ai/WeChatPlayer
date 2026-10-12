"""Fallback decoder for QQ Music's newer MFLAC mask format.

The format stores 368 bytes of metadata after the encrypted FLAC stream.  The
first 32 KiB of the stream contains enough known FLAC bytes to identify the
128-byte QMC mask without a QQ Music process or a per-song key.
"""

from __future__ import annotations

from pathlib import Path


FLAC_HEADER = b"fLaC"
MFLAC_TRAILER_SIZE = 0x170
MASK_SIZE = 128
SEARCH_SIZE = 0x8000
CHUNK_SIZE = 1024 * 1024

#: Public QMC footer markers, checked against the tail of the file.
FOOTER_MARKERS = {
    b"STag": "STag",
    b"QTag": "QTag",
    b"musicex\x00": "PcV2MusicEx",
}

#: Containers that never carry an embedded EKey.  Their per-file key comes from
#: QQ Music's servers after account authorization, so nothing in the file can
#: recover it: the mask fallback below cannot help, only a helper that holds an
#: authorized key can.
KEYLESS_CONTAINERS = frozenset({"PcV2MusicEx"})


class MflacDecodeError(Exception):
    """Raised when an MFLAC mask cannot be detected or decoded."""


class HelperRequiredError(MflacDecodeError):
    """Raised when only an authorized external key can decode this file."""


def detect_container(input_path: Path) -> str:
    """Return the QMC container kind from the file's footer, or ``unknown``."""

    try:
        size = input_path.stat().st_size
        with input_path.open("rb") as handle:
            if size > 1024:
                handle.seek(-1024, 2)
            tail = handle.read(1024)
    except OSError:
        return "unknown"
    for marker, kind in FOOTER_MARKERS.items():
        if tail.endswith(marker):
            return kind
    return "unknown"


def requires_helper(input_path: Path) -> bool:
    """True when the file needs an authorized key the server cannot obtain."""

    return detect_container(input_path) in KEYLESS_CONTAINERS


def _mask_from_candidate(candidate: bytes) -> bytes | None:
    """Validate a 128-byte candidate and return it as a mask matrix."""

    if len(candidate) != MASK_SIZE:
        return None
    super_a = candidate[0]
    super_b = candidate[8]
    matrix = bytearray(candidate)
    for row in range(8):
        start = row * 16
        if matrix[start] != super_a or matrix[start + 8] != super_b:
            return None
        left = matrix[start + 1 : start + 8]
        right = matrix[120 - start + 1 : 120 - start + 8]
        if left != right[::-1]:
            return None
    return bytes(matrix)


def _detect_mask(prefix: bytes) -> bytes:
    """Find the QMC mask by testing every 128-byte block in the prefix."""

    search_len = min(SEARCH_SIZE, len(prefix))
    for offset in range(0, search_len - MASK_SIZE + 1, MASK_SIZE):
        mask = _mask_from_candidate(prefix[offset : offset + MASK_SIZE])
        if mask is None:
            continue
        decoded = bytes(prefix[index] ^ mask[index % MASK_SIZE] for index in range(4))
        if decoded == FLAC_HEADER:
            return mask
    raise MflacDecodeError("MFLAC mask could not be detected")


def _decrypt_chunk(data: bytearray, mask: bytes, absolute_offset: int) -> None:
    """XOR one chunk in place using the QMC 32 KiB mask offset rule."""
    for index in range(len(data)):
        position = absolute_offset + index
        if position < SEARCH_SIZE:
            skipped = 0
        else:
            # The original implementation skips one mask position at 0x8000,
            # then at every subsequent 0x7fff-byte boundary.
            skipped = 1 + (position - SEARCH_SIZE) // (SEARCH_SIZE - 1)
        data[index] ^= mask[(position + skipped) % MASK_SIZE]


def decode_mflac(input_path: Path, output_path: Path) -> None:
    """Decode an MFLAC file to a FLAC file using bounded memory."""

    file_size = input_path.stat().st_size
    audio_size = file_size - MFLAC_TRAILER_SIZE
    if audio_size < MASK_SIZE:
        raise MflacDecodeError("MFLAC file is too small")

    with input_path.open("rb") as source:
        prefix = source.read(min(SEARCH_SIZE, audio_size))
        mask = _detect_mask(prefix)
        source.seek(0)
        with output_path.open("wb") as target:
            remaining = audio_size
            absolute_offset = 0
            while remaining:
                chunk = bytearray(source.read(min(CHUNK_SIZE, remaining)))
                if not chunk:
                    raise MflacDecodeError("MFLAC file ended before the encrypted stream")
                _decrypt_chunk(chunk, mask, absolute_offset)
                target.write(chunk)
                absolute_offset += len(chunk)
                remaining -= len(chunk)

    if output_path.stat().st_size < len(FLAC_HEADER):
        raise MflacDecodeError("MFLAC decoder produced an empty FLAC")
    with output_path.open("rb") as decoded:
        if decoded.read(len(FLAC_HEADER)) != FLAC_HEADER:
            raise MflacDecodeError("MFLAC decoder produced an invalid FLAC header")
