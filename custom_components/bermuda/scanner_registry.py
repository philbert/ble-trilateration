"""Helpers for Bermuda-owned scanner identity and registry cleanup."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .coordinator import BermudaDataUpdateCoordinator


def scanner_legacy_unique_id_candidates(scanner, suffix: str) -> set[str]:
    """Return legacy unique_id candidates for a scanner-owned entity suffix."""
    candidates: set[str] = set()
    for alias in scanner.scanner_legacy_identifier_aliases():
        candidates.add(f"{alias}_{suffix}")
    return candidates


def scanner_canonical_sensor_unique_id(scanner, suffix: str) -> str:
    """Return the canonical unique_id for a scanner-owned Bermuda entity."""
    return scanner.scanner_entity_unique_id(suffix)


def scanner_range_legacy_unique_id_candidates(tracked_device, scanner, suffix: str) -> set[str]:
    """Return legacy unique_id candidates for per-device scanner range entities."""
    candidates: set[str] = set()
    for alias in scanner.scanner_legacy_identifier_aliases():
        candidates.add(f"{tracked_device.unique_id}_{alias}_{suffix}")
    return candidates


def scanner_range_canonical_unique_id(tracked_device, scanner, suffix: str) -> str:
    """Return the canonical unique_id for per-device scanner range entities."""
    return f"{tracked_device.unique_id}_{scanner.scanner_entity_unique_id(suffix)}"


def cleanup_scanner_device_registry(
    hass: HomeAssistant,
    entry_id: str,
    coordinator: BermudaDataUpdateCoordinator,
) -> None:
    """Normalize Bermuda-owned scanner devices and remove stale ones when possible."""
    devreg = dr.async_get(hass)
    entreg = er.async_get(hass)

    for scanner in coordinator.get_scanners:
        if not scanner.scanner_identity_ready:
            continue

        canonical_identifier = (DOMAIN, scanner.scanner_device_identifier)
        legacy_identifiers = {
            (DOMAIN, alias)
            for alias in scanner.scanner_legacy_identifier_aliases()
            if alias != scanner.scanner_device_identifier
        }
        matching_devices = [
            device_entry
            for device_entry in dr.async_entries_for_config_entry(devreg, entry_id)
            if any(identifier in device_entry.identifiers for identifier in legacy_identifiers | {canonical_identifier})
        ]
        if not matching_devices:
            continue

        owned_devices = [
            device_entry
            for device_entry in matching_devices
            if all(identifier[0] == DOMAIN for identifier in device_entry.identifiers)
            and len(device_entry.config_entries) == 1
            and entry_id in device_entry.config_entries
        ]
        host_merged_devices = [device_entry for device_entry in matching_devices if device_entry not in owned_devices]

        for host_device in host_merged_devices:
            desired_identifiers = {
                identifier for identifier in host_device.identifiers if identifier not in legacy_identifiers | {canonical_identifier}
            }
            if desired_identifiers != host_device.identifiers:
                devreg.async_update_device(host_device.id, new_identifiers=desired_identifiers)

        if not owned_devices:
            continue

        primary = next(
            (device_entry for device_entry in owned_devices if canonical_identifier in device_entry.identifiers),
            owned_devices[0],
        )
        desired_identifiers = {canonical_identifier}

        update_kwargs = {}
        if primary.identifiers != desired_identifiers:
            update_kwargs["new_identifiers"] = desired_identifiers
        if primary.via_device_id is not None:
            update_kwargs["via_device_id"] = None
        if update_kwargs:
            devreg.async_update_device(primary.id, **update_kwargs)

        for stale_device in owned_devices:
            if stale_device.id == primary.id:
                continue
            if er.async_entries_for_device(entreg, stale_device.id, include_disabled_entities=True):
                continue
            devreg.async_remove_device(stale_device.id)
