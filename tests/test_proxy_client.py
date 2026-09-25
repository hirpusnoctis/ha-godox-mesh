from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from godox_mesh_bt.client import ProxyClient


@pytest.mark.asyncio
async def test_proxy_client_connect_disconnect() -> None:
    address = "AA:BB:CC:DD:EE:FF"
    mock_client = MagicMock()
    mock_client.connect = AsyncMock()
    mock_client.disconnect = AsyncMock()
    mock_client.is_connected = False

    def factory(addr: str) -> MagicMock:
        assert addr == address
        return mock_client

    client = ProxyClient(address, client_factory=factory)
    await client.connect()
    
    mock_client.connect.assert_called_once()
    
    # Simulate connection
    mock_client.is_connected = True
    assert client.is_connected is True
    
    await client.disconnect()
    mock_client.disconnect.assert_called_once()


@pytest.mark.asyncio
async def test_proxy_client_start_notify() -> None:
    address = "AA:BB:CC:DD:EE:FF"
    notifications: list[bytes] = []
    mock_client = MagicMock()
    mock_client.connect = AsyncMock()
    mock_client.start_notify = AsyncMock()
    mock_client.is_connected = False

    client = ProxyClient(address, client_factory=lambda _addr: mock_client)
    await client.connect()
    mock_client.is_connected = True

    await client.start_notify(lambda data: notifications.append(data))

    callback = mock_client.start_notify.call_args.args[1]
    callback("mock-char", bytearray(b"\x01\x02"))

    mock_client.start_notify.assert_called_once()
    assert notifications == [b"\x01\x02"]


@pytest.mark.asyncio
async def test_proxy_client_write_complete_message() -> None:
    address = "AA:BB:CC:DD:EE:FF"
    proxy_write_char = "00002add-0000-1000-8000-00805f9b34fb"
    # Network PDU (without Proxy Header)
    network_pdu = b"\x38\x00\x11\x22"
    
    mock_client = MagicMock()
    mock_client.write_gatt_char = AsyncMock()
    mock_client.is_connected = True

    client = ProxyClient(address, client_factory=MagicMock(return_value=mock_client))
    client._client = mock_client
    
    # We should have a higher-level method or handle it in write_proxy
    # Actually, pack_proxy_network_pdu already prepends 0x00
    # Let's say write_proxy takes the whole Proxy PDU
    full_pdu = b"\x00" + network_pdu
    await client.write_proxy(full_pdu)
    
    mock_client.write_gatt_char.assert_called_once_with(
        proxy_write_char, full_pdu, response=False
    )


@pytest.mark.asyncio
async def test_proxy_client_segments_a_message_to_fit_the_write_limit() -> None:
    """Every GATT write must fit, while preserving the original Proxy PDU type."""
    mock_client = MagicMock()
    mock_client.is_connected = True
    mock_client.max_write_without_response_size = 20
    mock_client.write_gatt_char = AsyncMock()
    client = ProxyClient("AA:BB", client_factory=lambda _address: mock_client)
    client._client = mock_client

    body = bytes(range(29))
    await client.write_proxy(b"\x02" + body)

    writes = [call.args[1] for call in mock_client.write_gatt_char.await_args_list]
    assert writes == [b"\x42" + body[:19], b"\xc2" + body[19:]]
    assert all(len(write) <= 20 for write in writes)


@pytest.mark.asyncio
async def test_proxy_client_uses_continuation_for_three_segments() -> None:
    mock_client = MagicMock()
    mock_client.is_connected = True
    mock_client.max_write_without_response_size = 10
    mock_client.write_gatt_char = AsyncMock()
    client = ProxyClient("AA:BB", client_factory=lambda _address: mock_client)
    client._client = mock_client

    body = bytes(range(24))
    await client.write_proxy(b"\x00" + body)

    writes = [call.args[1] for call in mock_client.write_gatt_char.await_args_list]
    assert writes == [
        b"\x40" + body[:9],
        b"\x80" + body[9:18],
        b"\xc0" + body[18:],
    ]


@pytest.mark.asyncio
async def test_proxy_notifications_are_reassembled_before_delivery() -> None:
    mock_client = MagicMock()
    mock_client.is_connected = True
    mock_client.start_notify = AsyncMock()
    client = ProxyClient("AA:BB", client_factory=lambda _address: mock_client)
    client._client = mock_client
    received: list[bytes] = []
    await client.start_notify(received.append)
    notify = mock_client.start_notify.call_args.args[1]

    notify("char", bytearray(b"\xc0orphan"))
    notify("char", bytearray(b"\x40first"))
    notify("char", bytearray(b"\x80middle"))
    assert received == []
    notify("char", bytearray(b"\xc0last"))
    assert received == [b"\x00firstmiddlelast"]

    notify("char", bytearray(b"\x42old"))
    notify("char", bytearray(b"\xc0wrong-type"))
    notify("char", bytearray(b"\x00complete"))
    assert received == [b"\x00firstmiddlelast", b"\x00complete"]


@pytest.mark.asyncio
async def test_disconnect_releases_ble_even_if_notify_session_is_gone() -> None:
    first = MagicMock()
    first.is_connected = True
    first.start_notify = AsyncMock()
    first.stop_notify = AsyncMock(side_effect=RuntimeError("No notify session started"))
    first.disconnect = AsyncMock()
    second = MagicMock()
    second.is_connected = False
    second.connect = AsyncMock()
    client = ProxyClient("AA:BB", client_factory=lambda _address: second)
    client._client = first
    await client.start_notify(lambda _data: None)

    await client.disconnect()

    first.disconnect.assert_awaited_once()
    assert client._client is None
    assert not client.is_connected
    await client.connect()
    assert client._client is second
