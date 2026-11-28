# BTV Observation API — Cloudflare R2

This optional layer publishes the latest five-day normalized observation snapshot to Cloudflare R2 and exposes read-only JSON endpoints through a Worker.

## Create the R2 bucket

Create an R2 bucket named **btv-observations** in the Cloudflare dashboard before deploying the Worker.

## GitHub Actions secrets

Add these repository Actions secrets:

- `CLOUDFLARE_API_TOKEN`: Cloudflare API token with permission to deploy Workers and manage Worker secrets.
- `CLOUDFLARE_ACCOUNT_ID`: Cloudflare account ID.
- `OBSERVATION_INGEST_TOKEN`: a long random bearer token. Use the same value in GitHub Actions and the Worker.
- `OBSERVATION_WORKER_URL`: deployed Worker URL, e.g. `https://btv-observation-api.<your-workers-subdomain>.workers.dev`.

The existing `SYNOPTIC_API_KEY` secret remains required for Synoptic ingestion.

## Deploy

After creating the bucket and adding the three deployment/ingest secrets, run **Deploy BTV Observation API** from GitHub Actions (or push a change under `cloudflare/`). The deployment workflow sets the Worker-side `INGEST_SECRET` and deploys the script.

After deployment, set `OBSERVATION_WORKER_URL` to the deployed `workers.dev` URL. The ingestion workflow will then publish the latest snapshot every ten minutes. If the two ingestion secrets are absent, the pipeline still runs and retains its GitHub Actions artifact as before.

## Daily historical archive

The ingestion pipeline now writes date-partitioned gzip NDJSON files alongside the existing rolling snapshot. Daily objects are stored in R2 under:

`archive/YYYY/MM/YYYY-MM-DD.ndjson.gz`

The workflow uploads these daily partitions on each successful run. This first stage is a rolling archive covering the pipeline's configured lookback window; it does not yet guarantee long-term retention or backfill beyond that window.

New endpoint:

- `GET /archive?date=2026-11-27`: fetch a single UTC day.
- `GET /archive?start=2026-11-25&end=2026-11-27&element=temperature`: fetch a date range, optionally filtered by element, variable, source, and station ID.
- Optional `start_time` and `end_time` ISO timestamps narrow the query within the selected date range.
- Supports `sort=time_desc` (default) or `sort=time_asc`, plus `limit` (max 1,000) and `offset`.
- Responses include `missing_days` so callers can distinguish unavailable partitions from empty observation results.
- Maximum request span is 120 days.

The existing `/observations`, `/latest`, and `/summary` endpoints remain unchanged during this migration.

## Endpoints

- `GET /health`: service health.
- `GET /manifest`: source health, latest timestamps, and snapshot counts.
- `GET /stations`: BTV CWA station/provider registry with station-level latest observation timestamps.
- `GET /observations?variable=temperature&start=2026-11-26T00:00:00Z&end=2026-11-27T00:00:00Z`: filtered observations, newest first.
- `GET /observations?source=IEM_ASOS&limit=250&offset=0`: source-filtered, paginated reports.

Supported observation filters: canonical `element`, `statistic`, `accumulation_hours`, raw `variable`, `source`, `station_id`, ISO-8601 `start`/`end`, numeric `min`/`max`, and `limit`/`offset`. Maximum page size is 1,000. For example, `/observations?element=precipitation&accumulation_hours=24` returns 24-hour precipitation accumulations where the source supplies a defined interval; daily observer reports remain marked as daily reports with no invented start time.

The ingestion endpoints are authenticated and only accept the three fixed `latest/` object keys. Read endpoints are public and contain weather observations and station metadata only.

## Storage behavior and limitations

- The Worker stores a rolling **five-day snapshot**, matching the longest configured query window (120 hours).
- The pipeline replaces the three `latest/` objects on each successful run; it does not yet provide long-term historical retention.
- GitHub Actions artifacts remain available for 14 days as a secondary diagnostic copy.
- R2 lifecycle/long-term retention policy should be selected separately if historical archives beyond five days are required.
- NYS Mesonet observation data remains disabled until redistribution/access approval is obtained.
