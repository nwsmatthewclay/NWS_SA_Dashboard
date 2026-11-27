#!/usr/bin/env python3
"""Connectivity-only probe for the BTV IRIS replacement data sources.

Never prints API tokens or full request URLs. Does not write observations
into the repository; it produces a small sanitized JSON report artifact.
"""
import csv
import io
import json
import os
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "NWS-BTV-IRIS-Observation-Source-Probe/1.0",
    "Accept": "application/json, text/csv, text/plain, */*",
})
RESULTS = {
    "generated_utc": datetime.now(timezone.utc).isoformat(),
    "scope": "source connectivity only; station inclusion must be spatially filtered to the official BTV CWA polygon",
    "sources": [],
}


def add(name, status, **details):
    row = {"source": name, "status": status}
    row.update(details)
    RESULTS["sources"].append(row)
    print(json.dumps(row, sort_keys=True))


def get(name, url, params=None, headers=None, timeout=35):
    try:
        r = SESSION.get(url, params=params, headers=headers, timeout=timeout)
        return r
    except requests.RequestException as exc:
        add(name, "REQUEST_ERROR", error=type(exc).__name__, detail=str(exc)[:240])
        return None


def json_payload(name, response):
    if response is None:
        return None
    try:
        payload = response.json()
    except ValueError:
        add(name, "NON_JSON", http_status=response.status_code,
            content_type=response.headers.get("Content-Type", ""),
            body_preview=response.text[:160])
        return None
    if not response.ok:
        add(name, "HTTP_ERROR", http_status=response.status_code,
            api_message=str(payload.get("message", payload.get("title", "")))[:200])
        return None
    return payload


# NWS API: a live station observation.
nws_name = "NWS API"
nws = get(nws_name, "https://api.weather.gov/stations/KBTV/observations/latest")
if nws is not None:
    p = json_payload(nws_name, nws)
    if p is not None:
        props = p.get("properties", {})
        add(nws_name, "PASS", http_status=nws.status_code,
            station="KBTV", timestamp=props.get("timestamp"),
            temperature=props.get("temperature", {}).get("value"),
            wind_speed_source_m_s=props.get("windSpeed", {}).get("value"),
            wind_speed_mph=round(props.get("windSpeed", {}).get("value") * 2.236936, 1)
                if isinstance(props.get("windSpeed", {}).get("value"), (int, float)) else None,
            wind_gust_source_m_s=props.get("windGust", {}).get("value"),
            wind_gust_mph=round(props.get("windGust", {}).get("value") * 2.236936, 1)
                if isinstance(props.get("windGust", {}).get("value"), (int, float)) else None)


# IEM ASOS/AWOS archive: five-day station-selected sample from airports in the BTV CWA.
# IEM's processed archive is synchronized approximately every ten minutes.
from scripts.asos_metar import normalize_iem_wind, parse_metar

asos_name = "IEM ASOS/AWOS"
asos_stations = "BTV,PBG,MSS,SLK,MPV,EFK,RUT"
asos = get(asos_name, "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py", {
    "station": asos_stations,
    "data": "all",
    "sts": (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%dT%H:%MZ"),
    "ets": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
    "tz": "Etc/UTC",
    "format": "onlycomma",
    "latlon": "yes",
    "elev": "no",
    "missing": "empty",
    "trace": "T",
})
if asos is not None:
    if asos.ok and asos.text.strip():
        rows = [normalize_iem_wind(row) for row in csv.DictReader(io.StringIO(asos.text))]
        rows.sort(key=lambda row: row.get("valid", ""))
        latest = rows[-1] if rows else {}
        latest_metar = parse_metar(latest.get("metar", "")) if latest else {}
        add(asos_name, "PASS" if rows else "PASS_EMPTY_PERIOD",
            http_status=asos.status_code,
            requested_stations=asos_stations.split(","),
            report_rows=len(rows),
            station_count=len({row.get("station") for row in rows if row.get("station")}),
            latest_report_time=latest.get("valid"),
            latest_station=latest.get("station"),
            latest_wind_mph=latest.get("wind_mph"),
            latest_gust_mph=latest.get("gust_mph"),
            latest_peak_gust_mph=latest.get("peak_wind_gust_mph"),
            latest_ice_accretion_1h_in=latest.get("ice_accretion_1hr"),
            latest_ice_accretion_3h_in=latest.get("ice_accretion_3hr"),
            latest_ice_accretion_6h_in=latest.get("ice_accretion_6hr"),
            latest_metar_ice_sensor_status=latest_metar.get("ice_sensor_status"),
            latest_metar_precip_1h_in=latest_metar.get("precip_1h_in"),
            latest_metar_precip_3h_or_6h_in=latest_metar.get("precip_3h_or_6h_in"),
            latest_metar_precip_3h_or_6h_period_hours=latest_metar.get("precip_3h_or_6h_period_hours"),
            latest_metar_precip_24h_in=latest_metar.get("precip_24h_in"),
            latest_present_weather=latest.get("wxcodes"),
            latest_hourly_precip_in=latest.get("p01i"),
            note="Wind and gusts normalized to mph; source knots retained. IEM archive data may lag real time.")
    else:
        add(asos_name, "HTTP_ERROR", http_status=asos.status_code,
            body_preview=asos.text[:160])


# Synoptic: station inventory, latest observations, and all requested precip windows.
synoptic_token = os.getenv("SYNOPTIC_API_KEY", "").strip()
if not synoptic_token:
    add("Synoptic", "SKIPPED", reason="SYNOPTIC_API_KEY Actions secret is not available to this workflow.")
else:
    base = "https://api.synopticdata.com/v2/stations"

    def synoptic_call(endpoint, params):
        q = dict(params)
        q["token"] = synoptic_token
        r = get("Synoptic " + endpoint, f"{base}/{endpoint}", q)
        if r is None:
            return None
        p = json_payload("Synoptic " + endpoint, r)
        if p is None:
            return None
        summary = p.get("SUMMARY", {})
        if str(summary.get("RESPONSE_CODE")) != "1":
            add("Synoptic " + endpoint, "API_ERROR",
                http_status=r.status_code,
                response_code=summary.get("RESPONSE_CODE"),
                response_message=summary.get("RESPONSE_MESSAGE"))
            return None
        return p

    meta = synoptic_call("metadata", {"cwa": "BTV", "complete": 1})
    if meta is not None:
        stations = meta.get("STATION", []) or []
        networks = {}
        cwop = []
        for st in stations:
            network = str(st.get("MNET_NAME") or st.get("MNET_ID") or "Unknown")
            networks[network] = networks.get(network, 0) + 1
            searchable = " ".join(str(v) for v in st.values()).upper()
            if "CWOP" in searchable or "APRS" in searchable:
                cwop.append(st.get("STID"))
        add("Synoptic station inventory", "PASS",
            station_count=len(stations),
            network_counts=networks,
            cwop_or_aprs_station_count=len(cwop),
            cwop_or_aprs_sample=cwop[:12])

    latest = synoptic_call("latest", {
        "cwa": "BTV",
        "within": 180,
        "vars": "air_temp,wind_speed,wind_gust,dew_point_temperature,relative_humidity",
        "units": "english",
        "obtimezone": "utc",
    })
    if latest is not None:
        stations = latest.get("STATION", []) or []
        add("Synoptic latest observations", "PASS",
            station_count=len(stations),
            sample_station_ids=[s.get("STID") for s in stations[:12]])

    precip = synoptic_call("precip", {
        "cwa": "BTV",
        "pmode": "last",
        "accum_hours": "1,3,6,12,24,48,72,120",
        "units": "english",
        "obtimezone": "utc",
        "complete": 1,
    })
    if precip is not None:
        stations = precip.get("STATION", []) or []
        add("Synoptic precipitation windows", "PASS",
            station_count=len(stations),
            requested_windows_hours=[1, 3, 6, 12, 24, 48, 72, 120],
            sample_station_ids=[s.get("STID") for s in stations[:12]])


# IEM COOP raw observations (daily observations; not sub-daily rolling data).
now = datetime.now(timezone.utc)
start_day = (now - timedelta(days=5)).date().isoformat()
end_day = now.date().isoformat()
for state in ("VT", "NY"):
    name = f"IEM COOP {state}"
    r = get(name, "https://mesonet.agron.iastate.edu/cgi-bin/request/coopobs.py", {
        "network": f"{state}_COOP",
        "stations": "_ALL",
        "sts": start_day,
        "ets": end_day,
        "what": "view",
        "delim": "comma",
    })
    if r is not None:
        if r.ok and r.text.strip():
            rows = list(csv.DictReader(io.StringIO(r.text)))
            add(name, "PASS" if rows else "PASS_EMPTY_PERIOD",
                http_status=r.status_code, report_rows=len(rows),
                columns=list(rows[0].keys()) if rows else [],
                note="Daily observer reports; timestamps represent the preceding observation period.")
        else:
            add(name, "HTTP_ERROR", http_status=r.status_code,
                body_preview=r.text[:160])


# RR3BTV: near-real-time COOP product text from the IEM AFOS archive.
rr3_name = "IEM RR3BTV"
rr3 = get(rr3_name, "https://mesonet.agron.iastate.edu/cgi-bin/afos/retrieve.py", {
    "pil": "RR3BTV",
    "fmt": "text",
    "limit": 100,
    "sdate": (now - timedelta(days=5)).strftime("%Y-%m-%dT%H:%MZ"),
    "edate": now.strftime("%Y-%m-%dT%H:%MZ"),
})
if rr3 is not None:
    has_product = rr3.ok and ("RR3BTV" in rr3.text or ".AR" in rr3.text)
    add(rr3_name, "PASS" if has_product else ("PASS_NO_PRODUCTS_IN_WINDOW" if rr3.ok else "HTTP_ERROR"),
        http_status=rr3.status_code,
        product_text_bytes=len(rr3.content),
        cooperative_report_lines=sum(1 for line in rr3.text.splitlines() if ".AR" in line),
        note="RR3 contains COOP/supplemental observer reports; parse each observation's actual valid period.")


# CoCoRaHS bulk daily CSV export. State-wide retrieval is only a connectivity
# test; the production ingestor must spatially clip stations to the BTV CWA.
for state in ("VT", "NY"):
    name = f"CoCoRaHS daily export {state}"
    r = get(name, "https://data.cocorahs.org/cocorahs/export/exportreports.aspx", {
        "ReportType": "Daily",
        "Format": "CSV",
        "State": state,
        "StartDate": (now - timedelta(days=5)).strftime("%m/%d/%Y"),
        "EndDate": now.strftime("%m/%d/%Y"),
        "TimesInGMT": "True",
    })
    if r is not None:
        if r.ok and r.text.strip():
            rows = list(csv.reader(io.StringIO(r.text)))
            add(name, "PASS" if len(rows) > 1 else "PASS_EMPTY_PERIOD",
                http_status=r.status_code, csv_rows=max(0, len(rows) - 1),
                header=rows[0][:18] if rows else [],
                note="Daily reports; preserve observer report date/time and accumulation period.")
        else:
            add(name, "HTTP_ERROR", http_status=r.status_code,
                body_preview=r.text[:160])


# NYSM public station metadata is discoverable. Observation access and any
# redistribution require a data agreement; this probe deliberately does not
# download observations until authorization is confirmed.
nysm_name = "NYS Mesonet station inventory"
nysm = get(nysm_name,
    "https://gisservices.its.ny.gov/arcgis/rest/services/NYS_Mesonet/MapServer/0/query",
    {"where": "1=1", "outFields": "station_na,lon,lat,code,Elev",
     "returnGeometry": "false", "f": "json"})
if nysm is not None:
    p = json_payload(nysm_name, nysm)
    if p is not None:
        features = p.get("features", []) or []
        add(nysm_name, "PASS", http_status=nysm.status_code,
            station_count=len(features),
            sample=[f.get("attributes", {}) for f in features[:5]],
            observation_access="PENDING_DATA_POLICY_APPROVAL",
            note="Public station inventory only; no NYSM observations requested.")


# Write a sanitized machine-readable report for the Actions artifact.
with open("observation_source_probe.json", "w", encoding="utf-8") as f:
    json.dump(RESULTS, f, indent=2)
print("\nProbe complete. No credentials or raw observation payloads were written.")
