import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from worker.main import process, open_geo
from worker.enrich import COLUMNS


@pytest.mark.asyncio
async def test_process_valid_batch():
    mock_rds = AsyncMock()
    mock_ch = MagicMock()

    valid_event = {
        "event_id": "11111111-1111-1111-1111-111111111111",
        "ts": "2026-10-16T18:00:00Z",
        "qr_id": "22222222-2222-2222-2222-222222222222",
        "workspace_id": "33333333-3333-3333-3333-333333333333",
        "host": "link.empresa.com",
        "slug": "promo-sp",
        "destination_url": "https://empresa.com/sp",
        "ip": "200.100.50.25",
        "ua": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)",
        "referer": "https://instagram.com",
        "lang": "pt-BR",
        "prefetch": "0",
        "geo_country": "BR",
    }

    msgs = [("1690000000000-0", valid_event)]

    await process(mock_rds, mock_ch, msgs, salt="test-salt-lgpd", geo=None)

    # ClickHouse insert called with rows
    mock_ch.insert.assert_called_once()
    args, kwargs = mock_ch.insert.call_args
    assert args[0] == "scans"
    rows = args[1]
    assert len(rows) == 1
    assert kwargs["column_names"] == COLUMNS

    # Redis ACK called for message
    mock_rds.xack.assert_called_once_with("scans", "analytics", "1690000000000-0")
    # No DLQ writes
    mock_rds.xadd.assert_not_called()


@pytest.mark.asyncio
async def test_process_malformed_event_routes_to_dlq():
    mock_rds = AsyncMock()
    mock_ch = MagicMock()

    # Event missing mandatory fields causing enrich to raise Exception
    bad_event = {"corrupted": "data"}
    msgs = [("bad-msg-id-1", bad_event)]

    await process(mock_rds, mock_ch, msgs, salt="test-salt-lgpd", geo=None)

    # ClickHouse shouldn't receive rows
    mock_ch.insert.assert_not_called()

    # DLQ should receive the error and event
    mock_rds.xadd.assert_called_once()
    dlq_args, dlq_kwargs = mock_rds.xadd.call_args
    assert dlq_args[0] == "scans:dlq"
    assert dlq_args[1]["id"] == "bad-msg-id-1"
    assert "KeyError" in dlq_args[1]["error"]

    # The bad message must still be ACKed so consumer does not get blocked indefinitely
    mock_rds.xack.assert_called_once_with("scans", "analytics", "bad-msg-id-1")


def test_open_geo_missing_file():
    with patch("pathlib.Path.exists", return_value=False):
        reader = open_geo()
        assert reader is None
