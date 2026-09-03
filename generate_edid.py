#!/usr/bin/env python3
"""
Hand-assemble a 128-byte EDID 1.3 base block describing a 2560x1600 @ 60 Hz
display matching the Samsung Tab S7 FE (physical 263x165 mm).

The detailed timing descriptor uses the VESA CVT-RB (reduced blanking) figures
for 2560x1600@60: pixel clock 268.5 MHz, htotal 2720, vtotal 1646.

Output: edid_2560x1600.bin in the same directory.
Verification: `edid-decode edid_2560x1600.bin` must report
  "EDID block does NOT conform to EDID 1.3!" → 0 errors,
  preferred detailed timing = 2560x1600@60.
"""
import struct
import sys
from pathlib import Path


def manufacturer_id(s: str) -> bytes:
    if len(s) != 3 or not s.isalpha() or not s.isupper():
        raise ValueError("manufacturer id must be 3 uppercase letters")
    v = ((ord(s[0]) - ord('A') + 1) << 10) \
      | ((ord(s[1]) - ord('A') + 1) << 5) \
      |  (ord(s[2]) - ord('A') + 1)
    return struct.pack(">H", v)


def dtd(pclk_khz: int,
        hactive: int, hblank: int, hsync_off: int, hsync_pw: int,
        vactive: int, vblank: int, vsync_off: int, vsync_pw: int,
        hmm: int, vmm: int,
        sync_flags: int = 0x1E) -> bytes:
    """Detailed timing descriptor (18 bytes).

    pclk_khz: pixel clock in kHz (must fit in 10kHz units up to 655350)
    hactive/hblank/vactive/vblank in pixels and lines
    hsync_off, hsync_pw: horizontal front-porch and sync pulse width (pixels)
    vsync_off, vsync_pw: vertical front-porch and sync pulse width (lines)
    hmm, vmm: physical image size in millimetres
    sync_flags byte 17: 0x1E = digital separate sync, +Vsync, +Hsync
    """
    pclk_10khz = round(pclk_khz / 10)
    if not (0 < pclk_10khz <= 0xFFFF):
        raise ValueError(f"bad pclk {pclk_khz} kHz")
    out = bytearray(18)
    struct.pack_into("<H", out, 0, pclk_10khz)
    out[2]  =  hactive       & 0xFF
    out[3]  =  hblank        & 0xFF
    out[4]  = ((hactive >> 4) & 0xF0) | ((hblank  >> 8) & 0x0F)
    out[5]  =  vactive       & 0xFF
    out[6]  =  vblank        & 0xFF
    out[7]  = ((vactive >> 4) & 0xF0) | ((vblank  >> 8) & 0x0F)
    out[8]  =  hsync_off     & 0xFF
    out[9]  =  hsync_pw      & 0xFF
    out[10] = ((vsync_off & 0x0F) << 4) | (vsync_pw & 0x0F)
    out[11] = (((hsync_off >> 8) & 0x03) << 6) \
            | (((hsync_pw  >> 8) & 0x03) << 4) \
            | (((vsync_off >> 4) & 0x03) << 2) \
            |  ((vsync_pw  >> 4) & 0x03)
    out[12] =  hmm           & 0xFF
    out[13] =  vmm           & 0xFF
    out[14] = ((hmm >> 4)     & 0xF0) | ((vmm >> 8) & 0x0F)
    out[15] = 0     # horizontal border (px)
    out[16] = 0     # vertical border (lines)
    out[17] = sync_flags
    return bytes(out)


def monitor_descriptor(tag: int, data: bytes) -> bytes:
    """Generic 18-byte non-DTD descriptor.

    Leading zero pclk distinguishes from DTD. tag = 0xFC name / 0xFD range /
    0xFE ASCII / 0xFF serial / 0x10 dummy. data is up to 13 bytes; padded
    with 0x0A then 0x20 for ASCII tags, with 0x00 otherwise.
    """
    if len(data) > 13:
        raise ValueError("descriptor data > 13 bytes")
    if tag in (0xFC, 0xFE, 0xFF):
        pad = bytes([0x0A]) + b" " * (12 - len(data))
        body = (data + pad)[:13]
    else:
        body = data + b"\x00" * (13 - len(data))
    return b"\x00\x00\x00" + bytes([tag]) + b"\x00" + body


def build_edid() -> bytes:
    e = bytearray()

    # 0-7: header
    e += b"\x00\xFF\xFF\xFF\xFF\xFF\xFF\x00"

    # 8-9: manufacturer id (QCM)
    e += manufacturer_id("QCM")

    # 10-11: product code 0x0001 (little-endian)
    e += struct.pack("<H", 0x0001)

    # 12-15: serial = 0
    e += b"\x00\x00\x00\x00"

    # 16: week of manufacture
    # 17: year of manufacture (1990 + N) — 2026 → 36
    e += bytes([1, 36])

    # 18-19: EDID version 1, revision 3
    e += bytes([1, 3])

    # 20: video input — digital (bit 7), no other 1.3 sub-fields
    e += bytes([0x80])
    # 21: max horizontal image size cm (263 mm ≈ 26 cm)
    # 22: max vertical image size cm   (165 mm ≈ 17 cm)
    e += bytes([26, 17])
    # 23: display gamma — (γ*100) - 100 = 120 for γ=2.2
    e += bytes([120])
    # 24: feature support — preferred timing native(bit1) + sRGB(bit2)
    #     + RGB color display type(bits 4-3 = 01 → bit 3 set)
    e += bytes([0x0E])

    # 25-34: chromaticity — standard sRGB primaries
    e += bytes.fromhex("ee 95 a3 54 4c 99 26 0f 50 54".replace(" ", ""))

    # 35-37: established timings I, II, manufacturers — all unset
    e += bytes([0x00, 0x00, 0x00])

    # 38-53: 8 standard timing identifiers, all unused (0x01 0x01)
    e += bytes([0x01, 0x01] * 8)

    # 54-71: preferred detailed timing — CVT-RB 2560x1600@60
    e += dtd(
        pclk_khz=268500,
        hactive=2560, hblank=160, hsync_off=48, hsync_pw=32,
        vactive=1600, vblank=46,  vsync_off=3,  vsync_pw=10,
        hmm=263, vmm=165,
        sync_flags=0x1E,           # digital separate, +V, +H
    )

    # 72-89: monitor range limits (0xFD)
    # vmin=50 Hz, vmax=75 Hz, hmin=30 kHz, hmax=110 kHz, max pclk = 270 MHz
    range_data = bytes([50, 75, 30, 110, 27, 0])
    e += monitor_descriptor(0xFD, range_data)

    # 90-107: monitor name "TabDisplay"
    e += monitor_descriptor(0xFC, b"TabDisplay")

    # 108-125: dummy descriptor (0x10)
    e += monitor_descriptor(0x10, b"")

    # 126: extension flag
    e += bytes([0x00])

    # 127: checksum so the whole block sums to 0 mod 256
    s = sum(e) & 0xFF
    e += bytes([(0x100 - s) & 0xFF])

    assert len(e) == 128, f"EDID is {len(e)} bytes, expected 128"
    assert sum(e) % 256 == 0, "checksum mismatch"
    return bytes(e)


def main() -> int:
    out = Path(__file__).resolve().parent / "edid_2560x1600.bin"
    edid = build_edid()
    out.write_bytes(edid)
    print(f"wrote {len(edid)} bytes to {out}")
    print("first 16 bytes:", edid[:16].hex(" "))
    print("DTD bytes (54-71):", edid[54:72].hex(" "))
    return 0


if __name__ == "__main__":
    sys.exit(main())
