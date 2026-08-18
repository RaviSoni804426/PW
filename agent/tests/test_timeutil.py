from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ongoingrec import timeutil


class TestParseTimestamp:
    def test_naive_is_interpreted_as_laptop_local_time(self):
        # The PRD's example timestamp, in IST, is 05:52:15Z.
        parsed = timeutil.parse_timestamp("2026-08-12T11:22:15")
        assert parsed == datetime(2026, 8, 12, 5, 52, 15, tzinfo=timezone.utc)

    def test_explicit_offset_is_honoured(self):
        parsed = timeutil.parse_timestamp("2026-08-12T11:22:15+00:00")
        assert parsed == datetime(2026, 8, 12, 11, 22, 15, tzinfo=timezone.utc)

    def test_zulu_suffix(self):
        assert timeutil.parse_timestamp("2026-08-12T11:22:15Z") == datetime(
            2026, 8, 12, 11, 22, 15, tzinfo=timezone.utc
        )

    @pytest.mark.parametrize("bad", ["", "   ", "not-a-time", "2026-13-45T99:99:99"])
    def test_rejects_garbage(self, bad):
        with pytest.raises(ValueError):
            timeutil.parse_timestamp(bad)

    def test_round_trips_through_format(self):
        original = datetime(2026, 8, 12, 11, 0, 0, tzinfo=timezone.utc)
        assert timeutil.parse_timestamp(timeutil.format_utc(original)) == original


class TestBoundaries:
    @pytest.mark.parametrize(
        "moment,expected_hour,expected_minute",
        [
            ("2026-08-12T11:00:00Z", 11, 0),
            ("2026-08-12T11:00:01Z", 11, 0),
            ("2026-08-12T11:22:15Z", 11, 0),
            ("2026-08-12T11:29:59Z", 11, 0),
            ("2026-08-12T11:30:00Z", 11, 30),
            ("2026-08-12T11:59:59Z", 11, 30),
        ],
    )
    def test_floor_lands_on_the_half_hour_grid(self, moment, expected_hour, expected_minute):
        got = timeutil.floor_to_boundary(timeutil.parse_timestamp(moment), 1800)
        assert (got.hour, got.minute, got.second) == (expected_hour, expected_minute, 0)

    def test_next_boundary_is_strictly_later_even_on_a_boundary(self):
        on_boundary = timeutil.parse_timestamp("2026-08-12T11:30:00Z")
        assert timeutil.next_boundary(on_boundary, 1800) == timeutil.parse_timestamp(
            "2026-08-12T12:00:00Z"
        )

    def test_grid_is_continuous_across_midnight(self):
        before = timeutil.parse_timestamp("2026-08-12T23:59:59Z")
        assert timeutil.next_boundary(before, 1800) == timeutil.parse_timestamp(
            "2026-08-13T00:00:00Z"
        )

    def test_rejects_nonpositive_period(self):
        with pytest.raises(ValueError):
            timeutil.floor_to_boundary(timeutil.utcnow(), 0)


class TestMonotonicClock:
    def test_elapsed_time_is_independent_of_the_system_clock(self, monkeypatch):
        """A clock jump mid-segment must not change measured elapsed time.

        This is the whole reason MonotonicClock exists: an NTP correction or a
        manual clock change while a 30-minute segment is recording would
        otherwise silently corrupt that segment's timeline.
        """
        anchor_wall = datetime(2026, 8, 12, 11, 0, 0, tzinfo=timezone.utc)
        clock = timeutil.MonotonicClock(wall_anchor=anchor_wall, mono_anchor=1000.0)

        monkeypatch.setattr(timeutil.time, "monotonic", lambda: 1600.0)
        assert clock.now() == anchor_wall + timedelta(seconds=600)

        # System clock leaps an hour forward; elapsed time is unmoved.
        monkeypatch.setattr(
            timeutil, "utcnow", lambda: anchor_wall + timedelta(seconds=600, hours=1)
        )
        assert clock.now() == anchor_wall + timedelta(seconds=600)
        assert clock.drift() == pytest.approx(3600.0)

    def test_at_elapsed_maps_offsets_to_wall_clock(self):
        anchor = datetime(2026, 8, 12, 11, 0, 0, tzinfo=timezone.utc)
        clock = timeutil.MonotonicClock(wall_anchor=anchor, mono_anchor=0.0)
        assert clock.at_elapsed(1800) == datetime(2026, 8, 12, 11, 30, tzinfo=timezone.utc)


class TestSampleConversion:
    def test_round_trip(self):
        assert timeutil.samples_to_seconds(16000 * 90, 16000) == 90.0
        assert timeutil.seconds_to_samples(90.0, 16000) == 16000 * 90

    def test_rejects_bad_rate(self):
        with pytest.raises(ValueError):
            timeutil.samples_to_seconds(100, 0)
