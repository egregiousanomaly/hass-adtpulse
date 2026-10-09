"""ADT Pulse Update Coordinator."""

import logging
from typing import Any
from asyncio import Task, CancelledError, sleep
from collections.abc import Callable

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.util.dt import utcnow, as_local, utc_from_timestamp
from pyadtpulse.exceptions import (
    PulseLoginException,
    PulseExceptionWithRetry,
    PulseExceptionWithBackoff,
)
from homeassistant.exceptions import ConfigEntryNotReady
from pyadtpulse.pyadtpulse_async import PyADTPulseAsync
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import ADTPULSE_DOMAIN

logger = logging.getLogger(__name__)

ALARM_CONTEXT = "Alarm"
ZONE_CONTEXT_PREFIX = "Zone "
ZONE_TROUBLE_PREFIX = " Trouble"
CONNECTION_STATUS_CONTEXT = "ConnectionStatus"
NEXT_REFRESH_CONTEXT = "NextRefresh"

# self-healing: reload the entry instead of letting the update loop die
RECONNECT_DELAYS = (60, 300, 900, 1800)  # seconds, last one repeats
LOGIN_RETRIES_BEFORE_REAUTH = 3


class ADTPulseDataUpdateCoordinator(DataUpdateCoordinator):
    """Update Coordinator for ADT Pulse entities."""

    def __init__(self, hass: HomeAssistant, pulse_service: PyADTPulseAsync):
        """Initialize Pulse data update coordinator.

        Args:
            hass (HomeAssistant): hass object
            pulse_service (PyADTPulseAsync): ADT Pulse API service object used to
                fetch updates and manage session state.
        """
        logger.debug("%s: creating update coordinator", ADTPULSE_DOMAIN)
        self._adt_pulse = pulse_service
        self._update_task: Task | None = None
        super().__init__(
            hass,
            logger,
            name=ADTPULSE_DOMAIN,
        )
        self._listener_dictionary: dict[str, CALLBACK_TYPE] = {}

    @property
    def adtpulse(self) -> PyADTPulseAsync:
        """Return the ADT Pulse service object."""
        return self._adt_pulse

    @callback
    def async_add_listener(
        self, update_callback: CALLBACK_TYPE, context: Any = None
    ) -> Callable[[], None]:
        """Listen for data updates."""
        self._listener_dictionary[context] = update_callback
        remove_listener = super().async_add_listener(update_callback, context)

        @callback
        def remove_and_forget() -> None:
            # drop the context too, so a removed/disabled entity isn't called later
            if self._listener_dictionary.get(context) is update_callback:
                del self._listener_dictionary[context]
            remove_listener()

        return remove_and_forget

    @callback
    def _call_listener(self, context: str) -> None:
        """Call the listener for context, if one is registered.

        Disabled or not-yet-added entities have no listener; skip them instead of
        raising KeyError (which used to kill the update task).
        """
        if (update_callback := self._listener_dictionary.get(context)) is not None:
            update_callback()

    @callback
    def async_update_listeners(self) -> None:
        """Update listeners based update returned data."""
        start_time = utcnow()
        if not self.data:
            super().async_update_listeners()
            logger.debug(
                "%s: async_update_listeners took %s",
                ADTPULSE_DOMAIN,
                utcnow() - start_time,
            )
            return
        data_to_update: tuple[bool, set[int]] = self.data
        if data_to_update[0]:
            self._call_listener(ALARM_CONTEXT)
        for zones in data_to_update[1]:
            self._call_listener(ZONE_CONTEXT_PREFIX + str(zones))
            self._call_listener(ZONE_CONTEXT_PREFIX + str(zones) + ZONE_TROUBLE_PREFIX)
        for i in CONNECTION_STATUS_CONTEXT, NEXT_REFRESH_CONTEXT:
            self._call_listener(i)
        logger.debug(
            "%s: partial async_update_listeners took %s",
            ADTPULSE_DOMAIN,
            utcnow() - start_time,
        )

    async def start(self) -> None:
        """Start ADT Pulse update coordinator.

        This doesn't really need to be async, but it is to yield the event loop.
        """
        if not self._update_task:
            ce = self.config_entry
            if ce:
                self._update_task = ce.async_create_background_task(
                    self.hass, self._async_update_data(), "ADT Pulse Data Update"
                )
            else:
                raise ConfigEntryNotReady

    async def stop(self):
        """Stop ADT Pulse update coordinator."""
        if self._update_task:
            if not self._update_task.done():
                self._update_task.cancel()
            try:
                await self._update_task
            except CancelledError:
                pass
            except Exception:  # noqa: BLE001
                # a crashed update task must not make the entry fail to unload
                logger.exception("%s: update task had failed", ADTPULSE_DOMAIN)
            self._update_task = None

    def _failure_count(self) -> int:
        return self.hass.data.setdefault(ADTPULSE_DOMAIN + "_reconnect", {}).get(
            self.config_entry.entry_id if self.config_entry else "", 0
        )

    def _set_failure_count(self, count: int) -> None:
        if self.config_entry:
            self.hass.data.setdefault(ADTPULSE_DOMAIN + "_reconnect", {})[
                self.config_entry.entry_id
            ] = count

    async def _reconnect_later(self, reason: str) -> None:
        """Wait with back-off, then reload the config entry to reconnect."""
        failures = self._failure_count()
        delay = RECONNECT_DELAYS[min(failures, len(RECONNECT_DELAYS) - 1)]
        self._set_failure_count(failures + 1)
        logger.warning(
            "%s: %s; reconnecting in %d seconds (attempt %d)",
            ADTPULSE_DOMAIN,
            reason,
            delay,
            failures + 1,
        )
        try:
            await sleep(delay)
        except CancelledError:
            # unload/stop while waiting: exit quietly like the main loop does
            return
        if self.config_entry and not self.hass.is_stopping:
            self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)

    async def _async_update_data(self) -> None:
        """Fetch data from ADT Pulse."""
        while not self._shutdown_requested and not self.hass.is_stopping:
            data = None
            logger.debug("%s: coordinator waiting for updates", ADTPULSE_DOMAIN)
            update_exception: Exception | None = None
            try:
                data = await self._adt_pulse.wait_for_update()
            except PulseLoginException as ex:
                logger.error(
                    "%s: ADT Pulse login failed during coordinator update: %s",
                    ADTPULSE_DOMAIN,
                    ex,
                )
                # ADT's login endpoint fails transiently; only ask for
                # re-auth after repeated failures
                if self._failure_count() < LOGIN_RETRIES_BEFORE_REAUTH:
                    await self._reconnect_later(f"login failed ({ex})")
                elif self.config_entry:
                    self.config_entry.async_start_reauth(self.hass)
                return
            except PulseExceptionWithRetry as ex:
                if ex.retry_time:
                    logger.debug(
                        "%s: coordinator received retryable exception will retry at %s",
                        ADTPULSE_DOMAIN,
                        as_local(utc_from_timestamp(ex.retry_time)),
                    )
                update_exception = ex
            except PulseExceptionWithBackoff as ex:
                update_exception = ex
                logger.debug(
                    "%s: coordinator received backoff exception, backing off for %s seconds",  # noqa: E501
                    ADTPULSE_DOMAIN,
                    ex.backoff.get_current_backoff_interval(),
                )
            except CancelledError:
                logger.debug("%s: coordinator received cancellation", ADTPULSE_DOMAIN)
                return
            except Exception as ex:
                logger.exception(
                    "%s: coordinator received unknown exception %s", ADTPULSE_DOMAIN, ex
                )
                await self._reconnect_later(f"update loop crashed ({ex!r})")
                return
            finally:
                # exceptions raised here would bypass the handlers above and
                # silently end the update loop, so contain them
                try:
                    if update_exception:
                        self.async_set_update_error(update_exception)
                        # async_set_update_error will only notify listeners on first
                        # error; it also doesn't reset data
                        self.data = None
                        if not self.last_update_success:
                            self.async_update_listeners()
                    elif data is not None:
                        self.last_exception = None
                        self._set_failure_count(0)
                        self.async_set_updated_data(data)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "%s: error notifying listeners of update", ADTPULSE_DOMAIN
                    )

            logger.debug(
                "%s: coordinator received update notification", ADTPULSE_DOMAIN
            )
