"""Support for ADT Pulse alarm control panels."""

import asyncio
import logging
from datetime import datetime
from functools import partial
from collections.abc import Callable, Coroutine

from pyadtpulse.site import ADTPulseSite
from homeassistant.core import HomeAssistant, callback
from homeassistant.util.dt import as_local
from pyadtpulse.exceptions import (
    PulseConnectionError,
    PulseNotLoggedInError,
    PulseGatewayOfflineError,
)
from pyadtpulse.alarm_panel import (
    ADT_ALARM_OFF,
    ADT_ALARM_AWAY,
    ADT_ALARM_HOME,
    ADT_ALARM_NIGHT,
    ADT_ALARM_ARMING,
    ADT_ALARM_DISARMING,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import (
    AddEntitiesCallback,
    async_get_current_platform,
)
from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelState,
    AlarmControlPanelEntity,
    AlarmControlPanelEntityFeature,
)

from .const import ADTPULSE_DOMAIN
from .utils import (
    get_alarm_unique_id,
    migrate_entity_name,
    system_can_be_armed,
    get_gateway_device_id,
)
from .base_entity import ADTPulseEntity
from .coordinator import ALARM_CONTEXT, ADTPulseDataUpdateCoordinator

logger = logging.getLogger(__name__)

ALARM_MAP = {
    ADT_ALARM_ARMING: AlarmControlPanelState.ARMING,
    ADT_ALARM_AWAY: AlarmControlPanelState.ARMED_AWAY,
    ADT_ALARM_DISARMING: AlarmControlPanelState.DISARMING,
    ADT_ALARM_HOME: AlarmControlPanelState.ARMED_HOME,
    ADT_ALARM_OFF: AlarmControlPanelState.DISARMED,
    ADT_ALARM_NIGHT: AlarmControlPanelState.ARMED_NIGHT,
}


# safety net: never hold the service call / assumed state longer than this
ALARM_ACTION_TIMEOUT = 180  # seconds

# real (mapped) states that mean a requested action has completed
_ARMED_STATES = {
    AlarmControlPanelState.ARMED_AWAY,
    AlarmControlPanelState.ARMED_HOME,
    AlarmControlPanelState.ARMED_NIGHT,
    AlarmControlPanelState.ARMED_CUSTOM_BYPASS,
}

# after the portal accepts a command, how long to wait for the panel to
# actually reach the requested state before retrying / giving up
ALARM_CONFIRM_TIMEOUT = 120  # seconds
ALARM_COMMAND_ATTEMPTS = 2  # first try + one retry
EVENT_COMMAND_FAILED = "adtpulse_command_failed"

FORCE_ARM = "force arm"
ARM_ERROR_MESSAGE = (
    f"Pulse system cannot be armed due to opened/tripped zone - use {FORCE_ARM}"
)


def _failure_reason(ex: BaseException) -> str:
    """Short, human readable reason a command could not be sent."""
    if isinstance(ex, PulseNotLoggedInError):
        return "not logged in to Pulse"
    if isinstance(ex, PulseGatewayOfflineError):
        return "Pulse gateway offline"
    if isinstance(ex, PulseConnectionError):
        return "cannot reach Pulse"
    return type(ex).__name__


async def async_setup_entry(
    hass: HomeAssistant,
    config: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up an alarm control panel for ADT Pulse."""
    coordinator: ADTPulseDataUpdateCoordinator = hass.data[ADTPULSE_DOMAIN][
        config.entry_id
    ]
    if not coordinator:
        logger.error("ADT Pulse service not initialized, cannot setup alarm platform")
        return
    site = coordinator.adtpulse.site
    migrate_entity_name(
        hass,
        site,
        "alarm_control_panel",
        get_alarm_unique_id(site),
    )
    alarm_devices = [ADTPulseAlarm(coordinator, site)]

    async_add_entities(alarm_devices)
    platform = async_get_current_platform()
    platform.async_register_entity_service(
        "force_stay", {}, "async_alarm_arm_force_stay"
    )
    platform.async_register_entity_service(
        "force_away", {}, "async_alarm_arm_custom_bypass"
    )


class ADTPulseAlarm(ADTPulseEntity, AlarmControlPanelEntity):
    """An alarm_control_panel implementation for ADT Pulse."""

    def __init__(
        self,
        coordinator: ADTPulseDataUpdateCoordinator,
        site: ADTPulseSite,
    ):
        """Initialize the alarm control panel."""
        logger.debug("%s: adding alarm control panel for %s", ADTPULSE_DOMAIN, site.id)
        self._name = f"ADT Alarm Panel - Site {site.id}"
        self._assumed_state: AlarmControlPanelState | None = None
        self._requested_action: AlarmControlPanelState | None = None
        # confirmation of the panel actually acting on a command
        self._confirm_target: AlarmControlPanelState | None = None
        self._confirm_event = asyncio.Event()
        self._confirm_task: asyncio.Task | None = None
        super().__init__(coordinator, ALARM_CONTEXT)

    @property
    def alarm_state(self) -> AlarmControlPanelState | None:
        """Return the current alarm state."""
        if self._assumed_state is not None:
            return self._assumed_state
        return ALARM_MAP.get(self._alarm.status)

    @property
    def assumed_state(self) -> bool:
        """Return if the alarm is in an assumed state."""
        return self._assumed_state is not None

    @property
    def supported_features(self) -> AlarmControlPanelEntityFeature:
        """Return the list of supported features."""
        return (
            AlarmControlPanelEntityFeature.ARM_AWAY
            | AlarmControlPanelEntityFeature.ARM_CUSTOM_BYPASS
            | AlarmControlPanelEntityFeature.ARM_HOME
            | AlarmControlPanelEntityFeature.ARM_NIGHT
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info.

        We set the identifiers to the site id since it is unique across all sites
        and the zones can be identified by site id and zone name
        """
        info = DeviceInfo(
            identifiers={(ADTPULSE_DOMAIN, self._site.id)},
            manufacturer=self._alarm.manufacturer,
            model=self._alarm.model,
            name=self._name,
        )
        if via_device_id := get_gateway_device_id(
            self.hass,
            self._site,
            self.coordinator.config_entry.entry_id
            if self.coordinator.config_entry
            else None,
        ):
            info["via_device_id"] = via_device_id
        return info

    @staticmethod
    def _confirm_state_for(
        action: AlarmControlPanelState,
    ) -> AlarmControlPanelState:
        """Real (mapped) Pulse state that proves the panel acted on a command.

        Force away (custom bypass) shows up as armed away in Pulse; force stay
        is requested as armed_home already.
        """
        if action == AlarmControlPanelState.ARMED_CUSTOM_BYPASS:
            return AlarmControlPanelState.ARMED_AWAY
        return action

    @staticmethod
    def _action_name(action: AlarmControlPanelState, force_arm: bool) -> str:
        """Human readable command name for logs, errors and notifications."""
        names = {
            AlarmControlPanelState.DISARMED: "disarm",
            AlarmControlPanelState.ARMED_HOME: "arm stay",
            AlarmControlPanelState.ARMED_AWAY: "arm away",
            AlarmControlPanelState.ARMED_NIGHT: "arm night",
            AlarmControlPanelState.ARMED_CUSTOM_BYPASS: "arm away",
        }
        name = names.get(action, str(action))
        return f"force {name}" if force_arm else name

    def _real_alarm_state(self) -> str:
        status = self._alarm.status
        mapped = ALARM_MAP.get(status)
        return str(mapped) if mapped is not None else str(status)

    async def _perform_alarm_action(
        self,
        arm_disarm_func: Callable[[], Coroutine[bool | None, None, bool]],
        action: AlarmControlPanelState,
        force_arm: bool = False,
    ) -> None:
        """Send an arm/disarm command.

        arm_disarm_func is a factory (site method + args) so the command can be
        re-sent; each call creates a fresh coroutine.  The service call returns
        as soon as the portal accepts the command; confirming that the panel
        actually acted on it (and retrying once) runs in the background so a
        slow/failed panel never stalls automations.
        """
        logger.debug("%s: Setting Alarm to %s", ADTPULSE_DOMAIN, action)
        coro = arm_disarm_func()
        if action != AlarmControlPanelState.DISARMED:
            try:
                # force arm bypasses open zones, so skip the "can be armed" check
                await self._check_if_system_armable(FORCE_ARM if force_arm else action)
            except Exception:
                coro.close()  # don't leak the never-awaited coroutine
                raise
        if self.alarm_state == action:
            coro.close()  # don't leak the never-awaited coroutine
            logger.warning("Attempting to set alarm to same state, ignoring")
            return
        self._cancel_confirm_task()
        name = self._action_name(action, force_arm)
        try:
            result = await self._send_alarm_command(coro, action)
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            # _send_alarm_command already cleared the optimistic arming state;
            # report it like any other failed command and raise an HA error so
            # continue_on_error in scripts/automations applies
            reason = _failure_reason(ex)
            logger.warning(
                "%s: could not %s ADT Pulse alarm: %s (%s)",
                ADTPULSE_DOMAIN,
                name,
                reason,
                ex,
            )
            self._clear_assumed_state()
            self._fire_command_failed(name, 1, reason)
            raise HomeAssistantError(f"Could not {name}: {reason}") from ex
        if not result:
            logger.warning("Could not %s ADT Pulse alarm", action)
            self._fire_command_failed(name, 1, "Pulse portal did not accept it")
            raise HomeAssistantError(f"Could not set alarm status to {action}")
        self._confirm_target = self._confirm_state_for(action)
        self._confirm_event.clear()
        task_name = f"{ADTPULSE_DOMAIN} confirm {name}"
        target = self._confirm_alarm_action(arm_disarm_func, action, name)
        if self.coordinator.config_entry is not None:
            self._confirm_task = (
                self.coordinator.config_entry.async_create_background_task(
                    self.hass, target, task_name
                )
            )
        else:
            self._confirm_task = self.hass.async_create_background_task(
                target, task_name
            )

    async def _send_alarm_command(
        self,
        coro: Coroutine[bool | None, None, bool],
        action: AlarmControlPanelState,
    ) -> bool:
        """Post one arm/disarm command to the portal, holding the assumed state."""
        if not self._gateway.is_online:
            self._assumed_state = action
        elif action == AlarmControlPanelState.DISARMED:
            self._assumed_state = AlarmControlPanelState.DISARMING
        else:
            self._assumed_state = AlarmControlPanelState.ARMING
        self._requested_action = action
        self.async_write_ha_state()
        result = False
        try:
            async with asyncio.timeout(ALARM_ACTION_TIMEOUT):
                result = await coro
        except TimeoutError:
            logger.warning(
                "%s: timed out after %d seconds waiting to %s ADT Pulse alarm",
                ADTPULSE_DOMAIN,
                ALARM_ACTION_TIMEOUT,
                action,
            )
        finally:
            # always clear, even on error/timeout/cancel, so state can't stick
            self._clear_assumed_state()
        return bool(result)

    def _confirm_target_reached(self) -> bool:
        return (
            self._confirm_target is not None
            and ALARM_MAP.get(self._alarm.status) == self._confirm_target
        )

    async def _wait_for_confirmation(self, timeout: float) -> bool:
        """Wait until Pulse reports the requested real state, or time out."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            if self._confirm_target_reached():
                return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            self._confirm_event.clear()
            # event is set from _handle_coordinator_update; the short cap is a
            # belt-and-braces re-check in case an update is missed
            try:
                async with asyncio.timeout(min(remaining, 10)):
                    await self._confirm_event.wait()
            except TimeoutError:
                pass

    async def _confirm_alarm_action(
        self,
        arm_disarm_func: Callable[[], Coroutine[bool | None, None, bool]],
        action: AlarmControlPanelState,
        name: str,
    ) -> None:
        """Confirm the panel acted on a command; retry once, then report."""
        attempts = 1
        try:
            if await self._wait_for_confirmation(ALARM_CONFIRM_TIMEOUT):
                logger.debug("%s: panel confirmed %s", ADTPULSE_DOMAIN, name)
                return
            while attempts < ALARM_COMMAND_ATTEMPTS:
                attempts += 1
                if (
                    action != AlarmControlPanelState.DISARMED
                    and ALARM_MAP.get(self._alarm.status)
                    != AlarmControlPanelState.DISARMED
                ):
                    # panel moved to some other state; Pulse can't arm from it
                    logger.warning(
                        "%s: panel did not confirm %s and is now %s; not retrying",
                        ADTPULSE_DOMAIN,
                        name,
                        self._real_alarm_state(),
                    )
                    break
                logger.warning(
                    "%s: panel did not confirm %s within %d s (status %s); "
                    "retrying (attempt %d)",
                    ADTPULSE_DOMAIN,
                    name,
                    ALARM_CONFIRM_TIMEOUT,
                    self._real_alarm_state(),
                    attempts,
                )
                try:
                    result = await self._send_alarm_command(arm_disarm_func(), action)
                except Exception:
                    logger.exception("%s: retry of %s failed", ADTPULSE_DOMAIN, name)
                    result = False
                if result and await self._wait_for_confirmation(ALARM_CONFIRM_TIMEOUT):
                    logger.info(
                        "%s: panel confirmed %s after retry", ADTPULSE_DOMAIN, name
                    )
                    return
            self._clear_assumed_state()
            logger.error(
                "ADT Pulse did not confirm %s (panel communication failure) "
                "after %d attempt(s); status %s",
                name,
                attempts,
                self._real_alarm_state(),
            )
            self._fire_command_failed(name, attempts)
        except asyncio.CancelledError:
            logger.debug("%s: confirmation of %s cancelled", ADTPULSE_DOMAIN, name)
            raise
        finally:
            if asyncio.current_task() is self._confirm_task:
                self._confirm_task = None
                self._confirm_target = None

    @callback
    def _fire_command_failed(
        self, name: str, attempts: int, reason: str | None = None
    ) -> None:
        if self.hass is None:
            return
        if reason is None:
            reason = "panel communication failure"
            message = f"ADT Pulse did not confirm {name} ({reason})"
        else:
            message = f"ADT Pulse could not {name} ({reason})"
        self.hass.bus.async_fire(
            EVENT_COMMAND_FAILED,
            {
                "entity_id": self.entity_id,
                "action": name,
                "attempts": attempts,
                "last_status": self._real_alarm_state(),
                "reason": reason,
                "message": message,
            },
        )

    @callback
    def _cancel_confirm_task(self) -> None:
        if self._confirm_task is not None and not self._confirm_task.done():
            self._confirm_task.cancel()
        self._confirm_task = None
        self._confirm_target = None

    async def async_will_remove_from_hass(self) -> None:
        """Stop any pending confirmation when the entity goes away."""
        self._cancel_confirm_task()
        await super().async_will_remove_from_hass()

    @callback
    def _clear_assumed_state(self) -> None:
        self._assumed_state = None
        self._requested_action = None
        if self.hass is not None:
            self.async_write_ha_state()

    def _real_state_satisfies_request(self) -> bool:
        """Return True if Pulse's real status matches/moved past the request."""
        if self._requested_action is None:
            return False
        real = ALARM_MAP.get(self._alarm.status)
        if real is None:
            return False
        if self._requested_action == AlarmControlPanelState.DISARMED:
            # disarm done once Pulse reports anything but armed
            return real not in _ARMED_STATES
        # arm done once Pulse reports arming or any armed state
        return real == AlarmControlPanelState.ARMING or real in _ARMED_STATES

    async def async_alarm_disarm(self, code: str | None = None) -> None:
        """Send disarm command."""
        await self._perform_alarm_action(
            arm_disarm_func=self._site.async_disarm,
            action=AlarmControlPanelState.DISARMED,
        )

    async def _check_if_system_armable(self, new_state: str) -> None:
        """Checks if we can arm the system, raises exceptions if not."""
        if self.alarm_state != AlarmControlPanelState.DISARMED:
            raise HomeAssistantError(
                f"Cannot set alarm to {new_state} "
                f"because currently set to {self.alarm_state}"
            )
        if not new_state == FORCE_ARM and not system_can_be_armed(self._site):
            raise HomeAssistantError(ARM_ERROR_MESSAGE)

    async def async_alarm_arm_home(self, code: str | None = None) -> None:
        """Send arm home command."""
        await self._perform_alarm_action(
            arm_disarm_func=self._site.async_arm_home,
            action=AlarmControlPanelState.ARMED_HOME,
        )

    async def async_alarm_arm_away(self, code: str | None = None) -> None:
        """Send arm away command."""
        await self._perform_alarm_action(
            arm_disarm_func=self._site.async_arm_away,
            action=AlarmControlPanelState.ARMED_AWAY,
        )

    # Pulse can arm away or home with bypass
    async def async_alarm_arm_custom_bypass(self, code: str | None = None) -> None:
        """Send force arm command."""
        await self._perform_alarm_action(
            arm_disarm_func=partial(self._site.async_arm_away, force_arm=True),
            action=AlarmControlPanelState.ARMED_CUSTOM_BYPASS,
            force_arm=True,
        )

    async def async_alarm_arm_night(self, code: str | None = None) -> None:
        """Send arm night command."""
        await self._perform_alarm_action(
            arm_disarm_func=self._site.async_arm_night,
            action=AlarmControlPanelState.ARMED_NIGHT,
        )

    async def async_alarm_arm_force_stay(self) -> None:
        """Send force arm stay command.

        This type of arming isn't implemented in HA, but we put it in anyway for
        use as a service call.
        """
        await self._perform_alarm_action(
            arm_disarm_func=partial(self._site.async_arm_home, force_arm=True),
            action=AlarmControlPanelState.ARMED_HOME,
            force_arm=True,
        )

    @property
    def extra_state_attributes(self) -> dict:
        """Return the state attributes."""
        return {
            "last_update_time": as_local(
                datetime.fromtimestamp(self._alarm.last_update)
            ),
            "alarm_state": self._alarm.status,
        }

    @property
    def unique_id(self) -> str:
        """Return HA unique id.

        Returns:
            str: the unique id
        """
        return get_alarm_unique_id(self._site)

    @property
    def code_format(self) -> None:
        """Return code format.

        Returns:
            None (not implmented)
        """
        return None

    @property
    def available(self) -> bool:
        """Alarm panel is always available even if gateway isn't."""
        return True

    @property
    def code_arm_required(self) -> bool:
        """Whether the code is required for arm actions."""
        return False

    @callback
    def _handle_coordinator_update(self) -> None:
        logger.debug(
            "Updating Pulse alarm to %s for site %s",
            ALARM_MAP.get(self._site.alarm_control_panel.status),
            self._site.id,
        )
        if self._assumed_state is not None and self._real_state_satisfies_request():
            logger.debug(
                "%s: real alarm status reached, clearing assumed state", ADTPULSE_DOMAIN
            )
            self._assumed_state = None
            self._requested_action = None
        if self._confirm_target_reached():
            self._confirm_event.set()
        self.async_write_ha_state()
