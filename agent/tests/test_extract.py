"""Retrieval correctness.

These tests answer the question the product lives or dies on: is the audio
returned for timestamp T actually the audio captured at T? The synthetic
source watermarks every second with its own wall-clock value, so decoding a
returned clip reveals precisely which moments it contains.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from ongoingrec.audio.watermark import CYCLE_SECONDS, decode_timeline
from ongoingrec.extract import (
    ExtractionError,
    NoRecordingError,
    extract_clip,
    plan_clip,
)
from ongoingrec.timeutil import parse_timestamp

from .helpers import decode_pcm, record_synthetic


def watermarks_of(clip_path, sample_rate):
    return decode_timeline(decode_pcm(clip_path, sample_rate), sample_rate)


def expected_from(start_dt, count):
    return [int(start_dt.timestamp() + i) % CYCLE_SECONDS for i in range(count)]


class TestWindowPlanning:
    def test_selects_only_the_containing_segment(self, recorded, config):
        """PRD section 16: 11:22:15 sits inside 11:00-11:30."""
        pieces, gaps = plan_clip(recorded, parse_timestamp("2026-08-12T11:22:15Z"), 600)
        assert gaps == []
        assert len(pieces) == 1
        assert pieces[0].segment.start == parse_timestamp("2026-08-12T11:00:00Z")
        assert pieces[0].start == parse_timestamp("2026-08-12T11:17:15Z")
        assert pieces[0].end == parse_timestamp("2026-08-12T11:27:15Z")

    def test_window_crossing_a_boundary_splits_across_two_segments(self, recorded):
        """PRD section 18: 11:29 with a 10-minute window spans 11:24-11:34."""
        pieces, gaps = plan_clip(recorded, parse_timestamp("2026-08-12T11:29:00Z"), 600)
        assert gaps == []
        assert len(pieces) == 2
        assert (pieces[0].start, pieces[0].end) == (
            parse_timestamp("2026-08-12T11:24:00Z"),
            parse_timestamp("2026-08-12T11:30:00Z"),
        )
        assert (pieces[1].start, pieces[1].end) == (
            parse_timestamp("2026-08-12T11:30:00Z"),
            parse_timestamp("2026-08-12T11:34:00Z"),
        )
        assert pieces[1].offset_into_segment == 0.0

    def test_offsets_into_each_segment_are_correct(self, recorded):
        pieces, _ = plan_clip(recorded, parse_timestamp("2026-08-12T11:29:00Z"), 600)
        assert pieces[0].offset_into_segment == pytest.approx(24 * 60)
        assert pieces[1].offset_into_segment == pytest.approx(0.0)

    def test_window_beyond_all_recording_is_not_found(self, recorded):
        with pytest.raises(NoRecordingError):
            plan_clip(recorded, parse_timestamp("2026-08-12T20:00:00Z"), 600)

    def test_window_is_trimmed_at_the_edge_of_recorded_audio(self, recorded):
        """A request near the end must not pad out time that never happened."""
        pieces, gaps = plan_clip(recorded, parse_timestamp("2026-08-12T12:28:00Z"), 600)
        assert gaps == []
        assert pieces[-1].end == parse_timestamp("2026-08-12T12:30:00Z")


class TestClipAlignment:
    def test_returned_audio_is_the_audio_from_the_requested_time(self, recorded, config):
        """The core guarantee, verified second by second."""
        requested = parse_timestamp("2026-08-12T11:22:15Z")
        clip = extract_clip(config, recorded, requested, window_seconds=600)

        assert clip.start == requested - timedelta(minutes=5)
        assert clip.end == requested + timedelta(minutes=5)
        assert clip.duration_seconds == pytest.approx(600, abs=0.1)
        assert not clip.is_partial

        decoded = watermarks_of(clip.path, config.sample_rate)
        assert len(decoded) >= 595
        assert decoded == expected_from(clip.start, len(decoded))

    def test_clip_spanning_a_segment_boundary_is_seamless(self, recorded, config):
        """PRD section 18, verified in the audio rather than in the metadata.

        A clip stitched from two segments must contain one unbroken run of
        seconds across the join -- no repeat, no skip, no silence.
        """
        requested = parse_timestamp("2026-08-12T11:29:00Z")
        clip = extract_clip(config, recorded, requested, window_seconds=600)

        assert len(clip.segment_ids) == 2
        assert clip.gaps == []

        decoded = watermarks_of(clip.path, config.sample_rate)
        assert decoded == expected_from(clip.start, len(decoded))

        # The join lands 6 minutes in; check that neighbourhood explicitly.
        join_index = 6 * 60
        around_join = decoded[join_index - 3 : join_index + 3]
        assert around_join == expected_from(
            clip.start + timedelta(seconds=join_index - 3), len(around_join)
        )

    def test_window_size_is_configurable(self, recorded, config):
        """PRD section 17: the retrieval window is a setting, not a constant."""
        requested = parse_timestamp("2026-08-12T11:22:15Z")
        clip = extract_clip(config, recorded, requested, window_seconds=120)
        assert clip.duration_seconds == pytest.approx(120, abs=0.1)
        assert clip.start == requested - timedelta(seconds=60)

        decoded = watermarks_of(clip.path, config.sample_rate)
        assert decoded == expected_from(clip.start, len(decoded))

    def test_default_window_comes_from_configuration(self, recorded, config):
        config.retrieval_window_seconds = 300
        clip = extract_clip(config, recorded, parse_timestamp("2026-08-12T11:22:15Z"))
        assert clip.window_seconds == 300
        assert clip.duration_seconds == pytest.approx(300, abs=0.1)

    def test_clip_at_the_exact_segment_boundary(self, recorded, config):
        requested = parse_timestamp("2026-08-12T11:30:00Z")
        clip = extract_clip(config, recorded, requested, window_seconds=600)
        decoded = watermarks_of(clip.path, config.sample_rate)
        assert decoded == expected_from(clip.start, len(decoded))


class TestGaps:
    @pytest.fixture
    def with_gap(self, config, db):
        """Two recording runs with a real 10-minute hole between them.

        This is what a laptop lid closing looks like: audio to 11:20, nothing
        until 11:30, then audio again.
        """
        record_synthetic(config, db, start=parse_timestamp("2026-08-12T11:00:00Z"), seconds=20 * 60)
        record_synthetic(config, db, start=parse_timestamp("2026-08-12T11:30:00Z"), seconds=20 * 60)
        return db

    def test_gap_is_reported_not_hidden(self, with_gap, config):
        clip = extract_clip(
            config, with_gap, parse_timestamp("2026-08-12T11:25:00Z"), window_seconds=1200
        )
        assert len(clip.gaps) == 1
        assert clip.gaps[0].start == parse_timestamp("2026-08-12T11:20:00Z")
        assert clip.gaps[0].end == parse_timestamp("2026-08-12T11:30:00Z")
        assert clip.gap_seconds == pytest.approx(600, abs=0.1)
        assert clip.is_partial

    def test_audio_after_a_gap_stays_at_its_true_offset(self, with_gap, config):
        """The reason gaps are padded rather than closed up.

        Without padding, 11:30's audio would appear ten minutes early in the
        clip and anyone timing the recording would misread it.
        """
        clip = extract_clip(
            config, with_gap, parse_timestamp("2026-08-12T11:25:00Z"), window_seconds=1200
        )
        decoded = watermarks_of(clip.path, config.sample_rate)

        expected: list[int | None] = []
        for i in range(len(decoded)):
            moment = clip.start + timedelta(seconds=i)
            in_gap = clip.gaps[0].start <= moment < clip.gaps[0].end
            expected.append(None if in_gap else int(moment.timestamp()) % CYCLE_SECONDS)

        # Allow the two windows straddling each gap edge to decode either way.
        edges = {
            int((clip.gaps[0].start - clip.start).total_seconds()),
            int((clip.gaps[0].end - clip.start).total_seconds()) - 1,
        }
        mismatches = [
            (i, decoded[i], expected[i])
            for i in range(len(decoded))
            if decoded[i] != expected[i] and i not in edges
        ]
        assert mismatches == []

    def test_sample_rounding_at_a_boundary_is_not_reported_as_a_gap(self, config, db):
        """A recording that did not start exactly on the grid still yields
        gap-free clips across its boundaries.

        Each segment ends a fraction of a sample before the next begins. Left
        untreated that rounding remainder would appear as a gap in the
        metadata of every boundary-crossing clip, teaching whoever reads it to
        ignore the gap field entirely.
        """
        config.segment_seconds = 60
        start = parse_timestamp("2026-08-12T11:00:20Z") + timedelta(microseconds=337103)
        record_synthetic(config, db, start=start, seconds=180, block_seconds=0.7)

        clip = extract_clip(config, db, parse_timestamp("2026-08-12T11:01:00Z"), window_seconds=60)

        assert clip.gaps == []
        assert not clip.is_partial
        assert len(clip.segment_ids) == 2

    def test_leading_gap_is_trimmed_rather_than_padded(self, config, db):
        """A request before recording began returns audio, not a silent prefix."""
        record_synthetic(config, db, start=parse_timestamp("2026-08-12T11:00:00Z"), seconds=600)
        clip = extract_clip(config, db, parse_timestamp("2026-08-12T11:02:00Z"), window_seconds=600)
        assert clip.start == parse_timestamp("2026-08-12T11:00:00Z")
        assert clip.gaps == []
        assert clip.is_partial  # shorter than asked for, and says so

        decoded = watermarks_of(clip.path, config.sample_rate)
        assert decoded == expected_from(clip.start, len(decoded))


class TestFailureModes:
    def test_no_recording_at_all(self, config, db):
        with pytest.raises(NoRecordingError):
            extract_clip(config, db, parse_timestamp("2026-08-12T11:22:15Z"))

    def test_index_row_whose_file_vanished_is_skipped(self, recorded, config):
        segment = recorded.segments_overlapping(
            parse_timestamp("2026-08-12T11:00:00Z"),
            parse_timestamp("2026-08-12T11:30:00Z"),
        )[0]
        segment.file_path.unlink()

        # 11:05 is covered only by the deleted file, so nothing can be built.
        with pytest.raises(NoRecordingError):
            extract_clip(config, recorded, parse_timestamp("2026-08-12T11:05:00Z"), window_seconds=60)

    def test_partially_deleted_window_still_returns_what_remains(self, recorded, config):
        segment = recorded.segments_overlapping(
            parse_timestamp("2026-08-12T11:00:00Z"),
            parse_timestamp("2026-08-12T11:30:00Z"),
        )[0]
        segment.file_path.unlink()

        clip = extract_clip(
            config, recorded, parse_timestamp("2026-08-12T11:29:00Z"), window_seconds=600
        )
        assert clip.start == parse_timestamp("2026-08-12T11:30:00Z")
        assert clip.is_partial

    def test_rejects_nonpositive_window(self, recorded, config):
        with pytest.raises(ValueError):
            extract_clip(config, recorded, parse_timestamp("2026-08-12T11:22:15Z"), window_seconds=0)

    def test_metadata_round_trips_to_json(self, recorded, config):
        clip = extract_clip(config, recorded, parse_timestamp("2026-08-12T11:22:15Z"))
        data = clip.to_dict()
        assert data["clip_start"].endswith("Z")
        assert data["segment_ids"]
        assert data["partial"] is False
