#!/usr/bin/env python3
"""Refresh live aviation TAF and ASOS observation feeds for aviation.html."""
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

SITES = ("KBTV", "KSLK", "KPBG", "KMPV", "KEFK", "KRUT", "KMSS")
IEM_STATIONS = {
    "KBTV": "BTV",
    "KSLK": "SLK",
    "KPBG": "PBG",
    "KMPV": "MPV",
    "KEFK": "EFK",
    "KRUT": "RUT",
    "KMSS": "MSS",
}
URL = "https://aviationweather.gov/api/data/taf"
IEM_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
OUT = Path("data/aviation_tafs.json")
OBS_OUT = Path("data/aviation_obs.json")
HEADERS = {"User-Agent": "NWS-SABTV-AviationDashboard/1.0 (GitHub Actions; public weather display)"}


def pattern(site):
    return re.compile(
        r"(?<![A-Z0-9])(?:TAF\s+)?(?:(?:AMD|COR)\s+)?"
        + re.escape(site)
        + r"\s+\d{6}Z\s+\d{4}/\d{4}\b",
        re.IGNORECASE,
    )


def parse_float(value):
    value = str(value or "").strip()
    if not value or value.upper() == "M":
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if number == number and abs(number) != float("inf") else None


def parse_timestamp(value):
    value = str(value or "").strip()
    if not value or value.upper() == "M":
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def wind_from_metar(raw):
    match = re.search(
        r"(?<![A-Z0-9])(VRB|\d{3})(\d{2,3})(?:G(\d{2,3}))?KT(?![A-Z0-9])",
        raw or "",
        re.IGNORECASE,
    )
    if not match:
        return None, None, None
    direction = None if match.group(1).upper() == "VRB" else parse_float(match.group(1))
    speed = parse_float(match.group(2))
    gust = parse_float(match.group(3)) if match.group(3) else None
    return direction, speed, gust


def fetch_observations(session):
    # One validated IEM request for all airport IDs. Do not add cache-busting
    # parameters here: the IEM endpoint validates its allowed query arguments.
    params = [("station", code) for code in IEM_STATIONS.values()]
    params.extend(("data", field) for field in ("drct", "sknt", "gust", "metar"))
    params.extend((("hours", "3"), ("tz", "UTC"), ("format", "tdf")))
    response = session.get(
        IEM_URL,
        params=params,
        headers={"Cache-Control": "no-cache"},
        timeout=25,
    )
    response.raise_for_status()
    lines = [line for line in response.text.splitlines() if line.strip()]
    header_index = -1
    header = []
    for index, line in enumerate(lines):
        candidate = [cell.strip().lower().lstrip("#").strip() for cell in line.split("\t")]
        if "station" in candidate and "valid" in candidate and "metar" in candidate:
            header_index, header = index, candidate
            break
    if header_index < 0:
        raise RuntimeError("IEM ASOS response did not contain the expected tab-delimited header")

    indexes = {name: header.index(name) for name in ("station", "valid", "metar")}
    for field in ("drct", "sknt", "gust"):
        indexes[field] = header.index(field) if field in header else -1

    newest = {}
    for line in lines[header_index + 1:]:
        if line.lstrip().startswith("#"):
            continue
        cells = line.split("\t")
        station = cells[indexes["station"]].strip().upper() if indexes["station"] < len(cells) else ""
        site = next((name for name, code in IEM_STATIONS.items() if code == station), None)
        if not site:
            continue

        timestamp_text = cells[indexes["valid"]].strip() if indexes["valid"] < len(cells) else ""
        stamp = parse_timestamp(timestamp_text)
        if stamp is None:
            continue
        raw = cells[indexes["metar"]].strip() if indexes["metar"] < len(cells) else ""
        if raw.upper() == "M":
            raw = ""

        def cell_number(field):
            index = indexes[field]
            return parse_float(cells[index]) if 0 <= index < len(cells) else None

        direction = cell_number("drct")
        speed = cell_number("sknt")
        gust = cell_number("gust")
        if speed is None:
            raw_direction, raw_speed, raw_gust = wind_from_metar(raw)
            direction = direction if direction is not None else raw_direction
            speed = raw_speed
            gust = gust if gust is not None else raw_gust

        if not raw and speed is None:
            continue
        record = {
            "timestamp": stamp.isoformat().replace("+00:00", "Z"),
            "raw": raw,
            "direction_deg": direction,
            "speed_kt": speed,
            "gust_kt": gust,
        }
        previous = newest.get(site)
        if previous is None or stamp > parse_timestamp(previous["timestamp"]):
            newest[site] = record

    if len(newest) < 3:
        raise RuntimeError(
            f"IEM returned usable observations for only {len(newest)}/7 airports; preserving last good observation feed"
        )

    for site in SITES:
        record = newest.get(site)
        if not record:
            print(f"{site}: no usable IEM observation in the last 3 hours")
            continue
        speed, direction, gust = record["speed_kt"], record["direction_deg"], record["gust_kt"]
        if speed is None:
            wind_text = "wind unavailable in newest METAR"
        elif speed == 0:
            wind_text = "Calm"
        else:
            wind_text = (f"{direction:03.0f}° " if direction is not None else "") + f"{speed:.0f} kt"
            if gust is not None and gust > speed:
                wind_text += f" G{gust:.0f} kt"
        print(f"{site}: IEM report={record['timestamp']} wind={wind_text}")

    payload = json.dumps({"source": "IEM ASOS", "observations": newest}, indent=2) + "\n"
    OBS_OUT.parent.mkdir(parents=True, exist_ok=True)
    if OBS_OUT.exists() and OBS_OUT.read_text(encoding="utf-8") == payload:
        print("IEM observations unchanged; no observation-feed commit needed.")
    else:
        OBS_OUT.write_text(payload, encoding="utf-8")
        print(f"Wrote {OBS_OUT} with {len(newest)}/7 airport observations.")
    return newest


def main():
    session = requests.Session()
    session.headers.update(HEADERS)

    # Refresh observations independently of TAF success so a temporary AWC
    # issue does not prevent the wind fields from updating.
    try:
        fetch_observations(session)
    except Exception as exc:
        print(f"IEM observation refresh failed; preserving last good feed: {exc}", file=sys.stderr)

    tafs = {}
    failures = []
    missing = []
    for index, site in enumerate(SITES):
        if index:
            time.sleep(0.35)
        try:
            response = session.get(URL, params={"ids": site, "format": "raw"}, timeout=20)
            response.raise_for_status()
            raw = response.text.strip()
            normalized = " ".join(raw.split())
            if not normalized or not pattern(site).search(normalized):
                missing.append(site)
                print(f"{site}: no valid TAF in response; recording as unavailable")
                continue
            tafs[site] = raw
            print(f"{site}: retrieved {len(raw)} characters")
        except Exception as exc:
            failures.append(f"{site}: {exc}")

    if failures:
        print("TAF refresh failed; leaving the last good TAF feed untouched:", file=sys.stderr)
        for failure in failures:
            print(" - " + failure, file=sys.stderr)
        return 1
    if len(tafs) < 3:
        print(f"Only {len(tafs)} valid TAFs found; expected at least 3. Leaving the last good TAF feed untouched.", file=sys.stderr)
        return 1
    print(f"Valid TAFs: {len(tafs)}/{len(SITES)}; unavailable stations: {', '.join(missing) if missing else 'none'}")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"tafs": tafs}, indent=2) + "\n"
    if OUT.exists() and OUT.read_text(encoding="utf-8") == payload:
        print("TAFs unchanged; no commit needed.")
        return 0
    OUT.write_text(payload, encoding="utf-8")
    print(f"Wrote {OUT} with {len(tafs)} validated TAFs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
