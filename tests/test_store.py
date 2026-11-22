import pytest

from reader import RecordNotFound, fetch_record
from store import RECORD_SIZE, Store, pack_record, parse_video_id, unpack_record


def customers(n: int) -> dict[int, dict]:
    return {i: {"id": i, "name": f"Customer {i}", "pad": "x" * 100} for i in range(1, n + 1)}


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path)


def test_pack_roundtrip_and_fixed_size():
    rec = {"id": 1, "name": "Zoë ✓"}
    raw = pack_record(rec)
    assert len(raw) == RECORD_SIZE
    assert unpack_record(raw) == rec


def test_pack_rejects_oversized_record():
    with pytest.raises(ValueError, match="too large"):
        pack_record({"blob": "x" * RECORD_SIZE})


def test_every_record_roundtrips(store):
    data = customers(40)  # 40 * 512 B spans 7 frames, so some records straddle two frames
    store.write_segment(data)
    for i, expected in data.items():
        record, _ = fetch_record(store, i)
        assert record == expected


def test_straddling_record_reads_two_frames(store):
    store.write_segment(customers(10))
    # record 7 lives at bytes 3072..3583; a frame carries 3335 payload bytes
    _, meta = fetch_record(store, 7)
    assert meta["frames"] == [0, 1]
    assert meta["source"] == "local"


def test_unknown_record(store):
    store.write_segment(customers(3))
    with pytest.raises(RecordNotFound):
        fetch_record(store, 99)


def test_updates_are_append_only_with_eventual_consistency(store):
    store.write_segment(customers(10))
    assert store.queue_update(3, {"name": "Renamed"}) == 1

    record, _ = fetch_record(store, 3)
    assert record["name"] == "Customer 3"  # not visible before the flush

    segment = store.flush()
    assert segment.name == "seg_0002"
    assert segment.frames == 1  # only the changed record was re-encoded
    assert store.stats() == {"segments": 2, "uploaded_segments": 0, "records": 10, "pending": 0}

    record, meta = fetch_record(store, 3)
    assert record == {"name": "Renamed", "id": 3}
    assert meta["segment"] == "seg_0002"

    other, meta = fetch_record(store, 4)  # untouched record still comes from the old video
    assert other["name"] == "Customer 4"
    assert meta["segment"] == "seg_0001"


def test_flush_with_nothing_pending(store):
    assert store.flush() is None


def test_queue_update_rejects_oversized_record(store):
    with pytest.raises(ValueError):
        store.queue_update(1, {"blob": "x" * 600})
    assert store.stats()["pending"] == 0


def test_register_and_youtube_fallback(store):
    store.write_segment(customers(3))
    _, meta = fetch_record(store, 1, source="youtube")  # not uploaded yet
    assert meta["source"] == "local-fallback"

    store.register("seg_0001", "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert store.segment("seg_0001").youtube_id == "dQw4w9WgXcQ"
    with pytest.raises(KeyError):
        store.register("seg_0099", "dQw4w9WgXcQ")


@pytest.mark.parametrize(
    "value,expected",
    [
        ("dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=3", "dQw4w9WgXcQ"),
    ],
)
def test_parse_video_id(value, expected):
    assert parse_video_id(value) == expected


@pytest.mark.parametrize("bad", ["", "short", "not a url at all!!", "https://example.com/watch?v=x"])
def test_parse_video_id_rejects_junk(bad):
    with pytest.raises(ValueError):
        parse_video_id(bad)
