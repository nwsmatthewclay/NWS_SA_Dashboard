"""Regression checks for CWA filtering in the observation pipeline."""

import unittest
import gzip
import json
import tempfile
from pathlib import Path
from datetime import datetime, timezone

from scripts import observation_pipeline as pipeline

from scripts.observation_pipeline import geometry_contains, in_btv, local_report_time, normalized_dimensions, canonical_station_key


class TestObservationPipelineGeometry(unittest.TestCase):
    def test_polygon_contains_interior_and_excludes_exterior(self):
        polygon = {"type": "Polygon", "coordinates": [[
            [-73.0, 44.0], [-72.0, 44.0], [-72.0, 45.0],
            [-73.0, 45.0], [-73.0, 44.0]
        ]]}
        self.assertTrue(geometry_contains(polygon, -72.5, 44.5))
        self.assertFalse(geometry_contains(polygon, -71.5, 44.5))

    def test_polygon_hole_is_excluded(self):
        polygon = {"type": "Polygon", "coordinates": [
            [[-73, 44], [-72, 44], [-72, 45], [-73, 45], [-73, 44]],
            [[-72.8, 44.2], [-72.2, 44.2], [-72.2, 44.8], [-72.8, 44.8], [-72.8, 44.2]]
        ]}
        self.assertFalse(geometry_contains(polygon, -72.5, 44.5))
        self.assertTrue(geometry_contains(polygon, -72.9, 44.5))

    def test_coop_observation_time_is_local_eastern(self):
        observed = local_report_time("2024-10-22", "08 AM")
        self.assertEqual(observed.isoformat(), "2024-10-22T12:00:00+00:00")

    def test_cocorahs_observation_time_can_be_utc(self):
        observed = local_report_time("2024-10-22", "08:00", "UTC")
        self.assertEqual(observed.isoformat(), "2024-10-22T08:00:00+00:00")

    def test_canonical_element_mapping(self):
        self.assertEqual(normalized_dimensions("daily_max_temperature"), ("temperature", "maximum"))
        self.assertEqual(normalized_dimensions("daily_precipitation"), ("precipitation", "daily_report"))
        self.assertEqual(normalized_dimensions("precipitation_accumulation", {"accumulation_hours": 24}),
                         ("precipitation", "24h_accumulation"))
        self.assertEqual(normalized_dimensions("precipitation_1h"), ("precipitation", "1h_accumulation"))
        self.assertEqual(normalized_dimensions("daily_new_snow"), ("snowfall", "daily_report"))
        self.assertEqual(normalized_dimensions("ice_accretion_1h"), ("ice_accretion", "1h_accumulation"))

    def test_station_aliases_collapse_asos_and_coop_duplicates(self):
        self.assertEqual(canonical_station_key("NWS", "KBTV"), "ASOS:BTV")
        self.assertEqual(canonical_station_key("IEM_ASOS", "BTV"), "ASOS:BTV")
        self.assertEqual(canonical_station_key("Synoptic", "KBTV"), "ASOS:BTV")
        self.assertEqual(canonical_station_key("IEM_COOP", "CORV1"), "COOP:CORV1")
        self.assertEqual(canonical_station_key("IEM_RR3_COOP", "CORV1"), "COOP:CORV1")
        self.assertEqual(canonical_station_key("Synoptic", "COOPCORV1"), "COOP:CORV1")

    def test_write_outputs_creates_daily_partition(self):
        original_base = pipeline.BASE
        original_observations = pipeline.OBSERVATIONS
        original_stations = pipeline.STATIONS
        original_now = pipeline.NOW
        with tempfile.TemporaryDirectory() as temp_dir:
            pipeline.BASE = Path(temp_dir)
            pipeline.NOW = datetime(2026, 11, 27, 12, tzinfo=timezone.utc)
            pipeline.STATIONS = {"ASOS:BTV": {"station_id": "ASOS:BTV", "latitude": 44.47, "longitude": -73.15}}
            pipeline.OBSERVATIONS = [{
                "station_id": "ASOS:BTV", "source": "IEM_ASOS", "observation_time": "2026-11-27T11:00:00+00:00",
                "latest_observation_at": "2026-11-27T11:00:00+00:00", "ingested_at": "2026-11-27T12:00:00+00:00",
                "variable": "temperature", "element": "temperature", "value": 32.0, "unit": "degF"
            }]
            try:
                pipeline.write_outputs()
                daily_path = Path(temp_dir) / "archive" / "2026" / "11" / "2026-11-27.ndjson.gz"
                self.assertTrue(daily_path.exists())
                with gzip.open(daily_path, "rt", encoding="utf-8") as stream:
                    rows = [json.loads(line) for line in stream if line.strip()]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["station_id"], "ASOS:BTV")
            finally:
                pipeline.BASE = original_base
                pipeline.OBSERVATIONS = original_observations
                pipeline.STATIONS = original_stations
                pipeline.NOW = original_now

    def test_multipolygon(self):
        geometry = {"type": "MultiPolygon", "coordinates": [
            [[[-73, 44], [-72, 44], [-72, 45], [-73, 45], [-73, 44]]],
            [[[-71, 44], [-70, 44], [-70, 45], [-71, 45], [-71, 44]]]
        ]}
        self.assertTrue(geometry_contains(geometry, -70.5, 44.5))
        self.assertFalse(geometry_contains(geometry, -69.5, 44.5))


if __name__ == "__main__":
    unittest.main()
