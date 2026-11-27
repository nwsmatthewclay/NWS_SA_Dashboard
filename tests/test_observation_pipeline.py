"""Regression checks for CWA filtering in the observation pipeline."""

import unittest

from scripts.observation_pipeline import geometry_contains, in_btv


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

    def test_multipolygon(self):
        geometry = {"type": "MultiPolygon", "coordinates": [
            [[[-73, 44], [-72, 44], [-72, 45], [-73, 45], [-73, 44]]],
            [[[-71, 44], [-70, 44], [-70, 45], [-71, 45], [-71, 44]]]
        ]}
        self.assertTrue(geometry_contains(geometry, -70.5, 44.5))
        self.assertFalse(geometry_contains(geometry, -69.5, 44.5))


if __name__ == "__main__":
    unittest.main()
