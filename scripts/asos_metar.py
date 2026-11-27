"""METAR/ASOS parsing and unit normalization for the BTV observation pipeline."""
from __future__ import annotations

import re
from datetime import datetime, timezone

KNOTS_TO_MPH = 1.150779448
WEATHER_CODES = {
    "RA": "rain", "DZ": "drizzle", "SN": "snow", "SG": "snow grains",
    "PL": "ice pellets", "IC": "ice crystals", "GR": "hail",
    "GS": "small hail/snow pellets", "UP": "unknown precipitation",
    "FZRA": "freezing rain", "FZDZ": "freezing drizzle",
    "FZFG": "freezing fog",
}


def knots_to_mph(value):
    """Convert knots to mph; preserve None and round display value to 1 decimal."""
    if value is None or value == "":
        return None
    return round(float(value) * KNOTS_TO_MPH, 1)


def _hundredths(group_value):
    if group_value == "////":
        return None
    return int(group_value) / 100.0


def parse_metar(raw: str, report_time: datetime | None = None) -> dict:
    """Extract wind, precipitation and ASOS icing remarks from a raw METAR/SPECI.

    Prrrr is hourly precipitation since the preceding routine METAR.
    6RRRR is 3h at 03/09/15/21Z and 6h at 00/06/12/18Z.
    7RRRR is 24h. All are hundredths of an inch.
    I1/I3/I6 groups are planar ice accretion in hundredths of an inch.
    """
    text = (raw or "").strip().upper()
    tokens = text.split()
    result = {
        "raw_metar": raw,
        "wind_direction_deg": None,
        "wind_speed_kt": None,
        "wind_speed_mph": None,
        "wind_gust_kt": None,
        "wind_gust_mph": None,
        "peak_wind_direction_deg": None,
        "peak_wind_speed_kt": None,
        "peak_wind_speed_mph": None,
        "precip_1h_in": None,
        "precip_3h_or_6h_in": None,
        "precip_3h_or_6h_period_hours": None,
        "precip_24h_in": None,
        "ice_accretion_1h_in": None,
        "ice_accretion_3h_in": None,
        "ice_accretion_6h_in": None,
        "ice_sensor_status": "not_reported",
        "present_weather_codes": [],
        "precipitation_types": [],
        "freezing_precipitation_reported": False,
    }

    for token in tokens:
        wind = re.fullmatch(r"(\d{3}|VRB)(\d{2,3})(?:G(\d{2,3}))?KT", token)
        if wind:
            direction, speed, gust = wind.groups()
            result["wind_direction_deg"] = None if direction == "VRB" else int(direction)
            result["wind_speed_kt"] = int(speed)
            result["wind_speed_mph"] = knots_to_mph(speed)
            result["wind_gust_kt"] = int(gust) if gust else None
            result["wind_gust_mph"] = knots_to_mph(gust)
            continue

        if token == "FZRANO":
            result["ice_sensor_status"] = "unavailable"
        icing = re.fullmatch(r"I([136])(\d{3}|///)", token)
        if icing:
            period, amount = icing.groups()
            field = f"ice_accretion_{period}h_in"
            result[field] = _hundredths(amount)
            result["ice_sensor_status"] = "missing" if amount == "///" else "reported"

        peak = re.fullmatch(r"PKWND(\d{3})(\d{2,3})/(\d{4})", token)
        if peak:
            direction, speed, _ = peak.groups()
            result["peak_wind_direction_deg"] = int(direction)
            result["peak_wind_speed_kt"] = int(speed)
            result["peak_wind_speed_mph"] = knots_to_mph(speed)

        if re.fullmatch(r"P(?:\d{4}|////)", token):
            result["precip_1h_in"] = _hundredths(token[1:])
        if re.fullmatch(r"6(?:\d{4}|////)", token):
            result["precip_3h_or_6h_in"] = _hundredths(token[1:])
            if report_time is not None:
                hour = report_time.astimezone(timezone.utc).hour
            else:
                # METAR observation time is HHMMZ; use it when supplied in the body.
                time_token = next((t for t in tokens if re.fullmatch(r"\d{6}Z", t)), None)
                hour = int(time_token[2:4]) if time_token else None
            result["precip_3h_or_6h_period_hours"] = (
                3 if hour in (3, 9, 15, 21) else
                6 if hour in (0, 6, 12, 18) else None
            )
        if re.fullmatch(r"7(?:\d{4}|////)", token):
            result["precip_24h_in"] = _hundredths(token[1:])

    # Weather groups are normally in the body, before RMK.
    body = tokens[:tokens.index("RMK")] if "RMK" in tokens else tokens
    for token in body:
        cleaned = token.lstrip("-+")
        codes = re.findall(r"FZRA|FZDZ|FZFG|RA|DZ|SN|SG|PL|IC|GR|GS|UP", cleaned)
        for code in codes:
            if code not in result["present_weather_codes"]:
                result["present_weather_codes"].append(code)
            label = WEATHER_CODES[code]
            if label not in result["precipitation_types"]:
                result["precipitation_types"].append(label)
    result["freezing_precipitation_reported"] = any(
        code in result["present_weather_codes"] for code in ("FZRA", "FZDZ")
    )
    return result


def normalize_iem_wind(record: dict) -> dict:
    """Add mph fields to an IEM ASOS record while retaining source knots."""
    out = dict(record)
    for source, target in (("sknt", "wind_mph"), ("gust", "gust_mph"),
                           ("peak_wind_gust", "peak_wind_gust_mph")):
        value = out.get(source)
        try:
            out[target] = knots_to_mph(value) if value not in (None, "", "M") else None
        except (TypeError, ValueError):
            out[target] = None
    return out
