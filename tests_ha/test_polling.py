"""Polling live state, which works on stock firmware for brightness."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from custom_components.godox_mesh._lib.protocol import parse_status_response
from custom_components.godox_mesh.const import (
    CONF_MESH,
    CONF_NODE_ADDRESS,
    CONF_NODES,
    CONF_RADIO_ID,
    CONF_READBACK,
    DOMAIN,
)
from custom_components.godox_mesh.mesh import GodoxMeshLink
from custom_components.godox_mesh.light import GodoxLight
from custom_components.godox_mesh._lib.protocol import StatusResponse
from homeassistant.components.light import ATTR_BRIGHTNESS, ATTR_COLOR_TEMP_KELVIN
from homeassistant.const import (
    ATTR_ASSUMED_STATE,
    ATTR_ENTITY_ID,
    CONF_ADDRESS,
    CONF_NAME,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from tests_ha.conftest import ADDRESS, MESH_STATE

BLE_PATH = "custom_components.godox_mesh.bluetooth.async_ble_device_from_address"
ENTITY = "light.key"

# Captured from an SL200III Bi.
COMMAND_ECHO = "a06441320000000c"  # 100% / 6500K, written by a command
PANEL_WRITE = "a04d3800ffff01f0"  # 77%, written by the light's own knob


async def _setup(
    hass: HomeAssistant, *, readback: bool, restore_on: bool = False
) -> MockConfigEntry:
    if restore_on:
        from homeassistant.const import STATE_ON
        from homeassistant.core import State
        from pytest_homeassistant_custom_component.common import mock_restore_cache

        mock_restore_cache(hass, [State(ENTITY, STATE_ON)])
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Key",
        unique_id=ADDRESS,
        data={CONF_ADDRESS: ADDRESS, CONF_MESH: dict(MESH_STATE)},
        options={
            CONF_NODES: [
                {CONF_NODE_ADDRESS: 2, CONF_NAME: "Key", CONF_RADIO_ID: "003F"}
            ],
            CONF_READBACK: readback,
        },
    )
    entry.add_to_hass(hass)
    with patch(BLE_PATH, return_value=object()):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


@pytest.mark.usefixtures("fake_ble")
async def test_polling_off_keeps_the_state_assumed(hass: HomeAssistant) -> None:
    """Without polling the entity is optimistic, as before."""
    await _setup(hass, readback=False)
    assert hass.states.get(ENTITY).attributes[ATTR_ASSUMED_STATE] is True


@pytest.mark.usefixtures("fake_ble")
async def test_status_brightness_does_not_override_power_commands(
    hass: HomeAssistant,
) -> None:
    """A0 keeps the last brightness after FE turns a FL15Bi's LEDs off."""
    from datetime import timedelta

    from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    from custom_components.godox_mesh.const import CONF_POLL_INTERVAL

    # Observed on the FL15Bi at 10% / 2800 K both before and after power-off.
    status = parse_status_response(bytes.fromhex("a00a1c32000000a5"))
    poll = AsyncMock(return_value=status)
    with patch.object(GodoxMeshLink, "async_request_status", poll):
        await _setup_nodes(
            hass,
            [
                {
                    CONF_NODE_ADDRESS: 2,
                    CONF_NAME: "Key",
                    CONF_RADIO_ID: "009F",
                    CONF_READBACK: True,
                    CONF_POLL_INTERVAL: 10,
                }
            ],
        )
        assert hass.states.get(ENTITY).state == STATE_OFF
        assert hass.states.get(ENTITY).attributes[ATTR_ASSUMED_STATE] is True

        await hass.services.async_call(
            "light", "turn_on", {ATTR_ENTITY_ID: ENTITY}, blocking=True
        )
        assert hass.states.get(ENTITY).state == STATE_ON
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
        await hass.async_block_till_done()
        assert hass.states.get(ENTITY).state == STATE_ON

        await hass.services.async_call(
            "light", "turn_off", {ATTR_ENTITY_ID: ENTITY}, blocking=True
        )
        assert hass.states.get(ENTITY).state == STATE_OFF
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=22))
        await hass.async_block_till_done()
        assert hass.states.get(ENTITY).state == STATE_OFF


@pytest.mark.usefixtures("fake_ble")
async def test_polling_reports_live_brightness(hass: HomeAssistant) -> None:
    """A polled light shows the brightness the hardware reports."""
    status = parse_status_response(bytes.fromhex(PANEL_WRITE))
    with patch.object(
        GodoxMeshLink, "async_request_status", AsyncMock(return_value=status)
    ):
        await _setup(hass, readback=True, restore_on=True)
        state = hass.states.get(ENTITY)
        # 77% of the 1-100 scale, converted to Home Assistant's 0-255.
        assert state.attributes[ATTR_BRIGHTNESS] == pytest.approx(196, abs=2)
        # The level is polled, but the separate power switch remains assumed.
        assert state.attributes[ATTR_ASSUMED_STATE] is True


@pytest.mark.usefixtures("fake_ble")
async def test_poll_cannot_replace_requested_brightness_during_turn_on(
    hass: HomeAssistant,
) -> None:
    """A timer poll overlapping the first FE write must not change its F0 frame."""
    current = StatusResponse(0xA0, 52, 2800, None, 0, b"")
    first_power_started = asyncio.Event()
    release_first_power = asyncio.Event()
    poll_started = asyncio.Event()
    sent_params: list[tuple[float, int]] = []
    power_calls = 0

    async def request_status(_self: GodoxMeshLink, _node: int) -> StatusResponse:
        return current

    async def turn_on(_self: GodoxMeshLink, _node: int) -> None:
        nonlocal power_calls
        power_calls += 1
        if power_calls == 1:
            first_power_started.set()
            await release_first_power.wait()

    async def set_light(_self: GodoxMeshLink, _node: int, **kwargs) -> None:
        nonlocal current
        brightness = kwargs["brightness_pct"]
        kelvin = kwargs["kelvin"]
        sent_params.append((brightness, kelvin))
        current = StatusResponse(0xA0, round(brightness), kelvin, None, 0, b"")

    with (
        patch.object(GodoxMeshLink, "async_request_status", request_status),
        patch.object(GodoxMeshLink, "async_turn_on", turn_on),
        patch.object(GodoxMeshLink, "async_set_light", set_light),
    ):
        await _setup_nodes(
            hass,
            [
                {
                    CONF_NODE_ADDRESS: 2,
                    CONF_NAME: "Key",
                    CONF_RADIO_ID: "009F",
                    CONF_READBACK: True,
                }
            ],
        )

        original_update = GodoxLight.async_update

        async def tracked_update(self: GodoxLight) -> None:
            poll_started.set()
            await original_update(self)

        with patch.object(GodoxLight, "async_update", tracked_update):
            command = asyncio.create_task(
                hass.services.async_call(
                    "light",
                    "turn_on",
                    {
                        ATTR_ENTITY_ID: ENTITY,
                        ATTR_BRIGHTNESS: 94,  # 37%
                        ATTR_COLOR_TEMP_KELVIN: 4000,
                    },
                    blocking=True,
                )
            )
            await asyncio.wait_for(first_power_started.wait(), 5)
            async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
            await asyncio.wait_for(poll_started.wait(), 5)
            # Let an unlocked poll complete before the paused first FE write.
            await asyncio.sleep(0)
            release_first_power.set()
            await command
            await hass.async_block_till_done()

    assert sent_params == [(37, 4000)]
    assert hass.states.get(ENTITY).attributes[ATTR_BRIGHTNESS] == pytest.approx(
        94, abs=2
    )


@pytest.mark.usefixtures("fake_ble")
async def test_lost_colour_write_is_retried_after_readback_mismatch(
    hass: HomeAssistant,
) -> None:
    """A successful BLE write alone does not prove the light applied its level."""
    from homeassistant.components.light import ATTR_BRIGHTNESS_PCT

    reads = 0

    async def request_status(_self: GodoxMeshLink, _node: int) -> StatusResponse:
        nonlocal reads
        reads += 1
        # Setup reads 100%. The first command is silently lost; the next F0
        # write reaches the light and its readback changes to 16%.
        brightness = 100 if reads <= 2 else 16
        return StatusResponse(0xA0, brightness, 2800, None, 0, b"")

    set_light = AsyncMock()
    with (
        patch.object(GodoxMeshLink, "async_request_status", request_status),
        patch.object(GodoxMeshLink, "async_turn_on", AsyncMock()),
        patch.object(GodoxMeshLink, "async_set_light", set_light),
    ):
        await _setup_nodes(
            hass,
            [
                {
                    CONF_NODE_ADDRESS: 2,
                    CONF_NAME: "Key",
                    CONF_RADIO_ID: "009F",
                    CONF_READBACK: True,
                }
            ],
        )
        await hass.services.async_call(
            "light",
            "turn_on",
            {ATTR_ENTITY_ID: ENTITY, ATTR_BRIGHTNESS_PCT: 16},
            blocking=True,
        )

    assert set_light.await_count == 2
    assert [c.kwargs["brightness_pct"] for c in set_light.await_args_list] == [16, 16]
    assert hass.states.get(ENTITY).attributes[ATTR_BRIGHTNESS] == pytest.approx(
        41, abs=2
    )


@pytest.mark.usefixtures("fake_ble")
async def test_unconfirmed_colour_write_reports_failure_and_real_level(
    hass: HomeAssistant,
) -> None:
    """Repeated mismatches must not leave HA claiming the requested level."""
    from homeassistant.components.light import ATTR_BRIGHTNESS_PCT
    from homeassistant.exceptions import HomeAssistantError

    status = StatusResponse(0xA0, 100, 2800, None, 0, b"")
    set_light = AsyncMock()
    with (
        patch.object(
            GodoxMeshLink, "async_request_status", AsyncMock(return_value=status)
        ),
        patch.object(GodoxMeshLink, "async_turn_on", AsyncMock()),
        patch.object(GodoxMeshLink, "async_set_light", set_light),
    ):
        await _setup_nodes(
            hass,
            [
                {
                    CONF_NODE_ADDRESS: 2,
                    CONF_NAME: "Key",
                    CONF_RADIO_ID: "009F",
                    CONF_READBACK: True,
                }
            ],
        )
        with pytest.raises(HomeAssistantError, match="did not confirm"):
            await hass.services.async_call(
                "light",
                "turn_on",
                {ATTR_ENTITY_ID: ENTITY, ATTR_BRIGHTNESS_PCT: 16},
                blocking=True,
            )

    assert set_light.await_count == 3
    assert hass.states.get(ENTITY).attributes[ATTR_BRIGHTNESS] == 255


@pytest.mark.usefixtures("fake_ble")
async def test_polling_accepts_cct_from_a_command_echo(hass: HomeAssistant) -> None:
    """A record written by a command reports the commanded colour temperature."""
    status = parse_status_response(bytes.fromhex(COMMAND_ECHO))
    with patch.object(
        GodoxMeshLink, "async_request_status", AsyncMock(return_value=status)
    ):
        await _setup(hass, readback=True, restore_on=True)
        assert hass.states.get(ENTITY).attributes[ATTR_COLOR_TEMP_KELVIN] == 6500


@pytest.mark.usefixtures("fake_ble")
async def test_polling_uses_the_reported_cct(hass: HomeAssistant) -> None:
    """The colour temperature a light reports is shown, not second-guessed.

    An earlier version discarded it whenever the reply carried the 0xFF markers,
    believing those meant "panel-written, so stale". Hardware disproved that: an
    SL60II Bi sends those markers on replies whose colour temperature is live and
    exact, so the rule threw away good data.
    """
    status = parse_status_response(bytes.fromhex(PANEL_WRITE))
    with patch.object(
        GodoxMeshLink, "async_request_status", AsyncMock(return_value=status)
    ):
        await _setup(hass, readback=True, restore_on=True)
        assert hass.states.get(ENTITY).attributes[ATTR_COLOR_TEMP_KELVIN] == status.cct


@pytest.mark.usefixtures("fake_ble")
async def test_a_model_that_reports_live_cct_has_it_used(hass: HomeAssistant) -> None:
    """Whatever the light reports is what is shown, for every model.

    Two attempts to infer from the wire format which colour temperatures were
    trustworthy were both wrong, so the heuristic was deleted. A reported value
    is used as received, whichever model sends it; a user whose light reports a
    wrong one turns colour-temperature polling off instead.
    """
    live = parse_status_response(bytes.fromhex("a03c283200000061"))  # 60% / 4000K
    with patch.object(
        GodoxMeshLink, "async_request_status", AsyncMock(return_value=live)
    ):
        await _setup(hass, readback=True, restore_on=True)
        assert hass.states.get(ENTITY).attributes[ATTR_COLOR_TEMP_KELVIN] == 4000


@pytest.mark.usefixtures("fake_ble")
async def test_a_light_that_does_not_answer_stays_usable(hass: HomeAssistant) -> None:
    """A failed poll must not break the entity."""
    from homeassistant.exceptions import HomeAssistantError

    with patch.object(
        GodoxMeshLink,
        "async_request_status",
        AsyncMock(side_effect=HomeAssistantError("no answer")),
    ):
        await _setup(hass, readback=True)
        assert hass.states.get(ENTITY) is not None


@pytest.mark.usefixtures("fake_ble")
async def test_a_node_that_does_not_answer_keeps_the_connection(
    hass: HomeAssistant,
) -> None:
    """Polling an off node must not tear down the shared proxy connection.

    The command fails at the mesh level -- the node did not reply -- while the
    proxy connection is still up. Dropping it would make every poll of an off
    light re-open the connection.
    """
    from custom_components.godox_mesh._lib import GodoxController

    # Fail the request itself, so it flows through the link's own error handling
    # rather than being short-circuited at the link method.
    with patch.object(
        GodoxController,
        "request_status",
        AsyncMock(side_effect=TimeoutError("node did not answer")),
    ):
        entry = await _setup(hass, readback=True)

    # The immediate poll failed, but the proxy connection is still held.
    assert entry.runtime_data.link._controller.is_connected


@pytest.mark.usefixtures("fake_ble")
async def test_colour_temperature_polling_can_be_turned_off(
    hass: HomeAssistant,
) -> None:
    """A user whose light reports a wrong colour temperature can opt out.

    Brightness keeps updating; only the colour temperature is left alone. This
    is the escape hatch for a light that reports a colour temperature which is
    not its real setting -- the SL200III Bi does, after its own dial is used.
    """
    from custom_components.godox_mesh.const import CONF_POLL_CCT
    from homeassistant.components.light import ATTR_BRIGHTNESS
    from homeassistant.const import STATE_ON
    from homeassistant.core import State
    from pytest_homeassistant_custom_component.common import mock_restore_cache

    mock_restore_cache(hass, [State(ENTITY, STATE_ON)])

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Key",
        unique_id=ADDRESS,
        data={CONF_ADDRESS: ADDRESS, CONF_MESH: dict(MESH_STATE)},
        options={
            CONF_NODES: [
                {CONF_NODE_ADDRESS: 2, CONF_NAME: "Key", CONF_RADIO_ID: "003F"}
            ],
            CONF_READBACK: True,
            CONF_POLL_CCT: False,
        },
    )
    entry.add_to_hass(hass)
    # a command echo, whose colour temperature would normally be used
    status = parse_status_response(bytes.fromhex(COMMAND_ECHO))
    assert status.cct == 6500
    with (
        patch(BLE_PATH, return_value=object()),
        patch.object(
            GodoxMeshLink, "async_request_status", AsyncMock(return_value=status)
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    state = hass.states.get(ENTITY)
    # brightness still tracks the light ...
    assert state.attributes[ATTR_BRIGHTNESS] is not None
    # ... but the reported colour temperature was not applied
    assert state.attributes[ATTR_COLOR_TEMP_KELVIN] != 6500


@pytest.mark.usefixtures("fake_ble")
async def test_brightness_polling_can_be_turned_off(hass: HomeAssistant) -> None:
    """A user whose light reports a wrong brightness can opt out of trusting it.

    Colour temperature keeps updating; only the brightness level is
    left at what was last commanded. This is the escape hatch for a light that
    reports a brightness which is not its real setting (some do after a firmware
    glitch, until power-cycled).
    """
    from custom_components.godox_mesh.const import CONF_POLL_BRIGHTNESS
    from homeassistant.const import STATE_ON
    from homeassistant.core import State
    from pytest_homeassistant_custom_component.common import mock_restore_cache

    mock_restore_cache(hass, [State(ENTITY, STATE_ON)])

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Key",
        unique_id=ADDRESS,
        data={CONF_ADDRESS: ADDRESS, CONF_MESH: dict(MESH_STATE)},
        options={
            CONF_NODES: [
                {CONF_NODE_ADDRESS: 2, CONF_NAME: "Key", CONF_RADIO_ID: "003F"}
            ],
            CONF_READBACK: True,
            CONF_POLL_BRIGHTNESS: False,
        },
    )
    entry.add_to_hass(hass)
    status = parse_status_response(bytes.fromhex(PANEL_WRITE))  # reports 77%
    assert status.brightness == 77
    with (
        patch(BLE_PATH, return_value=object()),
        patch.object(
            GodoxMeshLink, "async_request_status", AsyncMock(return_value=status)
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    state = hass.states.get(ENTITY)
    # The reported 77% (~196 on the 0-255 scale) was not applied: the level
    # stays at what was last commanded (the entity's default of full).
    assert state.attributes[ATTR_BRIGHTNESS] == 255
    # Power is restored from the last command, not inferred from brightness.
    assert state.state == "on"


async def _setup_nodes(hass: HomeAssistant, nodes: list[dict], **entry_opts):
    """Set up an entry with explicit node dicts and optional entry-wide options."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Key",
        unique_id=ADDRESS,
        data={CONF_ADDRESS: ADDRESS, CONF_MESH: dict(MESH_STATE)},
        options={CONF_NODES: nodes, **entry_opts},
    )
    entry.add_to_hass(hass)
    with patch(BLE_PATH, return_value=object()):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


@pytest.mark.usefixtures("fake_ble")
async def test_a_node_setting_overrides_the_entry_wide_fallback(
    hass: HomeAssistant,
) -> None:
    """An explicit per-node readback:false beats a legacy entry-wide readback:true."""
    from custom_components.godox_mesh.const import CONF_POLL_INTERVAL  # noqa: F401

    status = parse_status_response(bytes.fromhex(PANEL_WRITE))
    poll = AsyncMock(return_value=status)
    with patch.object(GodoxMeshLink, "async_request_status", poll):
        await _setup_nodes(
            hass,
            [
                {
                    CONF_NODE_ADDRESS: 2,
                    CONF_NAME: "Key",
                    CONF_RADIO_ID: "003F",
                    CONF_READBACK: False,
                }
            ],
            **{CONF_READBACK: True},
        )
    poll.assert_not_awaited()


@pytest.mark.usefixtures("fake_ble")
async def test_readback_is_per_node(hass: HomeAssistant) -> None:
    """One node can poll while a sibling on the same mesh does not."""
    status = parse_status_response(bytes.fromhex(PANEL_WRITE))
    poll = AsyncMock(return_value=status)
    with patch.object(GodoxMeshLink, "async_request_status", poll):
        await _setup_nodes(
            hass,
            [
                {
                    CONF_NODE_ADDRESS: 2,
                    CONF_NAME: "Key",
                    CONF_RADIO_ID: "003F",
                    CONF_READBACK: True,
                },
                {
                    CONF_NODE_ADDRESS: 4,
                    CONF_NAME: "Fill",
                    CONF_RADIO_ID: "003F",
                    CONF_READBACK: False,
                },
            ],
        )
    poll.assert_awaited_once_with(2)
    assert hass.states.get("light.key").attributes[ATTR_ASSUMED_STATE] is True
    assert hass.states.get("light.fill").attributes[ATTR_ASSUMED_STATE] is True


@pytest.mark.usefixtures("fake_ble")
async def test_a_light_goes_unavailable_after_repeated_failed_polls(
    hass: HomeAssistant,
) -> None:
    """A polled light that stops answering is shown unavailable after a few tries."""
    from datetime import timedelta

    from homeassistant.const import STATE_UNAVAILABLE
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    from custom_components.godox_mesh.const import (
        CONF_POLL_INTERVAL,
        FAILED_POLLS_BEFORE_UNAVAILABLE,
    )

    with patch.object(
        GodoxMeshLink,
        "async_request_status",
        AsyncMock(side_effect=HomeAssistantError("no answer")),
    ):
        await _setup_nodes(
            hass,
            [
                {
                    CONF_NODE_ADDRESS: 2,
                    CONF_NAME: "Key",
                    CONF_RADIO_ID: "003F",
                    CONF_READBACK: True,
                    CONF_POLL_INTERVAL: 10,
                }
            ],
        )
        # The immediate poll on add failed once, but one strike is not enough.
        assert hass.states.get(ENTITY).state != STATE_UNAVAILABLE

        for i in range(1, FAILED_POLLS_BEFORE_UNAVAILABLE + 1):
            async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11 * i))
            await hass.async_block_till_done()

        assert hass.states.get(ENTITY).state == STATE_UNAVAILABLE


@pytest.mark.usefixtures("fake_ble")
async def test_a_light_recovers_when_it_answers_again(hass: HomeAssistant) -> None:
    """Availability comes back on the first successful poll."""
    from datetime import timedelta

    from homeassistant.const import STATE_UNAVAILABLE
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    from custom_components.godox_mesh.const import CONF_POLL_INTERVAL

    status = parse_status_response(bytes.fromhex(PANEL_WRITE))
    poll = AsyncMock(side_effect=HomeAssistantError("no answer"))
    with patch.object(GodoxMeshLink, "async_request_status", poll):
        await _setup_nodes(
            hass,
            [
                {
                    CONF_NODE_ADDRESS: 2,
                    CONF_NAME: "Key",
                    CONF_RADIO_ID: "003F",
                    CONF_READBACK: True,
                    CONF_POLL_INTERVAL: 10,
                }
            ],
        )
        for i in range(1, 4):
            async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11 * i))
            await hass.async_block_till_done()
        assert hass.states.get(ENTITY).state == STATE_UNAVAILABLE

        # The light answers again.
        poll.side_effect = None
        poll.return_value = status
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=200))
        await hass.async_block_till_done()

        assert hass.states.get(ENTITY).state != STATE_UNAVAILABLE


@pytest.mark.usefixtures("fake_ble")
async def test_an_unpolled_light_never_goes_unavailable(hass: HomeAssistant) -> None:
    """A light without readback has no poll, so no availability signal -- it stays."""
    from homeassistant.const import STATE_UNAVAILABLE

    await _setup(hass, readback=False)
    assert hass.states.get(ENTITY).state != STATE_UNAVAILABLE


@pytest.mark.usefixtures("fake_ble")
async def test_a_command_success_resets_the_failed_poll_count(
    hass: HomeAssistant,
) -> None:
    """A command reaching an available light stops a slow poll drifting it out.

    Home Assistant drops service calls to *unavailable* entities, so a command
    cannot revive one -- recovery from unavailable is via a successful poll. But
    while a light is still available, a successful command is proof of reach and
    resets the strike count, so a model that answers commands yet is slow to
    answer a status poll does not creep to unavailable.
    """
    from datetime import timedelta

    from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    from custom_components.godox_mesh.const import (
        CONF_POLL_INTERVAL,
        FAILED_POLLS_BEFORE_UNAVAILABLE,
    )

    with patch.object(
        GodoxMeshLink,
        "async_request_status",
        AsyncMock(side_effect=HomeAssistantError("no answer")),
    ):
        await _setup_nodes(
            hass,
            [
                {
                    CONF_NODE_ADDRESS: 2,
                    CONF_NAME: "Key",
                    CONF_RADIO_ID: "003F",
                    CONF_READBACK: True,
                    CONF_POLL_INTERVAL: 10,
                }
            ],
        )
        # Run up to one short of unavailable (the poll on add is already one
        # strike, so a couple more timer polls get us close without tipping).
        for i in range(1, FAILED_POLLS_BEFORE_UNAVAILABLE - 1):
            async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11 * i))
            await hass.async_block_till_done()
        assert hass.states.get(ENTITY).state != STATE_UNAVAILABLE

        # A command succeeds (the light still being available), resetting strikes.
        await hass.services.async_call(
            "light", "turn_on", {ATTR_ENTITY_ID: ENTITY}, blocking=True
        )
        await hass.async_block_till_done()

        # After the reset a fresh run of failed polls -- as many as it took to
        # get close before -- still does not tip it over. Without the reset it
        # would already be unavailable.
        for i in range(1, FAILED_POLLS_BEFORE_UNAVAILABLE):
            async_fire_time_changed(
                hass, dt_util.utcnow() + timedelta(seconds=500 + 11 * i)
            )
            await hass.async_block_till_done()
        assert hass.states.get(ENTITY).state != STATE_UNAVAILABLE


@pytest.mark.usefixtures("fake_ble")
async def test_the_poll_interval_triggers_a_repeat_poll(hass: HomeAssistant) -> None:
    """The per-light timer polls again after its interval elapses."""
    from datetime import timedelta

    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed
    from custom_components.godox_mesh.const import CONF_POLL_INTERVAL

    status = parse_status_response(bytes.fromhex(PANEL_WRITE))
    poll = AsyncMock(return_value=status)
    with patch.object(GodoxMeshLink, "async_request_status", poll):
        await _setup_nodes(
            hass,
            [
                {
                    CONF_NODE_ADDRESS: 2,
                    CONF_NAME: "Key",
                    CONF_RADIO_ID: "003F",
                    CONF_READBACK: True,
                    CONF_POLL_INTERVAL: 10,
                }
            ],
        )
        after_setup = poll.await_count  # the immediate poll on add
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
        await hass.async_block_till_done()
        assert poll.await_count > after_setup
