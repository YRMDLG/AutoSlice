import unittest

from autoslice import timecode


class TimecodeTests(unittest.TestCase):
    def test_format_elapsed_keeps_timedelta_compatibility(self):
        expected = {
            0: "0:00:00",
            65: "0:01:05",
            3723: "1:02:03",
            65.9: "0:01:05",
            -1: "-1 day, 23:59:59",
        }
        for seconds, label in expected.items():
            with self.subTest(seconds=seconds):
                self.assertEqual(timecode.format_elapsed(seconds), label)

    def test_parse_hms_supports_minute_and_hour_forms(self):
        self.assertEqual(timecode.parse_hms("02:03"), 123)
        self.assertEqual(timecode.parse_hms("1:02:03"), 3723)
        self.assertEqual(timecode.parse_hms("00:00"), 0)

    def test_parse_hms_preserves_invalid_input_errors(self):
        with self.assertRaises(IndexError):
            timecode.parse_hms("12")
        with self.assertRaises(ValueError):
            timecode.parse_hms("xx:yy")


if __name__ == "__main__":
    unittest.main()
