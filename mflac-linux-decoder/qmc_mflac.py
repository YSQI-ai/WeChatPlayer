#!/usr/bin/env python3
"""Offline QQ Music QMC/MFLAC decoder.

This utility handles files with an embedded EKey (QTag and legacy PC
footers), plus PC MusicEx files when a legal EKey is supplied explicitly or
found in an exported player_process_db SQLite database.  It deliberately
does not contact QQ Music services or bypass a client process.
"""

from __future__ import annotations

import argparse
import base64
import math
import os
import sqlite3
import struct
import sys
from pathlib import Path

BOUNDARY = 0x7FFF
MAP_SIZE = 128
TEA_DELTA = 0x9E3779B9
V2_PREFIX = b"UVFNdXNpYyBFbmNWMixLZXk6"  # base64("QQMusic EncV2,Key:")
V2_KEY1 = bytes.fromhex("3338365A4A592140232A24255E262928")
V2_KEY2 = bytes.fromhex("2A2A232128232425265E6131635A2C54")


class DecodeError(Exception):
    pass


class Footer:
    def __init__(self, size: int, kind: str, ekey: str | None = None, **extra: str):
        self.size = size
        self.kind = kind
        self.ekey = ekey
        self.extra = extra


def _read_utf16_ascii(data: bytes) -> str:
    chars = []
    for i in range(0, len(data) - 1, 2):
        if data[i] == 0 and data[i + 1] == 0:
            break
        if data[i + 1] != 0 or not 0 < data[i] < 128:
            break
        chars.append(chr(data[i]))
    return "".join(chars)


def _is_b64(data: bytes) -> bool:
    return bool(data) and all(c in b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=" for c in data)


def parse_footer(tail: bytes) -> Footer | None:
    """Parse the public QMC footer variants from a tail window."""
    if tail.endswith(b"STag"):
        if len(tail) < 8:
            raise DecodeError("STag footer is truncated")
        payload, size_bytes = tail[:-8], tail[-8:-4]
        length = int.from_bytes(size_bytes, "big")
        csv = payload[-length:].decode("utf-8", "replace")
        parts = csv.split(",")
        if len(parts) != 3 or parts[1] != "2" or not parts[0].isdigit():
            raise DecodeError("invalid STag footer")
        return Footer(length + 8, "STag", resource_id=parts[0], media_mid=parts[2])

    if tail.endswith(b"QTag"):
        if len(tail) < 8:
            raise DecodeError("QTag footer is truncated")
        payload, size_bytes = tail[:-8], tail[-8:-4]
        length = int.from_bytes(size_bytes, "big")
        csv = payload[-length:].decode("utf-8", "replace")
        parts = csv.split(",")
        if len(parts) != 3 or parts[2] != "2" or not parts[1].isdigit():
            raise DecodeError("invalid QTag footer")
        ekey = parts[0]
        if not _is_b64(ekey.encode("ascii", "ignore")):
            raise DecodeError("QTag EKey is not base64")
        return Footer(length + 8, "QTag", ekey, resource_id=parts[1])

    if tail.endswith(b"musicex\x00"):
        if len(tail) < 16:
            raise DecodeError("MusicEx footer is truncated")
        payload = tail[:-8]
        version = int.from_bytes(payload[-4:], "little")
        data = payload[:-4]
        length = int.from_bytes(data[-4:], "little")
        if version != 1 or length != 0xC0:
            raise DecodeError("unsupported MusicEx footer")
        inner = data[:-4][-0xB0:]
        mid = _read_utf16_ascii(inner[12:72])
        media_filename = _read_utf16_ascii(inner[72:172])
        return Footer(length, "PcV2MusicEx", None, media_mid=mid, media_filename=media_filename)

    if len(tail) < 4:
        return None
    length = int.from_bytes(tail[-4:], "little")
    if length == 0 or length > 0x500 or len(tail) < length + 4:
        return None
    raw = tail[-4 - length:-4].split(b"\x00", 1)[0]
    if not _is_b64(raw):
        return None
    return Footer(length + 4, "PcV1Legacy", raw.decode("ascii"))


def _f32(value: float) -> float:
    return struct.unpack("f", struct.pack("f", value))[0]


def simple_key() -> bytes:
    step = _f32(0.1)
    out = bytearray()
    for i in range(8):
        value = _f32(106.0 + _f32(i * step))
        out.append(max(0, min(int(_f32(abs(math.tan(value)) * 100.0)), 255)))
    return bytes(out)


def _tea_round(value: int, total: int, key1: int, key2: int) -> int:
    return ((((value << 4) & 0xFFFFFFFF) + key1) ^ ((total + value) & 0xFFFFFFFF) ^ ((value >> 5) + key2)) & 0xFFFFFFFF


def _tea_decrypt_block(block: int, key: tuple[int, int, int, int]) -> int:
    y, z = block >> 32, block & 0xFFFFFFFF
    total = (TEA_DELTA * 16) & 0xFFFFFFFF
    for _ in range(16):
        z = (z - _tea_round(y, total, key[2], key[3])) & 0xFFFFFFFF
        y = (y - _tea_round(z, total, key[0], key[1])) & 0xFFFFFFFF
        total = (total - TEA_DELTA) & 0xFFFFFFFF
    return (y << 32) | z


def tea_cbc_decrypt(ciphertext: bytes, key16: bytes) -> bytes:
    if len(ciphertext) < 16 or len(ciphertext) % 8:
        raise DecodeError("invalid EKey TEA payload length")
    key = struct.unpack(">IIII", key16)
    previous_cipher = 0
    previous_plain = 0
    plain = bytearray()
    for pos in range(0, len(ciphertext), 8):
        cipher = int.from_bytes(ciphertext[pos:pos + 8], "big")
        value = _tea_decrypt_block(cipher ^ previous_plain, key)
        block = (value ^ previous_cipher) & 0xFFFFFFFFFFFFFFFF
        plain.extend(block.to_bytes(8, "big"))
        previous_cipher, previous_plain = cipher, value
    padding = plain[0] & 7
    start = 1 + padding + 2
    end = len(plain) - 7
    if start > end or any(plain[end:]):
        raise DecodeError("invalid EKey TEA padding")
    return bytes(plain[start:end])


def decrypt_ekey(value: str | bytes) -> bytes:
    raw = value.encode("ascii") if isinstance(value, str) else bytes(value)
    if raw.startswith(V2_PREFIX):
        payload = base64.b64decode(raw[len(V2_PREFIX):], validate=True)
        payload = tea_cbc_decrypt(payload, V2_KEY1)
        payload = tea_cbc_decrypt(payload, V2_KEY2)
        payload = payload.split(b"\x00", 1)[0]
        raw = payload
    try:
        decoded = base64.b64decode(raw, validate=True)
    except ValueError as exc:
        raise DecodeError("EKey is not valid base64") from exc
    if len(decoded) < 16:
        raise DecodeError("EKey payload is too short")
    header, cipher = decoded[:8], decoded[8:]
    tea_key = bytearray()
    for simple, header_byte in zip(simple_key(), header):
        tea_key.extend((simple, header_byte))
    return header + tea_cbc_decrypt(cipher, bytes(tea_key))


def _transform(data: bytes, offset: int, key: bytes) -> bytes:
    out = bytearray(data)
    pos = offset
    index = 0
    while index < len(out):
        if pos <= BOUNDARY:
            take = min(0x8000 - pos, len(out) - index)
            phase = pos % len(key)
        else:
            remainder = pos % BOUNDARY
            take = min(BOUNDARY - remainder, len(out) - index)
            phase = remainder % len(key)
        for i in range(take):
            out[index + i] ^= key[(phase + i) % len(key)]
        index += take
        pos += take
    return bytes(out)


def compress_key(key: bytes) -> bytes:
    if not key:
        raise DecodeError("master key is empty")
    out = bytearray()
    for i in range(MAP_SIZE):
        index = (i * i + 71214) % len(key)
        value = key[index]
        shift = (index + 4) % 8
        out.append(((value << shift) | (value >> shift)) & 0xFF)
    return bytes(out)


class MapCipher:
    def __init__(self, key: bytes):
        self.key = compress_key(key)

    def decrypt(self, data: bytes, offset: int) -> bytes:
        return _transform(data, offset, self.key)


def _segment_key(segment: int, seed: int, key_hash: float) -> int:
    if seed == 0:
        return 0
    return int(key_hash / (((segment + 1) * seed) & 0xFFFFFFFFFFFFFFFF) * 100.0)


class RC4Cipher:
    SEGMENT = 0x1400

    def __init__(self, key: bytes):
        self.key = key
        n = len(key)
        state = [i & 0xFF for i in range(n)]
        j = 0
        for i in range(n):
            j = (j + state[i] + key[i % n]) % n
            state[i], state[j] = state[j], state[i]
        self.state = state
        self.key_hash = 1
        for value in key:
            if value:
                next_hash = self.key_hash * value
                if next_hash == 0 or next_hash <= self.key_hash:
                    break
                self.key_hash = next_hash
        self.stream = bytearray()
        i = j = 0
        for _ in range(self.SEGMENT + 512):
            i = (i + 1) % n
            j = (j + state[i]) % n
            state[i], state[j] = state[j], state[i]
            self.stream.append(state[(state[i] + state[j]) % n])

    def decrypt(self, data: bytes, offset: int) -> bytes:
        out = bytearray(data)
        pos = offset
        cursor = 0
        if pos < 0x80:
            take = min(0x80 - pos, len(out))
            for i in range(take):
                index = _segment_key(pos + i, self.key[(pos + i) % len(self.key)], self.key_hash) % len(self.key)
                out[i] ^= self.key[index]
            cursor, pos = take, pos + take
        while cursor < len(out):
            segment = pos // self.SEGMENT
            block_offset = pos % self.SEGMENT
            take = min(self.SEGMENT - block_offset, len(out) - cursor)
            seed = self.key[segment % len(self.key)]
            skip = _segment_key(segment, seed, self.key_hash) & 0x1FF
            for i in range(take):
                out[cursor + i] ^= self.stream[skip + block_offset + i]
            cursor += take
            pos += take
        return bytes(out)


def make_cipher(master_key: bytes):
    return MapCipher(master_key) if len(master_key) <= 300 else RC4Cipher(master_key)


def _db_rows(db_path: Path):
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise DecodeError(f"cannot open key database: {exc}") from exc
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        specs = (
            ("audio_file_ekey_table", "file_path", "ekey"),
            ("EKeyFileInfo", "filePath", "eKey"),
            ("p2p_cache_info_table", "file_id", "ekey"),
        )
        for table, name_col, key_col in specs:
            if table not in tables:
                continue
            try:
                yield from connection.execute(f"SELECT {name_col}, {key_col} FROM {table}")
            except sqlite3.Error:
                continue
    finally:
        connection.close()


def find_ekey(db_path: Path, input_path: Path, footer: Footer) -> str | None:
    names = {input_path.name, input_path.stem}
    for value in (footer.extra.get("media_filename"), footer.extra.get("media_mid")):
        if value:
            names.add(value)
    for name, ekey in _db_rows(db_path):
        if not name or not ekey:
            continue
        text_name = str(name)
        if any(candidate in text_name or text_name in candidate for candidate in names):
            return str(ekey)
    return None


def decrypt_file(input_path: Path, output_path: Path, ekey: str | None = None, key_db: Path | None = None) -> Footer:
    total = input_path.stat().st_size
    with input_path.open("rb") as source:
        if total <= 1024:
            tail = source.read()
        else:
            source.seek(-1024, os.SEEK_END)
            tail = source.read(1024)
    footer = parse_footer(tail)
    if footer is None:
        raise DecodeError("unrecognized QMC footer; this tool does not guess a key")
    selected = ekey or footer.ekey
    if selected is None and key_db is not None:
        selected = find_ekey(key_db, input_path, footer)
    if selected is None:
        if footer.kind == "PcV2MusicEx":
            mid = footer.extra.get("media_mid") or "unknown"
            raise DecodeError(
                f"PC MusicEx file has no embedded EKey (media_mid={mid}). "
                "Provide --ekey or an exported player_process_db; QQ client/server authorization is required."
            )
        raise DecodeError(f"{footer.kind} file has no usable EKey; provide --ekey or --ekey-db")

    cipher = make_cipher(decrypt_ekey(selected))
    data_size = total - footer.size
    if data_size <= 0:
        raise DecodeError("encrypted audio payload is empty")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with input_path.open("rb") as source, output_path.open("wb") as target:
        offset = 0
        remaining = data_size
        while remaining:
            chunk = source.read(min(4 * 1024 * 1024, remaining))
            if not chunk:
                raise DecodeError("input ended before encrypted payload")
            target.write(cipher.decrypt(chunk, offset))
            offset += len(chunk)
            remaining -= len(chunk)
    with output_path.open("rb") as decoded:
        if decoded.read(4) not in (b"fLaC", b"OggS", b"RIFF", b"ID3"):
            output_path.unlink(missing_ok=True)
            raise DecodeError("decrypted output does not have a recognized audio header; check the EKey")
    return footer


def self_test() -> None:
    assert simple_key() == bytes.fromhex("695646382b20150b")
    probe = bytes(range(251))
    assert MapCipher(b"map-key").decrypt(MapCipher(b"map-key").decrypt(probe, 0), 0) == probe
    rc4_key = bytes(i % 256 for i in range(301))
    assert RC4Cipher(rc4_key).decrypt(RC4Cipher(rc4_key).decrypt(probe, 0), 0) == probe
    sample = Path(__file__).resolve().parent.parent / "cadode - 光の暈.mflac"
    if sample.exists():
        footer = parse_footer(sample.read_bytes()[-1024:])
        assert footer and footer.kind == "PcV2MusicEx"
        assert footer.extra.get("media_mid") == "003hu7tI4cI8wf"
    print("self-test: ok")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Decode QQ Music MFLAC/QMC files to standard audio")
    parser.add_argument("input", nargs="?", type=Path, help="input .mflac/.mgg file")
    parser.add_argument("output", nargs="?", type=Path, help="output .flac/.ogg path")
    parser.add_argument("--ekey", help="legal EKey string obtained from the QQ Music client/key database")
    parser.add_argument("--ekey-db", type=Path, help="read a player_process_db SQLite export")
    parser.add_argument("--self-test", action="store_true", help="run parser and algorithm smoke tests")
    parser.add_argument("--inspect", action="store_true", help="print footer metadata without decrypting")
    args = parser.parse_args(argv)
    try:
        if args.self_test:
            self_test()
            return 0
        if not args.input:
            parser.error("input is required unless --self-test is used")
        if args.inspect:
            with args.input.open("rb") as source:
                source.seek(max(0, args.input.stat().st_size - 1024))
                footer = parse_footer(source.read(1024))
            if footer is None:
                raise DecodeError("unrecognized QMC footer")
            print(f"footer={footer.kind} size={footer.size} ekey={'yes' if footer.ekey else 'no'} metadata={footer.extra}")
            return 0
        output = args.output or args.input.with_suffix(".flac")
        footer = decrypt_file(args.input, output, args.ekey, args.ekey_db)
        print(f"decoded {args.input} -> {output} ({footer.kind})")
        return 0
    except (OSError, DecodeError, ValueError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
