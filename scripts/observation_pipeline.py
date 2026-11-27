#!/usr/bin/env python3
"""First operational ingestion pass for the BTV IRIS replacement.

Writes normalized, timestamped observations to a daily compressed NDJSON file.
All station coordinates are checked against the official NWS BTV CWA polygon.
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

import requests
try:
    from .asos_metar import normalize_iem_wind, parse_metar
except ImportError:  # direct execution from the scripts directory
    from asos_metar import normalize_iem_wind, parse_metar

BASE = Path(os.getenv("OBSERVATION_OUTPUT_DIR", "observation_data"))
DAYS = int(os.getenv("OBSERVATION_LOOKBACK_DAYS", "5"))
SYNOPTIC_TOKEN = os.getenv("SYNOPTIC_API_KEY", "").strip()
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "NWS-BTV-IRIS-Observation-Pipeline/0.1 (operational research)",
    "Accept": "application/geo+json, application/json, text/csv, */*",
})
NOW = datetime.now(timezone.utc)
START = NOW - timedelta(days=DAYS)
OBSERVATIONS: list[dict] = []
STATIONS: dict[str, dict] = {}
SOURCE_STATUS: list[dict] = []
SEEN: set[tuple] = set()


def request_json(url: str, params: dict | None = None, timeout: int = 45) -> dict:
    response = SESSION.get(url, params=params, timeout=timeout)
    response.raise_for_status()
    return response.json()


def parse_time(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def inside_ring(lon, lat, ring):
    inside = False
    j = len(ring) - 1
    for i, point in enumerate(ring):
        xi, yi = point[0], point[1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-15) + xi):
            inside = not inside
        j = i
    return inside


def geometry_contains(geometry, lon, lat):
    if not geometry or lon is None or lat is None:
        return False
    kind, coords = geometry.get("type"), geometry.get("coordinates", [])
    polygons = [coords] if kind == "Polygon" else coords if kind == "MultiPolygon" else []
    for polygon in polygons:
        if not polygon or not inside_ring(lon, lat, polygon[0]):
            continue
        if not any(inside_ring(lon, lat, hole) for hole in polygon[1:]):
            return True
    return False


def load_btv_geometry():
    payload = request_json(
        "https://mapservices.weather.noaa.gov/static/rest/services/nws_reference_maps/nws_reference_map/FeatureServer/1/query",
        {"where": "cwa='BTV'", "outFields": "cwa,wfo", "returnGeometry": "true",
         "outSR": "4326", "f": "geojson"},
    )
    features = payload.get("features", [])
    if not features:
        raise RuntimeError("Official NWS CWA service returned no BTV polygon.")
    return [f["geometry"] for f in features if f.get("geometry")]


def in_btv(geometries, lon, lat):
    try:
        lon, lat = float(lon), float(lat)
    except (TypeError, ValueError):
        return False
    return any(geometry_contains(g, lon, lat) for g in geometries)


def station_record(source, station_id, name, lat, lon, network=None):
    if not in_btv(CWA_GEOMETRIES, lon, lat):
        return None
    key = f"{source}:{station_id}"
    item = {
        "station_id": key, "provider_station_id": str(station_id),
        "source": source, "name": name or str(station_id),
        "latitude": float(lat), "longitude": float(lon),
        "network": network,
    }
    STATIONS[key] = item
    return item


def add_observation(station, variable, value, unit, timestamp, raw_value=None,
                    raw_unit=None, period_start=None, period_end=None,
                    qc="unknown", raw_report=None, metadata=None):
    observed = parse_time(timestamp)
    if station is None or observed is None or (value in (None, "", "M", "T") and raw_value != "T"):
        return
    if observed < START - timedelta(days=1) or observed > NOW + timedelta(minutes=5):
        return
    key = (station["station_id"], variable, observed.isoformat(), str(value), period_start, period_end)
    if key in SEEN:
        return
    SEEN.add(key)
    record = {
        "station_id": station["station_id"],
        "provider_station_id": station["provider_station_id"],
        "source": station["source"],
        "network": station.get("network"),
        "station_name": station["name"],
        "latitude": station["latitude"], "longitude": station["longitude"],
        "variable": variable, "value": value, "unit": unit,
        "observation_time": observed.isoformat(),
        "latest_observation_at": observed.isoformat(),
        "ingested_at": NOW.isoformat(),
        "period_start": period_start, "period_end": period_end,
        "raw_value": raw_value if raw_value is not None else value,
        "raw_unit": raw_unit if raw_unit is not None else unit,
        "qc_status": qc,
    }
    if raw_report:
        record["raw_report"] = raw_report
    if metadata:
        record["metadata"] = metadata
    OBSERVATIONS.append(record)


def local_report_time(date_value, time_value):
    if not date_value or not time_value:
        return None
    date_text = str(date_value).strip()
    time_text = str(time_value).strip()
    dt = None
    for date_fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"):
        for time_fmt in ("%I:%M %p", "%I:%M:%S %p", "%H:%M", "%H:%M:%S"):
            try:
                dt = datetime.strptime(date_text + " " + time_text, date_fmt + " " + time_fmt)
                break
            except ValueError:
                continue
        if dt:
            break
    return dt.replace(tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc) if dt else None


def parse_report_amount(value):
    if value in (None, "", "M"):
        return None, None
    if str(value).strip().upper() == "T":
        return None, "trace"
    try:
        return float(value), None
    except (TypeError, ValueError):
        return None, None


def c_to_f(value):
    return round(float(value) * 9 / 5 + 32, 1)


def ms_to_mph(value):
    return round(float(value) * 2.2369362921, 1)


def meters_to_inches(value):
    return round(float(value) * 39.37007874, 3)


def ingest_nws(geometries):
    stations = ["KBTV", "KPBG", "KMSS", "KSLK", "KMPV", "KEFK", "KRUT"]
    count = 0
    for sid in stations:
        try:
            feature = request_json(f"https://api.weather.gov/stations/{sid}")
            props = feature.get("properties", {})
            coords = (feature.get("geometry") or {}).get("coordinates") or []
            lon, lat = (coords[0], coords[1]) if len(coords) >= 2 else (props.get("longitude"), props.get("latitude"))
            st = station_record("NWS", sid, props.get("name"), lat, lon, "NWS/ASOS")
            if st is None:
                continue
            latest = request_json(f"https://api.weather.gov/stations/{sid}/observations/latest").get("properties", {})
            ts = latest.get("timestamp")
            mapping = [
                ("temperature", "temperature", "degC", c_to_f, "degF"),
                ("dewpoint", "dewpoint", "degC", c_to_f, "degF"),
                ("windSpeed", "wind_speed", "m/s", ms_to_mph, "mph"),
                ("windGust", "wind_gust", "m/s", ms_to_mph, "mph"),
                ("windDirection", "wind_direction", "degree", lambda x: float(x), "degree"),
                ("relativeHumidity", "relative_humidity", "%", lambda x: float(x), "%"),
                ("precipitationLastHour", "precipitation_1h", "m", meters_to_inches, "inch"),
            ]
            for field, variable, raw_unit, convert, unit in mapping:
                item = latest.get(field) or {}
                raw = item.get("value") if isinstance(item, dict) else None
                if raw is not None:
                    add_observation(st, variable, convert(raw), unit, ts, raw, raw_unit)
                    count += 1
        except Exception as exc:
            SOURCE_STATUS.append({"source": "NWS API", "station": sid, "status": "error", "detail": str(exc)[:180]})
    SOURCE_STATUS.append({"source": "NWS API", "status": "ok" if count else "no_values", "observation_values": count})


def ingest_iem_asos(geometries):
    station_ids = "BTV,PBG,MSS,SLK,MPV,EFK,RUT"
    response = SESSION.get("https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py", params={
        "station": station_ids, "data": "all",
        "sts": START.strftime("%Y-%m-%dT%H:%MZ"), "ets": NOW.strftime("%Y-%m-%dT%H:%MZ"),
        "tz": "Etc/UTC", "format": "onlycomma", "latlon": "yes", "elev": "no",
        "missing": "empty", "trace": "T",
    }, timeout=60)
    response.raise_for_status()
    rows = list(csv.DictReader(io.StringIO(response.text)))
    count = 0
    for original in rows:
        row = normalize_iem_wind(original)
        sid = row.get("station")
        st = station_record("IEM_ASOS", sid, sid, row.get("lat"), row.get("lon"), "ASOS/AWOS")
        if st is None:
            continue
        ts = row.get("valid")
        mapping = [
            ("tmpf", "temperature", "degF"), ("dwpf", "dewpoint", "degF"),
            ("wind_mph", "wind_speed", "mph"), ("gust_mph", "wind_gust", "mph"),
            ("drct", "wind_direction", "degree"), ("p01i", "precipitation_1h", "inch"),
            ("ice_accretion_1hr", "ice_accretion_1h", "inch"),
            ("ice_accretion_3hr", "ice_accretion_3h", "inch"),
            ("ice_accretion_6hr", "ice_accretion_6h", "inch"),
        ]
        for field, variable, unit in mapping:
            value = row.get(field)
            if value not in (None, "", "M", "T"):
                try:
                    add_observation(st, variable, float(value), unit, ts,
                                    row.get({"wind_mph":"sknt","gust_mph":"gust"}.get(field, field)),
                                    "knot" if field in ("wind_mph", "gust_mph") else unit,
                                    qc="provider")
                    count += 1
                except (TypeError, ValueError):
                    pass
        metar = row.get("metar", "")
        if metar:
            parsed = parse_metar(metar, parse_time(ts))
            for field, variable, unit in (
                ("precip_1h_in", "metar_precipitation_1h", "inch"),
                ("precip_3h_or_6h_in", "metar_precipitation_3h_or_6h", "inch"),
                ("precip_24h_in", "metar_precipitation_24h", "inch"),
                ("ice_accretion_1h_in", "metar_ice_accretion_1h", "inch"),
                ("ice_accretion_3h_in", "metar_ice_accretion_3h", "inch"),
                ("ice_accretion_6h_in", "metar_ice_accretion_6h", "inch"),
            ):
                if parsed.get(field) is not None:
                    add_observation(st, variable, parsed[field], unit, ts, parsed[field], unit,
                                    raw_report=metar, metadata={"ice_sensor_status": parsed["ice_sensor_status"]})
            for code in parsed["present_weather_codes"]:
                add_observation(st, "present_weather", code, "code", ts, code, "METAR code",
                                raw_report=metar)
    SOURCE_STATUS.append({"source": "IEM ASOS/AWOS", "status": "ok", "rows": len(rows), "normalized_values": count})


def ingest_synoptic(geometries):
    if not SYNOPTIC_TOKEN:
        SOURCE_STATUS.append({"source": "Synoptic", "status": "skipped", "reason": "SYNOPTIC_API_KEY missing"})
        return
    base = "https://api.synopticdata.com/v2/stations"
    params = {"token": SYNOPTIC_TOKEN, "cwa": "BTV", "within": 180,
              "vars": "air_temp,wind_speed,wind_gust,dew_point_temperature,relative_humidity,wind_direction",
              "units": "english", "obtimezone": "utc", "complete": 1}
    payload = request_json(base + "/latest", params)
    summary = payload.get("SUMMARY", {})
    if str(summary.get("RESPONSE_CODE")) != "1":
        raise RuntimeError("Synoptic API: " + str(summary.get("RESPONSE_MESSAGE", summary)))
    count = 0
    for item in payload.get("STATION", []) or []:
        sid = item.get("STID")
        st = station_record("Synoptic", sid, item.get("NAME"), item.get("LATITUDE"), item.get("LONGITUDE"),
                            item.get("MNET_NAME") or item.get("MNET_ID"))
        if st is None:
            continue
        obs = item.get("OBSERVATIONS", {})
        station_time = obs.get("date_time") or item.get("OBSERVATION_TIME")
        variable_map = {
            "air_temp": ("temperature", "degF"), "dew_point_temperature": ("dewpoint", "degF"),
            "wind_speed": ("wind_speed", "mph"), "wind_gust": ("wind_gust", "mph"),
            "wind_direction": ("wind_direction", "degree"),
            "relative_humidity": ("relative_humidity", "%"),
        }
        for key, (variable, unit) in variable_map.items():
            candidate = obs.get(key + "_value_1")
            if isinstance(candidate, dict):
                value = candidate.get("value")
                ts = candidate.get("date_time") or station_time
            else:
                value, ts = candidate, station_time
            if value is not None:
                add_observation(st, variable, value, unit, ts, value, unit)
                count += 1
    SOURCE_STATUS.append({"source": "Synoptic", "status": "ok", "stations": len(payload.get("STATION", []) or []),
                          "normalized_values": count})



def ingest_coop_daily(geometries):
    for state in ("VT", "NY"):
        network = f"{state}_COOP"
        inventory = request_json(f"https://mesonet.agron.iastate.edu/geojson/network/{network}.geojson")
        station_lookup = {}
        for feature in inventory.get("features", []) or []:
            props = feature.get("properties", {})
            coords = (feature.get("geometry") or {}).get("coordinates") or []
            if len(coords) < 2:
                continue
            sid = props.get("sid") or props.get("station") or props.get("nwsli")
            if not sid:
                continue
            st = station_record("IEM_COOP", sid, props.get("name") or sid, coords[1], coords[0], network)
            if st:
                station_lookup[str(sid).upper()] = st
        response = SESSION.get("https://mesonet.agron.iastate.edu/cgi-bin/request/coopobs.py", params={
            "network": network, "stations": "_ALL",
            "sts": START.date().isoformat(), "ets": NOW.date().isoformat(),
            "what": "view", "delim": "comma",
        }, timeout=60)
        response.raise_for_status()
        rows = csv.DictReader(io.StringIO(response.text))
        row_count = 0
        count = 0
        for row in rows:
            sid = str(row.get("nwsli") or "").upper()
            st = station_lookup.get(sid)
            if not st:
                continue
            observed = local_report_time(row.get("date"), row.get("time"))
            if not observed:
                continue
            for field, variable, unit in (
                ("high_F", "daily_max_temperature", "degF"),
                ("low_F", "daily_min_temperature", "degF"),
                ("precip", "daily_precipitation", "inch"),
                ("snow_inch", "daily_new_snow", "inch"),
                ("snowd_inch", "daily_snow_depth", "inch"),
            ):
                value, flag = parse_report_amount(row.get(field))
                if value is not None or flag == "trace":
                    add_observation(st, variable, value, unit, observed,
                                    row.get(field), unit, period_end=observed.isoformat(),
                                    qc="trace" if flag else "observer_report",
                                    metadata={"report_date": row.get("date"), "report_time_local": row.get("time"),
                                              "period": "daily", "trace_reported": flag == "trace"})
                    count += 1
            row_count += 1
        SOURCE_STATUS.append({"source": f"IEM COOP {state}", "status": "ok",
                              "in_cwa_stations": len(station_lookup), "daily_reports": row_count,
                              "normalized_values": count})


def ingest_cocorahs_daily(geometries):
    total_rows = 0
    count = 0
    for state in ("VT", "NY"):
        response = SESSION.get("https://data.cocorahs.org/cocorahs/export/exportreports.aspx", params={
            "ReportType": "Daily", "Format": "CSV", "State": state,
            "StartDate": START.strftime("%m/%d/%Y"), "EndDate": NOW.strftime("%m/%d/%Y"),
            "TimesInGMT": "True",
        }, timeout=60)
        response.raise_for_status()
        rows = csv.DictReader(io.StringIO(response.text))
        for row in rows:
            st = station_record("CoCoRaHS", row.get("StationNumber"), row.get("StationName"),
                                row.get("Latitude"), row.get("Longitude"), "CoCoRaHS Daily")
            if not st:
                continue
            observed = local_report_time(row.get("ObservationDate"), row.get("ObservationTime"))
            if not observed:
                continue
            for field, variable, unit in (
                ("TotalPrecipAmt", "daily_precipitation", "inch"),
                ("NewSnowDepth", "daily_new_snow", "inch"),
                ("NewSnowSWE", "daily_new_snow_water_equivalent", "inch"),
                ("TotalSnowDepth", "daily_snow_depth", "inch"),
                ("TotalSnowSWE", "daily_snow_water_equivalent", "inch"),
            ):
                value, flag = parse_report_amount(row.get(field))
                if value is not None or flag == "trace":
                    add_observation(st, variable, value, unit, observed,
                                    row.get(field), unit, period_end=observed.isoformat(),
                                    qc="trace" if flag else "observer_report",
                                    metadata={"report_date": row.get("ObservationDate"),
                                              "report_time_local": row.get("ObservationTime"),
                                              "submitted_at": row.get("EntryDateTime"),
                                              "period": "daily", "trace_reported": flag == "trace"})
                    count += 1
            total_rows += 1
    SOURCE_STATUS.append({"source": "CoCoRaHS daily", "status": "ok",
                          "in_cwa_reports": total_rows, "normalized_values": count})


def ingest_nysm_inventory(geometries):
    """Register public NYSM station locations; intentionally fetch no observations."""
    payload = request_json(
        "https://nysgeohub.ny.gov/arcgis/rest/services/Weather/NYS_Mesonet/MapServer/0/query",
        {"where": "1=1", "outFields": "station_na,lon,lat,code,Elev",
         "returnGeometry": "false", "f": "json"},
    )
    count = 0
    for feature in payload.get("features", []) or []:
        attrs = feature.get("attributes", {})
        st = station_record("NYS_Mesonet_metadata", attrs.get("code"),
                            attrs.get("station_na"), attrs.get("lat"), attrs.get("lon"),
                            "NYS Mesonet (metadata only)")
        if st:
            st["elevation_m"] = attrs.get("Elev")
            st["observation_access"] = "pending_data_policy_approval"
            count += 1
    SOURCE_STATUS.append({"source": "NYS Mesonet station inventory", "status": "ok",
                          "statewide_station_count": len(payload.get("features", []) or []),
                          "btv_cwa_station_count": count,
                          "observations_enabled": False})


def write_outputs():
    day = NOW.strftime("%Y-%m-%d")
    outdir = BASE / day
    outdir.mkdir(parents=True, exist_ok=True)
    data_path = outdir / "observations.ndjson.gz"
    with gzip.open(data_path, "wt", encoding="utf-8") as handle:
        for item in sorted(OBSERVATIONS, key=lambda x: x["observation_time"]):
            handle.write(json.dumps(item, separators=(",", ":")) + "\n")
    station_path = BASE / "stations.json"
    station_path.parent.mkdir(parents=True, exist_ok=True)
    station_path.write_text(json.dumps(sorted(STATIONS.values(), key=lambda x: x["station_id"]), indent=2), encoding="utf-8")
    manifest = {
        "generated_at": NOW.isoformat(), "latest_observation_at": max(
            (x["observation_time"] for x in OBSERVATIONS), default=None),
        "observation_count": len(OBSERVATIONS), "station_count": len(STATIONS),
        "sources": SOURCE_STATUS, "data_file": str(data_path),
        "scope": "Official NWS BTV CWA polygon; LSRs excluded; NYSM observations not enabled",
    }
    (BASE / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    CWA_GEOMETRIES = load_btv_geometry()
    for adapter in (ingest_nws, ingest_iem_asos, ingest_synoptic, ingest_coop_daily, ingest_cocorahs_daily, ingest_nysm_inventory):
        try:
            adapter(CWA_GEOMETRIES)
        except Exception as exc:
            SOURCE_STATUS.append({"source": adapter.__name__, "status": "error", "detail": str(exc)[:300]})
    write_outputs()
    if not OBSERVATIONS:
        raise SystemExit("No observations were ingested; see manifest source errors.")
