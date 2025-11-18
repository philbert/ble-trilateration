"""Helper functions for Bermuda config flow."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.helpers import device_registry as dr

from .const import (
    get_logger,
    DOMAIN_PRIVATE_BLE_DEVICE,
    NAME,
)
from .util import mac_norm

_LOGGER = get_logger(__package__)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .bermuda_device import BermudaDevice
    from .coordinator import BermudaDataUpdateCoordinator


def get_bermuda_device_from_registry(
    hass: HomeAssistant,
    coordinator: BermudaDataUpdateCoordinator,
    registry_id: str,
) -> BermudaDevice | None:
    """
    Given a device registry device id, return the associated BermudaDevice.

    Returns None if the id can not be resolved to a tracked device.
    """
    devreg = dr.async_get(hass)
    device = devreg.async_get(registry_id)
    if device is None:
        _LOGGER.debug("_get_bermuda_device: HA device not found for registry_id %s", registry_id)
        return None

    device_address = None
    for connection in device.connections:
        if connection[0] in {
            DOMAIN_PRIVATE_BLE_DEVICE,
            dr.CONNECTION_BLUETOOTH,
            "ibeacon",
        }:
            device_address = connection[1]
            break

    if device_address is None:
        _LOGGER.debug("_get_bermuda_device: No bluetooth connection found for %s", device.name)
        return None

    # Normalize the address format to match coordinator.devices keys
    normalized_address = mac_norm(device_address)
    _LOGGER.debug(
        "_get_bermuda_device: Looking for address=%s, normalized=%s, in_devices=%s",
        device_address,
        normalized_address,
        normalized_address in coordinator.devices,
    )

    if normalized_address in coordinator.devices:
        result = coordinator.devices[normalized_address]
        _LOGGER.debug("_get_bermuda_device: Found! Returning device %s", result.name)
        return result

    # Try lowercase as fallback
    if device_address.lower() in coordinator.devices:
        result = coordinator.devices[device_address.lower()]
        _LOGGER.debug("_get_bermuda_device: Found via lowercase! Returning device %s", result.name)
        return result

    # We couldn't match the HA device id to a bermuda device mac.
    _LOGGER.warning("_get_bermuda_device: Address %s not found in coordinator.devices", normalized_address)
    return None
