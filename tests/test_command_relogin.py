"""0.7.9: arm/disarm refused as not logged in -> relogin in place, re-send once.

Runs against mocked pyadtpulse objects only; nothing talks to ADT.
Run: pytest tests/test_command_relogin.py (needs homeassistant + pyadtpulse).
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import HomeAssistantError
from pyadtpulse.alarm_panel import ADT_ALARM_HOME
from pyadtpulse.exceptions import PulseNotLoggedInError, PulseServerConnectionError

from custom_components.adtpulse import alarm_control_panel as acp
from custom_components.adtpulse.coordinator import ADTPulseDataUpdateCoordinator


def make_alarm(sends):
    """Alarm entity whose site.async_disarm returns/raises items of `sends`."""
    coordinator = MagicMock()
    coordinator.config_entry = None
    coordinator.async_relogin_for_command = AsyncMock()
    site = coordinator.adtpulse.site
    site.alarm_control_panel.status = ADT_ALARM_HOME
    site.gateway.is_online = True
    calls = []

    async def disarm():
        calls.append(1)
        item = sends[len(calls) - 1]
        if isinstance(item, BaseException):
            raise item
        return item

    site.async_disarm = disarm
    alarm = acp.ADTPulseAlarm(coordinator, site)
    alarm.hass = MagicMock()
    alarm.hass.async_create_background_task = lambda coro, name: (
        coro.close(),
        MagicMock(),
    )[1]
    alarm.entity_id = "alarm_control_panel.alarm"
    alarm.async_write_ha_state = MagicMock()
    return alarm, coordinator, calls


def failed_events(alarm):
    return [
        c
        for c in alarm.hass.bus.async_fire.call_args_list
        if c.args[0] == acp.EVENT_COMMAND_FAILED
    ]


@pytest.mark.asyncio
async def test_not_logged_in_then_relogin_and_retry_succeeds():
    alarm, coordinator, calls = make_alarm([PulseNotLoggedInError(), True])
    await alarm.async_alarm_disarm()
    assert len(calls) == 2  # first refused, one re-send
    coordinator.async_relogin_for_command.assert_awaited_once()
    assert failed_events(alarm) == []
    assert alarm._confirm_target is not None  # normal confirm path started


@pytest.mark.asyncio
async def test_both_attempts_fail_fires_once_and_raises():
    alarm, coordinator, calls = make_alarm(
        [PulseNotLoggedInError(), PulseNotLoggedInError()]
    )
    with pytest.raises(HomeAssistantError, match="not logged in"):
        await alarm.async_alarm_disarm()
    assert len(calls) == 2
    coordinator.async_relogin_for_command.assert_awaited_once()
    events = failed_events(alarm)
    assert len(events) == 1
    assert events[0].args[1]["attempts"] == 2
    assert alarm._assumed_state is None  # arming/disarming state cleared


@pytest.mark.asyncio
async def test_relogin_itself_fails_fires_once():
    alarm, coordinator, calls = make_alarm([PulseNotLoggedInError(), True])
    coordinator.async_relogin_for_command.side_effect = PulseNotLoggedInError()
    with pytest.raises(HomeAssistantError):
        await alarm.async_alarm_disarm()
    assert len(calls) == 1  # never re-sent
    assert len(failed_events(alarm)) == 1


@pytest.mark.asyncio
async def test_relogin_retry_is_time_limited(monkeypatch):
    monkeypatch.setattr(acp, "COMMAND_RELOGIN_TIMEOUT", 0.05)
    alarm, coordinator, calls = make_alarm([PulseNotLoggedInError(), True])

    async def slow():
        await asyncio.sleep(5)

    coordinator.async_relogin_for_command.side_effect = slow
    with pytest.raises(HomeAssistantError, match="timed out"):
        await alarm.async_alarm_disarm()
    assert len(calls) == 1
    assert len(failed_events(alarm)) == 1


@pytest.mark.asyncio
async def test_other_errors_are_not_retried():
    alarm, coordinator, calls = make_alarm([PulseServerConnectionError("down", MagicMock()), True])
    with pytest.raises(HomeAssistantError):
        await alarm.async_alarm_disarm()
    assert len(calls) == 1
    coordinator.async_relogin_for_command.assert_not_awaited()
    events = failed_events(alarm)
    assert len(events) == 1 and events[0].args[1]["attempts"] == 1


@pytest.mark.asyncio
async def test_retry_not_accepted_reports_two_attempts():
    alarm, coordinator, calls = make_alarm([PulseNotLoggedInError(), False])
    with pytest.raises(HomeAssistantError):
        await alarm.async_alarm_disarm()
    assert len(calls) == 2
    events = failed_events(alarm)
    assert len(events) == 1 and events[0].args[1]["attempts"] == 2


@pytest.mark.asyncio
async def test_coordinator_relogin_for_command():
    coord = object.__new__(ADTPulseDataUpdateCoordinator)
    pulse = MagicMock()
    pulse.is_connected = False
    pulse.async_login = AsyncMock()
    pulse._pulse_connection.quick_logout = AsyncMock()
    coord._adt_pulse = pulse
    coord._relogin_lock = asyncio.Lock()
    coord._restart_wait_pending = False
    coord._last_self_heal = 0.0
    await coord.async_relogin_for_command()
    pulse._pulse_connection.quick_logout.assert_awaited_once()
    pulse.async_login.assert_awaited_once()
    assert coord._restart_wait_pending is True
    assert coord._last_self_heal > 0
