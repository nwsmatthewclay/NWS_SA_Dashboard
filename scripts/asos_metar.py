"""METAR/ASOS parsing and unit normalization for the BTV observation pipeline."""
from __future__ import annotations
import re
from datetime import datetime, timedelta, timezone

KNOTS_TO_MPH = 1.150779448
WEATHER_CODES = {"FZRA":"freezing rain","FZDZ":"freezing drizzle","FZFG":"freezing fog","RA":"rain","DZ":"drizzle","SN":"snow","SG":"snow grains","PL":"ice pellets","IC":"ice crystals","GR":"hail","GS":"small hail/snow pellets","UP":"unknown precipitation"}

def wind_speed_to_mph(value, unit: str | None) -> float | None:
    """Normalize a speed to mph from an explicit source unit."""
    if value in (None, "", "M"):
        return None
    text = str(unit or "").strip().lower().replace("wmounit:", "").replace(" ", "")
    factors = {
        "mph": 1.0, "mi/h": 1.0, "mile/hour": 1.0, "miles/hour": 1.0,
        "kt": KNOTS_TO_MPH, "knot": KNOTS_TO_MPH, "knots": KNOTS_TO_MPH,
        "m/s": 2.2369362921, "ms-1": 2.2369362921, "m.s-1": 2.2369362921, "m_s-1": 2.2369362921,
        "km/h": 0.6213711922, "kmh-1": 0.6213711922, "km_h-1": 0.6213711922,
        "ft/s": 0.6818181818, "fts-1": 0.6818181818,
    }
    if text not in factors:
        raise ValueError(f"Unsupported wind-speed unit: {unit!r}")
    return round(float(value) * factors[text], 1)

def knots_to_mph(value):
    if value is None or value == "":
        return None
    return round(float(value) * KNOTS_TO_MPH, 1)

def _hundredths(value):
    return None if "/" in value else int(value) / 100.0

def is_metar_report(raw: str) -> bool:
    """True only for routine METAR reports, excluding SPECI and other report types."""
    tokens = (raw or "").strip().upper().split()
    return bool(tokens and tokens[0] == "METAR")


def is_speci_report(raw: str) -> bool:
    """Return True when an IEM ASOS report is explicitly identified as SPECI."""
    tokens = (raw or "").strip().upper().split()
    return bool(tokens and tokens[0] == "SPECI")


def metar_report_datetime(raw: str, fallback: datetime | None = None) -> datetime | None:
    """Decode the METAR DDHHMMZ issue time, resolving its date near fallback."""
    tokens = (raw or "").strip().upper().split()
    token = next((t for t in tokens if re.fullmatch(r"\d{6}Z", t)), None)
    if not token:
        return fallback.astimezone(timezone.utc) if fallback else None
    if fallback is None:
        return None
    day, hour, minute = int(token[:2]), int(token[2:4]), int(token[4:6])
    base = fallback.astimezone(timezone.utc)
    candidates = []
    for offset in (-1, 0, 1):
        month_anchor = (base.replace(day=1) + timedelta(days=offset * 28))
        try:
            candidate = month_anchor.replace(day=day, hour=hour, minute=minute, second=0, microsecond=0)
            candidates.append(candidate)
        except ValueError:
            continue
    return min(candidates, key=lambda d: abs((d - base).total_seconds())) if candidates else None


def is_routine_hourly_metar(raw: str, report_time: datetime | None, tolerance_minutes: int = 5) -> bool:
    """Accept routine METARs whose encoded issue time is near :53 past the hour."""
    tokens = (raw or "").strip().upper().split()
    if not tokens or tokens[0] != "METAR" or report_time is None:
        return False
    issued = metar_report_datetime(raw, report_time)
    if issued is None:
        return False
    distance = abs(issued.minute - 53)
    return distance <= tolerance_minutes


def parse_metar(raw: str, report_time: datetime | None = None) -> dict:
    """Decode standard U.S. METAR wind, precip and ASOS ice-accretion remarks."""
    text = (raw or "").strip().upper()
    tokens = text.split()
    result = {"raw_metar":raw,"wind_direction_deg":None,"wind_speed_kt":None,"wind_speed_mph":None,"wind_gust_kt":None,"wind_gust_mph":None,"peak_wind_direction_deg":None,"peak_wind_speed_kt":None,"peak_wind_speed_mph":None,"precip_1h_in":None,"precip_3h_or_6h_in":None,"precip_3h_or_6h_period_hours":None,"precip_24h_in":None,"ice_accretion_1h_in":None,"ice_accretion_3h_in":None,"ice_accretion_6h_in":None,"ice_sensor_status":"not_reported","present_weather_codes":[],"precipitation_types":[],"freezing_precipitation_reported":False}
    time_token = next((t for t in tokens if re.fullmatch(r"\d{6}Z",t)),None)
    hour = report_time.astimezone(timezone.utc).hour if report_time else (int(time_token[2:4]) if time_token else None)
    for token in tokens:
        wind = re.fullmatch(r"(\d{3}|VRB)(\d{2,3})(?:G(\d{2,3}))?KT",token)
        if wind:
            direction,speed,gust=wind.groups()
            result.update(wind_direction_deg=None if direction=="VRB" else int(direction),wind_speed_kt=int(speed),wind_speed_mph=knots_to_mph(speed),wind_gust_kt=int(gust) if gust else None,wind_gust_mph=knots_to_mph(gust))
        if token == "FZRANO":
            result["ice_sensor_status"]="unavailable"
        icing=re.fullmatch(r"I([136])(\d{3}|///)",token)
        if icing:
            period,amount=icing.groups()
            result[f"ice_accretion_{period}h_in"]=_hundredths(amount)
            result["ice_sensor_status"]="missing" if amount=="///" else "reported"
        if re.fullmatch(r"P(?:\d{4}|////)",token):
            result["precip_1h_in"]=_hundredths(token[1:])
        if re.fullmatch(r"6(?:\d{4}|////)",token):
            result["precip_3h_or_6h_in"]=_hundredths(token[1:])
            result["precip_3h_or_6h_period_hours"]=3 if hour in (3,9,15,21) else 6 if hour in (0,6,12,18) else None
        if re.fullmatch(r"7(?:\d{4}|////)",token):
            result["precip_24h_in"]=_hundredths(token[1:])
    peak=re.search(r"PK\s*WND\s+(\d{3})(\d{2,3})/(\d{4})",text)
    if peak:
        direction,speed,_=peak.groups()
        result.update(peak_wind_direction_deg=int(direction),peak_wind_speed_kt=int(speed),peak_wind_speed_mph=knots_to_mph(speed))
    body=tokens[:tokens.index("RMK")] if "RMK" in tokens else tokens
    for token in body:
        for code in re.findall(r"FZRA|FZDZ|FZFG|RA|DZ|SN|SG|PL|IC|GR|GS|UP",token.lstrip("-+")):
            if code not in result["present_weather_codes"]:
                result["present_weather_codes"].append(code)
                result["precipitation_types"].append(WEATHER_CODES[code])
    result["freezing_precipitation_reported"]=any(c in result["present_weather_codes"] for c in ("FZRA","FZDZ"))
    return result

def normalize_iem_wind(record: dict) -> dict:
    """Add mph values while retaining original knot-valued fields."""
    out=dict(record)
    for source,target in (("sknt","wind_mph"),("gust","gust_mph"),("peak_wind_gust","peak_wind_gust_mph")):
        value=out.get(source)
        try:
            out[target]=wind_speed_to_mph(value, "knot") if value not in (None,"","M") else None
        except (TypeError,ValueError):
            out[target]=None
    return out
