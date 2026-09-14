"""
Mount Mansfield Observed Slope Profile - Proof of Concept
----------------------------------------------------------

Builds a temperature + wind pseudo-sounding from surface observations
between Burlington and the Mount Mansfield summit.

Data sources
------------
NWS API:
    KBTV
    D0383
    E6664
    A3150
    UVM05
    UVM06

MMNV1:
    Temperature -> NWS BTV RR2
    Wind        -> IEM archived RRSBTV SHEF product

The profile:
    1. Retrieves the latest usable temperature/wind observations.
    2. Anchors pressure to observed KBTV station pressure when available.
    3. Uses the hypsometric equation to estimate pressure at each elevation.
    4. Plots temperature and wind on a MetPy Skew-T.
    5. Derives a "Winter Profile Diagnostics" panel (freezing level,
       wet-bulb zero, lapse rate, inversions, a KBTV-to-MMNV1 bulk
       shear, and a bulk Froude number / flow regime for the ridge).

V1 intentionally does NOT calculate:
    LCL
    full moist-adiabatic parcel ascent
    precipitation type

Requirements:
    pip install requests numpy matplotlib metpy google-api-python-client google-auth

Workflow note:
    This script writes its PNGs into outputs/. For the dashboard's
    <img> tags to load them reliably, the workflow needs "permissions:
    contents: write" and a commit/push step after this script runs -
    see the accompanying yml diff. Do not rely on the Drive upload
    below for the public-facing dashboard embed; Drive enforces an
    anonymous per-file download quota that makes direct hotlinking
    from a public webpage unreliable.
"""

import os
import re
import math
from datetime import datetime, timedelta, timezone

import numpy as np
import requests
import matplotlib

# GitHub Actions is headless.
matplotlib.use("Agg")

import matplotlib.pyplot as plt
from matplotlib.patches import Circle, Polygon, FancyBboxPatch
from matplotlib.ticker import FixedLocator, FixedFormatter, NullLocator, NullFormatter

import metpy.calc as mpcalc
from metpy.units import units
from metpy.plots import SkewT

import json
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload


# =====================================================================
# 1. CONFIGURATION
# =====================================================================

STATIONS = {
    "KBTV": 330,
    "D0383": 781,
    "E6664": 958,
    "A3150": 1293,
    "UVM05": 1309,
    "MMSV1": 2236,
    "UVM06": 2877,
    "MMNV1": 3891,
}

NWS_STATIONS = [
    "KBTV",
    "D0383",
    "E6664",
    "A3150",
    "UVM05",
    "MMSV1",
    "UVM06",
]

API_BASE = "https://api.weather.gov"

RR2_URL = (
    "https://forecast.weather.gov/product.php"
    "?site=NWS&product=RR2&issuedby=BTV"
)

RRS_URL = (
    "https://mesonet.agron.iastate.edu/wx/afos/p.php"
    "?pil=RRSBTV&e={timestamp}"
)

HEADERS = {
    "User-Agent": (
        "MountMansfieldPseudoSounding/1.1 "
        "(weather research/visualization)"
    ),
    "Accept": "application/geo+json",
}

TEXT_HEADERS = {
    "User-Agent": (
        "MountMansfieldPseudoSounding/1.1 "
        "(weather research/visualization)"
    ),
}

# Number of recent NWS observations to inspect.
NWS_OB_LIMIT = 20

# Number of hourly RRS products to search backward.
RRS_LOOKBACK_HOURS = 6

REPO_OUTPUT_DIR = "outputs"

OUTPUT_FILE = os.path.join(REPO_OUTPUT_DIR, "vt_pseudo_sounding.png")
CARD_OUTPUT_FILE = os.path.join(REPO_OUTPUT_DIR, "vt_dashboard_card.png")
TABLE_OUTPUT_FILE = os.path.join(REPO_OUTPUT_DIR, "vt_station_table.png")
DIAGNOSTICS_OUTPUT_FILE = os.path.join(REPO_OUTPUT_DIR, "vt_diagnostics.png")
DIAGNOSTICS_STATUS_FILE = os.path.join(REPO_OUTPUT_DIR, "vt_diagnostics_status.json")

# classify_precip_type()'s raw labels, mapped to the simpler wording
# used on the dashboard card (rain/snow/sleet/freezing rain).
PRECIP_TYPE_DISPLAY = {
    "All Rain": "Rain",
    "All Snow": "Snow",
    "Freezing Rain": "Freezing Rain",
    "Ice Pellets": "Sleet",
    "Rain / Wintry Mix": "Wintry Mix",
    "Rain": "Rain",
}

# Google Drive destination for the rendered PNG. Reuses the same
# service account as main.py (GOOGLE_CREDENTIALS). Falls back to
# GLWU_DRIVE_FOLDER_ID if a dedicated MANSFIELD_DRIVE_FOLDER_ID isn't
# set, so this works whether you want it in the same Drive folder as
# main.py's images or a separate one.

DRIVE_FOLDER_ID = (
    os.environ.get("MANSFIELD_DRIVE_FOLDER_ID")
    or os.environ.get("GLWU_DRIVE_FOLDER_ID")
)

DRIVE_UPLOAD_FILENAME = "vt_pseudo_sounding.png"

# Physical constants used by the diagnostics module below.
RD = 287.05      # J/(kg*K), dry air gas constant
G = 9.80665       # m/s^2
CP = 1004.0       # J/(kg*K), dry air specific heat at constant pressure

# Stations used for the low-level bulk shear diagnostic. KBTV (330 ft)
# to MMNV1 (3891 ft) spans about 3560 ft (~1.1 km), i.e. roughly the
# 0-1 km AGL layer, and both are always part of the fixed station set.
SHEAR_BASE_STID = "KBTV"
SHEAR_TOP_STID = "MMNV1"

# Bulk Froude number thresholds used to label flow regime around the
# ridge. Fr >= 1 flow tends to go over the barrier; Fr < ~0.5 flow
# tends to be blocked and diverts around/backs up upstream; the band
# in between is a partially-blocked regime. These are conventional
# rule-of-thumb cutoffs from mountain-flow literature, not a fitted
# threshold for Mansfield specifically.
FROUDE_UNBLOCKED = 1.0
FROUDE_BLOCKED = 0.5

# Approximate compass bearing (degrees) of the Mansfield ridge line
# itself - the summit ridge trends SSW-NNE, roughly 020-030 deg
# azimuth along the spine of the Green Mountains. Used to resolve the
# cross-barrier (ridge-normal) wind component for the Froude number,
# since along-ridge flow doesn't force air over or around the
# barrier the way cross-ridge flow does.
RIDGE_ORIENTATION_DEG = 25.0


# =====================================================================
# 2. GENERAL HELPERS
# =====================================================================

def qv_value(properties, name):
    """
    Extract numeric value from an api.weather.gov QuantitativeValue.
    """

    item = properties.get(name)

    if not isinstance(item, dict):
        return None

    return item.get("value")


def parse_iso_time(value):
    """
    Convert ISO timestamp to datetime.
    """

    if not value:
        return None

    try:
        return datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )
    except ValueError:
        return None


def observation_age_minutes(timestamp):
    """
    Return observation age in minutes.
    """

    dt = parse_iso_time(timestamp)

    if dt is None:
        return None

    now = datetime.now(timezone.utc)

    return (now - dt).total_seconds() / 60.0


# Any single field (temperature, dewpoint, or wind) older than this
# is treated as unusable - a station's WIND reading being 200+
# minutes stale while its TEMPERATURE is fresh (or vice versa) is a
# real thing that happens with these feeds, so this is checked per
# field in build_profile(), not once per station.
MAX_OBSERVATION_AGE_MINUTES = 100


def is_stale(timestamp, max_age_minutes=MAX_OBSERVATION_AGE_MINUTES):
    """
    True if `timestamp` is older than `max_age_minutes`, using the
    same age calculation as observation_age_minutes(). A missing
    timestamp is NOT itself treated as stale - a value with no
    timestamp attached is a separate "we don't know how old this is"
    situation, handled wherever that value gets used, not here.
    """

    age = observation_age_minutes(timestamp)

    if age is None:
        return False

    return age > max_age_minutes


def fmt(value, decimals=1):
    """
    Safe formatting helper.
    """

    if value is None:
        return "MISSING"

    return f"{value:.{decimals}f}"


def ft_to_m(feet):
    """
    Feet -> meters.
    """

    return feet * 0.3048


def choose_tick_interval(axis_range, target_ticks=8, candidates=(0.5, 1, 2, 2.5, 5, 10, 20)):
    """
    Pick the smallest interval from `candidates` that keeps the
    number of gridlines across `axis_range` at or below
    `target_ticks`. Used so the temperature axis gets a sensible
    number of gridlines whether the visible range is 8 C (a tightly
    zoomed layer) or 40 C, instead of MetPy's fixed 10 C default
    locator, which leaves a very narrow zoomed range with only one
    or two ticks.
    """

    for interval in candidates:

        if axis_range / interval <= target_ticks:
            return interval

    return candidates[-1]


def measure_skewt_height_to_width_ratio(
    bottom_pressure, top_pressure, left_temperature, right_temperature,
    probe_size_in=14.0,
):
    """
    Determine the height:width ratio MetPy's SkewT will actually
    render for the given pressure/temperature limits, independent of
    whatever box it's eventually placed in.

    SkewT locks its own axes aspect internally
    (ax.set_aspect(80.5, adjustable='box')) so the 45-degree skew
    lines render at a geometrically true 45 degrees on screen. That
    lock is NOT optional cosmetic behavior - it's part of what makes
    a Skew-T diagram a Skew-T (disabling it was tried directly and
    corrupted the coordinate math compute_skew_corrected_xlim relies
    on, since that function assumes an affine, consistent transform
    between its probe and the final render).

    Because the lock is tied to the data ranges (not the box we hand
    it), the box has to be SHAPED to match what the lock wants, not
    the other way around - guessing skew_width_in/skew_height_in
    independently just produces a box the axes doesn't fill, with
    the gap rendering as blank margin. For a shallow near-surface
    pressure slice like this one (a small fraction of a full
    troposphere), that natural shape is short and wide, not tall -
    there's no honest way around that without either showing a much
    larger pressure range or breaking the 45-degree geometry.

    Probes with a deliberately oversized, square box (14in) so
    neither dimension is the binding constraint, and reads back the
    axes' actual rendered pixel box to recover the aspect the lock
    wants for this pressure/temperature range. skew_width_in and
    skew_height_in in plot_skewt() are then DERIVED from this
    measurement - one fixed (width, since that's the dimension that
    matters for the rest of the dashboard layout), one computed -
    rather than picked by guesswork or clamped toward a guessed
    range, either of which reintroduces the same mismatch this
    probing exists to eliminate.
    """

    probe_fig = plt.figure(figsize=(probe_size_in, probe_size_in))

    probe_skew = SkewT(
        probe_fig, rotation=45, rect=(0.02, 0.02, 0.96, 0.96)
    )

    probe_skew.ax.set_ylim(bottom_pressure, top_pressure)
    probe_skew.ax.set_xlim(left_temperature, right_temperature)

    probe_fig.canvas.draw()

    bbox = probe_skew.ax.get_window_extent()

    ratio = bbox.height / bbox.width

    plt.close(probe_fig)

    return ratio


def compute_skew_corrected_xlim(
    bottom_pressure, top_pressure, points,
    box_width_in=10.0, box_height_in=10.0,
    min_width=8.0, pad=2.0,
):
    """
    Determine temperature-axis limits that keep every (temperature,
    pressure) point in `points` visible, accounting for MetPy's
    45-degree skew transform - not just each point's raw temperature.

    The skew shifts a point's rendered screen position to the right
    as pressure decreases (higher elevation), by an amount that
    depends on how much pressure range the chart spans. Sizing xlim
    from raw min/max temperature alone (as if the axes were
    unskewed) under-accounts for that shift once a chart shows more
    than a small pressure range - a point can have an unremarkable
    raw temperature yet still render very close to, or past, the
    edge of the visible box because of its altitude. This showed up
    directly: widening the pressure range to make the chart read
    more square (see the padding notes above) also increased how far
    the top of the profile shifts right, and pushed it toward the
    edge of the frame.

    box_width_in/box_height_in matter here: MetPy's skew-locked axes
    doesn't necessarily fill an arbitrary given rect - when the rect's
    aspect doesn't match the data's natural locked aspect, the actual
    rendered geometry differs from what a same-shaped-square probe
    would show. Since the actual output figure is now a FIXED size
    (see plot_skewt's sizing notes) rather than one shaped to match
    the data, the probe here has to use that same fixed box shape or
    the correction can be wrong for exactly the days whose natural
    aspect doesn't match it - confirmed directly: switching to a
    fixed box without this fix let 2 of 4 test scenarios clip.

    IMPORTANT: this probe deliberately does NOT touch the axes'
    aspect lock (no set_aspect override). The lock has to stay
    identical between this probe and the real chart in plot_skewt(),
    or the shift amounts measured here won't match what actually
    happens at render time - confirmed directly: disabling the lock
    here while the box shape was tuned for the LOCKED aspect produced
    wildly wrong (garbage) corrected limits.

    Approach: render the given points against a deliberately
    oversized, arbitrary xlim so nothing clips, measure where they
    actually land in axes-fraction terms, and back-convert that into
    the equivalent "effective x position" each point would have
    (skew shift included). Because the skew transform is affine, that
    effective position doesn't depend on which provisional xlim was
    used to measure it - only the final chosen xlim, which is set
    from the resulting min/max plus padding.
    """

    if not points:
        return -4.0, 4.0

    raw_temps = [p[0] for p in points]
    raw_span = max(max(raw_temps) - min(raw_temps), 10.0)

    provisional_half_width = raw_span * 3 + 40.0
    provisional_left = min(raw_temps) - provisional_half_width
    provisional_right = max(raw_temps) + provisional_half_width

    probe_fig = plt.figure(figsize=(box_width_in, box_height_in))

    probe_skew = SkewT(
        probe_fig, rotation=45, rect=(0.02, 0.02, 0.96, 0.96)
    )

    probe_skew.ax.set_ylim(bottom_pressure, top_pressure)
    probe_skew.ax.set_xlim(provisional_left, provisional_right)

    probe_fig.canvas.draw()

    bbox = probe_skew.ax.get_window_extent()

    shifted_x_values = []

    for temp_value, pressure_value in points:

        px, _py = probe_skew.ax.transData.transform((temp_value, pressure_value))
        frac = (px - bbox.x0) / (bbox.x1 - bbox.x0)

        shifted_x = provisional_left + frac * (provisional_right - provisional_left)
        shifted_x_values.append(shifted_x)

    plt.close(probe_fig)

    final_left = min(shifted_x_values) - pad
    final_right = max(shifted_x_values) + pad

    if final_right - final_left < min_width:

        midpoint = (final_left + final_right) / 2.0
        final_left = midpoint - min_width / 2.0
        final_right = midpoint + min_width / 2.0

    return final_left, final_right


# =====================================================================
# 3. NWS API OBSERVATIONS
# =====================================================================

def fetch_nws_recent(stid):
    """
    Retrieve recent observations for one NWS/MADIS station.

    Instead of /latest, inspect several observations so an incomplete
    newest record does not hide a valid temperature/wind observation.
    """

    url = f"{API_BASE}/stations/{stid}/observations"

    params = {
        "limit": NWS_OB_LIMIT,
    }

    try:

        response = requests.get(
            url,
            headers=HEADERS,
            params=params,
            timeout=30,
        )

        response.raise_for_status()

        return response.json().get("features", [])

    except requests.RequestException as exc:

        print(
            f"ERROR retrieving NWS observations for {stid}: {exc}"
        )

        return []

def extract_latest_nws_values(stid):
    """
    Search recent NWS observations for the newest usable values.

    Temperature, dewpoint, wind, and pressure do NOT have to come
    from the same observation record.
    """

    features = fetch_nws_recent(stid)

    if not features:
        return None

    temperature = None
    temperature_time = None

    dewpoint = None
    dewpoint_time = None

    wind_speed = None
    wind_direction = None
    wind_time = None

    pressure_pa = None
    pressure_time = None

    newest_time = None

    for feature in features:

        p = feature.get("properties", {})
        timestamp = p.get("timestamp")

        if newest_time is None and timestamp:
            newest_time = timestamp

        # ---------------------------------------------------------
        # Temperature
        # ---------------------------------------------------------

        temp = qv_value(p, "temperature")

        if temperature is None and temp is not None:
            temperature = temp
            temperature_time = timestamp

        # ---------------------------------------------------------
        # Dewpoint
        # ---------------------------------------------------------

        td = qv_value(p, "dewpoint")

        if dewpoint is None and td is not None:
            dewpoint = td
            dewpoint_time = timestamp

        # ---------------------------------------------------------
        # Wind
        # ---------------------------------------------------------

        speed = qv_value(p, "windSpeed")
        direction = qv_value(p, "windDirection")

        if (
            wind_speed is None
            and speed is not None
            and direction is not None
        ):
            wind_speed = speed
            wind_direction = direction
            wind_time = timestamp

        # ---------------------------------------------------------
        # Station pressure
        # ---------------------------------------------------------

        pressure = qv_value(p, "barometricPressure")

        if pressure_pa is None and pressure is not None:
            pressure_pa = pressure
            pressure_time = timestamp

        # ---------------------------------------------------------
        # Stop once everything has been found
        # ---------------------------------------------------------

        if (
            temperature is not None
            and dewpoint is not None
            and wind_speed is not None
            and pressure_pa is not None
        ):
            break

    return {
        "stid": stid,

        "temperature_C": temperature,
        "temperature_time": temperature_time,

        "dewpoint_C": dewpoint,
        "dewpoint_time": dewpoint_time,

        "wind_speed_kmh": wind_speed,
        "wind_direction_deg": wind_direction,
        "wind_time": wind_time,

        "barometric_pressure_Pa": pressure_pa,
        "pressure_time": pressure_time,

        "timestamp": temperature_time or newest_time,

        "source": "NWS API",
    }
    
# =====================================================================
# 4. MMNV1 TEMPERATURE - RR2
# =====================================================================

def fetch_mmvn1_temperature():
    """
    Retrieve MMNV1 temperature from the current BTV RR2 product.

    Expected SHEF form resembles:

        .A MMNV1 260724 Z DH1803/TAIRGZZ 63.7

    TAIR is air temperature in Fahrenheit.
    """

    try:

        response = requests.get(
            RR2_URL,
            headers=TEXT_HEADERS,
            timeout=30,
        )

        response.raise_for_status()

        text = response.text

    except requests.RequestException as exc:

        print(f"ERROR retrieving RR2: {exc}")

        return None, None

    # Remove HTML tags if product page was returned as HTML.

    text_clean = re.sub(
        r"<[^>]+>",
        " ",
        text
    )

    text_clean = (
        text_clean
        .replace("&nbsp;", " ")
        .replace("&amp;", "&")
    )

    # Example:
    #
    # .A MMNV1 260724 Z DH1803/TAIRGZZ 63.7

    pattern = re.compile(
        r"\.A\s+MMNV1\s+"
        r"(\d{6})\s+Z\s+"
        r"DH(\d{4})"
        r".*?"
        r"TAIR\w*\s+"
        r"(-?\d+(?:\.\d+)?)",
        re.IGNORECASE | re.DOTALL,
    )

    matches = pattern.findall(text_clean)

    if not matches:

        print(
            "WARNING: Could not find MMNV1 temperature in RR2."
        )

        return None, None

    date_code, hhmm, temp_f = matches[-1]

    try:

        dt = datetime.strptime(
            date_code + hhmm,
            "%y%m%d%H%M"
        ).replace(tzinfo=timezone.utc)

    except ValueError:

        dt = None

    temp_f = float(temp_f)

    temp_c = (temp_f - 32.0) * 5.0 / 9.0

    timestamp = (
        dt.isoformat()
        if dt
        else None
    )

    return temp_c, timestamp


# =====================================================================
# 5. MMNV1 WIND - RRSBTV
# =====================================================================

def candidate_rrs_times():
    """
    Generate candidate IEM RRS archive timestamps.

    RRSBTV normally updates around HH:12.

    We search backward rather than assuming the current hour's product
    has already arrived.
    """

    now = datetime.now(timezone.utc)

    candidates = []

    # Start at current hour :12.

    base = now.replace(
        minute=12,
        second=0,
        microsecond=0,
    )

    # If current time is before :12, start with previous hour.

    if now.minute < 12:
        base -= timedelta(hours=1)

    for hours_back in range(RRS_LOOKBACK_HOURS):

        dt = base - timedelta(hours=hours_back)

        candidates.append(
            dt.strftime("%Y%m%d%H%M")
        )

    return candidates


def fetch_rrs_product(timestamp):
    """
    Fetch one archived RRSBTV product from IEM.
    """

    url = RRS_URL.format(
        timestamp=timestamp
    )

    try:

        response = requests.get(
            url,
            headers=TEXT_HEADERS,
            timeout=30,
        )

        response.raise_for_status()

        return response.text

    except requests.RequestException:

        return None


def extract_shef_series(text, parameter):
    """
    Extract an MMNV1 SHEF .E series from RRSBTV.

    Example conceptually:

        .E MMNV1 20260724 DH1610/USIRG/DIN5/
        4.1/4.5/5.0/...

    parameter:
        USIRG -> wind speed
        UDIRG -> wind direction

    Returns:
        [(datetime, value), ...]
    """

    if not text:
        return []

    # Strip HTML because the IEM page can wrap the AFOS text.

    clean = re.sub(
        r"<[^>]+>",
        "\n",
        text
    )

    clean = (
        clean
        .replace("&nbsp;", " ")
        .replace("&amp;", "&")
    )

    # Normalize whitespace without destroying line structure.

    lines = [
        line.strip()
        for line in clean.splitlines()
        if line.strip()
    ]

    series = []

    i = 0

    while i < len(lines):

        line = lines[i]

        if (
            line.startswith(".E MMNV1")
            and parameter in line
        ):

            # Header example:
            #
            # .E MMNV1 20260724 DH1610/USIRG/DIN5/

            header = line

            # Some SHEF values may continue onto following lines.

            value_lines = []

            # Capture anything after DIN5/ on the header.

            match = re.search(
                rf"\.E\s+MMNV1\s+"
                rf"(\d{{8}})\s+"
                rf"DH(\d{{4}})/"
                rf"{parameter}/DIN(\d+)/?(.*)",
                header,
                re.IGNORECASE,
            )

            if not match:

                i += 1
                continue

            date_code = match.group(1)
            hhmm = match.group(2)
            interval_minutes = int(
                match.group(3)
            )

            remainder = match.group(4)

            if remainder:
                value_lines.append(remainder)

            # Continue until next SHEF record.

            j = i + 1

            while j < len(lines):

                next_line = lines[j]

                if next_line.startswith("."):
                    break

                value_lines.append(next_line)

                j += 1

            values_text = "/".join(value_lines)

            raw_values = [
                x.strip()
                for x in values_text.split("/")
                if x.strip()
            ]

            try:

                start = datetime.strptime(
                    date_code + hhmm,
                    "%Y%m%d%H%M"
                ).replace(
                    tzinfo=timezone.utc
                )

            except ValueError:

                i = j
                continue

            for n, raw_value in enumerate(raw_values):

                # SHEF missing values may appear as M.

                if raw_value.upper() in {
                    "M",
                    "MM",
                    "MSG",
                }:
                    continue

                # Keep first numeric token.

                numeric = re.match(
                    r"(-?\d+(?:\.\d+)?)",
                    raw_value
                )

                if not numeric:
                    continue

                value = float(
                    numeric.group(1)
                )

                obs_time = (
                    start
                    + timedelta(
                        minutes=n * interval_minutes
                    )
                )

                series.append(
                    (
                        obs_time,
                        value
                    )
                )

            i = j
            continue

        i += 1

    return series


def fetch_mmvn1_wind():
    """
    Search recent RRSBTV products and retrieve the latest MMNV1 wind
    observation containing both speed and direction.
    """

    for product_time in candidate_rrs_times():

        text = fetch_rrs_product(
            product_time
        )

        if not text:
            continue

        speed_series = extract_shef_series(
            text,
            "USIRG"
        )

        direction_series = extract_shef_series(
            text,
            "UDIRG"
        )

        if not speed_series or not direction_series:
            continue

        # Convert to timestamp dictionaries.

        speeds = {
            dt: value
            for dt, value in speed_series
        }

        directions = {
            dt: value
            for dt, value in direction_series
        }

        common_times = sorted(
            set(speeds)
            & set(directions),
            reverse=True,
        )

        if not common_times:
            continue

        latest_time = common_times[0]

        speed = speeds[latest_time]
        direction = directions[latest_time]

        # -------------------------------------------------------------
        # IMPORTANT:
        #
        # SHEF USIRG is treated here as wind speed in mph.
        #
        # Convert mph -> km/h so the normalized observation object
        # matches the NWS API station data.
        # -------------------------------------------------------------

        speed_kmh = speed * 1.609344

        return (
            speed_kmh,
            direction,
            latest_time.isoformat(),
            product_time,
        )

    print(
        "WARNING: Could not find MMNV1 wind in recent RRS products."
    )

    return None, None, None, None


# =====================================================================
# 6. BUILD MMNV1 OBSERVATION
# =====================================================================

def fetch_mmvn1():
    """
    Assemble MMNV1 observation from RR2 + RRS.
    """

    print(
        "\nRetrieving MMNV1 from RR2/RRS..."
    )

    temp_c, temp_time = (
        fetch_mmvn1_temperature()
    )

    (
        wind_speed_kmh,
        wind_direction,
        wind_time,
        rrs_product,
    ) = fetch_mmvn1_wind()

    if temp_c is None:

        print(
            "  MMNV1 temperature: MISSING"
        )

    else:

        print(
            f"  MMNV1 temperature: "
            f"{temp_c:.1f} C "
            f"({temp_time})"
        )

    if wind_speed_kmh is None:

        print(
            "  MMNV1 wind: MISSING"
        )

    else:

        wind_kt = (
            wind_speed_kmh
            * units("km/hour")
        ).to("knots").m

        print(
            f"  MMNV1 wind: "
            f"{wind_direction:.0f}/"
            f"{wind_kt:.0f} kt "
            f"({wind_time})"
        )

        print(
            f"  RRS product used: "
            f"{rrs_product}"
        )

    return {
        "stid": "MMNV1",
        "temperature_C": temp_c,
        "temperature_time": temp_time,
        "dewpoint_C": None,
        "dewpoint_time": None,
        "wind_speed_kmh": wind_speed_kmh,
        "wind_direction_deg": wind_direction,
        "wind_time": wind_time,
        "barometric_pressure_Pa": None,
        "pressure_time": None,
        "timestamp": temp_time,
        "source": "RR2 + RRSBTV",
    }


# =====================================================================
# 7. FETCH ALL OBSERVATIONS
# =====================================================================

def fetch_all():
    """
    Retrieve every station and normalize into a common dictionary.
    """

    observations = {}

    print()
    print("=" * 70)
    print("FETCHING LATEST OBSERVATIONS")
    print("=" * 70)

    for stid in NWS_STATIONS:

        print(
            f"\nFetching {stid}..."
        )

        obs = extract_latest_nws_values(
            stid
        )

        if obs is None:

            print(
                f"  {stid}: NO DATA"
            )

            continue

        observations[stid] = obs

        temp = obs["temperature_C"]
        speed = obs["wind_speed_kmh"]
        direction = obs[
            "wind_direction_deg"
        ]

        print(
            f"  Temperature: "
            f"{fmt(temp)} C"
        )

        if (
            speed is not None
            and direction is not None
        ):

            speed_kt = (
                speed
                * units("km/hour")
            ).to("knots").m

            print(
                f"  Wind: "
                f"{direction:.0f}/"
                f"{speed_kt:.0f} kt"
            )

        else:

            print(
                "  Wind: MISSING"
            )

        print(
            f"  Temp time: "
            f"{obs['temperature_time']}"
        )

        print(
            f"  Wind time: "
            f"{obs['wind_time']}"
        )

    observations["MMNV1"] = (
        fetch_mmvn1()
    )

    return observations


# =====================================================================
# 8. BUILD PROFILE
# =====================================================================

def build_profile(observations):
    """
    Convert normalized observations into an elevation-sorted profile.

    Temperature is required for a station to enter the thermal profile.
    Dewpoint and wind are optional. Any field older than
    MAX_OBSERVATION_AGE_MINUTES is dropped before the station is
    built - a stale TEMPERATURE is treated the same as a missing one
    (station skipped), while a stale DEWPOINT or WIND just nulls out
    that one field, since a station can easily have one fresh field
    and one stale one, and dropping the whole station over a single
    stale field would throw away otherwise-good data.
    """

    profile = []

    for stid, elevation_ft in STATIONS.items():

        obs = observations.get(stid)

        if not obs:

            print(
                f"Skipping {stid}: "
                f"no observation."
            )

            continue

        temperature = obs.get(
            "temperature_C"
        )

        temperature_time = obs.get(
            "temperature_time"
        )

        if temperature is not None and is_stale(temperature_time):

            print(
                f"Skipping {stid}: "
                f"temperature is "
                f"{observation_age_minutes(temperature_time):.0f} min old "
                f"(over {MAX_OBSERVATION_AGE_MINUTES:.0f} min limit)."
            )

            temperature = None

        if temperature is None:

            print(
                f"Skipping {stid}: "
                f"no usable temperature."
            )

            continue

        dewpoint = obs.get(
            "dewpoint_C"
        )

        dewpoint_time = obs.get(
            "dewpoint_time"
        )

        if dewpoint is not None and is_stale(dewpoint_time):

            print(
                f"  {stid}: dropping stale dewpoint "
                f"({observation_age_minutes(dewpoint_time):.0f} min old)."
            )

            dewpoint = None

        wind_speed = obs.get(
            "wind_speed_kmh"
        )

        wind_direction = obs.get(
            "wind_direction_deg"
        )

        wind_time = obs.get(
            "wind_time"
        )

        if (
            (wind_speed is not None or wind_direction is not None)
            and is_stale(wind_time)
        ):

            print(
                f"  {stid}: dropping stale wind "
                f"({observation_age_minutes(wind_time):.0f} min old)."
            )

            wind_speed = None
            wind_direction = None

        profile.append({
            "stid": stid,

            "elevation_ft": elevation_ft,

            "temperature_C":
                temperature,

            "temperature_time":
                temperature_time,

            "dewpoint_C":
                dewpoint,

            "dewpoint_time":
                dewpoint_time,

            "wind_speed_kmh":
                wind_speed,

            "wind_direction_deg":
                wind_direction,

            "wind_time":
                wind_time,

            "barometric_pressure_Pa":
                obs.get(
                    "barometric_pressure_Pa"
                ),

            "source":
                obs.get("source"),
        })

    profile.sort(
        key=lambda x:
        x["elevation_ft"]
    )

    if not profile:

        raise RuntimeError(
            "No valid observations available."
        )

    return profile
# =====================================================================
# 9. PRESSURE PROFILE
# =====================================================================

def calculate_pressures(profile):
    """
    Anchor pressure to KBTV and integrate upward with the
    hypsometric equation.

    Uses mean observed layer temperature.
    """

    Rd = 287.05
    g = 9.80665

    kbtv = next(
        (
            x
            for x in profile
            if x["stid"] == "KBTV"
        ),
        None,
    )

    if kbtv is None:

        raise RuntimeError(
            "KBTV is required as "
            "the pressure anchor."
        )

    pressure_pa = (
        kbtv.get(
            "barometric_pressure_Pa"
        )
    )

    if pressure_pa is not None:

        p0_hpa = (
            pressure_pa / 100.0
        )

        print(
            "\nUsing observed KBTV "
            f"station pressure: "
            f"{p0_hpa:.1f} hPa"
        )

    else:

        z_m = (
            kbtv["elevation_ft"]
            * 0.3048
        )

        p0_hpa = (
            1013.25
            * (
                1
                - 2.25577e-5
                * z_m
            )
            ** 5.25588
        )

        print(
            "\nWARNING: KBTV observed "
            "station pressure unavailable."
        )

        print(
            "Using standard-atmosphere "
            f"pressure: {p0_hpa:.1f} hPa"
        )

    # KBTV should be lowest profile station.

    profile[0][
        "pressure_hPa"
    ] = p0_hpa

    for i in range(
        1,
        len(profile)
    ):

        lower = profile[i - 1]
        upper = profile[i]

        z1 = (
            lower["elevation_ft"]
            * 0.3048
        )

        z2 = (
            upper["elevation_ft"]
            * 0.3048
        )

        dz = z2 - z1

        T1_K = (
            lower["temperature_C"]
            + 273.15
        )

        T2_K = (
            upper["temperature_C"]
            + 273.15
        )

        Tmean = (
            T1_K + T2_K
        ) / 2.0

        p1 = lower[
            "pressure_hPa"
        ]

        p2 = (
            p1
            * np.exp(
                -(g * dz)
                / (Rd * Tmean)
            )
        )

        upper[
            "pressure_hPa"
        ] = p2

    return profile


# =====================================================================
# 10. METPY ARRAYS
# =====================================================================

def make_metpy_arrays(profile):

    pressure = np.array([
        x["pressure_hPa"]
        for x in profile
    ]) * units.hPa

    temperature = np.array([
        x["temperature_C"]
        for x in profile
    ]) * units.degC

    wind_pressure = []
    wind_speed = []
    wind_direction = []

    for x in profile:

        speed = x[
            "wind_speed_kmh"
        ]

        direction = x[
            "wind_direction_deg"
        ]

        if (
            speed is None
            or direction is None
        ):
            continue

        wind_pressure.append(
            x["pressure_hPa"]
        )

        wind_speed.append(
            speed
        )

        wind_direction.append(
            direction
        )

    if wind_speed:

        wspd = (
            np.array(wind_speed)
            * units("km/hour")
        ).to("knots")

        wdir = (
            np.array(
                wind_direction
            )
            * units.degree
        )

        u, v = (
            mpcalc.wind_components(
                wspd,
                wdir,
            )
        )

        wind_pressure = (
            np.array(
                wind_pressure
            )
            * units.hPa
        )

    else:

        wind_pressure = None
        u = None
        v = None

    return (
        pressure,
        temperature,
        wind_pressure,
        u,
        v,
    )


# =====================================================================
# 11. QC TABLE
# =====================================================================

def print_profile(profile):

    print()
    print("=" * 112)

    print(
        f"{'STATION':<8}"
        f"{'ELEV':>8}"
        f"{'PRES':>10}"
        f"{'TEMP':>9}"
        f"{'WIND':>13}"
        f"{'TEMP AGE':>11}"
        f"{'WIND AGE':>11}"
        f"   SOURCE"
    )

    print("=" * 112)

    for x in profile:

        speed = x[
            "wind_speed_kmh"
        ]

        direction = x[
            "wind_direction_deg"
        ]

        if (
            speed is not None
            and direction is not None
        ):

            speed_kt = (
                speed
                * units("km/hour")
            ).to("knots").m

            wind_text = (
                f"{direction:03.0f}/"
                f"{speed_kt:.0f}kt"
            )

        else:

            wind_text = "MISSING"

        temp_age = (
            observation_age_minutes(
                x["temperature_time"]
            )
        )

        wind_age = (
            observation_age_minutes(
                x["wind_time"]
            )
        )

        temp_age_text = (
            f"{temp_age:.0f}m"
            if temp_age is not None
            else "--"
        )

        wind_age_text = (
            f"{wind_age:.0f}m"
            if wind_age is not None
            else "--"
        )

        print(
            f"{x['stid']:<8}"
            f"{x['elevation_ft']:>7.0f}'"
            f"{x['pressure_hPa']:>9.1f}"
            f"{x['temperature_C']:>9.1f}"
            f"{wind_text:>13}"
            f"{temp_age_text:>11}"
            f"{wind_age_text:>11}"
            f"   {x['source']}"
        )

    print("=" * 112)


# =====================================================================
# 11B. WINTER PROFILE DIAGNOSTICS
# =====================================================================
#
# This section derives a compact set of winter-weather diagnostics
# from the same station profile used for the Skew-T. With only 6-7
# low-level points instead of a full sounding, everything here is a
# bulk / coarse estimate intended for situational awareness, not a
# validated forecast product. Assumptions are called out inline.
#

def interp_crossing(z1, v1, z2, v2, target=0.0):
    """
    Linear interpolation for the elevation where a variable crosses
    `target`, given two bracketing (elevation, value) points.
    """

    if v2 == v1:
        return None

    frac = (target - v1) / (v2 - v1)

    if frac < 0 or frac > 1:
        return None

    return z1 + frac * (z2 - z1)

def analyze_zero_level(points):
    """
    Analyze the 0 C structure of an elevation-sorted series.

    Returns:
        {
            "status": str,
            "level_ft": float or None,
            "lower_crossing_ft": float or None,
            "upper_crossing_ft": float or None,
        }

    status:
        "above_layer"   - all observed values > 0 C
        "surface"   - all observed values < 0 C
        "normal"        - warm below, cold above
        "warm_layer"    - cold below, warm above
        "warm_nose"     - cold below, warm layer, cold again
        "unavailable"   - fewer than two valid observations
    """

    points = sorted(
        [p for p in points if p[1] is not None],
        key=lambda p: p[0]
    )

    result = {
        "status": "unavailable",
        "level_ft": None,
        "lower_crossing_ft": None,
        "upper_crossing_ft": None,
    }

    if len(points) < 2:
        return result

    values = [v for _, v in points]

    # Entire observed profile is warm.
    if all(v > 0 for v in values):
        result["status"] = "above_layer"
        return result

    # Entire observed profile is cold.
    if all(v < 0 for v in values):
        result["status"] = "surface"
        return result

    crossings = []

    for (z1, v1), (z2, v2) in zip(points, points[1:]):

        if v1 == 0:
            crossing = z1

        elif v2 == 0:
            crossing = z2

        elif (v1 > 0 > v2) or (v1 < 0 < v2):
            crossing = interp_crossing(z1, v1, z2, v2)

        else:
            continue

        if crossing is not None:
            if not crossings or abs(crossing - crossings[-1]) > 0.1:
                crossings.append(crossing)

    if not crossings:
        return result

    surface_value = points[0][1]

    # Warm at lowest observation -> traditional freezing level.
    if surface_value >= 0:
        result["status"] = "normal"
        result["level_ft"] = crossings[0]
        return result

    # Cold at lowest observation and at least two crossings:
    # elevated warm nose bounded by two zero crossings.
    if len(crossings) >= 2:
        result["status"] = "warm_nose"
        result["lower_crossing_ft"] = crossings[0]
        result["upper_crossing_ft"] = crossings[1]
        result["level_ft"] = crossings[0]
        return result

    # Cold at lowest observation with one upward crossing.
    result["status"] = "warm_layer"
    result["level_ft"] = crossings[0]
    result["lower_crossing_ft"] = crossings[0]

    return result

def mean_lapse_rate(profile):
    """
    Bulk lapse rate between the lowest and highest observed stations,
    in C/km, positive when temperature falls with height (the
    conventional sign).
    """

    bottom = profile[0]
    top = profile[-1]

    dz_km = ft_to_m(
        top["elevation_ft"] - bottom["elevation_ft"]
    ) / 1000.0

    if dz_km == 0:
        return None

    return (
        bottom["temperature_C"] - top["temperature_C"]
    ) / dz_km


def max_inversion(profile):
    """
    The single layer with the largest temperature increase with
    height (an inversion). Returns None if every layer cools with
    height.
    """

    best = None

    for lower, upper in zip(profile, profile[1:]):

        dT = upper["temperature_C"] - lower["temperature_C"]

        if dT > 0 and (best is None or dT > best["dT"]):

            best = {
                "dT": dT,
                "z1": lower["elevation_ft"],
                "z2": upper["elevation_ft"],
            }

    return best


def inversion_pressure_bands(profile):
    """
    Every layer (not just the single largest) that is an inversion OR
    isothermal - temperature not decreasing with height - as a list
    of (pressure_low, pressure_high) tuples ready for axhspan shading
    on the Skew-T. Isothermal layers are included alongside true
    inversions because both indicate a stable capping layer; a strict
    "warmer above" check misses a layer that's exactly flat. A profile
    can have more than one such layer (e.g. a shallow surface
    inversion plus a separate one aloft), unlike max_inversion() which
    only reports the single strongest true inversion.
    """

    bands = []

    for lower, upper in zip(profile, profile[1:]):

        if upper["temperature_C"] >= lower["temperature_C"]:

            p1 = lower["pressure_hPa"]
            p2 = upper["pressure_hPa"]

            bands.append((min(p1, p2), max(p1, p2)))

    return bands


def attach_derived_fields(profile):
    """
    Compute per-station wet-bulb temperature and relative humidity
    for every station that has both a dewpoint and a pressure, and
    attach them directly onto the station dict as "wetbulb_C" and
    "relative_humidity_pct".

    Stations without a dewpoint (MMNV1, or any NWS station missing
    one on a given pull) get both fields set to None rather than
    being dropped, so downstream code can display "--" instead of
    silently losing the row.
    """

    for x in profile:

        td = x.get("dewpoint_C")
        p = x.get("pressure_hPa")
        t = x.get("temperature_C")

        if td is None or p is None or t is None:

            x["wetbulb_C"] = None
            x["relative_humidity_pct"] = None

            continue

        try:

            tw = mpcalc.wet_bulb_temperature(
                p * units.hPa,
                t * units.degC,
                td * units.degC,
            ).to("degC").m

        except Exception:

            tw = None

        x["wetbulb_C"] = tw

        try:

            rh = mpcalc.relative_humidity_from_dewpoint(
                t * units.degC,
                td * units.degC,
            ).to("percent").m

        except Exception:

            rh = None

        x["relative_humidity_pct"] = rh

    return profile


def compute_wetbulb_series(profile):
    """
    (elevation_ft, wetbulb_C) pairs for every station that already
    has a wet-bulb value attached by attach_derived_fields().
    """

    series = []

    for x in profile:

        tw = x.get("wetbulb_C")

        if tw is None:
            continue

        series.append({
            "elevation_ft": x["elevation_ft"],
            "wetbulb_C": tw,
        })

    return series


def bulk_shear(profile, base_stid=SHEAR_BASE_STID, top_stid=SHEAR_TOP_STID):
    """
    Vector wind difference (kt) between two named stations - by
    default KBTV (330 ft, valley floor) and MMNV1 (3891 ft, summit),
    which together span roughly the lowest 1 km AGL of this profile.

    Returns None if either station is missing from the profile or is
    missing a wind observation.
    """

    base = next((x for x in profile if x["stid"] == base_stid), None)
    top = next((x for x in profile if x["stid"] == top_stid), None)

    if base is None or top is None:
        return None

    if (
        base.get("wind_speed_kmh") is None
        or base.get("wind_direction_deg") is None
        or top.get("wind_speed_kmh") is None
        or top.get("wind_direction_deg") is None
    ):
        return None

    speeds_kt = (
        np.array([base["wind_speed_kmh"], top["wind_speed_kmh"]])
        * units("km/hour")
    ).to("knots").m

    directions = np.array(
        [base["wind_direction_deg"], top["wind_direction_deg"]]
    )

    u, v = mpcalc.wind_components(
        speeds_kt * units.knots,
        directions * units.degree,
    )

    u = u.m
    v = v.m

    return float(np.hypot(u[1] - u[0], v[1] - v[0]))


def potential_temperature_profile(profile):
    """
    (height_m, theta_K) pairs for every station with both pressure
    and temperature, sorted by height.
    """

    points = []

    for x in profile:

        p = x.get("pressure_hPa")
        t = x.get("temperature_C")

        if p is None or t is None:
            continue

        theta = (t + 273.15) * (1000.0 / p) ** (RD / CP)

        points.append((ft_to_m(x["elevation_ft"]), theta))

    points.sort(key=lambda pt: pt[0])

    return points


def cross_barrier_component(speed, direction_from_deg, ridge_orientation_deg=RIDGE_ORIENTATION_DEG):
    """
    Magnitude of the wind component oriented perpendicular to the
    ridge line (cross-barrier flow), which is the component relevant
    to blocking/Froude-number behavior - a wind blowing along the
    ridge axis doesn't get forced up and over it the way cross-ridge
    flow does.

    `direction_from_deg` is the meteorological "wind from" direction.
    Returns an unsigned magnitude, since blocking behaves the same
    whether the flow approaches from the east or west side of the
    ridge.
    """

    toward_deg = (direction_from_deg + 180.0) % 360.0
    ridge_normal_deg = (ridge_orientation_deg + 90.0) % 360.0

    angle = np.radians(toward_deg - ridge_normal_deg)

    return abs(speed * np.cos(angle))


def brunt_vaisala_and_froude(profile, wind_stid=SHEAR_BASE_STID):
    """
    Dry Brunt-Vaisala frequency (N, 1/s) from a least-squares fit of
    potential temperature against height across every station in the
    profile, and a bulk Froude number Fr = U / (N * h) for the ridge,
    where U is the cross-barrier wind component at `wind_stid` (KBTV
    by default) and h is the profile's elevation span (base to
    summit).

    Fitting N through all stations rather than differencing just the
    top and bottom two is deliberate: with sub-degree temperature
    differences over a shallow layer, a two-point estimate is very
    sensitive to noise in either endpoint. A least-squares fit through
    every station is more stable, though still a coarse, order-of-
    magnitude estimate - not a substitute for an actual upstream
    sounding.

    Returns (None, None, None) if there are too few points, or if the
    fitted layer is neutral/unstable (dtheta/dz <= 0), since N is
    undefined there.
    """

    theta_points = potential_temperature_profile(profile)

    if len(theta_points) < 3:
        # A 2-point fit degenerates back into the noise-prone
        # difference this function is meant to avoid.
        return None, None, None

    heights = np.array([pt[0] for pt in theta_points])
    thetas = np.array([pt[1] for pt in theta_points])

    dtheta_dz, _intercept = np.polyfit(heights, thetas, 1)

    if dtheta_dz <= 0:
        # Neutral or unstable bulk layer - N (and therefore Fr) is
        # not meaningfully defined.
        return None, None, None

    theta_mean = float(np.mean(thetas))

    N = np.sqrt((G / theta_mean) * dtheta_dz)

    wind_station = next(
        (x for x in profile if x["stid"] == wind_stid), None
    )

    if (
        wind_station is None
        or wind_station.get("wind_speed_kmh") is None
        or wind_station.get("wind_direction_deg") is None
    ):
        return float(N), None, None

    speed_ms = (
        wind_station["wind_speed_kmh"] * units("km/hour")
    ).to("m/s").m

    U = cross_barrier_component(
        speed_ms, wind_station["wind_direction_deg"]
    )

    mountain_height_m = ft_to_m(
        profile[-1]["elevation_ft"] - profile[0]["elevation_ft"]
    )

    if mountain_height_m <= 0:
        return float(N), float(U), None

    Fr = U / (N * mountain_height_m)

    return float(N), float(U), float(Fr)


def classify_flow_regime(Fr):

    if Fr is None:
        return "Unknown"

    if Fr >= FROUDE_UNBLOCKED:
        return "Unblocked / flow-over"

    if Fr >= FROUDE_BLOCKED:
        return "Partially blocked"

    return "Blocked"


# =====================================================================
# 11C. MOUNTAIN WAVE POTENTIAL
# =====================================================================
#
# A composite, low-level-plus-model PROXY for mountain-wave/
# downslope-wind favorability at the ridge, from five ingredients:
# the Froude regime, the observed profile's stability structure, the
# cross-barrier wind at the summit, how directionally coherent the
# observed wind is, and - the one ingredient the observed
# surface-to-summit profile alone could never see - whether a
# critical level (a layer above the ridge where wind speed drops
# toward zero or reverses direction) is present in a RAP model wind
# profile. A critical level is the textbook trigger for the most
# severe downslope windstorms and wave-breaking events (Durran 1990
# or any standard mountain-meteorology reference); see the CRITICAL
# LEVEL DETECTION section above for exactly what that component can
# and can't tell you (it's a single model analysis hour over
# smoothed terrain, not an observation).
#
# Treat the whole index as "how favorable do the ingredients look",
# not "is a wave occurring right now" - it is a coarse favorability
# estimate, not a forecast, and not a substitute for an actual
# forecast discussion, AIRMET/SIGMET, or PIREP.

WAVE_LOW_MAX = 3
WAVE_MODERATE_MAX = 7

WAVE_WIND_STRONG_KT = 30.0
WAVE_WIND_MODERATE_KT = 15.0

WAVE_COHERENT_SPREAD_DEG = 30.0
WAVE_PARTIAL_SPREAD_DEG = 60.0


def ridge_top_station(profile, top_stid=SHEAR_TOP_STID):
    """
    The named summit station's full record from the profile, or
    None if it isn't present in today's pull.
    """

    return next((x for x in profile if x["stid"] == top_stid), None)


def ridge_top_cross_barrier_kt(profile, top_stid=SHEAR_TOP_STID):
    """
    Cross-barrier (ridge-normal) wind component AT THE SUMMIT, in
    knots - the level that actually matters for wave generation.
    This is deliberately separate from brunt_vaisala_and_froude's
    cross-barrier term, which is evaluated at KBTV (the valley
    floor) because that's what the bulk Froude number's upstream
    approach flow is conventionally referenced to; wave generation
    itself is driven by the flow actually crossing the ridge.

    Returns None if the summit station is missing wind data.
    """

    station = ridge_top_station(profile, top_stid)

    if (
        station is None
        or station.get("wind_speed_kmh") is None
        or station.get("wind_direction_deg") is None
    ):
        return None

    speed_kt = (
        station["wind_speed_kmh"] * units("km/hour")
    ).to("knots").m

    return float(
        cross_barrier_component(
            speed_kt, station["wind_direction_deg"]
        )
    )


def directional_consistency_deg(profile):
    """
    Maximum pairwise angular spread (degrees) among every station
    that has a wind direction observation - a coarse proxy for how
    coherent the flow is from valley floor to ridge. A tight spread
    suggests a single organized flow crossing the barrier; a wide
    spread suggests the wind is doing different things at different
    levels, which works against a clean, organized wave response.

    Returns None if fewer than two stations have wind direction data
    - can't assess a spread from a single point, and with wind
    sensors frequently missing at the mid-elevation NWS stations,
    this will often legitimately be "not enough data" rather than a
    real zero-spread reading.
    """

    directions = [
        x["wind_direction_deg"]
        for x in profile
        if x.get("wind_direction_deg") is not None
    ]

    if len(directions) < 2:
        return None

    max_spread = 0.0

    for i in range(len(directions)):
        for j in range(i + 1, len(directions)):

            diff = abs(directions[i] - directions[j])
            diff = min(diff, 360.0 - diff)

            max_spread = max(max_spread, diff)

    return max_spread


# =====================================================================
# CRITICAL LEVEL DETECTION (RAP model wind profile above the ridge)
# =====================================================================
#
# Everything above this point can only see the shallow surface-to-
# summit OBSERVED profile - which has no way to detect a critical
# level (a layer above the ridge where wind speed drops toward zero
# or reverses direction). That's the textbook trigger for the most
# severe downslope-windstorm and wave-breaking events (Durran 1990),
# and until now this script had no way to see it at all.
#
# The fix: a separate script in this same repo pulls full-depth RAP
# BUFKIT soundings at KBTV and exports the surface-through-500 hPa
# wind profile as JSON (see that script's export_wind_profile_json).
# The functions below fetch that JSON and scan it for a critical
# level within a few km above the ridge.
#
# This is a MODEL-based diagnostic, not an observation, and it
# deserves the same caution any single model analysis hour does:
# RAP's terrain is far smoother than the actual Green Mountains, so
# its near-surface wind can differ meaningfully from what's actually
# happening at the ridge. Treat a detected critical level as "this
# ingredient is present in the model", not a guarantee.

RAP_WIND_PROFILE_URL_TEMPLATE = (
    "https://raw.githubusercontent.com/matthewclay88/severe-dashboard/"
    "main/outputs/{site}_rap_wind_profile.json"
)

CRITICAL_LEVEL_SPEED_KT = 10.0            # at/below this, treat wind as "near-calm" - a classic critical-level signature
CRITICAL_LEVEL_DIR_REVERSAL_DEG = 120.0   # offset from ridge-top wind direction considered a reversal
CRITICAL_LEVEL_SEARCH_DEPTH_M = 3000.0    # how far above ridge-top to look - a critical level much higher than this rarely matters for surface downslope winds


def fetch_rap_wind_profile(site="kbtv", timeout=20):
    """
    Fetch the RAP wind-profile JSON exported by the companion
    BUFKIT/Sheets script - the surface-through-500 hPa vertical wind
    profile at `site` from the most recent RAP analysis hour.

    Returns None (never raises) on any failure - network issue,
    missing file, malformed JSON - since a missing wind profile
    should make the critical-level component of the mountain-wave
    index degrade to "unavailable", not crash the whole run. This is
    a network call, so it belongs in the fetch phase (see main()),
    not inside the otherwise-pure diagnostics computation.
    """

    url = RAP_WIND_PROFILE_URL_TEMPLATE.format(site=site.lower())

    try:

        response = requests.get(url, timeout=timeout)
        response.raise_for_status()

        payload = response.json()

    except Exception as exc:

        print(f"WARNING: could not fetch RAP wind profile: {exc}")

        return None

    if not payload.get("levels"):
        return None

    return payload


def critical_level_above_ridge(
    wind_profile, ridge_elevation_ft, ridge_wind_direction_deg
):
    """
    Scan a RAP wind-profile payload (see fetch_rap_wind_profile) from
    ridge-top upward through CRITICAL_LEVEL_SEARCH_DEPTH_M for the
    LOWEST level where either:

        - wind speed drops to or below CRITICAL_LEVEL_SPEED_KT, or
        - wind direction has swung more than
          CRITICAL_LEVEL_DIR_REVERSAL_DEG from the observed
          ridge-top direction

    Returns a dict describing the lowest such level found, or None
    if no critical level appears within the search depth (or if the
    wind profile / ridge wind direction aren't available).
    """

    if wind_profile is None or ridge_wind_direction_deg is None:
        return None

    ridge_elevation_m = ft_to_m(ridge_elevation_ft)
    search_top_m = ridge_elevation_m + CRITICAL_LEVEL_SEARCH_DEPTH_M

    levels = sorted(
        wind_profile["levels"], key=lambda lvl: lvl["height_m"]
    )

    for level in levels:

        if level["height_m"] < ridge_elevation_m:
            continue

        if level["height_m"] > search_top_m:
            break

        dir_diff = abs(level["wind_dir_deg"] - ridge_wind_direction_deg) % 360.0
        dir_diff = min(dir_diff, 360.0 - dir_diff)

        is_weak = level["wind_speed_kt"] <= CRITICAL_LEVEL_SPEED_KT
        is_reversed = dir_diff >= CRITICAL_LEVEL_DIR_REVERSAL_DEG

        if is_weak or is_reversed:

            return {
                "height_m": level["height_m"],
                "height_ft": level["height_m"] / 0.3048,
                "pressure_hPa": level["pressure_hPa"],
                "wind_speed_kt": level["wind_speed_kt"],
                "wind_dir_deg": level["wind_dir_deg"],
                "reason": "weak wind" if is_weak else "directional reversal",
            }

    return None



def mountain_wave_potential(profile, diagnostics, rap_wind_profile=None):
    """
    Composite Low/Moderate/High mountain-wave-potential index from
    five ingredients (0-2 points each, 0-10 total):

        1. Froude regime                       - already computed
        2. Ridge-top stability (a capping       - built here from
           inversion positioned in the upper      max_inversion /
           third of the profile scores           stability_label
           highest - that's the actual "lid"
           that traps wave energy, not just
           any low-level inversion)
        3. Cross-barrier wind AT THE SUMMIT     - built here
        4. Directional coherence across the     - built here
           observed layer
        5. A critical level above the ridge     - built here, from
           (see critical_level_above_ridge)       a RAP model wind
                                                    profile - the
                                                    ONE ingredient
                                                    the observed
                                                    profile alone
                                                    could never see

    Bucketed Low (0-3) / Moderate (4-7) / High (8-10). Each
    component contributes 0 points (not a penalty - genuinely "no
    signal") when its underlying data is missing. If BOTH
    load-bearing ingredients (Froude number and summit wind) are
    unavailable, the whole index reports "Indeterminate" rather than
    a manufactured Low score, since a fabricated 0 in that situation
    would misrepresent "we couldn't assess this" as "we assessed
    this as unfavorable".

    `rap_wind_profile` is optional and comes from
    fetch_rap_wind_profile() - pass None (the default) to run the
    original four-ingredient version if the wind profile isn't
    available for some reason; component 5 then reports
    "unavailable" like any other missing ingredient.

    See the CRITICAL LEVEL DETECTION section above for what
    component 5 can and can't tell you, and the section 11C module
    comment further above for why this whole index is a low-level
    favorability estimate, not a forecast.
    """

    points = 0
    reasons = []

    # ---------------- 1. Froude regime ----------------

    fr = diagnostics.get("froude_number")
    flow_regime = diagnostics.get("flow_regime")

    if fr is None:
        reasons.append("Froude number unavailable")
    elif flow_regime == "Blocked":
        reasons.append(f"Blocked flow (Fr {fr:.2f})")
    elif flow_regime == "Partially blocked":
        points += 2
        reasons.append(f"Near-resonant flow regime (Fr {fr:.2f})")
    else:
        # Unblocked / flow-over
        points += 1
        reasons.append(f"Unblocked flow-over (Fr {fr:.2f})")

    # ---------------- 2. Ridge-top stability ----------------

    inv = diagnostics.get("max_inversion")
    stability = diagnostics.get("stability_label")

    elevations = sorted(STATIONS.values())
    ridge_top_threshold_ft = elevations[0] + (
        (elevations[-1] - elevations[0]) * 2.0 / 3.0
    )

    if inv is not None and inv["z2"] >= ridge_top_threshold_ft:
        points += 2
        reasons.append(
            f"Capping inversion near ridge-top "
            f"(+{inv['dT']:.1f} C at {inv['z2']:.0f} ft)"
        )
    elif stability in ("Stable", "Very Stable"):
        points += 1
        reasons.append(f"{stability} layer, no distinct ridge-top cap")
    else:
        reasons.append(f"{stability or 'Unknown'} stratification")

    # ---------------- 3. Ridge-top cross-barrier wind ----------------

    ridge_wind_kt = ridge_top_cross_barrier_kt(profile)

    if ridge_wind_kt is None:
        reasons.append("Summit wind unavailable")
    elif ridge_wind_kt >= WAVE_WIND_STRONG_KT:
        points += 2
        reasons.append(f"Strong cross-barrier flow at summit ({ridge_wind_kt:.0f} kt)")
    elif ridge_wind_kt >= WAVE_WIND_MODERATE_KT:
        points += 1
        reasons.append(f"Moderate cross-barrier flow at summit ({ridge_wind_kt:.0f} kt)")
    else:
        reasons.append(f"Weak cross-barrier flow at summit ({ridge_wind_kt:.0f} kt)")

    # ---------------- 4. Directional coherence ----------------

    spread = directional_consistency_deg(profile)

    if spread is None:
        reasons.append("Insufficient wind-direction data")
    elif spread <= WAVE_COHERENT_SPREAD_DEG:
        points += 2
        reasons.append(f"Coherent flow ({spread:.0f}\u00b0 spread)")
    elif spread <= WAVE_PARTIAL_SPREAD_DEG:
        points += 1
        reasons.append(f"Somewhat coherent flow ({spread:.0f}\u00b0 spread)")
    else:
        reasons.append(f"Disorganized flow ({spread:.0f}\u00b0 spread)")

    # ---------------- 5. Critical level above the ridge ----------------
    #
    # The single most decisive ingredient in the literature when it's
    # actually present - a critical level traps wave energy instead
    # of letting it radiate away. Scored the same 0-2 scale as the
    # others for simplicity, even though it arguably deserves more
    # weight; the honesty of "we now have a real signal for this"
    # matters more here than precisely calibrating the weighting.

    ridge_station = ridge_top_station(profile)
    ridge_wind_dir = (
        ridge_station.get("wind_direction_deg") if ridge_station else None
    )

    critical_level = critical_level_above_ridge(
        rap_wind_profile,
        profile[-1]["elevation_ft"],
        ridge_wind_dir,
    )

    if rap_wind_profile is None:
        reasons.append("RAP wind profile unavailable")
    elif ridge_wind_dir is None:
        reasons.append("No observed ridge-top direction to compare against")
    elif critical_level is not None:
        points += 2
        reasons.append(
            f"Critical level aloft ({critical_level['reason']} at "
            f"{critical_level['height_ft']:.0f} ft, "
            f"{critical_level['wind_speed_kt']:.0f} kt)"
        )
    else:
        reasons.append(
            f"No critical level within "
            f"{CRITICAL_LEVEL_SEARCH_DEPTH_M:.0f} m above ridge (RAP)"
        )

    # ---------------- Bucket ----------------

    if fr is None and ridge_wind_kt is None:
        category = "Indeterminate"
    elif points <= WAVE_LOW_MAX:
        category = "Low"
    elif points <= WAVE_MODERATE_MAX:
        category = "Moderate"
    else:
        category = "High"

    return {
        "score": points,
        "max_score": 10,
        "category": category,
        "reasons": reasons,
        "critical_level": critical_level,
    }


# =====================================================================
# 11D. RIME ICING POTENTIAL
# =====================================================================
#
# Same pattern as 11C's mountain-wave index: a composite score built
# from ingredients this profile can actually assess, degrading
# honestly instead of guessing when something's missing.
#
# Rime ice forms when supercooled liquid droplets freeze on contact
# with an exposed surface - distinct from freezing rain, which
# classify_precip_type() already handles. Three ingredients matter:
# temperature in the supercooled-droplet range (roughly -2 C to
# -20 C - warmer than that nothing freezes, colder than that a lot
# of the liquid water has already glaciated into ice crystals),
# moisture/cloud presence (you need actual liquid water around,
# meaning the summit needs to be at or near saturation), and wind
# (drives accretion rate - it's specifically why rime grows into
# wind-pointing "feathers" rather than sitting like calm-air hoar
# frost).
#
# THE REAL LIMITATION: MMNV1 (the actual summit) never reports
# dewpoint - only temperature and wind, see fetch_mmvn1(). The
# moisture component below therefore has to use the next station
# down that DOES have a dewpoint as a proxy for "is the summit
# sitting in cloud", not a true summit reading. That component is
# treated as load-bearing: if no station in the profile has moisture
# data at all, the whole index reports "Indeterminate" rather than a
# manufactured score, since temperature and wind alone can't confirm
# there's any liquid water present to freeze in the first place.

RIME_LOW_MAX = 2
RIME_MODERATE_MAX = 4

RIME_TEMP_PRIME_LOW_C = -20.0
RIME_TEMP_PRIME_HIGH_C = -2.0
RIME_TEMP_MARGINAL_LOW_C = -25.0

RIME_RH_SATURATED_PCT = 90.0
RIME_RH_MOIST_PCT = 75.0

RIME_WIND_STRONG_KT = 25.0
RIME_WIND_MODERATE_KT = 10.0


def uppermost_station_with_dewpoint(profile):
    """
    The highest-elevation station in `profile` that has a dewpoint
    reading, or None if no station has one this run. Searched from
    the top down since MMNV1 - the actual summit - never reports
    dewpoint (see fetch_mmvn1()), so the next station down is
    usually the best available proxy for how moist the air is near
    the ridge. `profile` is already elevation-sorted ascending, so
    walking it in reverse starts at the highest station.
    """

    for station in reversed(profile):

        if station.get("dewpoint_C") is not None:
            return station

    return None


def rime_icing_potential(profile, diagnostics):
    """
    Composite Low/Moderate/High rime-icing-potential index for the
    summit, from three ingredients (0-2 points each, 0-6 total):

        1. Summit temperature in the classic supercooled-droplet
           range
        2. Moisture near the top of the profile (see the module
           comment above for why this is a proxy, not a direct
           summit reading) - LOAD-BEARING: if unavailable, the
           whole index reports "Indeterminate"
        3. Summit wind speed - the accretion-rate driver

    Bucketed Low (0-2) / Moderate (3-4) / High (5-6).

    Treat this index as "how favorable do the ingredients look for
    icing on exposed summit terrain", not a confirmed observation -
    it is not a substitute for an actual PIREP, webcam check, or
    on-mountain report.
    """

    points = 0
    reasons = []

    summit = profile[-1]

    # ---------------- 1. Summit temperature range ----------------

    summit_temp = summit.get("temperature_C")

    if summit_temp is None:
        reasons.append("Summit temperature unavailable")
    elif RIME_TEMP_PRIME_LOW_C <= summit_temp <= RIME_TEMP_PRIME_HIGH_C:
        points += 2
        reasons.append(f"Summit temp in classic rime range ({summit_temp:.1f} C)")
    elif (
        RIME_TEMP_MARGINAL_LOW_C <= summit_temp < RIME_TEMP_PRIME_LOW_C
    ) or (
        RIME_TEMP_PRIME_HIGH_C < summit_temp <= 0.0
    ):
        points += 1
        reasons.append(f"Summit temp marginal for rime ({summit_temp:.1f} C)")
    else:
        reasons.append(f"Summit temp outside rime range ({summit_temp:.1f} C)")

    # ---------------- 2. Moisture near the top ----------------

    moisture_station = uppermost_station_with_dewpoint(profile)

    rh = (
        moisture_station.get("relative_humidity_pct")
        if moisture_station is not None
        else None
    )

    if moisture_station is None or rh is None:
        reasons.append("No dewpoint data available near ridge-top")
    elif rh >= RIME_RH_SATURATED_PCT:
        points += 2
        reasons.append(
            f"Near-saturated air at {moisture_station['stid']} "
            f"({rh:.0f}% RH, {moisture_station['elevation_ft']:.0f} ft)"
        )
    elif rh >= RIME_RH_MOIST_PCT:
        points += 1
        reasons.append(
            f"Moist air at {moisture_station['stid']} "
            f"({rh:.0f}% RH, {moisture_station['elevation_ft']:.0f} ft)"
        )
    else:
        reasons.append(
            f"Dry air at {moisture_station['stid']} "
            f"({rh:.0f}% RH, {moisture_station['elevation_ft']:.0f} ft)"
        )

    # ---------------- 3. Summit wind speed ----------------

    summit_wind_kmh = summit.get("wind_speed_kmh")

    if summit_wind_kmh is None:
        reasons.append("Summit wind unavailable")
    else:

        summit_wind_kt = (
            summit_wind_kmh * units("km/hour")
        ).to("knots").m

        if summit_wind_kt >= RIME_WIND_STRONG_KT:
            points += 2
            reasons.append(
                f"Strong summit wind ({summit_wind_kt:.0f} kt) - rapid accretion"
            )
        elif summit_wind_kt >= RIME_WIND_MODERATE_KT:
            points += 1
            reasons.append(f"Moderate summit wind ({summit_wind_kt:.0f} kt)")
        else:
            reasons.append(f"Light summit wind ({summit_wind_kt:.0f} kt)")

    # ---------------- Bucket ----------------

    if moisture_station is None or rh is None:
        category = "Indeterminate"
    elif points <= RIME_LOW_MAX:
        category = "Low"
    elif points <= RIME_MODERATE_MAX:
        category = "Moderate"
    else:
        category = "High"

    return {
        "score": points,
        "max_score": 6,
        "category": category,
        "reasons": reasons,
    }



def layer_lapse_rates(profile):
    """
    Lapse rate (C/km, positive = cooling with height) of the layer
    immediately BELOW each station, keyed by stid. The lowest station
    has no layer below it in this profile and is omitted.
    """

    rates = {}

    for lower, upper in zip(profile, profile[1:]):

        dz_km = ft_to_m(
            upper["elevation_ft"] - lower["elevation_ft"]
        ) / 1000.0

        if dz_km == 0:
            continue

        rate = (
            lower["temperature_C"] - upper["temperature_C"]
        ) / dz_km

        rates[upper["stid"]] = rate

    return rates


def bourgouin_energy(profile):
    """
    Rough positive/negative "energy" areas (J/kg) referenced to the
    0 C isotherm, in the spirit of Bourgouin (2000)'s precipitation-
    type nomogram: Ep is the warm-nose area above freezing, En is the
    magnitude of the sub-freezing area below/around it.

    This is a coarse adaptation built from 6-7 near-surface station
    points rather than a full upper-air sounding, and uses the
    simplified single hypsometric-layer formula
    (E = Rd * ln(p_bottom/p_top) * T_mean_C) rather than the full
    published nomogram curves. Treat the resulting precip-type call
    as a rough guide, not a validated forecast.
    """

    Rd = 287.05

    Ep = 0.0
    En = 0.0

    for lower, upper in zip(profile, profile[1:]):

        p1 = lower["pressure_hPa"]
        p2 = upper["pressure_hPa"]

        t1 = lower["temperature_C"]
        t2 = upper["temperature_C"]

        same_sign = (t1 >= 0 and t2 >= 0) or (t1 <= 0 and t2 <= 0)

        if same_sign:

            t_mean = (t1 + t2) / 2.0
            area = Rd * np.log(p1 / p2) * t_mean

            if t_mean >= 0:
                Ep += area
            else:
                En += -area

            continue

        # Layer straddles 0 C - split at the crossing pressure,
        # found by interpolating T linearly against ln(p).

        lnp1 = np.log(p1)
        lnp2 = np.log(p2)

        frac = (0.0 - t1) / (t2 - t1)
        lnp0 = lnp1 + frac * (lnp2 - lnp1)
        p0 = np.exp(lnp0)

        area_lower = Rd * np.log(p1 / p0) * (t1 / 2.0)
        area_upper = Rd * np.log(p0 / p2) * (t2 / 2.0)

        if t1 >= 0:
            Ep += area_lower
            En += -area_upper
        else:
            En += -area_lower
            Ep += area_upper

    return Ep, En


def classify_precip_type(Ep, En, surface_temp_c):
    """
    Coarse precip-type call from the Bourgouin-style energy areas.
    Thresholds follow the commonly-cited simplified cutoffs (Ep/En
    around 2 J/kg to separate snow from a melting warm nose, and a
    refreeze threshold around 15-20 J/kg for ice pellets vs freezing
    rain) rather than the full published nomogram, since a 6-7 point
    near-surface profile doesn't support the original curves.
    """

    if Ep < 2.0:
        return "All Snow"

    if En < 2.0:
        return "All Rain" if surface_temp_c is not None and surface_temp_c > 0 else "Rain"

    if surface_temp_c is not None and surface_temp_c <= 0:

        if En < 15.0:
            return "Freezing Rain"

        return "Ice Pellets"

    return "Rain / Wintry Mix"


def melting_layer_summary(profile):
    """
    Walks the profile from the surface upward looking for a
    sub-freezing surface layer and, above it, a warm nose aloft.
    Elevations of 0 C crossings are linearly interpolated between
    bracketing stations. Returns None for any field that doesn't
    apply (e.g. no cold layer at all).
    """

    summary = {
        "cold_layer_ft": None,
        "warm_layer_ft": None,
        "subfreezing_depth_ft": None,
        "max_warm_nose_C": None,
    }

    if profile[0]["temperature_C"] >= 0:
        return summary

    z_top = profile[0]["elevation_ft"]

    for lower, upper in zip(profile, profile[1:]):

        if upper["temperature_C"] < 0:
            z_top = upper["elevation_ft"]
            continue

        crossing = interp_crossing(
            lower["elevation_ft"], lower["temperature_C"],
            upper["elevation_ft"], upper["temperature_C"],
        )

        z_top = crossing if crossing is not None else z_top

        break

    summary["cold_layer_ft"] = (profile[0]["elevation_ft"], z_top)
    summary["subfreezing_depth_ft"] = z_top - profile[0]["elevation_ft"]

    warm_points = [
        x for x in profile
        if x["elevation_ft"] >= z_top and x["temperature_C"] > 0
    ]

    if warm_points:

        summary["warm_layer_ft"] = (
            z_top,
            max(x["elevation_ft"] for x in warm_points),
        )

        summary["max_warm_nose_C"] = max(
            x["temperature_C"] for x in warm_points
        )

    return summary


def stability_label(mean_lapse, inversion):
    """
    Coarse stability label from the bulk (base-to-summit) lapse rate,
    compared against the dry adiabatic rate (~9.8 C/km), with a note
    if any individual layer in the profile is inverted. This is a
    bulk classification across the whole observed layer, not a
    level-by-level parcel analysis.
    """

    if mean_lapse is None:
        return "Unknown", ""

    if mean_lapse >= 9.5:
        label = "Unstable"
    elif mean_lapse >= 6.5:
        label = "Near Neutral"
    elif mean_lapse > 0:
        label = "Stable"
    else:
        label = "Very Stable"

    subtext = "Shallow Inversion" if inversion else ""

    return label, subtext


def build_diagnostics(profile, rap_wind_profile=None):
    """
    Assemble the full winter-profile diagnostics dictionary from an
    elevation-sorted, pressure-populated profile.

    `rap_wind_profile` is the optional RAP wind-profile payload from
    fetch_rap_wind_profile() (a network call, done earlier in the
    fetch phase - see main()) - passed through to
    mountain_wave_potential() for critical-level detection. None is
    a perfectly valid value; that component just reports
    "unavailable" instead.
    """

    diagnostics = {}

    # ---------------- THERMAL ----------------

    freezing_points = [
        (x["elevation_ft"], x["temperature_C"])
        for x in profile
    ]

    freezing_analysis = analyze_zero_level(freezing_points)

    diagnostics["freezing_level_ft"] = freezing_analysis["level_ft"]
    diagnostics["freezing_level_status"] = freezing_analysis["status"]
    diagnostics["freezing_lower_crossing_ft"] = freezing_analysis["lower_crossing_ft"]
    diagnostics["freezing_upper_crossing_ft"] = freezing_analysis["upper_crossing_ft"]

    wetbulb_series = compute_wetbulb_series(profile)

    if len(wetbulb_series) >= 2:

        wb_points = [
            (w["elevation_ft"], w["wetbulb_C"])
            for w in wetbulb_series
        ]

        wb_analysis = analyze_zero_level(wb_points)

    else:

        wb_analysis = {
            "status": "unavailable",
            "level_ft": None,
            "lower_crossing_ft": None,
            "upper_crossing_ft": None,
        }

    diagnostics["wet_bulb_zero_ft"] = wb_analysis["level_ft"]
    diagnostics["wet_bulb_zero_status"] = wb_analysis["status"]
    diagnostics["wet_bulb_lower_crossing_ft"] = wb_analysis["lower_crossing_ft"]
    diagnostics["wet_bulb_upper_crossing_ft"] = wb_analysis["upper_crossing_ft"]

    # ---------------- THERMAL / STABILITY ----------------

    rh_values = [
        x["relative_humidity_pct"]
        for x in profile
        if x.get("relative_humidity_pct") is not None
    ]

    diagnostics["mean_relative_humidity_pct"] = (
        float(np.mean(rh_values))
        if rh_values else None
    )

    diagnostics["mean_lapse_rate_C_km"] = mean_lapse_rate(profile)
    diagnostics["max_inversion"] = max_inversion(profile)
    diagnostics["layer_lapse_rates"] = layer_lapse_rates(profile)

    stability, stability_subtext = stability_label(
        diagnostics["mean_lapse_rate_C_km"],
        diagnostics["max_inversion"],
    )

    diagnostics["stability_label"] = stability
    diagnostics["stability_subtext"] = stability_subtext

    # ---------------- WIND / TERRAIN ----------------

    diagnostics["bulk_shear_kt"] = bulk_shear(profile)

    N, U, Fr = brunt_vaisala_and_froude(profile)

    diagnostics["brunt_vaisala_N"] = N
    diagnostics["froude_number"] = Fr
    diagnostics["flow_regime"] = classify_flow_regime(Fr)

    # ---------------- MELTING LAYER / PRECIP TYPE ----------------

    melt = melting_layer_summary(profile)
    diagnostics.update(melt)

    Ep, En = bourgouin_energy(profile)

    diagnostics["positive_energy_Jkg"] = Ep
    diagnostics["negative_energy_Jkg"] = En

    diagnostics["precip_type"] = classify_precip_type(
        Ep,
        En,
        profile[0]["temperature_C"],
    )

    # ---------------- MOUNTAIN WAVE POTENTIAL ----------------
    #
    # Depends on froude_number/flow_regime/stability_label/
    # max_inversion above, so this has to run last.

    diagnostics["mountain_wave"] = mountain_wave_potential(profile, diagnostics, rap_wind_profile)

    # ---------------- RIME ICING POTENTIAL ----------------
    #
    # Fully self-contained from the observed profile - unlike
    # mountain_wave above, this needs no RAP/network data.

    diagnostics["rime_icing"] = rime_icing_potential(profile, diagnostics)

    return diagnostics
    
def diagnostic_display_rows(diagnostics):
    """
    Build the (label, value, is_section_header) rows shared by the
    console printout and the figure table.
    """

    def val(x, suffix="", decimals=1, missing="None in observed layer"):
        if x is None:
            return missing
        return f"{x:.{decimals}f}{suffix}"

    inv = diagnostics["max_inversion"]

    if inv:
        inv_text = f"+{inv['dT']:.1f} C ({inv['z1']:.0f}-{inv['z2']:.0f} ft)"
    else:
        inv_text = "None"

    mean_rh_text = val(diagnostics["mean_relative_humidity_pct"], " %", 0)

    shear_val = diagnostics["bulk_shear_kt"]
    shear_text = (
        val(shear_val, " kt", 0)
        if shear_val is not None
        else "Insufficient data"
    )

    shear_depth_ft = STATIONS[SHEAR_TOP_STID] - STATIONS[SHEAR_BASE_STID]
    shear_label = (
        f"{SHEAR_BASE_STID}\u2192{SHEAR_TOP_STID} Shear "
        f"({shear_depth_ft/1000.0:.1f} kft)"
    )

    froude_val = diagnostics["froude_number"]
    froude_text = (
        val(froude_val, "", 2)
        if froude_val is not None
        else "N/A"
    )

    rows = [
        ("THERMAL", "", True),
        ("Freezing Level", val(diagnostics["freezing_level_ft"], " ft", 0), False),
        ("Wet-Bulb Zero", val(diagnostics["wet_bulb_zero_ft"], " ft", 0), False),
        ("Mean Lapse Rate", val(diagnostics["mean_lapse_rate_C_km"], " C/km"), False),
        ("Max Inversion", inv_text, False),
        ("Mean RH", mean_rh_text, False),
        ("WIND / TERRAIN", "", True),
        (shear_label, shear_text, False),
        ("Froude Number", froude_text, False),
        ("Flow Regime", diagnostics["flow_regime"], False),
        ("MOUNTAIN WAVE", "", True),
        (
            "Wave Potential",
            f"{diagnostics['mountain_wave']['category']} "
            f"({diagnostics['mountain_wave']['score']}/{diagnostics['mountain_wave']['max_score']})",
            False,
        ),
    ]

    for reason in diagnostics["mountain_wave"]["reasons"]:
        rows.append((f"  - {reason}", "", False))

    rows.append(("RIME ICING", "", True))
    rows.append((
        "Icing Potential",
        f"{diagnostics['rime_icing']['category']} "
        f"({diagnostics['rime_icing']['score']}/{diagnostics['rime_icing']['max_score']})",
        False,
    ))

    for reason in diagnostics["rime_icing"]["reasons"]:
        rows.append((f"  - {reason}", "", False))

    return rows


def key_diagnostics_rows(diagnostics):
    """
    (label, value, subtext) rows for the "KEY DIAGNOSTICS" dashboard
    panel. Subtext is the smaller line shown under the value.
    """

    def val(x, suffix="", decimals=1):
        if x is None:
            return "None", "in observed layer"
        return f"{x:.{decimals}f}{suffix}", ""

    freezing_val, freezing_sub = val(diagnostics["freezing_level_ft"], " ft", 0)
    wbz_val, wbz_sub = val(diagnostics["wet_bulb_zero_ft"], " ft", 0)
    lapse_val, lapse_sub = val(diagnostics["mean_lapse_rate_C_km"], " C/km")

    shear_val = diagnostics["bulk_shear_kt"]
    shear_depth_ft = STATIONS[SHEAR_TOP_STID] - STATIONS[SHEAR_BASE_STID]
    shear_text = (
        f"{shear_val:.0f} kt" if shear_val is not None else "N/A"
    )

    froude_val = diagnostics["froude_number"]
    froude_text = (
        f"{froude_val:.2f}" if froude_val is not None else "N/A"
    )

    return [
        (
            "Freezing Level", freezing_val, freezing_sub,
        ),
        (
            "Wet Bulb Zero", wbz_val, wbz_sub,
        ),
        (
            "Mean Lapse Rate", lapse_val,
            f"({profile_span_label()})",
        ),
        (
            f"{shear_depth_ft/1000.0:.1f} kft Bulk Shear", shear_text, "",
        ),
        (
            "Froude Number", froude_text, diagnostics["flow_regime"],
        ),
        (
            "Precip Type", diagnostics["precip_type"], "",
        ),
    ]


def profile_span_label():
    """
    "330 ft - 3891 ft" style label for the fixed station elevations.
    """

    elevations = sorted(STATIONS.values())

    return f"{elevations[0]:.0f} ft \u2013 {elevations[-1]:.0f} ft"


def layer_summary_rows(diagnostics):
    """
    (label, value, subtext) rows for the "LAYER SUMMARY" dashboard
    panel: warm nose / cold layer detection plus the stability call.
    """

    def ft_range(pair):
        if pair is None:
            return "None", "in observed layer"
        return f"{pair[1] - pair[0]:.0f} ft thick", f"{pair[0]:.0f}-{pair[1]:.0f} ft"

    warm_val, warm_sub = ft_range(diagnostics["warm_layer_ft"])
    cold_val, cold_sub = ft_range(diagnostics["cold_layer_ft"])

    subfreezing = diagnostics["subfreezing_depth_ft"]
    subfreezing_val = (
        f"{subfreezing:.0f} ft" if subfreezing is not None else "None"
    )
    subfreezing_sub = "" if subfreezing is not None else "in observed layer"

    warm_nose = diagnostics["max_warm_nose_C"]
    warm_nose_val = (
        f"{warm_nose:.1f} C" if warm_nose is not None else "N/A"
    )

    return [
        ("Warm Layer", warm_val, warm_sub),
        ("Cold Layer", cold_val, cold_sub),
        ("Subfreezing Depth", subfreezing_val, subfreezing_sub),
        ("Max Warm Nose", warm_nose_val, ""),
        (
            "Stability", diagnostics["stability_label"],
            diagnostics["stability_subtext"],
        ),
    ]


def print_diagnostics(diagnostics):

    rows = diagnostic_display_rows(diagnostics)

    print()
    print("=" * 60)
    print("WINTER PROFILE DIAGNOSTICS")
    print("=" * 60)

    for label, value, is_header in rows:

        if is_header:
            print(f"\n{label}")
        elif value == "":
            print(f"  {label}")
        else:
            print(f"  {label:<22}: {value}")

    print("=" * 60)

# =====================================================================
# 12. PLOT
# =====================================================================

MUTED_TEXT = "#868e96"
DIVIDER_COLOR = "#dee2e6"
CARD_BG = "#f8f9fa"

TEMP_COLOR = "red"
DEWPOINT_COLOR = "green"
WETBULB_COLOR = "purple"


def _icon_thermometer(ax, color):

    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    ax.set_aspect("equal")

    ax.plot(
        [0.5, 0.5], [0.28, 0.82],
        color=color, linewidth=5, solid_capstyle="round",
    )

    ax.add_patch(Circle((0.5, 0.22), 0.16, color=color))
    ax.add_patch(Circle((0.5, 0.55), 0.055, color="white"))


def _icon_wind(ax, color):

    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    ax.set_aspect("equal")

    for y0, length in [(0.68, 0.5), (0.48, 0.7), (0.28, 0.4)]:

        x = np.linspace(0.12, 0.12 + length, 20)
        yv = y0 + 0.035 * np.sin(np.linspace(0, np.pi, 20))

        ax.plot(x, yv, color=color, linewidth=3.2, solid_capstyle="round")


def _icon_mountain(ax, color):

    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    ax.set_aspect("equal")

    ax.add_patch(
        Polygon(
            [(0.08, 0.15), (0.5, 0.85), (0.92, 0.15)],
            closed=True, color=color,
        )
    )

    ax.add_patch(
        Polygon(
            [(0.4, 0.62), (0.5, 0.78), (0.6, 0.62), (0.5, 0.68)],
            closed=True, color="white",
        )
    )


def _icon_snowflake(ax, color):

    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")

    ax.text(
        0.5, 0.46, "\u2744",
        fontsize=26, ha="center", va="center", color=color,
    )


def _icon_droplet(ax, color):

    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    ax.set_aspect("equal")

    ax.add_patch(Circle((0.5, 0.33), 0.30, color=color))

    ax.add_patch(
        Polygon(
            [(0.5, 0.92), (0.26, 0.42), (0.74, 0.42)],
            closed=True, color=color,
        )
    )


def _build_diagnostic_cards(diagnostics):
    """
    Build the (icon_fn, icon_color, label, value, value_color,
    subtext) tuples for the diagnostic icon-card row, from a
    diagnostics dict as returned by build_diagnostics().
    """

    def val(x, suffix="", decimals=1):
        if x is None:
            return "\u2014", "None in layer"
        return f"{x:.{decimals}f}{suffix}", ""

    lapse_val, lapse_sub = val(
        diagnostics["mean_lapse_rate_C_km"], " \u00b0C/km"
    )

    shear_val_num = diagnostics["bulk_shear_kt"]
    shear_val = f"{shear_val_num:.0f} kt" if shear_val_num is not None else "\u2014"
    shear_sub = f"{STATIONS[SHEAR_BASE_STID]:.0f}\u2013{STATIONS[SHEAR_TOP_STID]:.0f} ft"

    froude_num = diagnostics["froude_number"]
    froude_val = f"{froude_num:.2f}" if froude_num is not None else "\u2014"
    froude_sub = diagnostics["flow_regime"]

    freezing_val, freezing_sub = val(diagnostics["freezing_level_ft"], " ft", 0)
    wbz_val, wbz_sub = val(diagnostics["wet_bulb_zero_ft"], " ft", 0)

    return [
        (
            _icon_thermometer, "#d9480f", "MEAN LAPSE RATE",
            lapse_val, "#d9480f",
            profile_span_label() if not lapse_sub else lapse_sub,
        ),
        (
            _icon_wind, "#1971c2",
            "BULK SHEAR",
            shear_val, "#1971c2", shear_sub,
        ),
        (
            _icon_mountain, "#7048e8", "FROUDE NUMBER",
            froude_val, "#7048e8", froude_sub,
        ),
        (
            _icon_snowflake, "#1c7ed6", "FREEZING LEVEL",
            freezing_val, "#e8590c" if freezing_val == "\u2014" else "#1c7ed6",
            freezing_sub,
        ),
        (
            _icon_droplet, "#1c7ed6", "WET-BULB ZERO",
            wbz_val, "#e8590c" if wbz_val == "\u2014" else "#1c7ed6",
            wbz_sub,
        ),
    ]


def _draw_diagnostic_cards(fig, rect, cards):
    """
    Draw a row of icon diagnostic cards spanning `rect` (figure
    fraction [x, y, w, h]). `cards` is a list of
    (icon_fn, icon_color, label, value, value_color, subtext).
    """

    x0, y0, w, h = rect

    band_ax = fig.add_axes(rect)
    band_ax.set_xlim(0, 1)
    band_ax.set_ylim(0, 1)
    band_ax.axis("off")

    band = FancyBboxPatch(
        (0.0, 0.0), 1.0, 1.0,
        boxstyle="round,pad=0,rounding_size=0.12",
        linewidth=1.0,
        edgecolor=DIVIDER_COLOR,
        facecolor=CARD_BG,
        transform=band_ax.transAxes,
        zorder=0,
    )

    band_ax.add_patch(band)

    fig_w_in, fig_h_in = fig.get_size_inches()

    n = len(cards)
    card_w = w / n

    for i, (icon_fn, icon_color, label, value, value_color, subtext) in enumerate(cards):

        cx0 = x0 + i * card_w

        if i > 0:

            band_ax.plot(
                [i / n, i / n], [0.18, 0.82],
                color=DIVIDER_COLOR, linewidth=1,
                transform=band_ax.transAxes,
            )

        icon_h_frac = h * 0.30
        icon_h_in = icon_h_frac * fig_h_in
        icon_w_frac = icon_h_in / fig_w_in

        icon_x = cx0 + card_w * 0.07
        icon_y = y0 + (h - icon_h_frac) / 2

        icon_ax = fig.add_axes(
            [icon_x, icon_y, icon_w_frac, icon_h_frac]
        )

        icon_fn(icon_ax, icon_color)

        text_x = icon_x + icon_w_frac + card_w * 0.06

        fig.text(
            text_x, y0 + h * 0.80, label,
            fontsize=9, color=MUTED_TEXT, ha="left", va="center",
            fontweight="bold",
        )

        fig.text(
            text_x, y0 + h * 0.50, value,
            fontsize=15, color=value_color, ha="left", va="center",
            fontweight="bold",
        )

        if subtext:

            fig.text(
                text_x, y0 + h * 0.16, subtext,
                fontsize=7.5, color=MUTED_TEXT, ha="left", va="center",
            )


def plot_skewt(
    profile,
    pressure,
    temperature,
    wind_pressure,
    u,
    v,
    diagnostics,
):

    # ==============================================================
    # PLOT LIMITS (computed before the figure, since figure sizing
    # below depends on them)
    # ==============================================================

    p_max = pressure.max().to("hPa").m
    p_min = pressure.min().to("hPa").m

    t_max = temperature.max().to("degC").m
    t_min = temperature.min().to("degC").m

    dewpoints = [
        station["dewpoint_C"]
        for station in profile
        if station.get("dewpoint_C") is not None
    ]

    if dewpoints:
        t_min = min(t_min, min(dewpoints))

    # ==============================================================
    # FIXED PRESSURE / TEMPERATURE WINDOW
    # ==============================================================
    #
    # These are constants, not derived from today's data - that's
    # the actual fix for the PNG changing size run to run. The
    # output's shape was never determined directly by the data; it
    # was determined by the PRESSURE and TEMPERATURE RANGES fed into
    # the sizing probe below, and those ranges used to come straight
    # from bottom_pressure/top_pressure/min_width, which varied with
    # KBTV's actual pressure and the day's actual temperature spread.
    # Fix those three inputs and the probe computes the same
    # "natural aspect" every time, which means skew_width_in,
    # skew_height_in, fig_width_in, and fig_height_in all come out
    # identical run after run - a real plug-and-play PNG.
    #
    # These are FLOORS, not hard caps: if one unusual day's data
    # genuinely needs more room to avoid clipping an actual
    # observation, the window widens/heightens just for that day
    # rather than silently cutting real weather data off screen. A
    # rare size deviation on an extreme day is a far smaller problem
    # on a dashboard than a hidden clipped point - but it should be
    # rare enough in practice that the output is effectively fixed.

    FIXED_BOTTOM_PRESSURE_HPA = 1040.0  # comfortably above any realistic KBTV (330 ft) station pressure
    FIXED_TOP_PRESSURE_HPA = 860.0      # tightened from 880 so it actually binds instead of getting overridden by a typical day's data
    FIXED_TEMP_WIDTH_C = 18.0           # narrower floor for a taller chart - height is derived from width, so this trades some x-axis padding for more vertical inches

    bottom_pressure = max(FIXED_BOTTOM_PRESSURE_HPA, p_max + 15.0)
    top_pressure = min(FIXED_TOP_PRESSURE_HPA, p_min - 15.0)

    if (
        bottom_pressure != FIXED_BOTTOM_PRESSURE_HPA
        or top_pressure != FIXED_TOP_PRESSURE_HPA
    ):
        print(
            f"[SIZING] pressure window widened beyond the fixed "
            f"defaults today: bottom={bottom_pressure:.1f} hPa "
            f"(fixed={FIXED_BOTTOM_PRESSURE_HPA}), "
            f"top={top_pressure:.1f} hPa (fixed={FIXED_TOP_PRESSURE_HPA})"
        )

    skew_points = []

    for station in profile:

        skew_points.append((station["temperature_C"], station["pressure_hPa"]))

        if station.get("dewpoint_C") is not None:
            skew_points.append((station["dewpoint_C"], station["pressure_hPa"]))

        if station.get("wetbulb_C") is not None:
            skew_points.append((station["wetbulb_C"], station["pressure_hPa"]))

    # ==============================================================
    # SKEW-T BOX SIZE - DERIVED FROM THE LOCKED ASPECT
    # ==============================================================
    #
    # SkewT's aspect lock (ax.set_aspect(80.5, adjustable='box')) is
    # what keeps the 45-degree skew lines geometrically true - it's
    # not optional cosmetic behavior, it's part of what makes a
    # Skew-T a Skew-T. Disabling it corrupts the coordinate math
    # (confirmed directly - it broke compute_skew_corrected_xlim's
    # shift-correction, which assumes the transform is affine and
    # consistent between its probe and the final render).
    #
    # Width is the fixed anchor, height is derived from a probe, and
    # neither is clamped, since a guessed clamp is exactly what
    # caused an earlier round of this same bug. With the pressure
    # and temperature window now fixed too (see above), this probe
    # measures the same ratio every run, so the derived dimensions
    # are effectively fixed as well - not just "wide", but the SAME
    # wide every time.

    TARGET_SKEW_WIDTH_IN = 13.0

    rough_left, rough_right = compute_skew_corrected_xlim(
        bottom_pressure, top_pressure, skew_points,
        box_width_in=12.0, box_height_in=12.0, min_width=FIXED_TEMP_WIDTH_C,
    )

    height_to_width = measure_skewt_height_to_width_ratio(
        bottom_pressure, top_pressure, rough_left, rough_right,
    )

    skew_width_in = TARGET_SKEW_WIDTH_IN
    skew_height_in = skew_width_in * height_to_width

    print(
        f"[SIZING] height_to_width={height_to_width:.3f}  "
        f"skew_width_in={skew_width_in:.2f}  "
        f"skew_height_in={skew_height_in:.2f}"
    )

    # Final xlim, corrected against the box we're ACTUALLY going to
    # use - has to match, not just be close (see
    # compute_skew_corrected_xlim's own docstring).
    left_temperature, right_temperature = compute_skew_corrected_xlim(
        bottom_pressure, top_pressure, skew_points,
        box_width_in=skew_width_in, box_height_in=skew_height_in,
        min_width=FIXED_TEMP_WIDTH_C,
    )


    # ==============================================================
    # FIGURE SIZING
    # ==============================================================
    #
    # header_in/gap1_in/bottom_margin_in/left_margin_in/
    # right_margin_in/wind_col_in are fixed chrome around the chart;
    # skew_width_in/skew_height_in (above) are now correctly derived
    # rather than guessed, so the total figure size below just adds
    # a small, constant amount of margin on top of them.

    wind_col_in = 0.9
    content_gap_in = 0.15
    content_width_in = skew_width_in + content_gap_in + wind_col_in

    # Asymmetric, not a symmetric fraction of total width - the
    # y-axis pressure labels live only on the left, and a 4-digit
    # value (any bottom_pressure >= 1000 hPa, which is a completely
    # ordinary sea-level-ish reading) needs more room than a 3-digit
    # one. A single symmetric margin sized for 3 digits clipped every
    # 4-digit label by several pixels - confirmed directly by
    # measuring rendered tick label bounding boxes against the figure
    # edge, not just eyeballing it. The right side only needs to
    # clear the wind barb column, which is a fixed-width axes, not
    # text that grows with the data - a smaller margin is fine there.
    left_margin_in = 0.78
    right_margin_in = 0.20

    header_in = 0.62
    gap1_in = 0.08
    bottom_margin_in = 0.52

    fig_width_in = left_margin_in + content_width_in + right_margin_in

    fig_height_in = header_in + gap1_in + skew_height_in + bottom_margin_in

    fig = plt.figure(figsize=(fig_width_in, fig_height_in))

    content_x0 = left_margin_in / fig_width_in

    skew_width_frac = skew_width_in / fig_width_in
    skew_height_frac = skew_height_in / fig_height_in

    skew_y0 = bottom_margin_in / fig_height_in

    skew = SkewT(
        fig,
        rotation=45,
        rect=(content_x0, skew_y0, skew_width_frac, skew_height_frac),
    )

    skew.ax.set_ylim(bottom_pressure, top_pressure)
    skew.ax.set_xlim(left_temperature, right_temperature)

    # Round-number temperature gridlines, spaced dynamically so a
    # tightly zoomed range (e.g. 8 C wide) still gets a reasonable
    # number of ticks instead of MetPy's fixed 10 C default locator
    # leaving only one or two.

    temp_range = right_temperature - left_temperature
    temp_tick_interval = choose_tick_interval(temp_range)

    xtick_start = math.ceil(left_temperature / temp_tick_interval) * temp_tick_interval

    xticks = []
    xtick = xtick_start

    while xtick <= right_temperature:

        xticks.append(xtick)
        xtick += temp_tick_interval

    xtick_decimals = 1 if temp_tick_interval < 1 else 0

    skew.ax.xaxis.set_major_locator(FixedLocator(xticks))
    skew.ax.xaxis.set_major_formatter(
        FixedFormatter([f"{t:.{xtick_decimals}f}" for t in xticks])
    )

    # Round-number pressure gridlines (every 20 hPa) anchored to
    # KBTV's actual surface pressure.

    kbtv_station = next(
        (x for x in profile if x["stid"] == "KBTV"), profile[0]
    )

    tick_base = 20.0 * round(kbtv_station["pressure_hPa"] / 20.0)

    yticks = []
    tick = tick_base

    while tick >= top_pressure - 20:

        if tick <= bottom_pressure + 20:
            yticks.append(tick)

        tick -= 20

    # FixedLocator/FixedFormatter (not plain set_yticks/set_yticklabels)
    # so these persist through any later redraw - a log-scale y-axis
    # otherwise falls back to matplotlib's default scientific-notation
    # formatter (the "9 x 10^2" style labels), which is what was
    # actually showing up instead of our intended round-number labels.
    # Minor ticks/labels are explicitly disabled for the same reason -
    # the log scale enables them by default with their own formatter.

    skew.ax.yaxis.set_major_locator(FixedLocator(yticks))
    skew.ax.yaxis.set_major_formatter(FixedFormatter([f"{t:.0f}" for t in yticks]))
    skew.ax.yaxis.set_minor_locator(NullLocator())
    skew.ax.yaxis.set_minor_formatter(NullFormatter())

    # ==============================================================
    # SKEW-T BACKGROUND
    # ==============================================================

    skew.plot_dry_adiabats(alpha=0.20)
    skew.plot_moist_adiabats(alpha=0.15)
    skew.plot_mixing_lines(alpha=0.12)

    # Subtle red shading over any layer where temperature increases
    # with height (an inversion) - every such layer, not just the
    # single strongest one, since a profile can have more than one.

    for p_low, p_high in inversion_pressure_bands(profile):

        skew.ax.axhspan(
            p_low, p_high,
            color="red", alpha=0.08, zorder=1, linewidth=0,
        )

    # ==============================================================
    # TEMPERATURE / DEWPOINT / WET-BULB
    # ==============================================================

    skew.plot(
        pressure, temperature,
        color=TEMP_COLOR, linewidth=3, marker="o", markersize=9,
        zorder=10, label="Temperature (\u00b0C)",
    )

    dewpoint_pressure = []
    dewpoint_temperature = []

    for station in profile:

        td = station.get("dewpoint_C")

        if td is None:
            continue

        dewpoint_pressure.append(station["pressure_hPa"])
        dewpoint_temperature.append(td)

    if len(dewpoint_temperature) >= 2:

        skew.plot(
            np.array(dewpoint_pressure) * units.hPa,
            np.array(dewpoint_temperature) * units.degC,
            color=DEWPOINT_COLOR, linewidth=3, marker="o", markersize=8,
            zorder=10, label="Dewpoint (\u00b0C)",
        )

    wetbulb_pressure = []
    wetbulb_temperature = []

    for station in profile:

        tw = station.get("wetbulb_C")

        if tw is None:
            continue

        wetbulb_pressure.append(station["pressure_hPa"])
        wetbulb_temperature.append(tw)

    if len(wetbulb_temperature) >= 2:

        skew.plot(
            np.array(wetbulb_pressure) * units.hPa,
            np.array(wetbulb_temperature) * units.degC,
            color=WETBULB_COLOR, linewidth=2, linestyle="--",
            marker="o", markersize=6, alpha=0.85, zorder=9,
            label="Wet-Bulb (\u00b0C)",
        )

    if left_temperature <= 0 <= right_temperature:

        skew.ax.axvline(
            0, color="black", linestyle="-", linewidth=1.2,
            alpha=0.6, zorder=5,
        )

    skew.ax.set_xlabel("Temperature (\u00b0C)", fontsize=12)
    skew.ax.set_ylabel("Pressure (hPa)", fontsize=12)

    legend = skew.ax.legend(
        loc="upper left", fontsize=10, framealpha=0.95,
        edgecolor=DIVIDER_COLOR,
    )

    # ==============================================================
    # SUMMARY BOXES (mountain wave + rime icing, upper-right of chart)
    # ==============================================================
    #
    # Anchored by their RIGHT edge in the chart's upper-right corner
    # - the legend already claims upper-left, and there's reliably
    # open gridded space here on a chart this wide. Box WIDTH is not
    # guessed: text is placed first, then fig.canvas.draw() forces a
    # real render so each line's actual pixel width can be measured
    # via get_window_extent() and converted back to axes-fraction
    # units - same probe-then-measure approach
    # compute_skew_corrected_xlim uses elsewhere in this file, just
    # measuring rendered text instead of data-point positions. A
    # fixed guessed width either leaves blank space (too wide) or
    # clips text (too narrow); this makes the box hug whichever line
    # is actually longest, so it never covers more of the profile
    # than the content genuinely needs.

    category_colors = {
        "Low": "#2f9e44",
        "Moderate": "#d9822b",
        "High": "#e03131",
        "Indeterminate": MUTED_TEXT,
    }

    box_right = 0.985
    box_top = 0.98
    box_height = 0.12
    box_gap = 0.012
    box_pad = 0.014  # inset between the text and the box's own edge

    wave = diagnostics.get("mountain_wave")
    rime = diagnostics.get("rime_icing")

    wave_texts = []
    rime_texts = []

    if wave is not None:

        wave_color = category_colors.get(wave["category"], MUTED_TEXT)

        critical_level = wave.get("critical_level")

        if critical_level is not None:
            wave_detail = (
                f"Critical level: {critical_level['height_ft']:.0f} ft "
                f"({critical_level['reason']})"
            )
        else:
            wave_detail = "No critical level in RAP profile"

        wave_texts = [
            skew.ax.text(
                box_right - box_pad, box_top - 0.022,
                "MOUNTAIN WAVE POTENTIAL",
                transform=skew.ax.transAxes,
                fontsize=6.5, fontweight="bold", color=MUTED_TEXT,
                ha="right", va="top", zorder=20,
            ),
            skew.ax.text(
                box_right - box_pad, box_top - 0.055,
                f"{wave['category']}  ({wave['score']}/{wave['max_score']})",
                transform=skew.ax.transAxes,
                fontsize=10.5, fontweight="bold", color=wave_color,
                ha="right", va="top", zorder=20,
            ),
            skew.ax.text(
                box_right - box_pad, box_top - 0.093,
                wave_detail,
                transform=skew.ax.transAxes,
                fontsize=6, color=MUTED_TEXT,
                ha="right", va="top", zorder=20,
            ),
        ]

    rime_box_top = (box_top - box_height - box_gap) if wave is not None else box_top

    if rime is not None:

        rime_color = category_colors.get(rime["category"], MUTED_TEXT)

        # Lead with the moisture reason (component 2) rather than
        # the first reason in the list - it's the load-bearing
        # ingredient (see rime_icing_potential's docstring), so it's
        # the most useful single line to show when space only
        # allows one.
        rime_detail = rime["reasons"][1] if len(rime["reasons"]) > 1 else ""

        rime_texts = [
            skew.ax.text(
                box_right - box_pad, rime_box_top - 0.022,
                "RIME ICING POTENTIAL",
                transform=skew.ax.transAxes,
                fontsize=6.5, fontweight="bold", color=MUTED_TEXT,
                ha="right", va="top", zorder=20,
            ),
            skew.ax.text(
                box_right - box_pad, rime_box_top - 0.055,
                f"{rime['category']}  ({rime['score']}/{rime['max_score']})",
                transform=skew.ax.transAxes,
                fontsize=10.5, fontweight="bold", color=rime_color,
                ha="right", va="top", zorder=20,
            ),
            skew.ax.text(
                box_right - box_pad, rime_box_top - 0.093,
                rime_detail,
                transform=skew.ax.transAxes,
                fontsize=6, color=MUTED_TEXT,
                ha="right", va="top", zorder=20,
            ),
        ]

    # Force a real render so the text objects above have actual
    # pixel extents to measure - matplotlib can't report layout
    # sizes for text that's never been drawn.

    if wave_texts or rime_texts:

        fig.canvas.draw()
        renderer = fig.canvas.get_renderer()

        ax_width_px = skew.ax.get_window_extent(renderer=renderer).width

        def widest_line_axes_fraction(text_objs):

            widest_px = max(
                t.get_window_extent(renderer=renderer).width
                for t in text_objs
            )

            return widest_px / ax_width_px

        if wave_texts:

            wave_box_width = widest_line_axes_fraction(wave_texts) + 2 * box_pad
            wave_box_left = box_right - wave_box_width

            skew.ax.add_patch(
                FancyBboxPatch(
                    (wave_box_left, box_top - box_height), wave_box_width, box_height,
                    boxstyle="round,pad=0.01,rounding_size=0.015",
                    transform=skew.ax.transAxes,
                    linewidth=1.0,
                    edgecolor=DIVIDER_COLOR,
                    facecolor="white",
                    alpha=0.92,
                    zorder=19,
                )
            )

        if rime_texts:

            rime_box_width = widest_line_axes_fraction(rime_texts) + 2 * box_pad
            rime_box_left = box_right - rime_box_width

            skew.ax.add_patch(
                FancyBboxPatch(
                    (rime_box_left, rime_box_top - box_height), rime_box_width, box_height,
                    boxstyle="round,pad=0.01,rounding_size=0.015",
                    transform=skew.ax.transAxes,
                    linewidth=1.0,
                    edgecolor=DIVIDER_COLOR,
                    facecolor="white",
                    alpha=0.92,
                    zorder=19,
                )
            )

    # ==============================================================
    # WIND COLUMN (separate axes, plain upright barbs)
    # ==============================================================

    wind_x0 = content_x0 + skew_width_frac + content_gap_in / fig_width_in
    wind_width_frac = wind_col_in / fig_width_in

    wind_ax = fig.add_axes(
        [wind_x0, skew_y0, wind_width_frac, skew_height_frac],
        sharey=skew.ax,
    )

    wind_ax.set_ylim(bottom_pressure, top_pressure)
    wind_ax.set_xlim(-1, 1)
    wind_ax.set_xticks([])

    for spine in wind_ax.spines.values():
        spine.set_visible(False)

    wind_ax.tick_params(left=False, labelleft=False)

    wind_ax.text(
        0.0, 1.02, "WIND",
        transform=wind_ax.transAxes,
        fontsize=11, fontweight="bold", ha="center", va="bottom",
    )

    if wind_pressure is not None:

        wind_ax.barbs(
            np.zeros(len(wind_pressure)),
            wind_pressure.to("hPa").m,
            u.to("knots").m,
            v.to("knots").m,
            length=6.5, linewidth=1.2,
        )

    # ==============================================================
    # HEADER
    # ==============================================================

    fig.text(
        0.03, (fig_height_in - 0.20) / fig_height_in,
        "MOUNT MANSFIELD OBSERVED SLOPE PROFILE",
        fontsize=13, fontweight="bold", color="black",
        ha="left", va="top",
    )

    fig.text(
        0.03, (fig_height_in - 0.44) / fig_height_in,
        profile_span_label(),
        fontsize=9, color=MUTED_TEXT,
        ha="left", va="top",
    )

    latest_times = []

    for station in profile:

        dt = parse_iso_time(station.get("temperature_time"))

        if dt is not None:
            latest_times.append(dt)

    if latest_times:

        newest = max(latest_times)

        fig.text(
            1.0 - right_margin_in / fig_width_in, (fig_height_in - 0.22) / fig_height_in,
            newest.strftime("%d %b %Y %H:%M UTC"),
            fontsize=11, fontweight="bold", color="black",
            ha="right", va="top",
        )

    # ==============================================================
    # SAVE
    # ==============================================================

    print(
        f"[SIZING] final fig_width_in={fig_width_in:.2f}  "
        f"fig_height_in={fig_height_in:.2f}  "
        f"aspect={fig_width_in / fig_height_in:.3f}"
    )

    plt.savefig(OUTPUT_FILE, dpi=175)
    plt.close(fig)

    print()
    print(f"Saved Skew-T to: {OUTPUT_FILE}")


def plot_diagnostics_image(diagnostics):
    """
    Render the diagnostic icon-card row as its own image
    (DIAGNOSTICS_OUTPUT_FILE), separate from the Skew-T. Sized
    comfortably rather than squeezed to a fraction of the chart's
    width, since it's no longer sharing a figure with it - the tiny
    fonts from the squeeze-to-75%-of-chart-width experiment aren't
    needed here.
    """

    cards = _build_diagnostic_cards(diagnostics)

    fig_width_in = 9.5
    icon_row_in = 1.6
    footer_in = 0.30

    fig = plt.figure(figsize=(fig_width_in, icon_row_in + footer_in))

    icon_row_y0 = footer_in / (icon_row_in + footer_in)
    icon_row_height = icon_row_in / (icon_row_in + footer_in)

    _draw_diagnostic_cards(
        fig,
        (0.02, icon_row_y0, 0.96, icon_row_height),
        cards,
    )

    fig.text(
        0.5, footer_in / (icon_row_in + footer_in) * 0.4,
        "Data sources:  NWS API (BTV) \u2022 RR2 \u2022 RRSBTV (IEM)",
        fontsize=8, color=MUTED_TEXT, ha="center", va="center",
        style="italic",
    )

    plt.savefig(DIAGNOSTICS_OUTPUT_FILE, dpi=175)
    plt.close(fig)

    print()
    print(f"Saved diagnostics cards to: {DIAGNOSTICS_OUTPUT_FILE}")


def plot_station_table(profile):
    """
    Render the station observation table as its own image
    (TABLE_OUTPUT_FILE), separate from the Skew-T. Split out so the
    dashboard can lay the two out independently - e.g. show the chart
    prominently and the table as a click-to-expand detail - and so
    the Skew-T itself isn't forced to reserve vertical space for a
    table that doesn't need to share its aspect ratio.
    """

    table_rows = []

    for station in reversed(profile):

        temp = station.get("temperature_C")
        temp_text = f"{temp:.1f}" if temp is not None else "--"

        dewpoint = station.get("dewpoint_C")
        dewpoint_text = f"{dewpoint:.1f}" if dewpoint is not None else "--"

        wetbulb = station.get("wetbulb_C")
        wetbulb_text = f"{wetbulb:.1f}" if wetbulb is not None else "--"

        speed = station.get("wind_speed_kmh")
        direction = station.get("wind_direction_deg")

        if speed is not None and direction is not None:

            speed_kt = (speed * units("km/hour")).to("knots").m

            if speed_kt < 0.5:
                wind_text = "Calm"
            else:
                wind_text = f"{direction:03.0f}\u00b0 / {speed_kt:.0f} kt"

        else:
            wind_text = "--"

        obs_time = parse_iso_time(station.get("temperature_time"))
        time_text = obs_time.strftime("%H:%MZ") if obs_time else "--"

        table_rows.append([
            station["stid"],
            f"{station['elevation_ft']:.0f}",
            f"{station['pressure_hPa']:.1f}",
            temp_text,
            dewpoint_text,
            wetbulb_text,
            wind_text,
            time_text,
        ])

    n_rows = len(table_rows)

    # Sized to the content rather than a fixed canvas - a handful of
    # inches of height per row plus a small margin, so this doesn't
    # carry the large fixed whitespace a fixed-size figure would.

    fig_width_in = 11.5
    fig_height_in = 0.55 + 0.42 * (n_rows + 1)

    fig = plt.figure(figsize=(fig_width_in, fig_height_in))

    table_ax = fig.add_axes([0.02, 0.04, 0.96, 0.90])
    table_ax.axis("off")

    table_ax.text(
        0.0, 1.06, "STATION OBSERVATIONS",
        transform=table_ax.transAxes,
        fontsize=12, fontweight="bold", color="black",
        ha="left", va="bottom",
    )

    col_labels = [
        "Station", "Elev (ft)", "Pressure (hPa)", "Temp (\u00b0C)",
        "Dewpoint (\u00b0C)", "Wet Bulb (\u00b0C)", "Wind (dir / spd)",
        "Time (UTC)",
    ]

    table = table_ax.table(
        cellText=table_rows,
        colLabels=col_labels,
        cellLoc="center", colLoc="center", loc="center",
    )

    table.auto_set_font_size(False)
    table.set_fontsize(10)
    table.scale(1.0, 1.7)

    n_cols = len(col_labels)

    for col in range(n_cols):

        header_cell = table[(0, col)]
        header_cell.set_text_props(weight="bold")
        header_cell.set_edgecolor(DIVIDER_COLOR)

    col_text_colors = {3: TEMP_COLOR, 4: DEWPOINT_COLOR, 5: WETBULB_COLOR}

    for row in range(1, len(table_rows) + 1):

        for col in range(n_cols):

            cell = table[(row, col)]
            cell.set_edgecolor(DIVIDER_COLOR)
            cell.get_text().set_color(col_text_colors.get(col, "black"))

    plt.savefig(TABLE_OUTPUT_FILE, dpi=175)
    plt.close(fig)

    print()
    print(f"Saved station table to: {TABLE_OUTPUT_FILE}")





# =====================================================================
# 13B. GOOGLE DRIVE UPLOAD
# =====================================================================

def get_drive_service():
    """
    Build an authenticated Drive API client from the GOOGLE_CREDENTIALS
    secret (a service account JSON key, same as main.py uses).
    """

    creds_raw = os.environ.get("GOOGLE_CREDENTIALS")

    if not creds_raw:

        raise RuntimeError(
            "GOOGLE_CREDENTIALS is not set - cannot authenticate to "
            "Google Drive."
        )

    creds_dict = json.loads(creds_raw)

    credentials = service_account.Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/drive"],
    )

    return build("drive", "v3", credentials=credentials)


def upload_to_drive(filepath, folder_id, filename=None):
    """
    Upload filepath to the given Drive folder, updating an existing
    file of the same name in place if one is already there instead of
    creating a new copy on every scheduled run.
    """

    if not folder_id:

        print(
            "WARNING: no Drive folder ID configured "
            "(MANSFIELD_DRIVE_FOLDER_ID / GLWU_DRIVE_FOLDER_ID) - "
            "skipping upload."
        )

        return

    filename = filename or os.path.basename(filepath)

    service = get_drive_service()

    query = (
        f"name = '{filename}' "
        f"and '{folder_id}' in parents "
        f"and trashed = false"
    )

    existing = service.files().list(
        q=query,
        fields="files(id, name)",
        spaces="drive",
    ).execute().get("files", [])

    media = MediaFileUpload(filepath, mimetype="image/png")

    if existing:

        file_id = existing[0]["id"]

        service.files().update(
            fileId=file_id,
            media_body=media,
        ).execute()

        print(f"Updated existing Drive file '{filename}' ({file_id})")

    else:

        metadata = {
            "name": filename,
            "parents": [folder_id],
        }

        created = service.files().create(
            body=metadata,
            media_body=media,
            fields="id",
        ).execute()

        print(f"Uploaded new Drive file '{filename}' ({created['id']})")


# =====================================================================
# 12B. COMPACT DASHBOARD CARD
# =====================================================================
#
# A second, purpose-built output alongside the full Skew-T: sized and
# styled to sit in a dashboard grid next to the existing station
# cards. Deliberately drops everything that only earns its place at
# "study this closely" size - the background adiabat/mixing lines,
# the station table, the wind barb column, dewpoint - and keeps only
# what answers "rain, snow, or something worse, and where."

CARD_BG_COLOR = "white"
CARD_BORDER_NORMAL = "#dee2e6"
CARD_BORDER_RISK = "#c1590f"
CARD_MUTED = "#6b7688"
CARD_BLUE = "#1e5fa8"
CARD_SNOW = "#3f7cc9"
CARD_RAIN = "#c1590f"


def temperature_sign_bands(profile):
    """
    Walk the elevation-sorted profile and return a list of
    (elev_low, elev_high, is_above_freezing) bands, splitting at any
    0 C crossing between stations (interpolated). Used to shade the
    compact card's background by freezing/non-freezing layer.
    """

    bands = []

    for lower, upper in zip(profile, profile[1:]):

        z1, t1 = lower["elevation_ft"], lower["temperature_C"]
        z2, t2 = upper["elevation_ft"], upper["temperature_C"]

        if (t1 >= 0) == (t2 >= 0):

            bands.append((z1, z2, t1 >= 0))
            continue

        crossing = interp_crossing(z1, t1, z2, t2, 0.0)

        if crossing is None:

            bands.append((z1, z2, t1 >= 0))
            continue

        bands.append((z1, crossing, t1 >= 0))
        bands.append((crossing, z2, t2 >= 0))

    return bands


def plot_dashboard_card(profile, diagnostics):
    """
    Render the compact dashboard card to CARD_OUTPUT_FILE.
    """

    fig = plt.figure(figsize=(4.0, 5.0))

    precip_type = diagnostics["precip_type"]
    has_warm_nose = diagnostics["warm_layer_ft"] is not None

    border_color = CARD_BORDER_RISK if has_warm_nose else CARD_BORDER_NORMAL

    # Card background + left accent border, matching the existing
    # dashboard's card vernacular (white card, colored left border).

    outer_ax = fig.add_axes([0, 0, 1, 1])
    outer_ax.set_xlim(0, 1)
    outer_ax.set_ylim(0, 1)
    outer_ax.axis("off")

    outer_ax.add_patch(
        FancyBboxPatch(
            (0.02, 0.02), 0.96, 0.96,
            boxstyle="round,pad=0,rounding_size=0.02",
            linewidth=1.2,
            edgecolor=CARD_BORDER_NORMAL,
            facecolor=CARD_BG_COLOR,
            zorder=0,
        )
    )

    outer_ax.add_patch(
        plt.Rectangle(
            (0.02, 0.02), 0.018, 0.96,
            facecolor=border_color,
            edgecolor="none",
            zorder=1,
        )
    )

    # ---------------- Header ----------------

    outer_ax.text(
        0.53, 0.955, "MOUNT MANSFIELD",
        fontsize=13, fontweight="bold", color="black",
        ha="center", va="top",
    )

    outer_ax.text(
        0.53, 0.915, "Green Mountains",
        fontsize=9.5, fontweight="bold", color=CARD_BLUE,
        ha="center", va="top",
    )

    if has_warm_nose:

        outer_ax.text(
            0.53, 0.865, "\u26a0 FZRA RISK",
            fontsize=9, fontweight="bold", color=CARD_BORDER_RISK,
            ha="center", va="top",
            bbox=dict(
                boxstyle="round,pad=0.35",
                facecolor="#fdeee0",
                edgecolor="none",
            ),
        )

    # ---------------- Simplified elevation profile ----------------

    chart_ax = fig.add_axes([0.20, 0.30, 0.68, 0.48])

    elevations = [x["elevation_ft"] for x in profile]
    temps = [x["temperature_C"] for x in profile]

    z_min, z_max = min(elevations), max(elevations)
    t_min, t_max = min(temps), max(temps)

    t_pad = max((t_max - t_min) * 0.25, 2.0)
    z_pad = (z_max - z_min) * 0.05

    chart_ax.set_xlim(t_min - t_pad, t_max + t_pad)
    chart_ax.set_ylim(z_min - z_pad, z_max + z_pad)

    for z1, z2, is_warm in temperature_sign_bands(profile):

        chart_ax.axhspan(
            z1, z2,
            color=CARD_RAIN if is_warm else CARD_SNOW,
            alpha=0.10, zorder=0,
        )

    if t_min - t_pad <= 0 <= t_max + t_pad:

        chart_ax.axvline(
            0, color="black", linewidth=1.0, alpha=0.5, zorder=2,
        )

    chart_ax.plot(
        temps, elevations,
        color=CARD_RAIN, linewidth=2.5,
        marker="o", markersize=5, zorder=5,
    )

    chart_ax.set_yticks(elevations)
    chart_ax.set_yticklabels(
        [f"{e:.0f}" for e in elevations], fontsize=7.5, color=CARD_MUTED,
    )

    chart_ax.set_xticks(
        [round(t_min - t_pad / 2), 0, round(t_max + t_pad / 2)]
    )
    chart_ax.tick_params(axis="x", labelsize=7.5, colors=CARD_MUTED)

    for spine_name in ("top", "right"):
        chart_ax.spines[spine_name].set_visible(False)

    for spine_name in ("left", "bottom"):
        chart_ax.spines[spine_name].set_color(CARD_BORDER_NORMAL)

    chart_ax.tick_params(colors=CARD_MUTED, length=3)

    # ---------------- Headline readout ----------------

    freezing_level = diagnostics["freezing_level_ft"]

    if freezing_level is not None:

        headline_value = f"{freezing_level:,.0f} ft"
        headline_color = CARD_BLUE

    elif profile[0]["temperature_C"] >= 0:

        headline_value = "All Rain"
        headline_color = CARD_RAIN

    else:

        headline_value = "All Snow"
        headline_color = CARD_SNOW

    outer_ax.text(
        0.53, 0.215, "RAIN / SNOW LINE",
        fontsize=9, fontweight="bold", color=CARD_MUTED,
        ha="center", va="top",
    )

    outer_ax.text(
        0.53, 0.185, headline_value,
        fontsize=20, fontweight="bold", color=headline_color,
        ha="center", va="top",
    )

    outer_ax.text(
        0.53, 0.09, f"Precip type: {precip_type}",
        fontsize=8.5, color=CARD_MUTED,
        ha="center", va="top",
    )

    # ---------------- Footer ----------------

    latest_times = [
        parse_iso_time(x.get("temperature_time"))
        for x in profile
        if parse_iso_time(x.get("temperature_time")) is not None
    ]

    if latest_times:

        newest = max(latest_times)

        outer_ax.text(
            0.53, 0.04, f"Updated {newest.strftime('%H:%M UTC')}",
            fontsize=7.5, color=CARD_MUTED,
            ha="center", va="top",
        )

    plt.savefig(CARD_OUTPUT_FILE, dpi=175)
    plt.close(fig)

    print()
    print(f"Saved dashboard card to: {CARD_OUTPUT_FILE}")


# =====================================================================
# 13. MAIN
# =====================================================================

def export_diagnostics_status(diagnostics, profile):
    """
    Export the subset of diagnostics the dashboard's small stat cards
    need (P-Type, Froude Number) as JSON, same pattern as
    mansfield_snow_depth.py's status file. Only plain JSON-safe types
    here (float/str/None) - the full diagnostics dict has tuples and
    numpy floats that don't serialize directly.
    """

    froude = diagnostics["froude_number"]
    precip_type_raw = diagnostics["precip_type"]

    latest_times = [
        parse_iso_time(x.get("temperature_time"))
        for x in profile
        if parse_iso_time(x.get("temperature_time")) is not None
    ]

    status = {
        "precip_type": PRECIP_TYPE_DISPLAY.get(precip_type_raw, precip_type_raw),
        "precip_type_raw": precip_type_raw,
        "froude_number": round(float(froude), 2) if froude is not None else None,
        "flow_regime": diagnostics["flow_regime"],
        
        "freezing_level_ft": diagnostics["freezing_level_ft"],
        "freezing_level_status": diagnostics["freezing_level_status"],
        "freezing_lower_crossing_ft": diagnostics["freezing_lower_crossing_ft"],
        "freezing_upper_crossing_ft": diagnostics["freezing_upper_crossing_ft"],

        "wet_bulb_zero_ft": diagnostics["wet_bulb_zero_ft"],
        "wet_bulb_zero_status": diagnostics["wet_bulb_zero_status"],
        "wet_bulb_lower_crossing_ft": diagnostics["wet_bulb_lower_crossing_ft"],
        "wet_bulb_upper_crossing_ft": diagnostics["wet_bulb_upper_crossing_ft"],
        
        "mean_lapse_rate_C_km": (
            round(float(diagnostics["mean_lapse_rate_C_km"]), 1)
            if diagnostics["mean_lapse_rate_C_km"] is not None else None
        ),
        "bulk_shear_kt": (
            round(float(diagnostics["bulk_shear_kt"]), 0)
            if diagnostics["bulk_shear_kt"] is not None else None
        ),
        "mountain_wave_category": diagnostics["mountain_wave"]["category"],
        "mountain_wave_score": diagnostics["mountain_wave"]["score"],
        "mountain_wave_max_score": diagnostics["mountain_wave"]["max_score"],
        "mountain_wave_reasons": diagnostics["mountain_wave"]["reasons"],
        "mountain_wave_critical_level": diagnostics["mountain_wave"]["critical_level"],
        "rime_icing_category": diagnostics["rime_icing"]["category"],
        "rime_icing_score": diagnostics["rime_icing"]["score"],
        "rime_icing_max_score": diagnostics["rime_icing"]["max_score"],
        "rime_icing_reasons": diagnostics["rime_icing"]["reasons"],
        "observed_at": max(latest_times).isoformat() if latest_times else None,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    with open(DIAGNOSTICS_STATUS_FILE, "w") as f:
        json.dump(status, f, indent=2)

    print(f"Saved diagnostics status to: {DIAGNOSTICS_STATUS_FILE}")

    return status


def main():

    os.makedirs(REPO_OUTPUT_DIR, exist_ok=True)

    observations = fetch_all()

    # Network call, so it belongs here in the fetch phase alongside
    # fetch_all() - not inside build_diagnostics(), which is
    # otherwise a pure transformation of already-fetched data. None
    # on failure is fine; mountain_wave_potential() just reports the
    # critical-level component as unavailable.
    rap_wind_profile = fetch_rap_wind_profile()

    profile = build_profile(
        observations
    )

    profile = calculate_pressures(
        profile
    )

    profile = attach_derived_fields(
        profile
    )

    print_profile(
        profile
    )

    diagnostics = build_diagnostics(
        profile,
        rap_wind_profile,
    )

    print_diagnostics(
        diagnostics
    )

    export_diagnostics_status(diagnostics, profile)

    (
        pressure,
        temperature,
        wind_pressure,
        u,
        v,
    ) = make_metpy_arrays(
        profile
    )

    plot_skewt(
        profile,
        pressure,
        temperature,
        wind_pressure,
        u,
        v,
        diagnostics,
    )

    plot_station_table(profile)

    plot_diagnostics_image(diagnostics)

    plot_dashboard_card(
        profile,
        diagnostics,
    )

    upload_to_drive(OUTPUT_FILE, DRIVE_FOLDER_ID, DRIVE_UPLOAD_FILENAME)

    # Not uploading CARD_OUTPUT_FILE/TABLE_OUTPUT_FILE to Drive yet -
    # add matching upload_to_drive(...) calls here whenever you're
    # ready, or (better, per the earlier Drive-hotlinking issue) just
    # let the workflow's git commit step pick up everything in
    # outputs/ the same way it already does for the other two files.


if __name__ == "__main__":
    main()
