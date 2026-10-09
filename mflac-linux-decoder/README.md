# QQ Music MFLAC decoder for Linux

`qmc_mflac.py` is a dependency-free Python 3 command-line decoder for QQ
Music QMC/MFLAC containers. It writes the encrypted audio payload back as
standard FLAC/OGG data and never re-encodes it.

## Requirements

- Linux with Python 3.8 or newer
- No pip package or network access is required
- A key is required for PC `MusicEx` files. The key may be supplied as an
  EKey string or read from an exported QQ Music Android `player_process_db`.

## Build/install

Copy this directory to the target machine and make the script executable:

```sh
chmod +x qmc_mflac.py
python3 qmc_mflac.py --self-test
```

## Usage

Embedded-key files (QTag or legacy PC footer):

```sh
python3 qmc_mflac.py input.mflac output.flac
```

PC MusicEx files, including the supplied `cadode - 光の暈.mflac`, do not
contain an EKey. Use a key obtained from a QQ Music client/key database:

```sh
python3 qmc_mflac.py input.mflac output.flac --ekey '<EKEY>'
python3 qmc_mflac.py input.mflac output.flac --ekey-db player_process_db
```

Inspect a file without writing output:

```sh
python3 qmc_mflac.py input.mflac --inspect
```

The supplied sample reports a `PcV2MusicEx` footer, `ekey=no`, and media
metadata. That is why QMC2 reports `unknown encryption method`: the older
QMC2 CLI only understands the legacy `QTag`/PC EKey footer and cannot decrypt
this newer layout without a key.

## Key and legal scope

The tool supports the QMCv2 Map cipher (short keys) and modified RC4 cipher
(long keys), plus the Tencent TEA EKey wrapper. It does not query QQ Music,
inject a client process, or attempt to derive a missing authorization key.
Use it only for audio you legally downloaded and are allowed to decode.

## Verification

The repository smoke checks are:

```sh
python3 -m py_compile qmc_mflac.py
python3 qmc_mflac.py --self-test
python3 qmc_mflac.py input.mflac --inspect
```

## References

- `pushbox/qmc2`: legacy QMC2 decoder and QMCv2 Map/RC4 implementation
  (<https://github.com/pushbox/qmc2>)
- ix64's MGG/MFLAC format notes and QTag research
  (<https://gist.github.com/ix64/bcd72c151f21e1b050c9cc52d6ff27d5>)
- `mzj3920/qqmusic-decrypt`: public Python implementation documenting the
  MusicEx footer and the need for a client/database EKey
  (<https://github.com/mzj3920/qqmusic-decrypt>)
