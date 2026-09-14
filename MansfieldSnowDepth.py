"""
Mount Mansfield Snow Depth
----------------------------------

Two independent data sources, kept separate on purpose:

1. CHART (Max/Min/Avg/Current-season lines) - the 4 NWS BTV feeds,
   maintained by NWS, matching the season chart on
   https://www.weather.gov/btv/recreation.

2. CURRENT DEPTH + DEPARTURE FROM NORMAL - Matt Parrilla's full-history
   CSV (https://matthewparrilla.com/mansfield-stake/), which tracks
   the same stake but goes back to 1954 (vs. the NWS feeds' single
   climatological average), so departure-from-normal here is checked
   against real season-by-season history rather than one blended
   average curve.

   Note: matthewparrilla.com also publishes a JSON feed
   (mansfield-observations.json) with temperature/wind/precip, but as
   of this writing it hasn't updated since Jan 2026 - that one is not
   used here. The CSV is the one that's current (verified via its S3
   Last-Modified header, not just eyeballing values).

Source format notes:
    - The NWS .xml files are not real XML - each is a thin
      <data><text>...</text></data> wrapper around a JS array literal
      of [Date.UTC(y,m,d), depth_inches] pairs. Date.UTC's month is
      0-indexed (0=Jan), unlike Python's date().month.
    - The Parrilla CSV is wide-format: one row per ski season
      ("2025-2026"), one column per day of the season ("9/1".."6/30" -
      it only tracks Sep-Jun, not summer), plus a final "Average
      Season" row with the climatological mean for each day. It's
      served gzip-Content-Encoded, which `requests` decompresses
      automatically - no manual gzip handling needed.

Data sources:
    https://www.weather.gov/source/btv/rec/mmn/2025-2026depth.xml
    https://www.weather.gov/source/btv/rec/mmn/avgdepth.xml
    https://www.weather.gov/source/btv/rec/mmn/maxdepth.xml
    https://www.weather.gov/source/btv/rec/mmn/mindepth.xml
    https://s3.amazonaws.com/matthewparrilla.com/snow-depth.csv

Requirements:
    pip install requests matplotlib
"""

import csv
import io
import json
import os
import re
from datetime import date, datetime, timezone

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

import requests

# ---- Chart data sources (NWS, unchanged) ----

CURRENT_URL = "https://www.weather.gov/source/btv/rec/mmn/2025-2026depth.xml"
AVERAGE_URL = "https://www.weather.gov/source/btv/rec/mmn/avgdepth.xml"
MAX_URL = "https://www.weather.gov/source/btv/rec/mmn/maxdepth.xml"
MIN_URL = "https://www.weather.gov/source/btv/rec/mmn/mindepth.xml"

# ---- Current depth / departure data source (Parrilla CSV) ----

HISTORY_CSV_URL = "https://s3.amazonaws.com/matthewparrilla.com/snow-depth.csv"
AVERAGE_ROW_LABEL = "Average Season"

HYD_URL = "https://forecast.weather.gov/product.php"
HYD_PARAMS = {"site": "BTV", "issuedby": "BTV", "product": "HYD", "format": "txt", "glossary": "0"}
HYD_MAX_VERSION_FALLBACK = 3

HYD_COLUMN_LABELS = ["24 Hrs", "Max", "Min", "Cur", "Weather", "New", "Total", "SWE"]
HYD_COLUMN_FIELD_NAMES = {
    "24 Hrs": "precip_24hr",
    "Max": "temp_max",
    "Min": "temp_min",
    "Cur": "temp_cur",
    "Weather": "present_weather",
    "New": "snow_new",
    "Total": "snow_total",
    "SWE": "snow_swe",
}

HEADERS = {
    "User-Agent": (
        "MountMansfieldSnowDepth/1.0 (dashboard status card)"
    ),
}

REPO_OUTPUT_DIR = "outputs"

STATUS_OUTPUT_FILE = os.path.join(REPO_OUTPUT_DIR, "vt_snow_depth_status.json")
CHART_OUTPUT_FILE = os.path.join(REPO_OUTPUT_DIR, "vt_snow_depth_chart.png")


# =====================================================================
# CHART (NWS feeds - unchanged)
# =====================================================================

def fetch_depth_series(url):
    """
    Download one NWS depth-series file and parse it into a
    {date: depth_inches} dict.
    """

    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()

    text = response.text

    match = re.search(r"<text>(.*)</text>", text, re.DOTALL)

    if not match:
        raise ValueError(f"Could not find <text> payload in {url}")

    inner = match.group(1)

    pairs = re.findall(
        r"Date\.UTC\((\d+),(\d+),(\d+)(?:,\d+)?\),\s*(-?\d+(?:\.\d+)?)",
        inner,
    )

    records = {}

    for year_str, month_str, day_str, value_str in pairs:

        year = int(year_str)
        month = int(month_str) + 1  # JS Date.UTC month is 0-indexed
        day = int(day_str)

        try:
            record_date = date(year, month, day)
        except ValueError:
            # A handful of source rows have used an invalid day (e.g.
            # day 31 in a 30-day month) - skip rather than guess.
            continue

        records[record_date] = float(value_str)

    return records


def plot_snow_depth_chart(current_series, average_series, max_series, min_series):
    """
    Render the Max/Min/Avg/Current-season depth lines as a single PNG,
    matching the season chart on weather.gov/btv/recreation.
    """

    def to_xy(series):
        dates = sorted(series.keys())
        return dates, [series[d] for d in dates]

    max_x, max_y = to_xy(max_series)
    min_x, min_y = to_xy(min_series)
    avg_x, avg_y = to_xy(average_series)
    cur_x, cur_y = to_xy(current_series)

    fig, ax = plt.subplots(figsize=(9.5, 3.2))

    ax.plot(max_x, max_y, color="#4dabf7", linewidth=1.5, label="Max Snow Depth")
    ax.plot(min_x, min_y, color="#5c5cd6", linewidth=1.5, label="Min Snow Depth")
    ax.plot(avg_x, avg_y, color="#37b24d", linewidth=1.5, label="Avg Snow Depth")
    ax.plot(cur_x, cur_y, color="#e03131", linewidth=2.5, label="Current Depth")

    ax.set_ylabel("Inches", fontsize=9)
    ax.set_ylim(bottom=0)

    ax.xaxis.set_major_locator(mdates.MonthLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b"))

    ax.tick_params(labelsize=8)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(axis="y", color="#e9ecef", linewidth=0.8)
    ax.set_axisbelow(True)

    ax.legend(
        loc="upper left", fontsize=7.5, frameon=False, ncol=4,
        bbox_to_anchor=(0.0, 1.18),
    )

    fig.tight_layout()

    plt.savefig(CHART_OUTPUT_FILE, dpi=175)
    plt.close(fig)

    print(f"Saved snow depth chart to: {CHART_OUTPUT_FILE}")


# =====================================================================
# CURRENT DEPTH / DEPARTURE (Parrilla full-history CSV)
# =====================================================================

def fetch_snow_depth_history():
    """
    Fetch and parse the full-history CSV: one row per ski season back
    to 1954, one column per day of the season (9/1 through 6/30 - it
    doesn't track summer), plus a final "Average Season" row.

    Returns (day_labels, season_rows) where day_labels is the ordered
    list of "M/D" column headers and season_rows is
    {season_label: [value_str, ...]} (values are raw strings - some
    cells are blank for unreported days).
    """

    response = requests.get(HISTORY_CSV_URL, headers=HEADERS, timeout=30)
    response.raise_for_status()

    reader = csv.reader(io.StringIO(response.text))
    rows = list(reader)

    day_labels = rows[0][1:]
    season_rows = {row[0]: row[1:] for row in rows[1:] if row}

    return day_labels, season_rows


def season_label_for_date(d):
    """
    Ski-season label ("2025-2026") the given calendar date falls in.
    The dataset only tracks Sep 1 - Jun 30; returns None for Jul/Aug
    (the off-season gap between one season ending and the next
    starting - there's genuinely nothing to report, not just a gap
    in an otherwise-continuous series).
    """

    if d.month >= 9:
        return f"{d.year}-{d.year + 1}"

    if d.month <= 6:
        return f"{d.year - 1}-{d.year}"

    return None


def latest_reported_index(day_labels, values, as_of_label):
    """
    Index of the most recent column at or before `as_of_label` ("M/D")
    that has a non-blank value in `values`, walking backward to skip
    gaps in reporting (this dataset has real gaps - scattered blank
    cells, not just zeros).
    """

    start = day_labels.index(as_of_label) if as_of_label in day_labels else len(day_labels) - 1

    for i in range(start, -1, -1):

        if i < len(values) and values[i].strip() != "":
            return i

    return None


def rank_for_day(day_labels, season_rows, day_index, current_depth):
    """
    Where the current depth ranks among all historical seasons' depth
    on this same day-of-season (1 = deepest on record for this date).
    Returns (rank, total_seasons_compared, deepest_season_label) or
    (None, 0, None) if there's nothing to compare against.
    """

    comparisons = []

    for season, values in season_rows.items():

        if season == AVERAGE_ROW_LABEL:
            continue

        if day_index >= len(values):
            continue

        raw = values[day_index].strip()

        if raw == "":
            continue

        try:
            comparisons.append((season, float(raw)))
        except ValueError:
            continue

    if not comparisons:
        return None, 0, None, None, None

    comparisons.sort(key=lambda pair: pair[1], reverse=True)

    deepest_season = comparisons[0][0]
    record_high_in = comparisons[0][1]
    record_low_in = comparisons[-1][1]

    rank = 1

    for season, value in comparisons:

        if value > current_depth:
            rank += 1

    return rank, len(comparisons), deepest_season, record_high_in, record_low_in

# =====================================================================
# CURRENT DEPTH (HYDBTV - Daily Hydrometeorological Data Summary)
# =====================================================================

def extract_hyd_pre_text(html):
    """
    Pull raw text out of the <pre>...</pre> block(s) on a
    forecast.weather.gov product.php page. Concatenates every <pre>
    block rather than assuming there's exactly one, same tolerant
    approach as the RRSBTV parsing on the JS side of the dashboard,
    since these NWS product pages have shown that same quirk.
    """

    matches = re.findall(r"<pre[^>]*>([\s\S]*?)</pre>", html)

    if not matches:
        return None

    text = "\n".join(matches)

    return (
        text.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
    )


def fetch_hyd_product_text(version):
    """
    Fetch one HYDBTV issuance (version=1 is current, version=2/3 are
    progressively older reissues) and return its raw product text, or
    None if the page didn't contain a <pre> block at all.
    """

    params = dict(HYD_PARAMS, version=str(version))

    response = requests.get(HYD_URL, params=params, headers=HEADERS, timeout=30)
    response.raise_for_status()

    return extract_hyd_pre_text(response.text)


def hyd_column_bounds(header_line):
    """
    (start, end) character ranges for each HYD sub-column, derived
    from the product's own header line rather than hardcoded offsets -
    this is boilerplate NWS output, but trusting the actual page over
    an assumed fixed offset costs nothing and protects against a
    future formatting tweak silently breaking this.
    """

    positions = []

    for label in HYD_COLUMN_LABELS:
        idx = header_line.find(label)
        if idx == -1:
            return None
        positions.append((label, idx))

    bounds = {}

    for i, (label, start) in enumerate(positions):
        end = positions[i + 1][1] if i + 1 < len(positions) else None
        bounds[label] = (start, end)

    return bounds


def parse_mansfield_row(product_text):
    """
    Slice the Mount Mansfield row by the header's own column
    positions - Mansfield frequently omits Precip/Present Weather/New/
    SWE, and position-based slicing (instead of assuming a fixed count
    of whitespace-separated tokens) is the only reliable way to land
    on "Total" regardless of which other fields are blank that day.

    Returns a dict of field_name -> raw stripped string, or None if
    this issuance has no Mount Mansfield row at all (the without-
    Mansfield-data reissue case).
    """

    header_match = re.search(r"^\s*24 Hrs.*SWE\s*$", product_text, re.MULTILINE)

    if not header_match:
        return None

    bounds = hyd_column_bounds(header_match.group(0))

    if bounds is None:
        return None

    row_match = re.search(r"^Mount Mansfield.*$", product_text, re.MULTILINE)

    if not row_match:
        return None

    row = row_match.group(0)

    fields = {}

    for label, (start, end) in bounds.items():
        raw = row[start:end] if end is not None else row[start:]
        fields[HYD_COLUMN_FIELD_NAMES[label]] = raw.strip()

    return fields


def parse_snow_total_inches(fields):
    """
    Mansfield's "Total" field as a float, or None if it's blank
    (not reported) or "M" (missing/instrument outage).
    """

    if not fields:
        return None

    raw = fields.get("snow_total", "")

    if raw in ("", "M", "T"):
        return None

    try:
        return float(raw)
    except ValueError:
        return None


def fetch_current_mansfield_depth():
    """
    Mount Mansfield's current total snow depth straight from HYDBTV,
    rather than waiting on Parrilla's CSV to catch up to it. Walks
    backward through the last few issuances (version=1, 2, 3) until
    one has a usable Mansfield Total - see the HYD_MAX_VERSION_FALLBACK
    comment above for why that's necessary.
    """

    for version in range(1, HYD_MAX_VERSION_FALLBACK + 1):

        try:
            product_text = fetch_hyd_product_text(version)
        except requests.RequestException as error:
            print(f"HYD version={version} fetch failed: {error}")
            continue

        if not product_text:
            continue

        depth = parse_snow_total_inches(parse_mansfield_row(product_text))

        if depth is not None:
            return {"depth_in": depth, "hyd_version": version}

    print(
        "HYD: no usable Mount Mansfield Total depth found in the last "
        f"{HYD_MAX_VERSION_FALLBACK} issuances."
    )

    return None

def build_snow_depth_observation(as_of=None):
    """
    Current depth (preferring live HYDBTV, falling back to the CSV's
    current-season value) + departure from normal and rank against
    70+ years of history (both only computable when the CSV has a
    day-of-season column to compare against, i.e. during the tracked
    Sep-Jun season).

    HYDBTV is attempted unconditionally, regardless of season - it's
    a live station reading, not bound by the CSV's season-only
    column range, so "off-season" per the CSV doesn't mean HYDBTV
    has nothing to say. Departure/normal/rank are the only pieces
    that gracefully degrade to None off-season, since those
    genuinely depend on a day-of-season column that doesn't exist
    for Jul/Aug.
    """

    as_of = as_of or datetime.now(timezone.utc).date()

    hyd_result = fetch_current_mansfield_depth()

    day_labels, season_rows = fetch_snow_depth_history()

    season_label = season_label_for_date(as_of)

    base_result = {
        "station": "Mount Mansfield Stake",
        "source": "matthewparrilla.com/mansfield-stake (NWS Daily Hydromet Report)",
        "season": season_label,
        "as_of_date": as_of.isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    # No CSV day-of-season column to compare against (Jul/Aug) - but
    # HYDBTV may still have a real live current-depth reading, so
    # surface that even though departure/normal/rank can't be
    # computed against a day-of-season column that doesn't exist.
    if season_label is None or season_label not in season_rows:

        if hyd_result is not None:

            return {
                **base_result,
                "observed_date_label": None,
                "current_depth_in": hyd_result["depth_in"],
                "current_depth_source": f"HYDBTV (version={hyd_result['hyd_version']})",
                "normal_depth_in": None,
                "record_high_in": None,
                "record_low_in": None,
                "departure_in": None,
                "departure_text": "Off-season \u2014 normal unavailable",
                "rank": None,
                "rank_of": None,
            }

        return {
            **base_result,
            "observed_date_label": None,
            "current_depth_in": None,
            "normal_depth_in": None,
            "record_high_in": None,
            "record_low_in": None,
            "departure_in": None,
            "departure_text": "Off-season \u2014 no current tracking",
            "rank": None,
            "rank_of": None,
        }

    as_of_label = f"{as_of.month}/{as_of.day}"

    current_values = season_rows[season_label]
    idx = latest_reported_index(day_labels, current_values, as_of_label)

    if idx is None and hyd_result is None:

        return {
            **base_result,
            "observed_date_label": None,
            "current_depth_in": None,
            "current_depth_source": None,
            "normal_depth_in": None,
            "record_high_in": None,
            "record_low_in": None,
            "departure_in": None,
            "departure_text": "No data reported yet this season",
            "rank": None,
            "rank_of": None,
        }

    # idx/obs_label are still needed (even when HYD has the current
    # depth) to look up normal_depth_in and rank_for_day() against
    # the right day-of-season column. Fall back to the most recent
    # CSV-reported day if the CSV itself has nothing for today yet
    # but HYD does.
    if idx is None:
        idx = latest_reported_index(day_labels, current_values, day_labels[-1])

    obs_label = day_labels[idx] if idx is not None else None

    # Prefer the live HYDBTV reading over the CSV's current-season
    # value when we can get one - the CSV lags behind HYDBTV itself
    # (Parrilla's site says it updates "within the hour" of a new
    # HYDBTV issuance, so the CSV is never actually the freshest
    # source).
    if hyd_result is not None:
        current_depth = hyd_result["depth_in"]
        current_depth_source = f"HYDBTV (version={hyd_result['hyd_version']})"
    elif idx is not None:
        current_depth = float(current_values[idx])
        current_depth_source = "matthewparrilla.com/mansfield-stake (fallback, HYDBTV unavailable)"
    else:
        current_depth = None
        current_depth_source = None

    average_values = season_rows.get(AVERAGE_ROW_LABEL, [])
    normal_depth = None

    if idx is not None and idx < len(average_values) and average_values[idx].strip() != "":
        normal_depth = float(average_values[idx])

    if normal_depth is None or current_depth is None:

        departure = None
        departure_text = "Normal unavailable" if current_depth is not None else "No data reported yet this season"

    else:

        departure = current_depth - normal_depth

        if abs(departure) < 0.5:
            departure_text = "Near normal"
        elif departure > 0:
            departure_text = f"+{departure:.0f} in above normal"
        else:
            departure_text = f"{departure:.0f} in below normal"

    if idx is not None and current_depth is not None:
        rank, rank_of, deepest_season, record_high_in, record_low_in = rank_for_day(
            day_labels, season_rows, idx, current_depth
        )
    else:
        rank, rank_of, deepest_season, record_high_in, record_low_in = None, None, None, None, None

    return {
        **base_result,
        "observed_date_label": obs_label,
        "current_depth_in": current_depth,
        "current_depth_source": current_depth_source,
        "normal_depth_in": normal_depth,
        "record_high_in": record_high_in,
        "record_low_in": record_low_in,
        "departure_in": departure,
        "departure_text": departure_text,
        "rank": rank,
        "rank_of": rank_of,
        "deepest_season_on_this_date": deepest_season,
    }


# =====================================================================
# MAIN
# =====================================================================

def main():

    os.makedirs(REPO_OUTPUT_DIR, exist_ok=True)

    # Chart: NWS feeds, unchanged.

    current_series = fetch_depth_series(CURRENT_URL)
    average_series = fetch_depth_series(AVERAGE_URL)
    max_series = fetch_depth_series(MAX_URL)
    min_series = fetch_depth_series(MIN_URL)

    plot_snow_depth_chart(current_series, average_series, max_series, min_series)

    # Current depth + departure: HYDBTV live reading preferred,
    # Parrilla's full-history CSV for normal/rank/fallback.

    status = build_snow_depth_observation()

    print(json.dumps(status, indent=2))

    with open(STATUS_OUTPUT_FILE, "w") as f:
        json.dump(status, f, indent=2)

    print(f"\nSaved status to {STATUS_OUTPUT_FILE}")


if __name__ == "__main__":
    main()
