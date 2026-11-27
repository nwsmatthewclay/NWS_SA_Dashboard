"""Regression checks for CWA filtering in the observation pipeline."""

import unittest

from scripts.observation_pipeline import geometry_contains, in_btv, local_report_time, normalized_dimensions


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

    def test_multipolygon(self):
        geometry = {"type": "MultiPolygon", "coordinates": [
            [[[-73, 44], [-72, 44], [-72, 45], [-73, 45], [-73, 44]]],
            [[[-71, 44], [-70, 44], [-70, 45], [-71, 45], [-71, 44]]]
        ]}
        self.assertTrue(geometry_contains(geometry, -70.5, 44.5))
        self.assertFalse(geometry_contains(geometry, -69.5, 44.5))


if __name__ == "__main__":
    unittest.main()
