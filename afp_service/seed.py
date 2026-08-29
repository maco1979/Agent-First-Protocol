# -*- coding: utf-8 -*-
"""M0 seed: create schema + 5 sample components + 1 real PDF blob.

Idempotent: skips when component_meta already has rows.
Usage: python seed.py   (or ensure_seed() from tests/conftest.py)
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Optional

from store import ComponentStore

ROOT = Path(__file__).resolve().parent.parent


def build_minimal_pdf(title: str, lines: list[str]) -> bytes:
    """Build a small but structurally valid one-page PDF."""
    parts = ["BT", "/F1 16 Tf", "72 720 Td"]
    for ln in [title] + lines:
        safe = ln.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        parts.append(f"({safe}) Tj")
        parts.append("0 -22 Td")
    parts.append("ET")
    stream = "\n".join(parts).encode("latin-1", "replace")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]"
        b" /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out: list[bytes] = [b"%PDF-1.4\n"]
    offsets: list[int] = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(sum(len(x) for x in out))
        out.append(f"{i} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref_pos = sum(len(x) for x in out)
    size = len(objects) + 1
    xref = [f"xref\n0 {size}\n0000000000 65535 f \n"]
    for off in offsets:
        xref.append(f"{off:010d} 00000 n \n")
    xref.append(f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n")
    out.append("".join(xref).encode())
    return b"".join(out)


COMPONENTS: list[dict] = [
    {
        "part_number": "MAX30011",
        "manufacturer": "ADI",
        "category": "EEG/ECG analog front-end",
        "package": "WLP",
        "source_file": "ADI official datasheet",
        "fts_chunk": "MAX30011 ADI biopotential analog front end single lead ECG AFE WLP datasheet",
        "payload": {
            "part_number": "MAX30011",
            "manufacturer": "ADI",
            "package": "WLP",
            "revision": "Rev-C",
            "description": "Single-lead biopotential analog front-end for ECG/EEG",
            "pins": [
                {"pin_num": "1", "pin_name": "VDD", "pin_type": "power_in"},
                {"pin_num": "2", "pin_name": "GND", "pin_type": "power"},
                {"pin_num": "3", "pin_name": "SDA", "pin_type": "bidirectional"},
                {"pin_num": "4", "pin_name": "SCL", "pin_type": "input"},
                {"pin_num": "5", "pin_name": "NC", "pin_type": "no_connect"},
            ],
            "absolute_max_rating": [
                {"param": "VDD", "max": "3.6", "unit": "V"},
                {"param": "T_storage", "max": "150", "unit": "degC"},
            ],
            "forbidden_connect": ["NC pins must be left floating; external signals forbidden"],
        },
    },
    {
        "part_number": "ADS1299",
        "manufacturer": "TI",
        "category": "8-channel EEG amplifier",
        "package": "TQFP",
        "source_file": "TI official datasheet",
        "fts_chunk": "ADS1299 TI 8 channel low noise EEG amplifier biopotential AFE TQFP datasheet",
        "payload": {
            "part_number": "ADS1299",
            "manufacturer": "TI",
            "package": "TQFP",
            "revision": "Rev-B",
            "description": "Low-noise 8-channel analog front-end for EEG acquisition",
            "pins": [
                {"pin_num": "1", "pin_name": "AVDD", "pin_type": "power_in"},
                {"pin_num": "2", "pin_name": "AVSS", "pin_type": "power"},
                {"pin_num": "3", "pin_name": "SPI_CLK", "pin_type": "input"},
                {"pin_num": "4", "pin_name": "SPI_DIN", "pin_type": "input"},
                {"pin_num": "5", "pin_name": "SPI_DOUT", "pin_type": "output"},
            ],
            "absolute_max_rating": [
                {"param": "AVDD", "max": "5.5", "unit": "V"},
                {"param": "T_storage", "max": "150", "unit": "degC"},
            ],
            "forbidden_connect": ["Do not exceed analog supply ratings; ESD-sensitive inputs"],
        },
    },
    {
        "part_number": "OPA2340",
        "manufacturer": "TI",
        "category": "rail-to-rail op-amp",
        "package": "SOT-23",
        "source_file": "TI official datasheet",
        "fts_chunk": "OPA2340 TI rail to rail CMOS operational amplifier dual SOT-23 datasheet",
        "payload": {
            "part_number": "OPA2340",
            "manufacturer": "TI",
            "package": "SOT-23",
            "revision": "Rev-A",
            "description": "Rail-to-rail CMOS dual operational amplifier, 5.5 MHz GBW",
            "pins": [
                {"pin_num": "1", "pin_name": "OUT_A", "pin_type": "output"},
                {"pin_num": "2", "pin_name": "IN_A-", "pin_type": "input"},
                {"pin_num": "3", "pin_name": "IN_A+", "pin_type": "input"},
                {"pin_num": "4", "pin_name": "V-", "pin_type": "power_in"},
                {"pin_num": "5", "pin_name": "V+", "pin_type": "power_in"},
            ],
            "absolute_max_rating": [
                {"param": "V_supply", "max": "6.0", "unit": "V"},
                {"param": "T_junction", "max": "150", "unit": "degC"},
            ],
            "forbidden_connect": ["Inputs must stay within supply rails; no phase reversal tolerance"],
        },
    },
    {
        "part_number": "STM32F103C8T6",
        "manufacturer": "ST",
        "category": "ARM Cortex-M3 MCU",
        "package": "LQFP48",
        "source_file": "ST official datasheet",
        "fts_chunk": "STM32F103C8T6 ST ARM Cortex M3 72 MHz microcontroller LQFP48 datasheet",
        "payload": {
            "part_number": "STM32F103C8T6",
            "manufacturer": "ST",
            "package": "LQFP48",
            "revision": "Rev-Y",
            "description": "ARM Cortex-M3 72 MHz MCU, 64 KB flash, 20 KB SRAM",
            "pins": [
                {"pin_num": "1", "pin_name": "VBAT", "pin_type": "power_in"},
                {"pin_num": "5", "pin_name": "OSC_IN", "pin_type": "input"},
                {"pin_num": "6", "pin_name": "OSC_OUT", "pin_type": "output"},
                {"pin_num": "23", "pin_name": "VSS", "pin_type": "power"},
                {"pin_num": "24", "pin_name": "VDD", "pin_type": "power_in"},
            ],
            "absolute_max_rating": [
                {"param": "VDD", "max": "4.0", "unit": "V"},
                {"param": "T_storage", "max": "150", "unit": "degC"},
            ],
            "forbidden_connect": ["5V-tolerant pins only where datasheet marks FT; BOOT0 must not float"],
        },
    },
    {
        "part_number": "ESP32-WROOM-32",
        "manufacturer": "Espressif",
        "category": "WiFi/BT module",
        "package": "Module",
        "source_file": "Espressif official datasheet",
        "fts_chunk": "ESP32-WROOM-32 Espressif WiFi Bluetooth module SoC datasheet",
        "payload": {
            "part_number": "ESP32-WROOM-32",
            "manufacturer": "Espressif",
            "package": "Module",
            "revision": "Rev-1",
            "description": "WiFi 802.11 b/g/n + BT/BLE module with integrated antenna",
            "pins": [
                {"pin_num": "1", "pin_name": "GND", "pin_type": "power"},
                {"pin_num": "2", "pin_name": "3V3", "pin_type": "power_in"},
                {"pin_num": "3", "pin_name": "EN", "pin_type": "input"},
                {"pin_num": "4", "pin_name": "IO0", "pin_type": "bidirectional"},
                {"pin_num": "5", "pin_name": "TXD0", "pin_type": "output"},
            ],
            "absolute_max_rating": [
                {"param": "VDD", "max": "3.6", "unit": "V"},
                {"param": "T_ambient", "max": "85", "unit": "degC"},
            ],
            "forbidden_connect": ["Do not drive EN low during flash boot; antenna keep-out zone mandatory"],
        },
    },
]


def ensure_seed(db_path: Optional[str] = None, blob_dir: Optional[str] = None) -> str:
    """Idempotent seeding. Returns the MAX30011 datasheet sha256."""
    db = db_path or os.environ.get("AFP_DB", str(ROOT / "afp.db"))
    bdir = Path(blob_dir or os.environ.get("AFP_BLOB_DIR", str(ROOT / "data")))
    store = ComponentStore(db)
    if store.count() > 0:
        row = store.lookup_exact("MAX30011")
        return row["datasheet_sha256"] if row else ""
    bdir.mkdir(parents=True, exist_ok=True)
    pdf = build_minimal_pdf(
        "MAX30011 Datasheet (seed)",
        [
            "Analog Devices MAX30011 biopotential AFE",
            "Seed document for AFP service M0",
            "Content-addressed by sha256; see X-Blob-Sha256 header",
        ],
    )
    sha = hashlib.sha256(pdf).hexdigest()
    (bdir / f"{sha}.bin").write_bytes(pdf)
    for c in COMPONENTS:
        store.insert_component(
            part_number=c["part_number"],
            manufacturer=c["manufacturer"],
            category=c["category"],
            package=c["package"],
            payload=c["payload"],
            datasheet_sha256=sha if c["part_number"] == "MAX30011" else None,
            source_file=c["source_file"],
            fts_chunk=c["fts_chunk"],
        )
    print(f"seeded {len(COMPONENTS)} components; blob {sha} at {bdir}")
    return sha


if __name__ == "__main__":
    ensure_seed()
