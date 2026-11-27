"""METAR/ASOS parsing and unit normalization for the BTV observation pipeline."""
from __future__ import annotations
import re
from datetime import datetime, timezone

KNOTS_TO_MPH = 1.150779448
WEATHER_CODES = {"FZRA":"freezing rain","FZDZ":"freezing drizzle","FZFG":"freezing fog","RA":"rain","DZ":"drizzle","SN":"snow","SG":"snow grains","PL":"ice pellets","IC":"ice crystals","GR":"hail","GS":"small hail/snow pellets","UP":"unknown precipitation"}

def knots_to_mph(value):
    if value is None or value == "":
        return None
    return round(float(value) * KNOTS_TO_MPH, 1)

def _hundredths(value):
    return None if "/" in value else int(value) / 100.0

def is_speci_report(raw: str) -> bool:
    """Return True when an IEM ASOS report is explicitly identified as SPECI."""
    tokens = (raw or "").strip().upper().split()
    return bool(tokens and tokens[0] == "SPECI")


def is_routine_hourly_metar(raw: str, report_time: datetime | None, tolerance_minutes: int = 3) -> bool:
    """Accept routine METARs near :53 past the hour for hourly precipitation."""
    tokens = (raw or "").strip().upper().split()
    if not tokens or tokens[0] != "METAR" or report_time is None:
        return False
    minute = report_time.astimezone(timezone.utc).minute
    distance = min((minute - 53) % 60, (53 - minute) % 60)
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
            out[target]=knots_to_mph(value) if value not in (None,"","M") else None
        except (TypeError,ValueError):
            out[target]=None
    return out
