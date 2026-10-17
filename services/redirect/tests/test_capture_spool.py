import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis
import pytest

from app.capture import EventPipeline, build_event, client_ip


@pytest.fixture
def fake_async_redis():
    server = fakeredis.FakeServer()
    return fakeredis.FakeAsyncRedis(server=server, decode_responses=True)


def test_client_ip_extraction():
    headers = {
        "cf-connecting-ip": "203.0.113.195",
        "x-forwarded-for": "10.0.0.1, 10.0.0.2",
    }
    # Trusted header present
    assert client_ip(headers, "192.168.1.1", "cf-connecting-ip") == "203.0.113.195"
    # Trusted header absent, fallback to peer
    assert client_ip({}, "192.168.1.1", "cf-connecting-ip") == "192.168.1.1"
    # Both absent
    assert client_ip({}, None, "cf-connecting-ip") == ""


def test_build_event():
    headers = {
        "user-agent": "Mozilla/5.0 Test",
        "referer": "https://google.com",
        "accept-language": "pt-BR,pt;q=0.9",
        "sec-purpose": "prefetch",
        "cf-ipcountry": "BR",
    }
    payload = {
        "qr_id": "11111111-1111-1111-1111-111111111111",
        "workspace_id": "22222222-2222-2222-2222-222222222222",
    }
    event = build_event(
        headers,
        "127.0.0.1",
        payload,
        host="meuqr.com",
        slug="blackfriday",
        destination="https://loja.com/promo",
        trusted_ip_header="cf-connecting-ip",
        country_header="cf-ipcountry",
    )
    assert event["host"] == "meuqr.com"
    assert event["slug"] == "blackfriday"
    assert event["destination_url"] == "https://loja.com/promo"
    assert event["prefetch"] == "1"
    assert event["geo_country"] == "BR"
    assert event["ua"] == "Mozilla/5.0 Test"
    assert event["referer"] == "https://google.com"


@pytest.mark.asyncio
async def test_queue_overflow_triggers_immediate_spool(tmp_path: Path, fake_async_redis):
    # Queue with capacity of 2 items
    pipeline = EventPipeline(
        fake_async_redis,
        stream="scans",
        maxlen=1000,
        spool_dir=tmp_path,
        queue_size=2,
    )
    ev1 = {"event_id": "1", "slug": "s1"}
    ev2 = {"event_id": "2", "slug": "s2"}
    ev3 = {"event_id": "3", "slug": "s3"}

    pipeline.capture(ev1)
    pipeline.capture(ev2)
    assert pipeline.queue.qsize() == 2
    assert pipeline.spooled == 0

    # 3rd item overflows queue and must be spooled immediately
    pipeline.capture(ev3)
    assert pipeline.spooled == 1

    spool_files = list(tmp_path.glob("spool-*.jsonl"))
    assert len(spool_files) == 1
    content = spool_files[0].read_text()
    assert '"event_id": "3"' in content


@pytest.mark.asyncio
async def test_stop_gracefully_flushes_and_spools(tmp_path: Path):
    # Redis that fails on send
    class FailRedis:
        def pipeline(self, transaction=False):
            raise ConnectionError("Redis is completely down")

    pipeline = EventPipeline(
        FailRedis(),
        stream="scans",
        maxlen=1000,
        spool_dir=tmp_path,
        queue_size=10,
    )
    pipeline.start()

    ev = {"event_id": "100", "slug": "stop-test"}
    pipeline.capture(ev)

    # Stop should drain queue, attempt send, fail, and spool to disk
    await pipeline.stop()
    assert pipeline.spooled >= 1
    spool_files = list(tmp_path.glob("spool-*.jsonl"))
    assert len(spool_files) >= 1
    assert any('"event_id": "100"' in f.read_text() for f in spool_files)


@pytest.mark.asyncio
async def test_replay_spool_success(tmp_path: Path, fake_async_redis):
    pipeline = EventPipeline(
        fake_async_redis,
        stream="scans",
        maxlen=1000,
        spool_dir=tmp_path,
        queue_size=10,
    )
    # Create mock spool file
    spool_file = tmp_path / "spool-999.jsonl"
    events = [
        {"event_id": f"e{i}", "slug": f"slug-{i}", "ts": "2026-10-16T12:00:00Z"}
        for i in range(5)
    ]
    spool_file.write_text("\n".join(json.dumps(e) for e in events) + "\n")

    replayed = await pipeline.replay_spool()
    assert replayed == 5
    # The spool file should have been cleaned up
    assert not spool_file.exists()
    assert len(list(tmp_path.glob("spool-*.jsonl"))) == 0

    # Events should now be in Redis stream
    entries = await fake_async_redis.xrange("scans")
    assert len(entries) == 5
    assert entries[0][1]["slug"] == "slug-0"


@pytest.mark.asyncio
async def test_replay_spool_redis_down(tmp_path: Path):
    class FailRedis:
        def pipeline(self, transaction=False):
            raise ConnectionError("Redis down")

    pipeline = EventPipeline(
        FailRedis(),
        stream="scans",
        maxlen=1000,
        spool_dir=tmp_path,
        queue_size=10,
    )
    spool_file = tmp_path / "spool-123.jsonl"
    events = [{"event_id": "e1", "slug": "s1"}]
    spool_file.write_text(json.dumps(events[0]) + "\n")

    replayed = await pipeline.replay_spool()
    assert replayed == 0
    # Spool file or new spool file must still preserve the event
    files = list(tmp_path.glob("spool-*.jsonl"))
    assert len(files) >= 1
    assert any('"event_id": "e1"' in f.read_text() for f in files)


def test_spool_oserror_handled(tmp_path: Path, fake_async_redis):
    pipeline = EventPipeline(
        fake_async_redis,
        stream="scans",
        maxlen=1000,
        spool_dir=tmp_path,
        queue_size=10,
    )
    with patch("builtins.open", side_effect=OSError("Disk write failed")):
        # Should not raise exception
        pipeline._spool([{"event_id": "1"}])
