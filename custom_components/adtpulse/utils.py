"""ADT Pulse utility functions."""

import logging
from typing import Any

from pyadtpulse.site import ADTPulseSite
from pyadtpulse.gateway import ADTPulseGateway
from pyadtpulse.const import STATE_OK, STATE_ONLINE
from pyadtpulse.zones import ADTPulseZoneData
from homeassistant.core import HomeAssistant
from homeassistant.util import slugify
from homeassistant.helpers import entity_registry, device_registry

from .const import ADTPULSE_DOMAIN

logger = logging.getLogger(__name__)

# Gateway timestamp fields that pyadtpulse 1.2.x sets to None when the gateway
# page can't be parsed (e.g. after a silent Pulse logout returns the sign-in
# page). Its keepalive task then crashes on `next_update < time.time()`.
GATEWAY_TIME_FIELDS = ("next_update", "last_update")


def install_pyadtpulse_workarounds() -> None:
    """Keep pyadtpulse gateway timestamps numeric (never None).

    pyadtpulse 1.2.14 ``ADTPulseGateway.set_gateway_attributes`` stores None for
    an unparseable next_update/last_update, and its keepalive task compares
    ``next_update < time.time()``. The TypeError kills the keepalive task, and
    because pyadtpulse re-awaits that dead task inside async_login() and
    async_logout(), every later relogin and logout re-raises it (2026-11-11:
    entry stuck in FAILED_UNLOAD). Wrapping the setter so None becomes 0 (the
    dataclass default, meaning "refresh on the next keepalive") stops the crash
    at its source. Idempotent; safe across entry reloads.
    """
    original = ADTPulseGateway.set_gateway_attributes
    if getattr(original, "_adtpulse_none_guard", False):
        return

    def set_gateway_attributes(self, gateway_attributes: dict[str, str]) -> None:
        original(self, gateway_attributes)
        normalize_gateway_times(self)

    set_gateway_attributes._adtpulse_none_guard = True  # type: ignore[attr-defined]
    ADTPulseGateway.set_gateway_attributes = set_gateway_attributes  # type: ignore[method-assign]


def normalize_gateway_times(gateway: Any) -> None:
    """Replace None gateway timestamps with 0 (pyadtpulse's own default)."""
    for name in GATEWAY_TIME_FIELDS:
        try:
            if getattr(gateway, name, 0) is None:
                setattr(gateway, name, 0)
        except Exception:  # noqa: BLE001
            logger.debug("%s: could not reset gateway %s", ADTPULSE_DOMAIN, name)


def discard_dead_pulse_tasks(pulse: Any) -> None:
    """Drop finished pyadtpulse background tasks so they aren't re-awaited.

    pyadtpulse awaits finished tasks in _clean_done_tasks() (called from
    async_login/async_logout/wait_for_update), re-raising whatever killed
    them. Consume and log the exception here instead, and clear the slot so
    async_login() starts a fresh keepalive task and wait_for_update() a fresh
    sync-check task. Touches pyadtpulse 1.2.x internals; guarded.
    """
    try:
        normalize_gateway_times(pulse.site.gateway)
    except Exception:  # noqa: BLE001
        pass
    for attr in ("_timeout_task", "_sync_task"):
        task = getattr(pulse, attr, None)
        if task is None or not task.done():
            continue
        if not task.cancelled() and (ex := task.exception()) is not None:
            logger.warning(
                "%s: discarding dead pyadtpulse task %s (%s: %s)",
                ADTPULSE_DOMAIN,
                task.get_name(),
                type(ex).__name__,
                ex,
            )
        try:
            setattr(pulse, attr, None)
        except Exception:  # noqa: BLE001
            logger.debug("%s: could not clear %s", ADTPULSE_DOMAIN, attr)


def migrate_entity_name(
    hass: HomeAssistant,
    site: ADTPulseSite,
    platform_name: str,
    entity_uid: str,
) -> None:
    """Migrate old entity names."""
    registry = entity_registry.async_get(hass)
    if registry is None:
        return
    # this seems backwards
    entity_id = registry.async_get_entity_id(
        domain=platform_name,
        platform=ADTPULSE_DOMAIN,
        unique_id=entity_uid,
    )
    if entity_id is not None:
        # change has_entity_name to True and set name to None for devices
        registry.async_update_entity(
            entity_id=entity_id,
            has_entity_name=True,
            name=None,
        )
        # rename site name to site id for entities which have site name
        slugified_site_name = slugify(site.name)
        if slugified_site_name in entity_id:
            registry.async_update_entity(
                entity_id=entity_id,
                new_entity_id=entity_id.replace(slugified_site_name, site.id),
            )


def get_gateway_unique_id(site: ADTPulseSite) -> str:
    """Get entity unique id for the gateway."""
    return f"adt_pulse_gateway_{site.id}"


def get_gateway_device_id(
    hass: HomeAssistant, site: ADTPulseSite, config_entry_id: str | None
) -> str | None:
    """Get the device registry id of the gateway, for use as via_device_id.

    `via_device` (an identifier tuple) is deprecated in HA 2026.9 and removed in
    2027.8; device_info must now reference the parent by device registry id.
    """
    if config_entry_id is None:
        return None
    device = device_registry.async_get(hass).async_get_device_by_identifier(
        (ADTPULSE_DOMAIN, get_gateway_unique_id(site)), config_entry_id
    )
    return device.id if device else None


def get_alarm_unique_id(site: ADTPulseSite) -> str:
    """Get entity unique ID for alarm."""
    return f"adt_pulse_alarm_{site.id}"


def zone_is_open(zone: ADTPulseZoneData) -> bool:
    """Determine if a zone is opened."""
    return zone.state != STATE_OK


def zone_is_in_trouble(zone: ADTPulseZoneData) -> bool:
    """Determine if a zone is in trouble state."""
    return zone.status != STATE_ONLINE


def system_can_be_armed(site: ADTPulseSite) -> bool:
    """Determine is the system is able to be armed without being forced."""
    zones = site.zones_as_dict
    if zones is None:
        return False
    for zone in zones.values():
        if zone_is_open(zone) or zone_is_in_trouble(zone):
            return False
    return True
