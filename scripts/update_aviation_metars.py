#!/usr/bin/env python3
"""Refresh a small static METAR fallback feed for the BTV aviation dashboard."""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

SITES = ["KBTV", "KSLK", "KPBG", "KMPV", "KEFK", "KRUT", "KMSS"]
URL = "https://aviationweather.gov/api/data/metar"

def main():
    response = requests.get(
        URL,
        params={"ids": ",".join(SITES), "format": "json", "taf": "false"},
        headers={"User-Agent": "NWS-BTV-Aviation-Dashboard (public weather display)"},
        timeout=25,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError("METAR endpoint returned an unexpected payload")
    records = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        station = str(item.get("icaoId") or item.get("stationId") or "").upper()
        raw = str(item.get("rawOb") or item.get("rawMETAR") or "").strip()
        if station in SITES and raw and station in raw:
            records[station] = {
                "rawMessage": raw,
                "observationTime": item.get("obsTime"),
                "retrievedAt": datetime.now(timezone.utc).isoformat(),
                "source": "Aviation Weather Center METAR feed",
            }
    if not records:
        raise RuntimeError("METAR endpoint returned no usable reports; refusing to overwrite feed")
    # Preserve the last known valid feed for stations omitted from a partial response.
    path = Path("data/aviation_metars.json")
    old = {}
    if path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8")).get("stations", {})
        except (ValueError, OSError, AttributeError):
            old = {}
    old.update(records)
    output = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "source": URL,
        "stations": old,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("Updated METAR fallback for: " + ", ".join(sorted(records)))

if __name__ == "__main__":
    main()
