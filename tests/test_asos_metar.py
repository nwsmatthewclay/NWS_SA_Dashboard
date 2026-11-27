import unittest
from datetime import datetime, timezone

from scripts.asos_metar import knots_to_mph, normalize_iem_wind, parse_metar, is_speci_report, is_routine_hourly_metar, metar_report_datetime


class TestASOSMetar(unittest.TestCase):
    def test_iem_station_prefixed_metar_is_routine(self):
        self.assertTrue(is_metar_report("KSLK 270551Z AUTO 08013G23KT RMK I1007 I6019"))
        self.assertFalse(is_metar_report("SPECI KSLK 270545Z AUTO 08013G23KT RMK I1007"))

    def test_spei_reports_are_identified(self):
        self.assertTrue(is_speci_report("SPECI KBTV 261205Z 00000KT 1SM FZRA"))
        self.assertFalse(is_speci_report("METAR KBTV 261153Z 00000KT 10SM CLR"))

    def test_hourly_rainfall_uses_routine_metar_near_53(self):
        from datetime import datetime, timezone
        self.assertTrue(is_routine_hourly_metar(
            "METAR KBTV 261153Z 00000KT 10SM CLR RMK AO2 P0001",
            datetime(2026, 11, 26, 11, 53, tzinfo=timezone.utc)))
        self.assertTrue(is_routine_hourly_metar(
            "METAR KBTV 261150Z 00000KT 10SM CLR",
            datetime(2026, 11, 26, 11, 50, tzinfo=timezone.utc)))
        self.assertFalse(is_routine_hourly_metar(
            "SPECI KBTV 261153Z 00000KT 10SM CLR",
            datetime(2026, 11, 26, 11, 53, tzinfo=timezone.utc)))
        self.assertFalse(is_routine_hourly_metar(
            "METAR KBTV 261200Z 00000KT 10SM CLR",
            datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)))

    def test_hourly_selection_uses_encoded_metar_issue_time(self):
        from datetime import datetime, timezone
        fallback = datetime(2026, 11, 27, 3, 17, tzinfo=timezone.utc)
        raw = "METAR KBTV 270353Z 00000KT 10SM CLR RMK AO2 P0001"
        self.assertTrue(is_routine_hourly_metar(raw, fallback))
        self.assertEqual(metar_report_datetime(raw, fallback),
                         datetime(2026, 11, 27, 3, 53, tzinfo=timezone.utc))
        self.assertFalse(is_routine_hourly_metar(
            "METAR KBTV 270317Z 00000KT 10SM CLR RMK AO2 P0001", fallback))

    def test_wind_conversion(self):
        self.assertEqual(knots_to_mph(10), 11.5)
        self.assertEqual(knots_to_mph(0), 0.0)
        self.assertIsNone(knots_to_mph(None))

    def test_unit_aware_wind_normalization(self):
        self.assertEqual(wind_speed_to_mph(20, "mph"), 20.0)
        self.assertEqual(wind_speed_to_mph(20, "knot"), 23.0)
        self.assertEqual(wind_speed_to_mph(36, "wmoUnit:km_h-1"), 22.4)
        self.assertEqual(wind_speed_to_mph(10, "wmoUnit:m_s-1"), 22.4)
        self.assertEqual(wind_speed_to_mph(0, "mph"), 0.0)

    def test_wind_and_peak_gust(self):
        parsed = parse_metar("METAR KBTV 261951Z 27015G25KT 10SM CLR 04/M02 A3012 RMK PK WND 28032/1940")
        self.assertEqual(parsed["wind_speed_kt"], 15)
        self.assertEqual(parsed["wind_speed_mph"], 17.3)
        self.assertEqual(parsed["wind_gust_mph"], 28.8)
        self.assertEqual(parsed["peak_wind_speed_mph"], 36.8)

    def test_precipitation_groups(self):
        parsed = parse_metar("METAR KBTV 261200Z 00000KT 1SM -FZRA OVC003 01/00 A2990 RMK AO2 P0009 60012 70025")
        self.assertEqual(parsed["precip_1h_in"], 0.09)
        self.assertEqual(parsed["precip_3h_or_6h_in"], 0.12)
        self.assertEqual(parsed["precip_3h_or_6h_period_hours"], 6)
        self.assertEqual(parsed["precip_24h_in"], 0.25)
        self.assertTrue(parsed["freezing_precipitation_reported"])

    def test_three_hour_precip_period(self):
        parsed = parse_metar("METAR KBTV 261500Z 00000KT 10SM RA OVC020 RMK 60004")
        self.assertEqual(parsed["precip_3h_or_6h_period_hours"], 3)

    def test_ice_accretion_and_sensor_status(self):
        parsed = parse_metar("METAR KBTV 261200Z 00000KT 1SM FZRA OVC003 RMK AO2 I1005 I6012")
        self.assertEqual(parsed["ice_accretion_1h_in"], 0.05)
        self.assertEqual(parsed["ice_accretion_6h_in"], 0.12)
        self.assertEqual(parsed["ice_sensor_status"], "reported")

    def test_missing_and_unavailable_ice_sensor(self):
        missing = parse_metar("METAR KBTV 261200Z 00000KT 10SM CLR RMK I1///")
        self.assertIsNone(missing["ice_accretion_1h_in"])
        self.assertEqual(missing["ice_sensor_status"], "missing")
        unavailable = parse_metar("METAR KBTV 261200Z 00000KT 10SM CLR RMK FZRANO")
        self.assertEqual(unavailable["ice_sensor_status"], "unavailable")

    def test_iem_wind_fields_preserve_knots(self):
        row = normalize_iem_wind({"sknt": "10", "gust": "20", "peak_wind_gust": "30"})
        self.assertEqual(row["sknt"], "10")
        self.assertEqual(row["wind_mph"], 11.5)
        self.assertEqual(row["gust_mph"], 23.0)
        self.assertEqual(row["peak_wind_gust_mph"], 34.5)


if __name__ == "__main__":
    unittest.main()
