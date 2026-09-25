"""BLE client wrappers for raw GATT and Mesh Proxy traffic.

Examples
--------
>>> MESH_PROXY_DATA_IN_UUID
'00002add-0000-1000-8000-00805f9b34fb'
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from bleak import BleakClient, BleakScanner

logger = logging.getLogger(__name__)


NotificationCallback = Callable[[bytes], None]
DeviceResolver = Callable[[str], Awaitable[Any | None]]

MESH_PROXY_DATA_IN_UUID = "00002add-0000-1000-8000-00805f9b34fb"
MESH_PROXY_DATA_OUT_UUID = "00002ade-0000-1000-8000-00805f9b34fb"

# The Proxy header carries two SAR bits above its six-bit message type. A
# minimum ATT MTU of 23 leaves 20 bytes for each characteristic value.
_SAR_COMPLETE = 0
_SAR_FIRST = 1
_SAR_CONTINUATION = 2
_SAR_LAST = 3
_DEFAULT_WRITE_SIZE = 20
_SAR_TIMEOUT_SECONDS = 20.0
_MAX_PROXY_MESSAGE_BYTES = 384


class ProxyClient:
    """Small BLE wrapper for Bluetooth Mesh Proxy characteristics.

    Parameters
    ----------
    address
        Platform-specific BLE address or identifier.
    client_factory
        Optional Bleak-compatible client factory for tests or custom transports.

    Examples
    --------
    >>> client = ProxyClient("AA:BB", client_factory=lambda address: object())
    >>> client.address
    'AA:BB'
    """

    def __init__(
        self,
        address: str,
        *,
        client_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self.address = address
        self._client_factory = client_factory or BleakClient
        self._client: Any | None = None
        self._callbacks: list[NotificationCallback] = []
        self._notifications_started = False
        self._write_lock = asyncio.Lock()
        self._rx_type: int | None = None
        self._rx_body = bytearray()
        self._rx_started_at = 0.0

    @property
    def is_connected(self) -> bool:
        """Return whether the underlying BLE client is connected.

        Returns
        -------
        bool
            ``True`` when a client exists and reports ``is_connected``.

        Examples
        --------
        >>> ProxyClient("AA:BB", client_factory=lambda address: object()).is_connected
        False
        """

        return bool(self._client and getattr(self._client, "is_connected", False))

    async def connect(self) -> None:
        """Connect the proxy client if it is not already connected.

        Returns
        -------
        None
            The underlying BLE connection is opened in place. Reconnecting
            after a dropped link resets notification state, so callers must
            re-register any notification callbacks afterwards.

        Examples
        --------
        >>> import asyncio
        >>> class FakeClient:
        ...     is_connected = False
        ...     async def connect(self): self.is_connected = True
        >>> proxy = ProxyClient("AA:BB", client_factory=lambda address: FakeClient())
        >>> asyncio.run(proxy.connect())
        >>> proxy.is_connected
        True
        """

        if self.is_connected:
            return
        logger.debug("connecting proxy client to %s", self.address)
        # A previous link may have dropped without disconnect() ever running —
        # the light lost power, a proxy rebooted, the device went out of range.
        # The replacement client carries none of the old session's state, so
        # notification bookkeeping has to start over with it or the resubscribe
        # below would be skipped and the device's beacon never seen again.
        self._notifications_started = False
        self._callbacks.clear()
        self._reset_reassembly()
        self._client = self._client_factory(self.address)
        await self._client.connect()
        logger.debug("proxy client connected (GATT write limit=%s)", self._max_write_size())

    async def disconnect(self) -> None:
        """Disconnect the proxy client and clear notification callbacks.

        Returns
        -------
        None
            Connection state is updated on the underlying BLE client.

        Examples
        --------
        >>> import asyncio
        >>> class FakeClient:
        ...     is_connected = True
        ...     async def connect(self): pass
        ...     async def disconnect(self): self.is_connected = False
        >>> proxy = ProxyClient("AA:BB", client_factory=lambda address: FakeClient())
        >>> asyncio.run(proxy.connect())
        >>> asyncio.run(proxy.disconnect())
        >>> proxy.is_connected
        False
        """

        if self._client is None:
            return
        logger.debug("disconnecting proxy client from %s", self.address)
        client = self._client
        try:
            if self._notifications_started:
                try:
                    await self.stop_notify()
                except Exception as err:  # noqa: BLE001 - teardown still must continue
                    logger.debug("proxy notify session already gone: %s", err)
            await client.disconnect()
        finally:
            # A failed StopNotify or disconnect must never leave the old client
            # looking connected to the next poll or command.
            self._client = None
            self._notifications_started = False
            self._callbacks.clear()
            self._reset_reassembly()
        logger.debug("proxy client disconnected")

    def _max_write_size(self) -> int:
        """Return the GATT write limit, conservatively defaulting to 20 bytes.

        BlueZ can report its default MTU of 23 even after negotiating a larger
        one. Home Assistant's transport supplies the safe write limit directly;
        direct Bleak clients may expose either that value or a usable MTU.
        """
        assert self._client is not None
        size = getattr(self._client, "max_write_without_response_size", None)
        if isinstance(size, int) and size >= 2:
            return size
        services = getattr(self._client, "services", None)
        get_characteristic = getattr(services, "get_characteristic", None)
        if callable(get_characteristic):
            characteristic = get_characteristic(MESH_PROXY_DATA_IN_UUID)
            size = getattr(characteristic, "max_write_without_response_size", None)
            if isinstance(size, int) and size >= 2:
                return size
        mtu = getattr(self._client, "mtu_size", None)
        if isinstance(mtu, int) and mtu >= 5:
            return mtu - 3
        return _DEFAULT_WRITE_SIZE

    async def write_proxy(self, data: bytes) -> None:
        """Write a complete Mesh Proxy PDU to Data In.

        Parameters
        ----------
        data
            Raw proxy PDU bytes. The first byte carries SAR/type; the remaining
            bytes carry the mesh PDU body.

        Returns
        -------
        None
            The byte payload is written without GATT response.

        Examples
        --------
        >>> import asyncio
        >>> class FakeClient:
        ...     is_connected = True
        ...     mtu_size = 23
        ...     writes = []
        ...     async def connect(self): pass
        ...     async def write_gatt_char(self, uuid, data, response):
        ...         self.writes.append((uuid, bytes(data), response))
        >>> fake = FakeClient()
        >>> proxy = ProxyClient("AA:BB", client_factory=lambda address: fake)
        >>> asyncio.run(proxy.connect())
        >>> asyncio.run(proxy.write_proxy(b"\\x00\\x01"))
        >>> fake.writes[0][1]
        b'\\x00\\x01'
        """

        if not self.is_connected or self._client is None:
            raise RuntimeError("proxy client is not connected")
        if not data or data[0] & 0xC0:
            raise ValueError("write_proxy expects a complete Proxy PDU")
        max_write = self._max_write_size()
        message_type = data[0] & 0x3F
        body = data[1:]
        async with self._write_lock:
            if len(data) <= max_write:
                logger.debug("writing %d proxy byte(s)", len(data))
                await self._client.write_gatt_char(
                    MESH_PROXY_DATA_IN_UUID, data, response=False
                )
                return
            segment_size = max_write - 1
            logger.debug(
                "segmenting %d-byte proxy PDU into %d-byte GATT writes",
                len(data), max_write,
            )
            for start in range(0, len(body), segment_size):
                end = start + segment_size
                sar = (
                    _SAR_FIRST if start == 0 else
                    _SAR_LAST if end >= len(body) else _SAR_CONTINUATION
                )
                segment = bytes([(sar << 6) | message_type]) + body[start:end]
                await self._client.write_gatt_char(
                    MESH_PROXY_DATA_IN_UUID, segment, response=False
                )

    def _reset_reassembly(self) -> None:
        self._rx_type = None
        self._rx_body.clear()
        self._rx_started_at = 0.0

    def _reassemble(self, fragment: bytes) -> bytes | None:
        """Return a complete Proxy PDU after all GATT segments have arrived."""
        if not fragment:
            return None
        header = fragment[0]
        sar = header >> 6
        message_type = header & 0x3F
        body = fragment[1:]
        logger.debug(
            "proxy GATT notification SAR=%d type=0x%02x bytes=%d",
            sar, message_type, len(fragment),
        )
        if sar == _SAR_COMPLETE:
            self._reset_reassembly()
            return bytes([message_type]) + body
        if sar == _SAR_FIRST:
            self._reset_reassembly()
            if not body or len(body) > _MAX_PROXY_MESSAGE_BYTES:
                return None
            self._rx_type = message_type
            self._rx_body.extend(body)
            self._rx_started_at = time.monotonic()
            return None
        if (
            self._rx_type != message_type
            or self._rx_type is None
            or time.monotonic() - self._rx_started_at > _SAR_TIMEOUT_SECONDS
            or len(self._rx_body) + len(body) > _MAX_PROXY_MESSAGE_BYTES
        ):
            self._reset_reassembly()
            return None
        self._rx_body.extend(body)
        if sar == _SAR_CONTINUATION:
            return None
        complete = bytes([message_type]) + bytes(self._rx_body)
        self._reset_reassembly()
        return complete

    async def start_notify(self, callback: NotificationCallback) -> None:
        """Start Mesh Proxy Data Out notifications.

        Parameters
        ----------
        callback
            Function called with each notification payload as immutable ``bytes``.

        Returns
        -------
        None
            Notification subscription is started once per connected session.

        Examples
        --------
        >>> import asyncio
        >>> seen = []
        >>> class FakeClient:
        ...     is_connected = True
        ...     async def connect(self): pass
        ...     async def start_notify(self, uuid, callback): callback(uuid, bytearray(b"\\x01"))
        >>> proxy = ProxyClient("AA:BB", client_factory=lambda address: FakeClient())
        >>> asyncio.run(proxy.connect())
        >>> asyncio.run(proxy.start_notify(seen.append))
        >>> seen
        [b'\\x01']
        """

        if not self.is_connected or self._client is None:
            raise RuntimeError("proxy client is not connected")

        if callback not in self._callbacks:
            self._callbacks.append(callback)

        if self._notifications_started:
            return

        logger.debug("starting proxy notifications on %s", MESH_PROXY_DATA_OUT_UUID)

        def bleak_callback(_characteristic: Any, data: bytearray) -> None:
            pdu = self._reassemble(bytes(data))
            if pdu is None:
                return
            for cb in list(self._callbacks):
                cb(pdu)

        await self._client.start_notify(MESH_PROXY_DATA_OUT_UUID, bleak_callback)
        self._notifications_started = True

    async def stop_notify(self, callback: NotificationCallback | None = None) -> None:
        """Stop Mesh Proxy notifications.

        Parameters
        ----------
        callback
            Optional callback to unregister. When omitted, all callbacks are
            cleared and GATT notifications are stopped.

        Returns
        -------
        None
            Notification subscription is stopped when no callbacks remain.

        Examples
        --------
        >>> import asyncio
        >>> class FakeClient:
        ...     is_connected = True
        ...     async def connect(self): pass
        ...     async def stop_notify(self, uuid): self.stopped = uuid
        >>> fake = FakeClient()
        >>> proxy = ProxyClient("AA:BB", client_factory=lambda address: fake)
        >>> asyncio.run(proxy.connect())
        >>> asyncio.run(proxy.stop_notify())
        >>> fake.stopped == MESH_PROXY_DATA_OUT_UUID
        True
        """

        if not self.is_connected or self._client is None:
            raise RuntimeError("proxy client is not connected")

        if callback is not None:
            if callback in self._callbacks:
                self._callbacks.remove(callback)
            if self._callbacks:
                return

        logger.debug("stopping proxy notifications on %s", MESH_PROXY_DATA_OUT_UUID)
        try:
            await self._client.stop_notify(MESH_PROXY_DATA_OUT_UUID)
        finally:
            self._notifications_started = False
            self._callbacks.clear()
            self._reset_reassembly()


class GodoxMeshClient:
    """Generic BLE client for raw Godox GATT operations.

    Parameters
    ----------
    address
        Platform-specific BLE address or identifier.
    client_factory
        Bleak-compatible client factory.
    device_resolver
        Optional coroutine that resolves the address to a platform BLE object.

    Examples
    --------
    >>> GodoxMeshClient("AA:BB").address
    'AA:BB'
    """

    def __init__(
        self,
        address: str,
        *,
        client_factory: Callable[[str], Any] = BleakClient,
        device_resolver: DeviceResolver | None = None,
    ) -> None:
        self.address = address
        self._client_factory = client_factory
        self._device_resolver = device_resolver or _resolve_ble_device
        self._client: Any | None = None

    @property
    def is_connected(self) -> bool:
        """Return whether the underlying BLE client is connected.

        Returns
        -------
        bool
            ``True`` when a client exists and reports ``is_connected``.

        Examples
        --------
        >>> GodoxMeshClient("AA:BB").is_connected
        False
        """

        return bool(self._client and getattr(self._client, "is_connected", False))

    async def __aenter__(self) -> GodoxMeshClient:
        """Connect and return this client for async context manager use.

        Returns
        -------
        GodoxMeshClient
            Connected client instance.

        Examples
        --------
        >>> GodoxMeshClient("AA:BB").address
        'AA:BB'
        """

        await self.connect()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        """Disconnect when leaving an async context manager.

        Parameters
        ----------
        exc_type
            Exception type raised inside the context, if any.
        exc
            Exception value raised inside the context, if any.
        tb
            Traceback raised inside the context, if any.

        Returns
        -------
        None
            The BLE connection is closed.
        """

        await self.disconnect()

    async def connect(self) -> None:
        """Resolve and connect to the BLE device.

        Returns
        -------
        None
            The underlying BLE connection is opened in place.

        Examples
        --------
        >>> import asyncio
        >>> class FakeClient:
        ...     is_connected = False
        ...     async def connect(self): self.is_connected = True
        >>> async def resolver(address): return address
        >>> client = GodoxMeshClient("AA:BB", client_factory=lambda target: FakeClient(), device_resolver=resolver)
        >>> asyncio.run(client.connect())
        >>> client.is_connected
        True
        """

        if self.is_connected:
            return
        target = await self._device_resolver(self.address)
        logger.debug("connecting UL60Bi client to %s", target or self.address)
        self._client = self._client_factory(target or self.address)
        await self._client.connect()

    async def disconnect(self) -> None:
        """Disconnect from the BLE device.

        Returns
        -------
        None
            Connection state is updated on the underlying BLE client.

        Examples
        --------
        >>> import asyncio
        >>> class FakeClient:
        ...     is_connected = True
        ...     async def connect(self): pass
        ...     async def disconnect(self): self.is_connected = False
        >>> async def resolver(address): return address
        >>> client = GodoxMeshClient("AA:BB", client_factory=lambda target: FakeClient(), device_resolver=resolver)
        >>> asyncio.run(client.connect())
        >>> asyncio.run(client.disconnect())
        >>> client.is_connected
        False
        """

        if self._client is None:
            return
        logger.debug("disconnecting UL60Bi client from %s", self.address)
        await self._client.disconnect()

    async def write_raw(
        self,
        characteristic: str,
        payload: bytes,
        *,
        response: bool,
    ) -> None:
        """Write raw bytes to a GATT characteristic.

        Parameters
        ----------
        characteristic
            Characteristic UUID or handle accepted by Bleak.
        payload
            Exact byte payload to send; length and byte order are preserved.
        response
            Whether to request a GATT write response.

        Returns
        -------
        None
            Payload is written to the connected device.

        Examples
        --------
        >>> import asyncio
        >>> class FakeClient:
        ...     is_connected = True
        ...     writes = []
        ...     async def connect(self): pass
        ...     async def write_gatt_char(self, characteristic, payload, response):
        ...         self.writes.append((characteristic, bytes(payload), response))
        >>> fake = FakeClient()
        >>> async def resolver(address): return address
        >>> client = GodoxMeshClient("AA:BB", client_factory=lambda target: fake, device_resolver=resolver)
        >>> asyncio.run(client.connect())
        >>> asyncio.run(client.write_raw("char", b"\\x01\\x02", response=True))
        >>> fake.writes
        [('char', b'\\x01\\x02', True)]
        """

        client = self._require_client()
        logger.debug("writing raw payload to %s", characteristic)
        await client.write_gatt_char(characteristic, payload, response=response)

    async def start_notify(
        self,
        characteristic: str,
        callback: NotificationCallback,
    ) -> None:
        """Start notifications for a raw GATT characteristic.

        Parameters
        ----------
        characteristic
            Characteristic UUID or handle accepted by Bleak.
        callback
            Function called with each notification payload as immutable ``bytes``.

        Returns
        -------
        None
            Notification subscription is started on the connected device.

        Examples
        --------
        >>> import asyncio
        >>> seen = []
        >>> class FakeClient:
        ...     is_connected = True
        ...     async def connect(self): pass
        ...     async def start_notify(self, characteristic, callback):
        ...         callback(characteristic, bytearray(b"\\x03"))
        >>> async def resolver(address): return address
        >>> client = GodoxMeshClient("AA:BB", client_factory=lambda target: FakeClient(), device_resolver=resolver)
        >>> asyncio.run(client.connect())
        >>> asyncio.run(client.start_notify("char", seen.append))
        >>> seen
        [b'\\x03']
        """

        client = self._require_client()
        logger.debug("starting notifications on %s", characteristic)

        def bleak_callback(_characteristic: Any, data: bytearray) -> None:
            callback(bytes(data))

        await client.start_notify(characteristic, bleak_callback)

    async def stop_notify(self, characteristic: str) -> None:
        """Stop notifications for a raw GATT characteristic.

        Parameters
        ----------
        characteristic
            Characteristic UUID or handle accepted by Bleak.

        Returns
        -------
        None
            Notification subscription is stopped on the connected device.

        Examples
        --------
        >>> import asyncio
        >>> class FakeClient:
        ...     is_connected = True
        ...     async def connect(self): pass
        ...     async def stop_notify(self, characteristic): self.stopped = characteristic
        >>> fake = FakeClient()
        >>> async def resolver(address): return address
        >>> client = GodoxMeshClient("AA:BB", client_factory=lambda target: fake, device_resolver=resolver)
        >>> asyncio.run(client.connect())
        >>> asyncio.run(client.stop_notify("char"))
        >>> fake.stopped
        'char'
        """

        client = self._require_client()
        await client.stop_notify(characteristic)

    def _require_client(self) -> Any:
        if not self.is_connected or self._client is None:
            raise RuntimeError("client is not connected")
        return self._client


async def _resolve_ble_device(address: str) -> Any | None:
    return await BleakScanner.find_device_by_address(address, timeout=10.0)
