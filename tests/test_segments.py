from __future__ import annotations

from datetime import timedelta

import pytest

from ongoingrec.audio.watermark import decode_timeline
from ongoingrec.index import STATUS_COMPLETE, STATUS_MISSING, STATUS_TRUNCATED
from ongoingrec.segments import repair_open_segments, segment_path
from ongoingrec.timeutil import parse_timestamp

from .helpers import decode_pcm, record_synthetic


class TestSegmentPaths:
    def test_path_is_utc_and_says_so(self, config):
        path = segment_path(config.recordings_dir, parse_timestamp("2026-08-12T11:00:00Z"))
        assert path.parent.name == "2026-08-12"
        assert path.name == "11-00-00Z.mp3"


class TestSegmentation:
    def test_ninety_minutes_produces_three_aligned_segments(self, config, db):
        """PRD section 6: continuous audio in 30-minute segments on the grid."""
        start = parse_timestamp("2026-08-12T11:00:00Z")
        record_synthetic(config, db, start=start, seconds=90 * 60)

        segments = db.segments_overlapping(start, start + timedelta(hours=2))
        assert len(segments) == 3
        assert [s.start for s in segments] == [
            parse_timestamp("2026-08-12T11:00:00Z"),
            parse_timestamp("2026-08-12T11:30:00Z"),
            parse_timestamp("2026-08-12T12:00:00Z"),
        ]
        for segment in segments:
            assert segment.status == STATUS_COMPLETE
            assert segment.duration_seconds == pytest.approx(1800, abs=0.01)
            assert segment.file_path.exists()

    def test_starting_mid_window_snaps_the_next_segment_back_to_the_grid(self, config, db):
        """A service started at 11:07 must not put every later boundary at :07.

        The first segment runs short to 11:30; everything after it is aligned,
        which is what makes "which segment holds 11:22:15" a pure lookup.
        """
        start = parse_timestamp("2026-08-12T11:07:00Z")
        record_synthetic(config, db, start=start, seconds=60 * 60)

        segments = db.segments_overlapping(start, start + timedelta(hours=2))
        assert [s.start for s in segments] == [
            parse_timestamp("2026-08-12T11:07:00Z"),
            parse_timestamp("2026-08-12T11:30:00Z"),
            parse_timestamp("2026-08-12T12:00:00Z"),
        ]
        assert segments[0].duration_seconds == pytest.approx(23 * 60, abs=0.01)
        assert segments[1].duration_seconds == pytest.approx(1800, abs=0.01)

    def test_a_short_run_produces_one_short_segment(self, config, db):
        start = parse_timestamp("2026-08-12T11:00:00Z")
        record_synthetic(config, db, start=start, seconds=45)

        segments = db.segments_overlapping(start, start + timedelta(minutes=5))
        assert len(segments) == 1
        assert segments[0].duration_seconds == pytest.approx(45, abs=0.5)
        assert segments[0].status == STATUS_COMPLETE

    def test_buffer_boundaries_do_not_have_to_align_with_segment_boundaries(self, config, db):
        """A block straddling :30 is split sample-exactly, losing nothing.

        7-second blocks divide neither into 1800 nor into the run length, so
        the boundary lands mid-block every time.
        """
        start = parse_timestamp("2026-08-12T11:00:00Z")
        record_synthetic(config, db, start=start, seconds=35 * 60, block_seconds=7.0)

        segments = db.segments_overlapping(start, start + timedelta(hours=1))
        assert segments[0].duration_seconds == pytest.approx(1800, abs=0.01)
        total = sum(s.duration_seconds for s in segments)
        assert total == pytest.approx(35 * 60, abs=0.5)


class TestRotationRounding:
    """Regression tests for a bug a live run caught.

    Recording from 12:56:20.337103 produced a zero-length segment at
    12:56:59.999978. The next segment's start was recomputed as
    ``stream_start + captured_samples / rate``, and because the previous
    segment's capacity had been rounded to a whole number of samples, that
    landed a third of a sample *before* the boundary -- so a new segment was
    opened with 22 microseconds of capacity, filled instantly, and closed
    empty. Over a long day the same rounding would also let the grid drift.
    """

    @pytest.fixture
    def rotated(self, config, db):
        config.segment_seconds = 60
        # Deliberately awkward: a fractional offset from the boundary, so
        # every rotation has a rounding remainder to mishandle.
        start = parse_timestamp("2026-08-12T11:00:20Z") + timedelta(
            microseconds=337103
        )
        record_synthetic(config, db, start=start, seconds=245, block_seconds=0.7)
        return db.segments_overlapping(start, start + timedelta(minutes=10))

    def test_no_zero_length_segments_are_created(self, rotated):
        assert rotated
        for segment in rotated:
            assert segment.duration_seconds >= 1.0, f"sliver segment at {segment.start}"

    def test_segments_land_exactly_on_the_grid_after_the_first(self, rotated):
        for segment in rotated[1:]:
            assert (segment.start.second, segment.start.microsecond) == (0, 0)

    def test_segments_are_contiguous_to_within_a_sample(self, rotated, config):
        """Segments must join with no overlap and no real gap.

        Exact equality is not achievable and should not be asserted: a segment
        contains a whole number of samples, so when its span is not an exact
        multiple of the sample period its end lands a fraction of a sample
        before the boundary the next segment starts on. Something has to
        absorb that fraction. What matters is that the discrepancy stays below
        one sample -- extraction treats anything that small as rounding rather
        than as a recording hole.
        """
        sample_period = 1.0 / config.sample_rate
        for earlier, later in zip(rotated, rotated[1:]):
            drift = (later.start - earlier.end).total_seconds()
            assert 0 <= drift < sample_period, f"{earlier.end} -> {later.start}"

    def test_no_audio_is_lost_across_many_rotations(self, rotated):
        total = sum(segment.duration_seconds for segment in rotated)
        assert total == pytest.approx(245, abs=0.05)


class TestTimelineIntegrity:
    """The audio at offset d must be the audio captured at segment.start + d."""

    def test_segment_audio_is_aligned_to_its_recorded_start_time(self, config, db):
        start = parse_timestamp("2026-08-12T11:00:00Z")
        record_synthetic(config, db, start=start, seconds=40 * 60)

        segment = db.segments_overlapping(
            parse_timestamp("2026-08-12T11:30:00Z"),
            parse_timestamp("2026-08-12T11:31:00Z"),
        )[0]
        assert segment.start == parse_timestamp("2026-08-12T11:30:00Z")

        samples = decode_pcm(segment.file_path, config.sample_rate)
        decoded = decode_timeline(samples[: config.sample_rate * 10], config.sample_rate)
        expected = [int(segment.start.timestamp() + i) % 900 for i in range(len(decoded))]
        assert decoded == expected

    def test_audio_is_continuous_across_a_segment_boundary(self, config, db):
        """No sample is lost or duplicated where one segment ends and the next begins."""
        start = parse_timestamp("2026-08-12T11:00:00Z")
        record_synthetic(config, db, start=start, seconds=35 * 60)
        first, second = db.segments_overlapping(start, start + timedelta(hours=1))[:2]

        tail = decode_pcm(first.file_path, config.sample_rate)[-config.sample_rate * 5 :]
        head = decode_pcm(second.file_path, config.sample_rate)[: config.sample_rate * 5]

        boundary = second.start.timestamp()
        assert decode_timeline(tail, config.sample_rate) == [
            int(boundary - 5 + i) % 900 for i in range(5)
        ]
        assert decode_timeline(head, config.sample_rate) == [
            int(boundary + i) % 900 for i in range(5)
        ]


class TestCrashRecovery:
    def test_interrupted_segment_is_recovered_with_its_true_duration(self, config, db):
        """Power loss mid-segment must not discard the audio already written."""
        start = parse_timestamp("2026-08-12T11:00:00Z")
        record_synthetic(config, db, start=start, seconds=5 * 60)
        segment = db.segments_overlapping(start, start + timedelta(minutes=10))[0]

        # Simulate the crash: the row goes back to 'recording' with no end,
        # exactly as it would be found after an abrupt power cut.
        db.conn.execute(
            "UPDATE segments SET status='recording', end_epoch=NULL, end_utc=NULL WHERE id=?",
            (segment.id,),
        )

        assert repair_open_segments(config, db) == 1
        repaired = db.get_segment(segment.id)
        assert repaired.status == STATUS_TRUNCATED
        assert repaired.duration_seconds == pytest.approx(300, abs=1.0)
        assert repaired.end is not None

    def test_row_without_a_file_is_marked_missing(self, config, db):
        start = parse_timestamp("2026-08-12T11:00:00Z")
        record_synthetic(config, db, start=start, seconds=60)
        segment = db.segments_overlapping(start, start + timedelta(minutes=5))[0]

        segment.file_path.unlink()
        db.conn.execute("UPDATE segments SET status='recording' WHERE id=?", (segment.id,))

        repair_open_segments(config, db)
        assert db.get_segment(segment.id).status == STATUS_MISSING

    def test_missing_segments_are_excluded_from_retrieval(self, config, db):
        start = parse_timestamp("2026-08-12T11:00:00Z")
        record_synthetic(config, db, start=start, seconds=60)
        segment = db.segments_overlapping(start, start + timedelta(minutes=5))[0]

        db.mark_status(segment.id, STATUS_MISSING)
        assert db.segments_overlapping(start, start + timedelta(minutes=5)) == []
