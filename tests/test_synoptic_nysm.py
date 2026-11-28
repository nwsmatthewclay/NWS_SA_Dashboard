"""Tests for NYSM network discovery and provider selection."""
import unittest

from scripts.observation_pipeline import resolve_synoptic_nysm_network


class TestSynopticNYSMNetwork(unittest.TestCase):
    def test_matches_nysm_long_or_short_name(self):
        payload = {"MNET": [
            {"ID": "1", "LONGNAME": "ASOS/AWOS", "SHORTNAME": "asos"},
            {"ID": "777", "LONGNAME": "New York State Mesonet", "SHORTNAME": "NYSM"},
        ]}
        self.assertEqual(resolve_synoptic_nysm_network(payload), "777")

    def test_no_false_match(self):
        payload = {"MNET": [
            {"ID": "1", "LONGNAME": "New York City Weather", "SHORTNAME": "NYC"},
        ]}
        self.assertIsNone(resolve_synoptic_nysm_network(payload))


if __name__ == "__main__":
    unittest.main()
