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
    from .asos_metar import normalize_iem_wind, parse_metar, is_speci_report, is_routine_hourly_metar, metar_report_datetime, is_metar_report, wind_speed_to_mph
except ImportError:  # direct execution from the scripts directory
    from asos_metar import normalize_iem_wind, parse_metar, is_speci_report, is_routine_hourly_metar, metar_report_datetime, is_metar_report, wind_speed_to_mph

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


def canonical_station_key(source, station_id, network=None):
    sid = str(station_id or "").strip().upper()
    if source in ("NWS", "IEM_ASOS") or (source == "Synoptic" and re.fullmatch(r"K[A-Z0-9]{3}", sid)):
        return f"ASOS:{sid[1:] if sid.startswith('K') else sid}"
    if source in ("IEM_COOP", "IEM_RR3_COOP"):
        return f"COOP:{sid}"
    if source == "Synoptic" and sid.startswith("COOP") and len(sid) > 4:
        return f"COOP:{sid[4:]}"
    return f"{source}:{sid}"


def station_record(source, station_id, name, lat, lon, network=None):
    if not in_btv(CWA_GEOMETRIES, lon, lat):
        return None
    key = canonical_station_key(source, station_id, network)
    # A small coordinate tolerance catches cross-provider aliases whose station IDs differ
    # (e.g., Synoptic RWIS IDs versus IEM DOT IDs) without merging nearby highway sites.
    for existing_key, existing in STATIONS.items():
        if existing_key == key:
            continue
        same_site = (abs(float(existing["latitude"]) - float(lat)) <= 0.00010 and
                     abs(float(existing["longitude"]) - float(lon)) <= 0.00010)
        if same_site:
            key = existing_key
            break
    item = STATIONS.get(key)
    if item is None:
        report_type = ("ASOS" if key.startswith("ASOS:") else "COOP" if key.startswith("COOP:") else
                       "RWIS" if source == "IEM_RWIS" else "CoCoRaHS" if source == "CoCoRaHS" else "Public")
        item = {"station_id": key, "provider_station_id": str(station_id),
                "source": source, "report_type": report_type, "name": name or str(station_id),
                "latitude": float(lat), "longitude": float(lon), "network": network, "source_aliases": []}
        STATIONS[key] = item
    alias = {"source": source, "provider_station_id": str(station_id), "network": network}
    if alias not in item["source_aliases"]:
        item["source_aliases"].append(alias)
    # Keep observation provenance tied to the adapter currently requesting this station.
    item["source"] = source
    item["network"] = network
    item["provider_station_id"] = str(station_id)
    if name and str(name).strip().upper() not in (str(station_id).strip().upper(), "UNKNOWN"):
        if item["name"] == item["provider_station_id"] or len(str(name)) > len(str(item["name"])):
            item["name"] = str(name).strip()
    return item


def normalized_dimensions(variable, metadata=None):
    metadata = metadata or {}
    if variable in ("temperature", "daily_max_temperature", "daily_min_temperature"):
        statistic = "maximum" if variable == "daily_max_temperature" else "minimum" if variable == "daily_min_temperature" else "instantaneous"
        return "temperature", statistic
    if variable == "dewpoint":
        return "dewpoint", "instantaneous"
    if variable in ("wind_speed", "wind_gust", "wind_direction", "relative_humidity"):
        return variable, "instantaneous"
    if "precipitation" in variable:
        if variable.startswith("daily_"):
            return "precipitation", "daily_report"
        hours = metadata.get("accumulation_hours") or metadata.get("precipitation_period_hours")
        if hours is None:
            match = re.search(r"_(\d+)h(?:r)?$", variable)
            hours = int(match.group(1)) if match else None
        return "precipitation", f"{hours}h_accumulation" if hours else "reported_accumulation"
    if variable == "daily_new_snow":
        return "snowfall", "daily_report"
    if variable == "daily_snow_depth":
        return "snow_depth", "daily_report"
    if variable == "daily_new_snow_water_equivalent" or variable == "daily_snow_water_equivalent":
        return "snow_water_equivalent", "daily_report"
    if "ice_accretion" in variable:
        match = re.search(r"_(\d+)h$", variable)
        hours = int(match.group(1)) if match else None
        return "ice_accretion", f"{hours}h_accumulation" if hours else "reported_accumulation"
    if variable == "present_weather":
        return "present_weather", "reported_code"
    return variable, "reported"


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
    element, statistic = normalized_dimensions(variable, metadata)
    accumulation_hours = (metadata or {}).get("accumulation_hours") or (metadata or {}).get("precipitation_period_hours")
    if accumulation_hours is None:
        match = re.search(r"_(\d+)h(?:r)?$", variable)
        if match:
            accumulation_hours = int(match.group(1))
    record = {
        "station_id": station["station_id"],
        "provider_station_id": station["provider_station_id"],
        "source": station["source"],
        "report_type": station.get("report_type") or ("ASOS" if station["source"] == "IEM_ASOS" else "COOP" if station["source"] == "IEM_COOP" else "CoCoRaHS" if station["source"] == "CoCoRaHS" else "Public"),
        "network": station.get("network"),
        "station_name": station["name"],
        "latitude": station["latitude"], "longitude": station["longitude"],
        "variable": variable, "element": element, "statistic": statistic,
        "accumulation_hours": accumulation_hours,
        "value": value, "unit": unit,
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


def local_report_time(date_value, time_value, timezone_name="America/New_York"):
    if not date_value or not time_value:
        return None
    date_text = str(date_value).strip()
    time_text = str(time_value).strip()
    dt = None
    for date_fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%m-%d-%Y"):
        for time_fmt in ("%I:%M %p", "%I:%M:%S %p", "%H:%M", "%H:%M:%S", "%H%M", "%I %p", "%I%p"):
            try:
                dt = datetime.strptime(date_text + " " + time_text, date_fmt + " " + time_fmt)
                break
            except ValueError:
                continue
        if dt:
            break
    return dt.replace(tzinfo=ZoneInfo(timezone_name)).astimezone(timezone.utc) if dt else None


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
    return wind_speed_to_mph(value, "m/s")


def meters_to_inches(value):
    return round(float(value) * 39.37007874, 3)


def ingest_nws(geometries):
    stations = ["KBTV", "KPBG", "KMSS", "KSLK", "KMPV", "KEFK", "KRUT", "KMVL", "KVSF"]
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
            ]
            for field, variable, default_raw_unit, convert, unit in mapping:
                item = latest.get(field) or {}
                raw = item.get("value") if isinstance(item, dict) else None
                if raw is not None:
                    if variable in ("wind_speed", "wind_gust"):
                        raw_unit = item.get("unitCode") or default_raw_unit
                        normalized = wind_speed_to_mph(raw, raw_unit)
                    else:
                        raw_unit = default_raw_unit
                        normalized = convert(raw)
                    add_observation(st, variable, normalized, unit, ts, raw, raw_unit)
                    count += 1
        except Exception as exc:
            SOURCE_STATUS.append({"source": "NWS API", "station": sid, "status": "error", "detail": str(exc)[:180]})
    SOURCE_STATUS.append({"source": "NWS API", "status": "ok" if count else "no_values", "observation_values": count})


def ingest_iem_asos(geometries):
    station_ids = "BTV,PBG,MSS,SLK,MPV,EFK,RUT,MVL,VSF"
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
        metar = row.get("metar", "")
        # SPECI observations are additional reports, not separate hourly samples.
        if is_speci_report(metar):
            continue
        ts = row.get("valid")
        routine_metar = is_metar_report(metar)
        sid = row.get("station")
        st = station_record("IEM_ASOS", sid, sid, row.get("lat"), row.get("lon"), "ASOS/AWOS")
        if st is None:
            continue
        mapping = [
            ("tmpf", "temperature", "degF"), ("dwpf", "dewpoint", "degF"),
            ("wind_mph", "wind_speed", "mph"), ("gust_mph", "wind_gust", "mph"),
            ("drct", "wind_direction", "degree"),
        ]
        # Only actual routine METARs contribute ASOS precipitation and ice.
        # SPECI and non-METAR high-frequency samples are excluded.
        if routine_metar:
            mapping.extend([
                ("ice_accretion_1hr", "ice_accretion_1h", "inch"),
                ("ice_accretion_3hr", "ice_accretion_3h", "inch"),
                ("ice_accretion_6hr", "ice_accretion_6h", "inch"),
            ])
        for field, variable, unit in mapping:
            value = row.get(field)
            if value not in (None, "", "M", "T"):
                try:
                    observation_ts = metar_report_datetime(metar, parse_time(ts)) if variable.startswith("ice_accretion_") else ts
                    add_observation(st, variable, float(value), unit, observation_ts or ts,
                                    row.get({"wind_mph":"sknt","gust_mph":"gust"}.get(field, field)),
                                    "knot" if field in ("wind_mph", "gust_mph") else unit,
                                    qc="provider",
                                    metadata={"hourly_selection": "METAR_only"} if variable.startswith("ice_accretion_") else None)
                    count += 1
                except (TypeError, ValueError):
                    pass
        if metar:
            parsed = parse_metar(metar, parse_time(ts))
            if routine_metar and parsed.get("precip_1h_in") is not None:
                issued = metar_report_datetime(metar, parse_time(ts))
                add_observation(st, "metar_precipitation_1h", parsed["precip_1h_in"], "inch",
                                issued or ts, parsed["precip_1h_in"], "inch",
                                raw_report=metar, metadata={"hourly_selection": "METAR_only"})
            metar_fields = [
                ("precip_3h_or_6h_in", "metar_precipitation_3h_or_6h", "inch"),
                ("precip_24h_in", "metar_precipitation_24h", "inch"),
                ("ice_accretion_3h_in", "metar_ice_accretion_3h", "inch"),
                ("ice_accretion_6h_in", "metar_ice_accretion_6h", "inch"),
            ]
            if routine_metar:
                metar_fields.append(("ice_accretion_1h_in", "metar_ice_accretion_1h", "inch"))
            for field, variable, unit in metar_fields:
                if parsed.get(field) is not None:
                    issued = metar_report_datetime(metar, parse_time(ts)) if field.startswith("ice_accretion_") else None
                    add_observation(st, variable, parsed[field], unit, issued or ts, parsed[field], unit,
                                    raw_report=metar, metadata={"ice_sensor_status": parsed["ice_sensor_status"],
                                              "hourly_selection": "METAR_only" if field == "ice_accretion_1h_in" and routine_metar else None,
                                              "precipitation_period_hours": parsed.get("precip_3h_or_6h_period_hours") if field == "precip_3h_or_6h_in" else (1 if field == "precip_1h_in" else 24 if field == "precip_24h_in" else None)})
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
              # Synoptic defaults English wind speeds to knots; normalized dashboard
              # values are mph, so request mph explicitly rather than relabeling knots.
              "units": "english,speed|mph", "obtimezone": "utc", "complete": 1}
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
        if isinstance(station_time, list):
            station_time = station_time[-1] if station_time else None
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
    precip_payload = request_json(base + "/precip", {
        "token": SYNOPTIC_TOKEN, "cwa": "BTV", "pmode": "last",
        "accum_hours": "1,3,6,12,24,48,72,120",
        "units": "english", "obtimezone": "utc", "complete": 1,
    })
    precip_summary = precip_payload.get("SUMMARY", {})
    if str(precip_summary.get("RESPONSE_CODE")) != "1":
        raise RuntimeError("Synoptic precipitation API: " + str(precip_summary.get("RESPONSE_MESSAGE", precip_summary)))
    precip_count = 0
    precip_stations = precip_payload.get("STATION", []) or []
    for item in precip_stations:
        st = station_record("Synoptic", item.get("STID"), item.get("NAME"),
                            item.get("LATITUDE"), item.get("LONGITUDE"),
                            item.get("MNET_NAME") or item.get("MNET_ID"))
        if st is None:
            continue
        obs = item.get("OBSERVATIONS", {})
        for total in obs.get("precipitation", []) or []:
            amount = total.get("total")
            end_time = total.get("last_report")
            start_time = total.get("first_report")
            hours = total.get("accum_hours")
            if amount is None or not end_time:
                continue
            add_observation(st, "precipitation_accumulation", amount, "inch",
                            end_time, amount, "inch",
                            period_start=start_time, period_end=end_time,
                            qc="provider_derived",
                            metadata={"accumulation_hours": hours,
                                      "source_report_count": total.get("count"),
                                      "report_type": total.get("report_type")})
            precip_count += 1
    SOURCE_STATUS.append({"source": "Synoptic", "status": "ok", "stations": len(payload.get("STATION", []) or []),
                          "normalized_values": count, "precipitation_stations": len(precip_stations),
                          "precipitation_windows": precip_count})



def resolve_synoptic_nysm_network(networks_payload):
    """Find the NYSM network in Synoptic's network inventory."""
    for network in networks_payload.get("MNET", []) or []:
        name = " ".join(str(network.get(k) or "") for k in ("LONGNAME", "SHORTNAME")).upper()
        if "NYSM" in name or "NEW YORK STATE MESONET" in name:
            return str(network.get("ID") or network.get("SHORTNAME") or network.get("LONGNAME"))
    return None


def ingest_synoptic_nysm(geometries):
    """Ingest NYSM stations/observations from Synoptic, spatially clipped to BTV CWA."""
    if not SYNOPTIC_TOKEN:
        SOURCE_STATUS.append({"source": "Synoptic NYSM", "status": "skipped",
                            "reason": "SYNOPTIC_API_KEY missing"})
        return
    base = "https://api.synopticdata.com/v2"
    networks = request_json(base + "/networks", {"token": SYNOPTIC_TOKEN})
    summary = networks.get("SUMMARY", {})
    if str(summary.get("RESPONSE_CODE")) != "1":
        raise RuntimeError("Synoptic networks API: " + str(summary.get("RESPONSE_MESSAGE", summary)))
    network_id = resolve_synoptic_nysm_network(networks)
    if not network_id:
        SOURCE_STATUS.append({"source": "Synoptic NYSM", "status": "no_network_match",
                            "network_count": len(networks.get("MNET", []) or [])})
        return

    station_params = {"token": SYNOPTIC_TOKEN, "network": network_id, "status": "active", "complete": 1}
    metadata = request_json(base + "/stations/metadata", station_params)
    meta_summary = metadata.get("SUMMARY", {})
    if str(meta_summary.get("RESPONSE_CODE")) != "1":
        raise RuntimeError("Synoptic NYSM metadata API: " + str(meta_summary.get("RESPONSE_MESSAGE", meta_summary)))
    station_meta = metadata.get("STATION", []) or []
    station_lookup = {}
    for item in station_meta:
        sid = item.get("STID")
        st = station_record("Synoptic NYSM", sid, item.get("NAME"), item.get("LATITUDE"),
                            item.get("LONGITUDE"), "NYSM via Synoptic")
        if st is not None:
            station_lookup[str(sid)] = st

    latest = request_json(base + "/stations/latest", {
        "token": SYNOPTIC_TOKEN, "network": network_id, "within": 180,
        "vars": "air_temp,wind_speed,wind_gust,dew_point_temperature,relative_humidity,wind_direction",
        "units": "english,speed|mph", "obtimezone": "utc", "complete": 1,
    })
    latest_summary = latest.get("SUMMARY", {})
    if str(latest_summary.get("RESPONSE_CODE")) != "1":
        raise RuntimeError("Synoptic NYSM latest API: " + str(latest_summary.get("RESPONSE_MESSAGE", latest_summary)))
    variable_map = {
        "air_temp": ("temperature", "degF"), "dew_point_temperature": ("dewpoint", "degF"),
        "wind_speed": ("wind_speed", "mph"), "wind_gust": ("wind_gust", "mph"),
        "wind_direction": ("wind_direction", "degree"),
        "relative_humidity": ("relative_humidity", "%"),
    }
    count = 0
    observed_station_ids = set()
    for item in latest.get("STATION", []) or []:
        sid = str(item.get("STID") or "")
        st = station_lookup.get(sid)
        if st is None:
            # Provider station IDs can differ between metadata/latest responses;
            # still register using coordinates if they are present.
            st = station_record("Synoptic NYSM", sid, item.get("NAME"), item.get("LATITUDE"),
                                item.get("LONGITUDE"), "NYSM via Synoptic")
        if st is None:
            continue
        observed_station_ids.add(sid)
        obs = item.get("OBSERVATIONS", {})
        station_time = obs.get("date_time") or item.get("OBSERVATION_TIME")
        if isinstance(station_time, list):
            station_time = station_time[-1] if station_time else None
        for key, (variable, unit) in variable_map.items():
            candidate = obs.get(key + "_value_1")
            if isinstance(candidate, dict):
                value = candidate.get("value")
                ts = candidate.get("date_time") or station_time
            else:
                value, ts = candidate, station_time
            if value is not None:
                add_observation(st, variable, value, unit, ts, value, unit,
                               qc="provider_reported", metadata={"provider_network": "NYSM", "synoptic_network_id": network_id})
                count += 1
    SOURCE_STATUS.append({"source": "Synoptic NYSM", "status": "ok",
                         "network_id": network_id, "metadata_stations_in_btv": len(station_lookup),
                         "stations_reporting": len(observed_station_ids), "normalized_values": count})


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
        raw_count = 0
        count = 0
        report_ids = []
        for row in rows:
            raw_count += 1
            sid = str(row.get("nwsli") or "").upper()
            if len(report_ids) < 8:
                report_ids.append(sid)
            st = station_lookup.get(sid)
            if not st:
                continue
            observed = local_report_time(row.get("date"), row.get("time"))
            if not observed:
                continue
            period_end = observed.isoformat()
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
                                    row.get(field), unit, period_end=period_end,
                                    qc="trace" if flag else "observer_report",
                                    metadata={"report_date": row.get("date"), "report_time_local": row.get("time"),
                                              "period": "daily", "period_start_not_provided": True, "trace_reported": flag == "trace"})
                    count += 1
            row_count += 1
        SOURCE_STATUS.append({"source": f"IEM COOP {state}", "status": "ok",
                              "in_cwa_stations": len(station_lookup), "raw_daily_reports": raw_count,
                              "matched_daily_reports": row_count, "normalized_values": count,
                              "sample_report_station_ids": report_ids,
                              "sample_inventory_station_ids": list(station_lookup)[:8]})


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
            observed = local_report_time(row.get("ObservationDate"), row.get("ObservationTime"), "UTC")
            if not observed:
                continue
            period_end = observed.isoformat()
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
                                    row.get(field), unit, period_end=period_end,
                                    qc="trace" if flag else "observer_report",
                                    metadata={"report_date": row.get("ObservationDate"),
                                              "report_time_utc": row.get("ObservationTime"),
                                              "submitted_at": row.get("EntryDateTime"),
                                              "period": "daily", "period_start_not_provided": True, "trace_reported": flag == "trace"})
                    count += 1
            total_rows += 1
    SOURCE_STATUS.append({"source": "CoCoRaHS daily", "status": "ok",
                          "in_cwa_reports": total_rows, "normalized_values": count})



def ingest_rr3_coop(geometries):
    """Ingest supplemental BTV COOP observer reports from IEM's RR3BTV AFOS archive."""
    station_lookup = {}
    for state in ("VT", "NY"):
        inventory = request_json(f"https://mesonet.agron.iastate.edu/geojson/network/{state}_COOP.geojson")
        for feature in inventory.get("features", []) or []:
            props = feature.get("properties", {})
            coords = (feature.get("geometry") or {}).get("coordinates") or []
            sid = str(props.get("sid") or props.get("station") or props.get("nwsli") or "").upper()
            if not sid or len(coords) < 2:
                continue
            st = station_record("IEM_RR3_COOP", sid, props.get("name") or sid,
                                coords[1], coords[0], f"{state}_COOP supplemental RR3")
            if st:
                station_lookup[sid] = st

    response = SESSION.get("https://mesonet.agron.iastate.edu/cgi-bin/afos/retrieve.py", params={
        "pil": "RR3BTV", "fmt": "text", "limit": 500,
        "sdate": START.strftime("%Y-%m-%dT%H:%MZ"),
        "edate": NOW.strftime("%Y-%m-%dT%H:%MZ"),
    }, timeout=60)
    response.raise_for_status()
    products = response.text
    # RR3 .AR lines encode station, report date, local report time and SHEF-like values.
    report_count = 0
    value_count = 0
    unmatched = set()
    for line in products.splitlines():
        match = re.search(r"\.AR1?\s+([A-Z0-9]+)\s+(\d{6})\s+[A-Z]\s+DH(\d{4})(.*)", line.strip())
        if not match:
            continue
        sid, date_code, time_code, payload = match.groups()
        st = station_lookup.get(sid.upper())
        if not st:
            unmatched.add(sid.upper())
            continue
        try:
            year = 2000 + int(date_code[:2])
            report_date = datetime(year, int(date_code[2:4]), int(date_code[4:6])).strftime("%Y-%m-%d")
            observed = local_report_time(report_date, time_code, "America/New_York")
        except (ValueError, TypeError):
            continue
        if not observed:
            continue
        report_count += 1
        # Preserve observer-reported amounts; trace remains a trace, not zero.
        fields = re.findall(r"/(TA|PP|SF|SD)\s*([+-]?\d+(?:\.\d+)?|T)", payload.upper())
        for code, raw in fields:
            if code == "TA":
                variable, unit = "temperature", "degF"
            elif code == "PP":
                variable, unit = "daily_precipitation", "inch"
            elif code == "SF":
                variable, unit = "daily_new_snow", "inch"
            else:
                variable, unit = "daily_snow_depth", "inch"
            value, flag = parse_report_amount(raw)
            if code == "TA" and value is None:
                continue
            if value is not None or flag == "trace":
                add_observation(st, variable, value, unit, observed, raw, unit,
                    period_end=observed.isoformat(),
                    qc="trace" if flag else "observer_report",
                    raw_report=line.strip(),
                    metadata={"report_type":"supplemental_RR3_COOP",
                              "report_date":report_date,"report_time_local":time_code,
                              "period":"observer_report","trace_reported":flag=="trace"})
                value_count += 1
    SOURCE_STATUS.append({"source":"IEM RR3BTV supplemental COOP",
        "status":"ok" if report_count else "no_reports",
        "products_bytes":len(products.encode("utf-8")),
        "matched_station_reports":report_count,"normalized_values":value_count,
        "unmatched_station_ids":sorted(unmatched)[:30],
        "note":"Supplemental reports are retained separately from routine COOP daily summaries."})



def ingest_rwis(geometries):
    """Ingest Vermont and northern New York RWIS atmospheric/pavement observations via IEM."""
    total_rows = total_values = total_stations = 0
    for state in ("VT", "NY"):
        network = f"{state}_RWIS"
        inventory = request_json(f"https://mesonet.agron.iastate.edu/geojson/network/{network}.geojson")
        station_lookup = {}
        for feature in inventory.get("features", []) or []:
            props = feature.get("properties", {})
            coords = (feature.get("geometry") or {}).get("coordinates") or []
            sid = props.get("sid") or props.get("station") or props.get("stid") or props.get("station_id") or props.get("id")
            if not sid or len(coords) < 2:
                continue
            st = station_record("IEM_RWIS", sid, props.get("name") or props.get("sname") or sid,
                                coords[1], coords[0], network)
            if st:
                station_lookup[str(sid).upper()] = st
        total_stations += len(station_lookup)
        if not station_lookup:
            SOURCE_STATUS.append({"source": f"IEM {network}", "status": "no_stations",
                                  "inventory_features": len(inventory.get("features", []) or []), "sample_property_keys": list((inventory.get("features", [{}])[0].get("properties", {}) or {}).keys()) if inventory.get("features") else [], "sample_properties": (inventory.get("features", [{}])[0].get("properties", {}) or {}) if inventory.get("features") else {}})
            continue
        response = SESSION.get("https://mesonet.agron.iastate.edu/cgi-bin/request/rwis.py", params={
            "network": network, "stations": ",".join(station_lookup), "src": "atmos",
            "sts": START.strftime("%Y-%m-%dT%H:%M"), "ets": NOW.strftime("%Y-%m-%dT%H:%M"),
            "tz": "UTC", "what": "download", "delim": "comma", "gis": "true",
        }, timeout=90)
        response.raise_for_status()
        rows = list(csv.DictReader(io.StringIO(response.text)))
        values = 0
        sample_columns = list(rows[0].keys()) if rows else []
        sample_row = {k: v for k, v in (rows[0].items() if rows else []) if k.lower() not in ("lat", "lon", "latitude", "longitude")}
        for row in rows:
            normalized_row = {str(k).strip().lower(): v for k, v in row.items()}
            sid = str(next((normalized_row.get(k) for k in ("station", "sid", "station_id", "stid", "stationid", "network_station") if normalized_row.get(k)), "")).upper()
            st = station_lookup.get(sid)
            if st is None and sid:
                st = station_record("IEM_RWIS", sid, row.get("name") or sid, row.get("lat"), row.get("lon"), network)
            if st is None:
                continue
            ts = next((normalized_row.get(k) for k in ("valid", "valid_utc", "datetime", "observation_time", "date_time", "timestamp", "obtime") if normalized_row.get(k)), None)
            if not ts:
                continue
            mappings = (
                (("tmpf", "air_temp", "temperature"), "temperature", "degF", "degF"),
                (("dwpf", "dewpoint"), "dewpoint", "degF", "degF"),
                (("relh", "rh"), "relative_humidity", "%", "%"),
                (("sknt", "wind_speed"), "wind_speed", "mph", "knot"),
                (("gust", "gust_sknt"), "wind_gust", "mph", "knot"),
                (("drct", "wind_dir"), "wind_direction", "degree", "degree"),
                (("road_tmpf", "pavement_temp", "surface_temp", "tfs0"), "road_temperature", "degF", "degF"),
                (("subf", "subsurface_temp"), "subsurface_temperature", "degF", "degF"),
                (("pcpn", "precip"), "precipitation_1h", "inch", "inch"),
            )
            for fields, variable, unit, raw_unit in mappings:
                raw = next((normalized_row.get(field) for field in fields if normalized_row.get(field) not in (None, "", "M", "null")), None)
                if raw is None:
                    continue
                try:
                    value = float(raw)
                    if variable in ("wind_speed", "wind_gust"):
                        value = wind_speed_to_mph(value, "knot")
                    add_observation(st, variable, value, unit, ts, raw, raw_unit, qc="provider",
                                    metadata={"network": network, "source_product": "IEM_RWIS"})
                    values += 1
                except (TypeError, ValueError):
                    continue
            condition = normalized_row.get("scond0") or normalized_row.get("surface_condition") or normalized_row.get("road_condition") or normalized_row.get("tfs0_text")
            if condition not in (None, "", "M", "null"):
                add_observation(st, "road_condition", str(condition), "code", ts, condition, "IEM RWIS code",
                                qc="provider", metadata={"network": network, "source_product": "IEM_RWIS"})
                values += 1
        total_rows += len(rows)
        total_values += values
        SOURCE_STATUS.append({"source": f"IEM {network}", "status": "ok", "stations": len(station_lookup),
                              "rows": len(rows), "normalized_values": values,
                              "sample_columns": sample_columns, "sample_row": sample_row})
    SOURCE_STATUS.append({"source": "IEM RWIS total", "status": "ok" if total_values else "no_values",
                          "stations": total_stations, "rows": total_rows, "normalized_values": total_values})


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


def deduplicate_observations():
    """Collapse duplicate provider reports at the same canonical station/time/element."""
    priority = {"IEM_ASOS": 0, "IEM_COOP": 0, "IEM_RWIS": 0, "NWS": 1,
                "IEM_RR3_COOP": 1, "Synoptic": 2, "CoCoRaHS": 0}
    chosen = {}
    duplicates = 0
    for row in OBSERVATIONS:
        key = (row["station_id"], row["variable"], row["observation_time"],
               row.get("period_start"), row.get("period_end"))
        previous = chosen.get(key)
        if previous is None:
            chosen[key] = row
            continue
        duplicates += 1
        if priority.get(row.get("source"), 5) < priority.get(previous.get("source"), 5):
            chosen[key] = row
    OBSERVATIONS[:] = list(chosen.values())
    SOURCE_STATUS.append({"source": "station/observation deduplication", "status": "ok",
                          "duplicate_observation_rows_removed": duplicates,
                          "canonical_station_count": len(STATIONS)})


def write_outputs():
    latest_by_station = {}
    latest_by_source = {}
    for observation in OBSERVATIONS:
        station_id = observation["station_id"]
        observed_at = observation["observation_time"]
        latest_by_station[station_id] = max(latest_by_station.get(station_id, observed_at), observed_at)
        source = observation["source"]
        latest_by_source[source] = max(latest_by_source.get(source, observed_at), observed_at)
    for station_id, station in STATIONS.items():
        station["latest_observation_at"] = latest_by_station.get(station_id)
    for observation in OBSERVATIONS:
        observation["latest_observation_at"] = latest_by_station.get(observation["station_id"])
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
        "observation_count": len(OBSERVATIONS),
        "station_record_count": len(STATIONS),
        "latest_by_source": latest_by_source,
        "sources": SOURCE_STATUS, "data_file": str(data_path),
        "scope": "Official NWS BTV CWA polygon; LSRs excluded; NYSM observations not enabled",
    }
    (BASE / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    latest_dir = BASE / "latest"
    latest_dir.mkdir(parents=True, exist_ok=True)
    latest_data_path = latest_dir / "observations.ndjson.gz"
    with gzip.open(latest_data_path, "wt", encoding="utf-8") as handle:
        for item in sorted(OBSERVATIONS, key=lambda x: x["observation_time"]):
            handle.write(json.dumps(item, separators=(",", ":")) + "\n")
    (latest_dir / "stations.json").write_text(
        json.dumps(sorted(STATIONS.values(), key=lambda x: x["station_id"]), indent=2), encoding="utf-8")
    (latest_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    CWA_GEOMETRIES = load_btv_geometry()
    for adapter in (ingest_nws, ingest_iem_asos, ingest_synoptic, ingest_synoptic_nysm, ingest_coop_daily, ingest_cocorahs_daily, ingest_rr3_coop, ingest_rwis, ingest_nysm_inventory):
        try:
            adapter(CWA_GEOMETRIES)
        except Exception as exc:
            SOURCE_STATUS.append({"source": adapter.__name__, "status": "error", "detail": str(exc)[:300]})
    deduplicate_observations()
    write_outputs()
    if not OBSERVATIONS:
        raise SystemExit("No observations were ingested; see manifest source errors.")
