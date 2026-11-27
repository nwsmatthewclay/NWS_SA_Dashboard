const ALLOWED_OBJECTS = new Set([
  "latest/observations.ndjson.gz",
  "latest/stations.json",
  "latest/manifest.json",
]);

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, PUT, OPTIONS",
  "Access-Control-Allow-Headers": "Authorization, Content-Type",
  "Access-Control-Max-Age": "86400",
};

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", ...CORS_HEADERS },
  });
}

function authorized(request, env) {
  const header = request.headers.get("Authorization") || "";
  return Boolean(env.INGEST_SECRET) && header === `Bearer ${env.INGEST_SECRET}`;
}

async function readLatestRows(bucket) {
  const object = await bucket.get("latest/observations.ndjson.gz");
  if (!object) return null;
  const decompressed = object.body.pipeThrough(new DecompressionStream("gzip"));
  const body = await new Response(decompressed).text();
  return body.split("\n").filter(Boolean).map((line) => JSON.parse(line));
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "") || "/";

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS_HEADERS });
    }

    if (request.method === "PUT" && path.startsWith("/ingest/")) {
      if (!authorized(request, env)) return json({ error: "Unauthorized" }, 401);
      const key = path.slice("/ingest/".length);
      if (!ALLOWED_OBJECTS.has(key)) return json({ error: "Object key not allowed" }, 400);
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
      return json({ ok: true, key, bytes: body.byteLength });
    }

    if (request.method !== "GET") return json({ error: "Method not allowed" }, 405);

    if (path === "/health") return json({ ok: true, service: "BTV observation API" });

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
      let candidateRows = rows;
      const requestedIceHours = element === "ice_accretion" && statistic
        ? Number((statistic.match(/^(\\d+)h_accumulation$/) || [])[1] || 0) : 0;
      if (requestedIceHours > 0) {
        // Build rolling N-hour ice totals from consecutive hourly reports.
        const hourly = rows.filter((row) =>
          row.element === "ice_accretion" && row.accumulation_hours === 1 &&
          typeof row.value === "number" &&
          (!source || row.source === source) &&
          (!stationId || row.station_id === stationId) &&
          (!qcStatus || row.qc_status === qcStatus)
        );
        const groups = new Map();
        for (const row of hourly) {
          const key = row.station_id + "|" + row.source;
          if (!groups.has(key)) groups.set(key, new Map());
          const time = Date.parse(row.observation_time);
          const old = groups.get(key).get(time);
          const rank = row.variable === "ice_accretion_1h" ? 2 : 1;
          const oldRank = old?.variable === "ice_accretion_1h" ? 2 : 1;
          if (!old || rank > oldRank) groups.get(key).set(time, row);
        }
        const native = rows.filter((row) => row.element === "ice_accretion" &&
          row.accumulation_hours === requestedIceHours);
        const nativeEnds = new Set(native.map((row) =>
          row.station_id + "|" + row.source + "|" + Date.parse(row.observation_time)));
        const derived = [];
        for (const [key, reportMap] of groups) {
          const reports = [...reportMap.values()].sort((a,b) =>
            Date.parse(a.observation_time) - Date.parse(b.observation_time));
          for (let i = requestedIceHours - 1; i < reports.length; i++) {
            const components = reports.slice(i - requestedIceHours + 1, i + 1);
            const times = components.map((row) => Date.parse(row.observation_time));
            if (times.slice(1).some((time,j) => time-times[j] < 45*60000 || time-times[j] > 75*60000)) continue;
            const endTime = times[times.length - 1];
            const startTime = endTime - requestedIceHours * 3600000;
            if (startMs !== null && endTime < startMs) continue;
            if (endMs !== null && endTime > endMs) continue;
            const last = components[components.length - 1];
            if (nativeEnds.has(last.station_id + "|" + last.source + "|" + endTime)) continue;
            derived.push({
              ...last,
              variable: "ice_accretion_" + requestedIceHours + "h_derived",
              statistic: requestedIceHours + "h_accumulation",
              accumulation_hours: requestedIceHours,
              value: Number(components.reduce((sum,row)=>sum+Number(row.value),0).toFixed(3)),
              observation_time: new Date(endTime).toISOString(),
              period_start: new Date(startTime).toISOString(),
              period_end: new Date(endTime).toISOString(),
              raw_value: null, raw_unit: null, qc_status: "derived_complete",
              metadata: {
                derivation: "sum_of_consecutive_1h_reports",
                accumulation_hours: requestedIceHours,
                source_report_count: components.length,
                component_reports: components.map(row => ({
                  variable: row.variable, value: row.value, unit: row.unit,
                  observation_time: row.observation_time,
                  raw_value: row.raw_value, raw_unit: row.raw_unit,
                  qc_status: row.qc_status
                }))
              }
            });
          }
        }
        candidateRows = rows.concat(derived);
      }
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
          const key = row.station_id;
          const prior = best.get(key);
          if (!prior || (extreme === "highest" ? row.value > prior.value : row.value < prior.value)) best.set(key,row);
        }
        filtered = [...best.values()];
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
  },
};
