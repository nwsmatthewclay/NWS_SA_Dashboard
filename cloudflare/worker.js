const ALLOWED_OBJECTS = new Set([
  "latest/observations.ndjson.gz",
  "latest/stations.json",
  "latest/manifest.json",
]);
function allowedObjectKey(key) {
  return ALLOWED_OBJECTS.has(key) || /^archive\/\d{4}\/\d{2}\/\d{4}-\d{2}-\d{2}\.ndjson\.gz$/.test(key);
}

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, PUT, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Authorization, Content-Type",
  "Access-Control-Max-Age": "86400",
};

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", ...CORS_HEADERS },
  });
}

const QUALITY_OBJECT_KEY = "quality/site-quarantine.json";
function normalizeSiteCode(value) {
  let code = String(value || "").trim().toUpperCase().split(":").pop();
  if (/^K[A-Z0-9]{3}$/.test(code)) code = code.slice(1);
  return code;
}
function qualityKey(providerStationId, stationId, element) {
  const site = normalizeSiteCode(providerStationId) || String(stationId || "").trim();
  return site + "::" + String(element || "").trim().toLowerCase();
}
function defaultQualityState() {
  const flaggedAt = new Date().toISOString();
  const initial = { key: qualityKey("UVM06", "", "precipitation"), station_id: null,
    provider_station_id: "UVM06", station_name: "Mt. Mansfield (East)", element: "precipitation",
    reason: "Precipitation reports identified as unreliable; excluded pending data-quality review.",
    flagged_by: "Initial operational quarantine", flagged_at: flaggedAt };
  return { version: 1, updated_at: flaggedAt, active: [initial], history: [{ ...initial, action: "flagged" }] };
}
async function readQualityState(bucket) {
  const object = await bucket.get(QUALITY_OBJECT_KEY);
  if (!object) return defaultQualityState();
  const state = JSON.parse(await object.text());
  return { version: 1, updated_at: state.updated_at || null,
    active: Array.isArray(state.active) ? state.active : [],
    history: Array.isArray(state.history) ? state.history : [] };
}
async function writeQualityState(bucket, state) {
  state.updated_at = new Date().toISOString();
  state.history = (state.history || []).slice(-500);
  await bucket.put(QUALITY_OBJECT_KEY, JSON.stringify(state, null, 2), {
    httpMetadata: { contentType: "application/json; charset=utf-8", cacheControl: "no-store" },
    customMetadata: { updated_at: state.updated_at },
  });
}
function isQuarantined(row, active) {
  return active.some((entry) => entry.element === row.element &&
    (entry.provider_station_id
      ? normalizeSiteCode(entry.provider_station_id) === normalizeSiteCode(row.provider_station_id)
      : Boolean(entry.station_id) && entry.station_id === row.station_id));
}
function qualityMutationOriginAllowed(request) {
  return request.headers.get("Origin") === "https://nwsmatthewclay.github.io";
}

function authorized(request, env) {
  const header = request.headers.get("Authorization") || "";
  return Boolean(env.INGEST_SECRET) && header === `Bearer ${env.INGEST_SECRET}`;
}

// Reuse the parsed archive inside a Worker isolate. The summary and time-series
// views can issue many concurrent element queries; without single-flight caching,
// every request independently downloads, decompresses and parses the full archive.
let cachedObservationRows = null;
let cachedObservationRowsAt = 0;
let cachedObservationIndexes = null;
let observationRowsLoad = null;
const OBSERVATION_CACHE_TTL_MS = 30000;
let observationCacheHits = 0;
let observationCacheMisses = 0;
let observationArchiveLoadMs = null;
let observationArchiveRows = null;
let observationArchiveBytes = null;

// Build reusable lookup indexes once per archive refresh. These reduce repeated
// endpoint scans to the requested element/source/station subset.
function buildObservationIndexes(rows) {
  const byElement = new Map(), byVariable = new Map(), bySource = new Map(), byStation = new Map();
  for (const row of rows) {
    const add = (map, key) => {
      if (!key) return;
      if (!map.has(key)) map.set(key, []);
      map.get(key).push(row);
    };
    add(byElement, row.element);
    add(byVariable, row.variable);
    add(bySource, row.source);
    add(byStation, row.station_id);
  }
  return { byElement, byVariable, bySource, byStation };
}
function indexedRows({ element = null, variable = null, source = null, stationId = null } = {}) {
  if (!cachedObservationRows) return [];
  const indexes = cachedObservationIndexes;
  let candidates = element ? (indexes?.byElement.get(element) || [])
    : variable ? (indexes?.byVariable.get(variable) || [])
    : cachedObservationRows;
  if (source) candidates = candidates.filter(row => row.source === source);
  if (stationId) candidates = candidates.filter(row => row.station_id === stationId);
  return candidates;
}

async function readLatestRows(bucket) {
  const now = Date.now();
  if (cachedObservationRows && now - cachedObservationRowsAt < OBSERVATION_CACHE_TTL_MS) {
    observationCacheHits++;
    return cachedObservationRows;
  }
  observationCacheMisses++;
  if (observationRowsLoad) return observationRowsLoad;

  observationRowsLoad = (async () => {
    const loadStarted = Date.now();
    const object = await bucket.get("latest/observations.ndjson.gz");
    if (!object) return null;
    observationArchiveBytes = object.size ?? null;
    const decompressed = object.body.pipeThrough(new DecompressionStream("gzip"));
    const body = await new Response(decompressed).text();
    const rows = body.split("\n").filter(Boolean).map((line) => JSON.parse(line));
    cachedObservationRows = rows;
    cachedObservationIndexes = buildObservationIndexes(rows);
    cachedObservationRowsAt = Date.now();
    observationArchiveRows = rows.length;
    observationArchiveLoadMs = Date.now() - loadStarted;
    return rows;
  })();

  try {
    return await observationRowsLoad;
  } finally {
    observationRowsLoad = null;
  }
}

export default {
  async fetch(request, env) {
    try {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "") || "/";

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS_HEADERS });
    }

    if (request.method === "PUT" && path.startsWith("/ingest/")) {
      if (!authorized(request, env)) return json({ error: "Unauthorized" }, 401);
      const key = path.slice("/ingest/".length);
      if (!allowedObjectKey(key)) return json({ error: "Object key not allowed" }, 400);
      const body = await request.arrayBuffer();
      if (body.byteLength === 0) return json({ error: "Empty upload" }, 400);
      if (body.byteLength > 20 * 1024 * 1024) return json({ error: "Upload exceeds 20 MB" }, 413);
      await env.OBSERVATIONS.put(key, body, {
        httpMetadata: {
          contentType: key.endsWith(".gz") ? "application/gzip" : "application/json; charset=utf-8",
          cacheControl: key === "latest/observations.ndjson.gz" ? "public, max-age=300" : "public, max-age=60",
        },
        customMetadata: { uploaded_at: new Date().toISOString() },
      });
      if (key === "latest/observations.ndjson.gz") {
        cachedObservationRows = null;
        cachedObservationRowsAt = 0;
        cachedObservationIndexes = null;
      }
      return json({ ok: true, key, bytes: body.byteLength });
    }

    if (path === "/quality" && request.method === "GET") {
      return json(await readQualityState(env.OBSERVATIONS));
    }
    if (path === "/quality/flag" && request.method === "POST") {
      if (!qualityMutationOriginAllowed(request)) return json({ error: "Origin not allowed" }, 403);
      const body = await request.json();
      const providerStationId = String(body.provider_station_id || "").trim();
      const stationId = String(body.station_id || "").trim();
      const element = String(body.element || "").trim().toLowerCase();
      const actor = String(body.actor || "").trim().slice(0, 80);
      const reason = String(body.reason || "").trim().slice(0, 500);
      if ((!providerStationId && !stationId) || !/^[a-z0-9_]{1,64}$/.test(element) || !actor || !reason)
        return json({ error: "station, element, reporter and reason are required" }, 400);
      const archivedRows = await readLatestRows(env.OBSERVATIONS);
      const knownReport = (archivedRows || []).some((row) =>
        row.element === element &&
        (providerStationId
          ? normalizeSiteCode(row.provider_station_id) === normalizeSiteCode(providerStationId)
          : row.station_id === stationId));
      if (!knownReport) return json({ error: "No archived reports found for this station and element" }, 404);
      const state = await readQualityState(env.OBSERVATIONS);
      const key = qualityKey(providerStationId, stationId, element);
      if (state.active.length >= 100) return json({ error: "The active quarantine limit has been reached" }, 429);
      if (state.active.some((entry) => entry.key === key))
        return json({ error: "This station and element are already quarantined", key }, 409);
      const entry = { key, station_id: stationId || null, provider_station_id: providerStationId || null,
        station_name: String(body.station_name || providerStationId || stationId).trim().slice(0, 160),
        element, reason, flagged_by: actor, flagged_at: new Date().toISOString() };
      state.active.push(entry);
      state.history.push({ ...entry, action: "flagged" });
      await writeQualityState(env.OBSERVATIONS, state);
      return json({ ok: true, entry, state });
    }
    if (path === "/quality/restore" && request.method === "POST") {
      if (!qualityMutationOriginAllowed(request)) return json({ error: "Origin not allowed" }, 403);
      if (!env.QUALITY_REVIEW_TOKEN)
        return json({ error: "Quality review is not configured. Add the QUALITY_REVIEW_TOKEN Worker secret before restoring sites." }, 503);
      if ((request.headers.get("Authorization") || "") !== `Bearer ${env.QUALITY_REVIEW_TOKEN}`)
        return json({ error: "Reviewer authorization failed" }, 401);
      const body = await request.json();
      const key = String(body.key || "").trim();
      const actor = String(body.actor || "").trim().slice(0, 80);
      if (!key || !actor) return json({ error: "quarantine key and reviewer name are required" }, 400);
      const state = await readQualityState(env.OBSERVATIONS);
      const index = state.active.findIndex((entry) => entry.key === key);
      if (index < 0) return json({ error: "Active quarantine not found" }, 404);
      const [entry] = state.active.splice(index, 1);
      if (actor.toLowerCase() === String(entry.flagged_by || "").trim().toLowerCase()) {
        state.active.push(entry);
        return json({ error: "A different reviewer must confirm the site is healthy" }, 403);
      }
      state.history.push({ ...entry, action: "restored", restored_by: actor, restored_at: new Date().toISOString() });
      await writeQualityState(env.OBSERVATIONS, state);
      return json({ ok: true, restored: entry, state });
    }

    if (request.method !== "GET") return json({ error: "Method not allowed" }, 405);

    if (path === "/health") return json({ ok: true, service: "BTV observation API" });
    if (path === "/diagnostics") {
      const loaded = Boolean(cachedObservationRows);
      const indexes = cachedObservationIndexes;
      return json({
        service: "BTV observation API",
        archive_loaded_in_this_isolate: loaded,
        archive_rows: observationArchiveRows,
        compressed_archive_bytes: observationArchiveBytes,
        cache_age_seconds: loaded ? Math.max(0, Math.round((Date.now() - cachedObservationRowsAt) / 1000)) : null,
        cache_ttl_seconds: OBSERVATION_CACHE_TTL_MS / 1000,
        cache_hits: observationCacheHits,
        cache_misses: observationCacheMisses,
        last_archive_load_ms: observationArchiveLoadMs,
        index_counts: loaded ? {
          elements: indexes?.byElement.size || 0,
          variables: indexes?.byVariable.size || 0,
          sources: indexes?.bySource.size || 0,
          stations: indexes?.byStation.size || 0
        } : null
      });
    }

    if (path === "/manifest" || path === "/stations") {
      const key = path === "/manifest" ? "latest/manifest.json" : "latest/stations.json";
      const object = await env.OBSERVATIONS.get(key);
      if (!object) return json({ error: "Data not available yet" }, 404);
      return new Response(object.body, {
        headers: {
          "Content-Type": "application/json; charset=utf-8",
          "Cache-Control": "public, max-age=60",
          ...CORS_HEADERS,
        },
      });
    }

    if (path === "/archive") {
      const startText = url.searchParams.get("start") || url.searchParams.get("date");
      const endText = url.searchParams.get("end") || startText;
      const validDate = value => /^\d{4}-\d{2}-\d{2}$/.test(value || "") &&
        Number.isFinite(Date.parse(value + "T00:00:00Z")) &&
        new Date(value + "T00:00:00Z").toISOString().slice(0,10) === value;
      if (!validDate(startText) || !validDate(endText))
        return json({ error: "start/end must be valid YYYY-MM-DD dates; date may be used for a single day" }, 400);
      const startDay = new Date(startText + "T00:00:00Z");
      const endDay = new Date(endText + "T00:00:00Z");
      if (endDay < startDay) return json({ error: "end must be on or after start" }, 400);
      const dayCount = Math.floor((endDay - startDay) / 86400000) + 1;
      if (dayCount > 120) return json({ error: "Archive requests are limited to 120 days" }, 400);
      const rows = [];
      const missing_days = [];
      for (let day = 0; day < dayCount; day++) {
        const date = new Date(startDay.getTime() + day * 86400000);
        const dayKey = date.toISOString().slice(0,10);
        const objectKey = `archive/${dayKey.slice(0,4)}/${dayKey.slice(5,7)}/${dayKey}.ndjson.gz`;
        const object = await env.OBSERVATIONS.get(objectKey);
        if (!object) { missing_days.push(dayKey); continue; }
        const decompressed = object.body.pipeThrough(new DecompressionStream("gzip"));
        const body = await new Response(decompressed).text();
        for (const line of body.split("\n")) if (line.trim()) rows.push(JSON.parse(line));
      }
      const startMs = url.searchParams.has("start_time") ? Date.parse(url.searchParams.get("start_time")) : startDay.getTime();
      const endMs = url.searchParams.has("end_time") ? Date.parse(url.searchParams.get("end_time")) : endDay.getTime() + 86400000 - 1;
      if (!Number.isFinite(startMs) || !Number.isFinite(endMs)) return json({ error: "start_time/end_time must be ISO timestamps" }, 400);
      const element = url.searchParams.get("element"), variable = url.searchParams.get("variable");
      const source = url.searchParams.get("source"), stationId = url.searchParams.get("station_id");
      const filtered = rows.filter(row => {
        const time = Date.parse(row.observation_time);
        return (!element || row.element === element) && (!variable || row.variable === variable) &&
          (!source || row.source === source) && (!stationId || row.station_id === stationId) &&
          time >= startMs && time <= endMs && Number.isFinite(Number(row.value));
      });
      const sort = url.searchParams.get("sort") || "time_desc";
      filtered.sort(sort === "time_asc" ? (a,b)=>Date.parse(a.observation_time)-Date.parse(b.observation_time)
        : (a,b)=>Date.parse(b.observation_time)-Date.parse(a.observation_time));
      const limit = Math.min(1000, Math.max(1, Number(url.searchParams.get("limit") || 250)));
      const offset = Math.max(0, Number(url.searchParams.get("offset") || 0));
      if (!Number.isInteger(limit) || !Number.isInteger(offset)) return json({ error: "limit/offset must be integers" }, 400);
      return json({ start: startText, end: endText, day_count: dayCount, missing_days, total: filtered.length, limit, offset,
        results: filtered.slice(offset, offset + limit) });
    }

    // Lightweight aggregate endpoints avoid transferring large archives to the browser.
    if (path === "/latest") {
      const rows = await readLatestRows(env.OBSERVATIONS);
      if (!rows) return json({ error: "Observation archive not available yet" }, 404);
      const element = url.searchParams.get("element");
      const source = url.searchParams.get("source");
      const stationId = url.searchParams.get("station_id");
      const qualityState = await readQualityState(env.OBSERVATIONS);
      const filtered = indexedRows({ element, source, stationId }).filter(row =>
        !isQuarantined(row, qualityState.active) && Number.isFinite(Date.parse(row.observation_time)) &&
        Number.isFinite(Number(row.value)));
      const latestByStation = new Map();
      for (const row of filtered) {
        const key = row.station_id || row.provider_station_id;
        const prior = latestByStation.get(key);
        if (!prior || Date.parse(row.observation_time) > Date.parse(prior.observation_time))
          latestByStation.set(key, row);
      }
      return json({ count: latestByStation.size, results: [...latestByStation.values()].sort((a,b)=>String(a.station_name||a.provider_station_id).localeCompare(String(b.station_name||b.provider_station_id))) });
    }

    if (path === "/summary") {
      const rows = await readLatestRows(env.OBSERVATIONS);
      if (!rows) return json({ error: "Observation archive not available yet" }, 404);
      const hours = Number(url.searchParams.get("hours") || 24);
      const top = Math.min(10, Math.max(1, Number(url.searchParams.get("top") || 5)));
      if (!Number.isFinite(hours) || hours < 1 || hours > 120 || !Number.isInteger(top))
        return json({ error: "hours must be 1-120 and top must be an integer" }, 400);
      const startMs = Date.now() - hours * 3600000;
      const qualityState = await readQualityState(env.OBSERVATIONS);
      const definitions = [
        ["precipitation","highest","in","precipitation"],
        ["snowfall","highest","in","snowfall"],
        ["ice_accretion","highest","in","ice_accretion"],
        ["heat_index","highest","°F","heat_index"],
        ["wind_chill","lowest","°F","wind_chill"],
        ["wind_gust","highest","mph","wind_gust"],
        ["snow_depth","highest","in","snow_depth"],
        ["temperature","highest","°F","temperature"],
        ["temperature","lowest","°F","temperature"]
      ];
      const output = {};
      for (const [element,mode,unit,key] of definitions) {
        const candidates = indexedRows({ element }).filter(row =>
          Date.parse(row.observation_time) >= startMs &&
          !isQuarantined(row, qualityState.active) && Number.isFinite(Number(row.value)) &&
          (element !== "precipitation" || row.accumulation_hours === hours) &&
          (element !== "snowfall" || row.accumulation_hours === hours || (hours === 24 && row.statistic === "daily_report")));
        candidates.sort((a,b)=>mode==="highest"?Number(b.value)-Number(a.value):Number(a.value)-Number(b.value));
        output[key+(mode==="lowest"?"_min":"_max")] = candidates.slice(0,top).map(row=>({station_id:row.station_id,provider_station_id:row.provider_station_id,station_name:row.station_name,source:row.source,observation_time:row.observation_time,value:row.value,unit:row.unit||unit,latitude:row.latitude,longitude:row.longitude,elevation_m:row.elevation_m,county:row.county,accumulation_hours:row.accumulation_hours}));
      }
      return json({ hours, top, generated_at:new Date().toISOString(), results:output });
    }

    if (path === "/observations") {
      const rows = await readLatestRows(env.OBSERVATIONS);
      if (!rows) return json({ error: "Observation archive not available yet" }, 404);
      const variable = url.searchParams.get("variable");
      const element = url.searchParams.get("element");
      const statistic = url.searchParams.get("statistic");
      const accumulationHours = url.searchParams.has("accumulation_hours")
        ? Number(url.searchParams.get("accumulation_hours")) : null;
      const source = url.searchParams.get("source");
      const stationId = url.searchParams.get("station_id");
      const start = url.searchParams.get("start");
      const end = url.searchParams.get("end");
      const minValue = url.searchParams.has("min") ? Number(url.searchParams.get("min")) : null;
      const maxValue = url.searchParams.has("max") ? Number(url.searchParams.get("max")) : null;
      if ((minValue !== null && !Number.isFinite(minValue)) ||
          (maxValue !== null && !Number.isFinite(maxValue)) ||
          (accumulationHours !== null && !Number.isFinite(accumulationHours))) {
        return json({ error: "min/max must be numeric" }, 400);
      }
      const startMs = start ? Date.parse(start) : null;
      const endMs = end ? Date.parse(end) : null;
      if ((start && !Number.isFinite(startMs)) || (end && !Number.isFinite(endMs))) {
        return json({ error: "start/end must be ISO-8601 timestamps" }, 400);
      }
      const qcStatus = url.searchParams.get("qc_status");
      const reportType = url.searchParams.get("report_type");
      const sort = url.searchParams.get("sort") || "time_desc";
      const sorters = {
        time_desc: (a, b) => Date.parse(b.observation_time) - Date.parse(a.observation_time),
        time_asc: (a, b) => Date.parse(a.observation_time) - Date.parse(b.observation_time),
        value_desc: (a, b) => (Number(b.value) - Number(a.value)) || (Date.parse(b.observation_time) - Date.parse(a.observation_time)),
        value_asc: (a, b) => (Number(a.value) - Number(b.value)) || (Date.parse(b.observation_time) - Date.parse(a.observation_time)),
      };
      if (!sorters[sort]) return json({ error: "Unsupported sort option" }, 400);
      let candidateRows = element || variable || source || stationId
        ? indexedRows({ element, variable, source, stationId })
        : rows;

      // Calculate apparent temperature from co-timed normalized station
      // observations. Temperature/dewpoint are Fahrenheit; wind is mph.
      if (element === "wind_chill" || element === "heat_index") {
        const grouped = new Map();
        const derivedInputRows = ["temperature", "dewpoint", "wind_speed"]
          .flatMap(key => cachedObservationIndexes?.byElement.get(key) || []);
        for (const row of derivedInputRows) {
          if (!["temperature", "dewpoint", "wind_speed"].includes(row.element) ||
              row.statistic !== "instantaneous" || !Number.isFinite(Number(row.value))) continue;
          const key = [row.station_id, row.source, row.observation_time].join("|");
          if (!grouped.has(key)) grouped.set(key, {});
          const bucket = grouped.get(key);
          if (row.element === "temperature") bucket.temperature = row;
          if (row.element === "dewpoint") bucket.dewpoint = row;
          if (row.element === "wind_speed") bucket.wind_speed = row;
        }
        candidateRows = [];
        for (const inputs of grouped.values()) {
          const temp = inputs.temperature;
          if (!temp) continue;
          const tempF = Number(temp.value);
          let value, metadata;
          if (element === "wind_chill") {
            const wind = inputs.wind_speed;
            if (!wind || tempF > 50 || Number(wind.value) < 3) continue;
            const mph = Number(wind.value);
            value = 35.74 + 0.6215*tempF - 35.75*Math.pow(mph,0.16) +
              0.4275*tempF*Math.pow(mph,0.16);
            metadata = {
              calculation: "NWS_Wind_Chill_Index",
              inputs: {temperature_f:tempF, wind_speed_mph:mph},
              formula: "NWS wind chill equation; valid at temperatures <=50 F and wind speeds >=3 mph"
            };
          } else {
            const dew = inputs.dewpoint;
            if (!dew || tempF < 80) continue;
            const dewF = Number(dew.value);
            const tempC = (tempF-32)*5/9, dewC = (dewF-32)*5/9;
            const rh = Math.max(0,Math.min(100,100*Math.exp(
              (17.625*dewC/(243.04+dewC))-(17.625*tempC/(243.04+tempC)))));
            if (rh < 40) continue;
            const t=tempF, r=rh;
            let hi=-42.379+2.04901523*t+10.14333127*r-0.22475541*t*r-
              0.00683783*t*t-0.05481717*r*r+0.00122874*t*t*r+
              0.00085282*t*r*r-0.00000199*t*t*r*r;
            if (r<13 && t>=80 && t<=112) hi-=((13-r)/4)*Math.sqrt((17-Math.abs(t-95))/17);
            else if (r>85 && t>=80 && t<=87) hi+=((r-85)/10)*((87-t)/5);
            value=hi;
            metadata={
              calculation:"NOAA_NWS_Heat_Index_Rothfusz",
              inputs:{temperature_f:tempF,dewpoint_f:dewF,relative_humidity_percent:Number(rh.toFixed(1))},
              formula:"NOAA/NWS Rothfusz regression; RH estimated from temperature and dewpoint; valid at temperature >=80 F and RH >=40%"
            };
          }
          const wind=inputs.wind_speed;
          candidateRows.push({
            ...temp, element, variable:element, statistic:"instantaneous",
            value:Number(value.toFixed(1)), unit:"degF",
            raw_value:Number(value.toFixed(1)), raw_unit:"degF",
            qc_status:"derived_calculated",
            metadata:{
              ...metadata,
              source_observations:{
                temperature_time:temp.observation_time,
                dewpoint_time:inputs.dewpoint?.observation_time||null,
                wind_speed_time:wind?.observation_time||null
              }
            }
          });
        }
      }

      // Ice accretion is intentionally sourced ONLY from the I1 group in
      // routine METAR reports. Do not mix I3/I6 groups, IEM-derived periods,
      // SPECI, or other networks into this hourly report series.
      if (element === "ice_accretion") {
        const expectedMinute = {
          KBTV: 54, KPBG: 53, KSLK: 51, KMPV: 51,
          KMSS: 53, KMVL: 54, KVSF: 53,
          BTV: 54, PBG: 53, SLK: 51, MPV: 51, MSS: 53, MVL: 54, VSF: 53,
        };
        const selected = new Map();
        for (const row of candidateRows) {
          if (row.element !== "ice_accretion" ||
              row.accumulation_hours !== 1 ||
              row.source !== "IEM_ASOS" ||
              !["metar_ice_accretion_1h", "ice_accretion_1h"].includes(row.variable) ||
              row.metadata?.hourly_selection !== "METAR_only") continue;
          const time = Date.parse(row.observation_time);
          if (!Number.isFinite(time)) continue;
          const hour = Math.floor(time / 3600000);
          const station = String(row.provider_station_id || row.station_id || "").split(":").pop();
          const targetMinute = expectedMinute[station];
          const minute = new Date(time).getUTCMinutes();
          const distance = targetMinute === undefined
            ? 0
            : Math.min(Math.abs(minute - targetMinute), 60 - Math.abs(minute - targetMinute));
          const key = row.station_id + "|" + hour;
          const prior = selected.get(key);
          if (!prior || distance < prior.distance ||
              (distance === prior.distance && time > prior.time)) {
            selected.set(key, { row, time, distance });
          }
        }
        candidateRows = [...selected.values()].map(item => item.row);

        // Derive multi-hour totals only from deduplicated routine METAR I1
        // reports, retaining each hourly component for quality control.
        const requestedIceHours = statistic
          ? Number((statistic.match(/^(\d+)h_accumulation$/) || [])[1] || 0)
          : 0;
        if (requestedIceHours > 1) {
          const hourly = candidateRows.filter((row) => {
            const time = Date.parse(row.observation_time);
            return row.accumulation_hours === 1 &&
              typeof row.value === "number" &&
              (!source || row.source === source) &&
              (!stationId || row.station_id === stationId) &&
              (!qcStatus || row.qc_status === qcStatus) &&
              (startMs === null || time >= startMs) &&
              (endMs === null || time <= endMs);
          });
          const groups = new Map();
          for (const row of hourly) {
            const key = row.station_id + "|" + row.source;
            if (!groups.has(key)) groups.set(key, new Map());
            const time = Date.parse(row.observation_time);
            groups.get(key).set(Math.floor(time / 3600000), row);
          }
          const derived = [];
          for (const componentsMap of groups.values()) {
            const components = [...componentsMap.values()].sort((a, b) =>
              Date.parse(a.observation_time) - Date.parse(b.observation_time));
            if (!components.length) continue;
            const first = components[0], last = components[components.length - 1];
            const missingHours = Math.max(0, requestedIceHours - components.length);
            derived.push({
              ...last,
              variable: "ice_accretion_" + requestedIceHours + "h_reported_sum",
              statistic: requestedIceHours + "h_accumulation",
              accumulation_hours: requestedIceHours,
              value: Number(components.reduce((sum, row) => sum + Number(row.value), 0).toFixed(3)),
              period_start: first.observation_time,
              period_end: last.observation_time,
              raw_value: null,
              raw_unit: null,
              qc_status: "derived_reported_sum",
              metadata: {
                derivation: "sum_of_deduplicated_routine_METAR_I1_reports",
                requested_window_hours: requestedIceHours,
                source_report_count: components.length,
                missing_hour_count: missingHours,
                coverage_percent: Number((components.length / requestedIceHours * 100).toFixed(1)),
                complete_hourly_coverage: missingHours === 0,
                component_reports: components.map(row => ({
                  variable: row.variable, value: row.value, unit: row.unit,
                  observation_time: row.observation_time,
                  raw_value: row.raw_value, raw_unit: row.raw_unit,
                  qc_status: row.qc_status
                }))
              }
            });
          }
          candidateRows = derived;
        }
      }

      const qualityState = await readQualityState(env.OBSERVATIONS);
      candidateRows = candidateRows.filter((row) => !isQuarantined(row, qualityState.active));
      let filtered = candidateRows.filter((row) => {
        const time = Date.parse(row.observation_time);
        const value = typeof row.value === "number" ? row.value : null;
        if (variable && row.variable !== variable) return false;
        if (element && row.element !== element) return false;
        if (statistic && row.statistic !== statistic) return false;
        if (accumulationHours !== null && row.accumulation_hours !== accumulationHours) return false;
        if (source && row.source !== source) return false;
        const rowReportType = row.report_type || (row.source === "IEM_ASOS" ? "ASOS" : row.source === "IEM_COOP" ? "COOP" : row.source === "CoCoRaHS" ? "CoCoRaHS" : "Public");
        if (reportType && rowReportType !== reportType) return false;
        if (stationId && row.station_id !== stationId) return false;
        if (qcStatus && row.qc_status !== qcStatus) return false;
        if (startMs !== null && time < startMs) return false;
        if (endMs !== null && time > endMs) return false;
        if (minValue !== null && (value === null || value < minValue)) return false;
        if (maxValue !== null && (value === null || value > maxValue)) return false;
        return true;
      });
      const extreme = url.searchParams.get("extreme");
      if (extreme === "highest" || extreme === "lowest") {
        const best = new Map();
        for (const row of filtered) {
          if (typeof row.value !== "number") continue;
          const providerId = String(row.provider_station_id || row.station_id || "").toUpperCase().replace(/^K(?=[A-Z0-9]{3}$)/, "");
          const key = providerId || row.station_id;
          const prior = best.get(key);
          if (!prior || (extreme === "highest" ? row.value > prior.value : row.value < prior.value)) best.set(key,row);
        }
        filtered = [...best.values()];
      }
      // Convert ASOS flat-ice accretion to radial ice using the
      // operational 0.40 factor. Preserve the original flat value unchanged.
      if (element === "ice_accretion") {
        filtered = filtered.map((row) => ({
          ...row,
          radial_value: Number((Number(row.value) * 0.40).toFixed(3)),
          radial_unit: row.unit || "inch",
          metadata: row.metadata?.component_reports
            ? {
                ...row.metadata,
                component_reports: row.metadata.component_reports.map((component) => ({
                  ...component,
                  radial_value: Number((Number(component.value) * 0.40).toFixed(3)),
                  radial_unit: component.unit || row.unit || "inch",
                })),
              }
            : row.metadata,
        }));
      }

      filtered.sort(sorters[sort]);
      const total = filtered.length;
      const requestedLimit = Number(url.searchParams.get("limit") || 250);
      const requestedOffset = Number(url.searchParams.get("offset") || 0);
      if (!Number.isInteger(requestedLimit) || !Number.isInteger(requestedOffset) || requestedOffset < 0) {
        return json({ error: "limit and offset must be whole numbers; offset cannot be negative" }, 400);
      }
      const limit = Math.min(1000, Math.max(1, requestedLimit));
      const offset = requestedOffset;
      return json({
        total,
        limit,
        offset,
        results: filtered.slice(offset, offset + limit),
      });
    }

    return json({ error: "Not found" }, 404);
    } catch (error) {
      // Ensure unexpected runtime/R2 failures remain readable to browser clients.
      console.error("BTV Observation API request failed:", error);
      return json({ error: "Internal server error", message: String(error?.message || error) }, 500);
    }
  },
};
