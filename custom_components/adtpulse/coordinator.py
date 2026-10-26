"""ADT Pulse Update Coordinator."""

import time
import asyncio
import logging
from typing import Any
from asyncio import Task, CancelledError, sleep
from collections.abc import Callable

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.util.dt import utcnow, as_local, utc_from_timestamp
from pyadtpulse.exceptions import (
    PulseLoginException,
    PulseNotLoggedInError,
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

# Session health guard (0.7.6). pyadtpulse's wait_for_update() only returns when
# data changes or its sync task reports an error, so a session that dies quietly
# (authenticated flag cleared, background task gone) blocks it forever and the
# status sensor kept saying "Connection OK" (seen 2026-10-14..25). The update
# loop now wakes every HEALTH_CHECK_INTERVAL seconds to look at the session.
HEALTH_CHECK_INTERVAL = 60  # seconds
# relogins clear the authenticated flag for a few seconds; only report a
# problem that has lasted this long
UNHEALTHY_GRACE = 180  # seconds
# the keepalive task relogs in every 0.75-1.0 x relogin_interval; no relogin for
# 2 x relogin_interval (+1 keepalive tick) means at least one cycle was missed
STALE_RELOGIN_FACTOR = 2


class ADTPulseSessionStaleError(Exception):
    """The Pulse session stopped refreshing (no relogin or a dead task)."""


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
        self._wait_task: Task | None = None
        self._unhealthy_since: float | None = None
        self._health_exception: Exception | None = None
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
        self._cancel_wait_task()

    def _cancel_wait_task(self) -> None:
        if self._wait_task is not None and not self._wait_task.done():
            self._wait_task.cancel()
        self._wait_task = None

    def _session_problem(self, now: float) -> Exception | None:
        """Return an exception describing a dead/stale session, or None."""
        pulse = self._adt_pulse
        try:
            if not pulse.is_connected:
                return PulseNotLoggedInError()
            # pyadtpulse internals (1.2.x); guarded so a library change only
            # disables the extra checks instead of breaking updates
            keepalive_task = getattr(pulse, "_timeout_task", None)
            if keepalive_task is None or keepalive_task.done():
                return ADTPulseSessionStaleError("keepalive task not running")
            sync_task = getattr(pulse, "_sync_task", None)
            if sync_task is not None and sync_task.done():
                return ADTPulseSessionStaleError("sync check task not running")
            relogin = pulse.relogin_interval * 60
            auth = getattr(pulse, "_authentication_properties", None)
            last_login = auth.last_login_time if auth is not None else 0
            if relogin and last_login:
                limit = STALE_RELOGIN_FACTOR * relogin + pulse.keepalive_interval * 60
                if now - last_login > limit:
                    return ADTPulseSessionStaleError(
                        f"no relogin for {int((now - last_login) / 60)} minutes"
                    )
        except Exception as ex:  # noqa: BLE001
            logger.debug("%s: session health check failed: %s", ADTPULSE_DOMAIN, ex)
        return None

    @callback
    def _check_session_health(self, now: float | None = None) -> None:
        """Flag a dead/stale session on the status sensor; clear it on recovery."""
        now = time.time() if now is None else now
        problem = self._session_problem(now)
        if problem is None:
            self._unhealthy_since = None
            if (
                self._health_exception is not None
                and self.last_exception is self._health_exception
            ):
                logger.warning("%s: Pulse session healthy again", ADTPULSE_DOMAIN)
                self._health_exception = None
                self.last_exception = None
                self.last_update_success = True
                self.data = None
                self.async_update_listeners()
            self._health_exception = None
            return
        if self._unhealthy_since is None:
            self._unhealthy_since = now
        if now - self._unhealthy_since < UNHEALTHY_GRACE:
            return
        # don't hide a more specific error reported by pyadtpulse itself
        if self.last_exception is not None and (
            self.last_exception is not self._health_exception
        ):
            return
        if self._health_exception is not None and type(problem) is type(
            self._health_exception
        ):
            return
        logger.warning(
            "%s: Pulse session unhealthy (%s: %s)",
            ADTPULSE_DOMAIN,
            type(problem).__name__,
            problem,
        )
        self._health_exception = problem
        self.async_set_update_error(problem)
        self.data = None
        self.async_update_listeners()

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

    async def _async_update_data(self) -> None:  # noqa: PLR0912, PLR0915
        """Fetch data from ADT Pulse."""
        while not self._shutdown_requested and not self.hass.is_stopping:
            data = None
            logger.debug("%s: coordinator waiting for updates", ADTPULSE_DOMAIN)
            update_exception: Exception | None = None
            try:
                if self._wait_task is None:
                    self._wait_task = asyncio.ensure_future(
                        self._adt_pulse.wait_for_update()
                    )
                done, _ = await asyncio.wait(
                    {self._wait_task}, timeout=HEALTH_CHECK_INTERVAL
                )
                if not done:
                    # nothing from Pulse yet: check the session is still alive
                    self._check_session_health()
                    continue
                wait_task, self._wait_task = self._wait_task, None
                data = wait_task.result()
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
                self._cancel_wait_task()
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
                        self._health_exception = None
                        self.async_set_update_error(update_exception)
                        # async_set_update_error will only notify listeners on first
                        # error; it also doesn't reset data
                        self.data = None
                        if not self.last_update_success:
                            self.async_update_listeners()
                    elif data is not None:
                        self._health_exception = None
                        self._unhealthy_since = None
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
