#!/usr/bin/env python3
"""
Mi (Body Composition) Scale -> HTTP-Webhook Reader.

Scannt passiv die BLE-Advertisements einer Xiaomi Mi Waage, dekodiert
Gewicht + Impedanz und postet stabile Messungen an einen konfigurierbaren
Endpoint (dein PWA-Backend). Ungesendete Messungen landen in einer lokalen
SQLite und werden erneut versucht.

Laeuft headless auf dem Raspberry Pi ueber BlueZ (siehe docker-compose.yml).
"""

import asyncio
import logging
import os
import sqlite3
from dataclasses import dataclass, asdict
from datetime import datetime, timezone

import httpx
from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

# --- Konfiguration ueber Environment ---
SCALE_MAC = os.getenv("SCALE_MAC", "").upper().strip()      # z.B. "AA:BB:CC:DD:EE:FF"
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()          # PWA-Backend-Endpoint
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN", "").strip()      # optional
DB_PATH = os.getenv("DB_PATH", "/data/readings.db")
EMIT_COOLDOWN_S = int(os.getenv("EMIT_COOLDOWN_S", "30"))   # gleiche Messung nicht spammen
WEIGHT_EPS = float(os.getenv("WEIGHT_EPS", "0.3"))          # kg-Aenderung fuer neue Messung
RETRY_INTERVAL_S = int(os.getenv("RETRY_INTERVAL_S", "60"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

BODY_SVC = "0000181b"    # Body Composition (V2: Gewicht + Impedanz)
WEIGHT_SVC = "0000181d"  # Weight Scale (V1: nur Gewicht)

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("miscale")

LB_TO_KG = 0.45359237
JIN_TO_KG = 0.5

# Bump this string on every change to scale_reader.py -- logged loudly at
# startup so a redeploy can be confirmed from the container logs alone,
# without guessing whether the image was actually rebuilt from fresh code.
BUILD_TAG = "unitfix-2026-07-30-01"


@dataclass
class Reading:
    weight_kg: float
    weight_native: float
    unit: str
    impedance: int | None
    source_mac: str
    scale_ts: str | None
    captured_ts: str


def _u16(b: bytes) -> int:
    return int.from_bytes(b, "little")


def _scale_ts(b: bytes) -> str | None:
    """7 Bytes: year(u16 LE), month, day, hour, min, sec."""
    try:
        year = _u16(b[0:2])
        if not (2015 <= year <= 2100):
            return None
        return datetime(year, b[2], b[3], b[4], b[5], b[6]).isoformat()
    except (ValueError, IndexError):
        return None


def _to_kg(weight: float, unit: str) -> float:
    if unit == "kg":
        return weight
    if unit == "lbs":
        return weight * LB_TO_KG
    return weight * JIN_TO_KG  # jin


def parse_body_composition(data: bytes, mac: str) -> Reading | None:
    """UUID 0x181B, 13-Byte Payload."""
    if len(data) < 13:
        return None
    ctrl = data[1]
    stabilized = bool(ctrl & 0x20)
    load_removed = bool(ctrl & 0x80)
    impedance_stable = bool(ctrl & 0x02)
    if load_removed or not stabilized:
        return None

    unit_byte = data[0]
    if unit_byte & 0x01:
        unit, div = "lbs", 100.0
    elif unit_byte & 0x10:
        unit, div = "jin", 100.0
    else:
        unit, div = "kg", 200.0  # Standard; in Zepp Life auf kg stellen
    weight = _u16(data[11:13]) / div

    impedance = _u16(data[9:11])
    if impedance in (0, 0xFFFF) or not impedance_stable:
        impedance = None

    return Reading(
        weight_kg=round(_to_kg(weight, unit), 2),
        weight_native=round(weight, 2),
        unit=unit,
        impedance=impedance,
        source_mac=mac,
        scale_ts=_scale_ts(data[2:9]),
        captured_ts=datetime.now(timezone.utc).isoformat(),
    )


def parse_weight_v1(data: bytes, mac: str) -> Reading | None:
    """UUID 0x181D, 10-Byte Payload (nur Gewicht)."""
    if len(data) < 3:
        return None
    ctrl = data[0]
    if not (ctrl & 0x20):          # nicht stabilisiert
        return None
    if ctrl & 0x01:
        unit, div = "lbs", 100.0
    elif ctrl & 0x10:
        unit, div = "jin", 100.0
    else:
        unit, div = "kg", 200.0
    weight = _u16(data[1:3]) / div
    return Reading(
        weight_kg=round(_to_kg(weight, unit), 2),
        weight_native=round(weight, 2),
        unit=unit,
        impedance=None,
        source_mac=mac,
        scale_ts=_scale_ts(data[3:10]),
        captured_ts=datetime.now(timezone.utc).isoformat(),
    )


def _match_svc(adv: AdvertisementData, prefix: str) -> bytes | None:
    for uuid, val in adv.service_data.items():
        if uuid.lower().startswith(prefix):
            return bytes(val)
    return None


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_ts TEXT, weight_kg REAL, weight_native REAL,
                unit TEXT, impedance INTEGER, source_mac TEXT,
                scale_ts TEXT, delivered INTEGER DEFAULT 0
            )""")
        self.db.commit()

    def add(self, r: Reading) -> int:
        cur = self.db.execute(
            "INSERT INTO readings (captured_ts,weight_kg,weight_native,unit,"
            "impedance,source_mac,scale_ts) VALUES (?,?,?,?,?,?,?)",
            (r.captured_ts, r.weight_kg, r.weight_native, r.unit,
             r.impedance, r.source_mac, r.scale_ts))
        self.db.commit()
        return cur.lastrowid

    def mark_delivered(self, row_id: int):
        self.db.execute("UPDATE readings SET delivered=1 WHERE id=?", (row_id,))
        self.db.commit()

    def undelivered(self):
        cur = self.db.execute(
            "SELECT id,captured_ts,weight_kg,weight_native,unit,impedance,"
            "source_mac,scale_ts FROM readings WHERE delivered=0 ORDER BY id")
        cols = ["id", "captured_ts", "weight_kg", "weight_native", "unit",
                "impedance", "source_mac", "scale_ts"]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


async def deliver(client: httpx.AsyncClient, row: dict) -> bool:
    if not WEBHOOK_URL:
        return False
    headers = {"Authorization": f"Bearer {WEBHOOK_TOKEN}"} if WEBHOOK_TOKEN else {}
    payload = {k: v for k, v in row.items() if k != "id"}
    try:
        resp = await client.post(WEBHOOK_URL, json=payload, headers=headers, timeout=10)
        resp.raise_for_status()
        return True
    except httpx.HTTPError as e:
        log.warning("POST fehlgeschlagen: %s", e)
        return False


async def retry_loop(store: Store, client: httpx.AsyncClient):
    while True:
        for row in store.undelivered():
            if await deliver(client, row):
                store.mark_delivered(row["id"])
                log.info("Nachgeliefert: %.2f kg", row["weight_kg"])
        await asyncio.sleep(RETRY_INTERVAL_S)


async def main():
    log.warning("=== scale_reader BUILD_TAG=%s ===", BUILD_TAG)
    if not WEBHOOK_URL:
        log.warning("Kein WEBHOOK_URL gesetzt - Messungen werden nur lokal gespeichert.")
    if not SCALE_MAC:
        log.warning("Kein SCALE_MAC gesetzt - verarbeite ALLE Mi-Waagen und logge MACs.")

    store = Store(DB_PATH)
    queue: asyncio.Queue[Reading] = asyncio.Queue()
    last_emit: dict[str, tuple[float, float]] = {}  # mac -> (ts, weight_kg)

    def on_adv(device: BLEDevice, adv: AdvertisementData):
        mac = (device.address or "").upper()
        if SCALE_MAC and mac != SCALE_MAC:
            return
        body = _match_svc(adv, BODY_SVC)
        r = parse_body_composition(body, mac) if body else None
        if r is None:
            v1 = _match_svc(adv, WEIGHT_SVC)
            r = parse_weight_v1(v1, mac) if v1 else None
        if r is None:
            if not SCALE_MAC and (body or _match_svc(adv, WEIGHT_SVC)):
                log.info("Mi-Waage gesehen: MAC=%s (setze SCALE_MAC hierauf)", mac)
            return

        now = asyncio.get_event_loop().time()
        prev = last_emit.get(mac)
        if prev and (now - prev[0]) < EMIT_COOLDOWN_S and abs(r.weight_kg - prev[1]) < WEIGHT_EPS:
            return  # gleiche Messung, Dauer-Broadcast -> unterdruecken
        last_emit[mac] = (now, r.weight_kg)
        queue.put_nowait(r)

    async with httpx.AsyncClient() as client:
        scanner = BleakScanner(detection_callback=on_adv)
        asyncio.create_task(retry_loop(store, client))
        await scanner.start()
        log.info("Scanne nach Mi-Waage ...")
        try:
            while True:
                r = await queue.get()
                log.info("Messung: %.2f kg, Impedanz=%s", r.weight_kg, r.impedance)
                row_id = store.add(r)
                row = {"id": row_id, **asdict(r)}
                if await deliver(client, row):
                    store.mark_delivered(row_id)
        finally:
            await scanner.stop()


if __name__ == "__main__":
    asyncio.run(main())
