import unittest
from datetime import datetime, timedelta, timezone

from radar.storage.identifiers import (
    CHANGE,
    CHECK,
    KINDS,
    SNAPSHOT,
    SOURCE,
    format_timestamp,
    is_valid_id,
    new_id,
    parse_timestamp,
    utc_now,
)


class IdentifierTests(unittest.TestCase):
    def test_new_ids_have_kind_prefix_and_random_part(self):
        for kind in (SOURCE, CHECK, SNAPSHOT, CHANGE):
            with self.subTest(kind=kind):
                value = new_id(kind)
                self.assertRegex(value, rf"^{kind}_[0-9a-f]{{32}}$")
                self.assertTrue(is_valid_id(value, kind))

    def test_new_ids_are_unique(self):
        values = {new_id(CHECK) for _ in range(10_000)}
        self.assertEqual(len(values), 10_000)

    def test_rejects_unknown_kind(self):
        for kind in ("", "check", "x", None):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                new_id(kind)

    def test_kinds_are_the_four_record_types(self):
        self.assertEqual(KINDS, {"src", "chk", "snap", "chg"})

    def test_validates_kind(self):
        value = new_id(SNAPSHOT)
        self.assertTrue(is_valid_id(value))
        self.assertTrue(is_valid_id(value, SNAPSHOT))
        self.assertFalse(is_valid_id(value, CHECK))

    def test_rejects_malformed_ids(self):
        good = new_id(CHECK)
        for bad in (
            None, 7, "", "chk_", "chk_123", good.upper(), good + "0", good[:-1],
            "chk-" + good[4:], "xyz_" + good[4:], " " + good, good + "\n",
            "chk_" + "g" * 32, "chk_../../etc",
        ):
            with self.subTest(value=bad):
                self.assertFalse(is_valid_id(bad))


class TimestampTests(unittest.TestCase):
    def test_round_trips_in_utc_with_microseconds(self):
        value = datetime(2026, 10, 12, 15, 56, 1, 42, tzinfo=timezone.utc)
        text = format_timestamp(value)
        self.assertEqual(text, "2026-10-12T15:56:01.000042+00:00")
        self.assertEqual(parse_timestamp(text), value)

    def test_converts_other_timezones_to_utc(self):
        pacific = timezone(timedelta(hours=-7))
        value = datetime(2026, 10, 12, 8, 0, tzinfo=pacific)
        self.assertEqual(format_timestamp(value), "2026-10-12T15:00:00.000000+00:00")

    def test_text_order_is_time_order(self):
        times = [
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 1, 0, 0, 0, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc),
            datetime(2026, 12, 31, 23, 59, 59, 999999, tzinfo=timezone.utc),
        ]
        texts = [format_timestamp(t) for t in times]
        self.assertEqual(sorted(texts), texts)
        self.assertEqual(len(set(map(len, texts))), 1)

    def test_rejects_naive_datetimes(self):
        with self.assertRaises(ValueError):
            format_timestamp(datetime(2026, 10, 12, 15, 0))

    def test_rejects_non_datetimes(self):
        for bad in ("2026-10-12", 0, None):
            with self.subTest(value=bad), self.assertRaises(TypeError):
                format_timestamp(bad)

    def test_parse_accepts_only_stored_format(self):
        for bad in (
            None, "", "2026-10-12", "2026-10-12T15:00:00+00:00",
            "2026-10-12T15:00:00.000000Z", "2026-10-12T15:00:00.000000-07:00",
            "2026-10-12 15:00:00.000000+00:00", "2026-13-12T15:00:00.000000+00:00",
        ):
            with self.subTest(text=bad), self.assertRaises(ValueError):
                parse_timestamp(bad)

    def test_utc_now_is_aware_and_formattable(self):
        now = utc_now()
        self.assertEqual(now.utcoffset(), timedelta(0))
        self.assertEqual(parse_timestamp(format_timestamp(now)), now)


if __name__ == "__main__":
    unittest.main()
