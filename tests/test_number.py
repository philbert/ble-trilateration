"""Tests for Bermuda number entities."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ble_trilateration.bermuda_device import BermudaDevice
from custom_components.ble_trilateration.const import DOMAIN, NAME, REPAIR_TRILAT_WITHOUT_ANCHORS

from .const import MOCK_CONFIG


async def setup_integration(hass):
    """Set up the Bermuda config entry with coordinator refresh mocked."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="test-number", title=NAME)
    entry.add_to_hass(hass)

    with patch("custom_components.ble_trilateration.BermudaDataUpdateCoordinator.async_refresh"):
        assert await async_setup_component(hass, DOMAIN, {})

    await hass.async_block_till_done()
    assert entry.state == ConfigEntryState.LOADED
    return entry


def _create_scanner(coordinator, address: str) -> BermudaDevice:
    """Helper to register a scanner device with the coordinator."""
    scanner = BermudaDevice(address, coordinator)
    scanner._is_scanner = True  # noqa: SLF001 - test helper to mark as scanner
    scanner._is_remote_scanner = False  # noqa: SLF001 - test helper to mark as resolved scanner
    coordinator.devices[scanner.address] = scanner
    coordinator.scanner_list_add(scanner)
    return scanner


async def test_scanner_anchor_numbers_created_without_legacy_numbers(hass) -> None:
    """Ensure scanners only expose anchor coordinates, not legacy calibration numbers."""
    entry = await setup_integration(hass)
    coordinator = entry.runtime_data.coordinator

    scanner = _create_scanner(coordinator, "AA:BB:CC:DD:EE:01")
    await hass.async_block_till_done()

    ent_reg = er.async_get(hass)

    assert ent_reg.async_get_entity_id("number", DOMAIN, f"{scanner.unique_id}_rssi_offset") is None
    assert ent_reg.async_get_entity_id("number", DOMAIN, f"{scanner.unique_id}_attenuation") is None
    assert ent_reg.async_get_entity_id("number", DOMAIN, f"{scanner.unique_id}_max_radius") is None

    anchor_x = ent_reg.async_get_entity_id("number", DOMAIN, f"{scanner.unique_id}_anchor_x_m")
    anchor_y = ent_reg.async_get_entity_id("number", DOMAIN, f"{scanner.unique_id}_anchor_y_m")
    anchor_z = ent_reg.async_get_entity_id("number", DOMAIN, f"{scanner.unique_id}_anchor_z_m")

    assert anchor_x is not None
    assert anchor_y is not None
    assert anchor_z is not None


async def test_scanner_anchor_numbers_created_for_existing_scanners_on_startup(hass) -> None:
    """Scanner anchor numbers should be created even when scanners predate number setup."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="test-number-startup", title=NAME)
    entry.add_to_hass(hass)

    async def _seed_scanner(self) -> None:
        scanner = BermudaDevice("AA:BB:CC:DD:EE:16", self)
        scanner._is_scanner = True  # noqa: SLF001 - test helper
        scanner._is_remote_scanner = False  # noqa: SLF001 - test helper
        self.devices[scanner.address] = scanner
        self.scanner_list_add(scanner)

    with patch("custom_components.ble_trilateration.BermudaDataUpdateCoordinator.async_refresh", new=_seed_scanner):
        assert await async_setup_component(hass, DOMAIN, {})

    await hass.async_block_till_done()
    assert entry.state == ConfigEntryState.LOADED

    ent_reg = er.async_get(hass)
    unique_id = "aa:bb:cc:dd:ee:16"

    assert ent_reg.async_get_entity_id("number", DOMAIN, f"{unique_id}_anchor_x_m") is not None
    assert ent_reg.async_get_entity_id("number", DOMAIN, f"{unique_id}_anchor_y_m") is not None
    assert ent_reg.async_get_entity_id("number", DOMAIN, f"{unique_id}_anchor_z_m") is not None


async def test_legacy_scanner_number_entities_removed_on_setup(hass) -> None:
    """Ensure stale legacy per-scanner number entities are pruned on startup."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="test-number-cleanup", title=NAME)
    entry.add_to_hass(hass)

    ent_reg = er.async_get(hass)
    stale_entry = ent_reg.async_get_or_create(
        "number",
        DOMAIN,
        "aa:bb:cc:dd:ee:02_rssi_offset",
        config_entry=entry,
        suggested_object_id="legacy_scanner_rssi_offset",
    )

    with patch("custom_components.ble_trilateration.BermudaDataUpdateCoordinator.async_refresh"):
        assert await async_setup_component(hass, DOMAIN, {})

    await hass.async_block_till_done()

    assert ent_reg.async_get(stale_entry.entity_id) is None


async def test_scanner_anchor_numbers_persist_to_storage(hass) -> None:
    """Anchor coordinate updates should also be mirrored into Bermuda storage."""
    entry = await setup_integration(hass)
    coordinator = entry.runtime_data.coordinator

    scanner = _create_scanner(coordinator, "AA:BB:CC:DD:EE:03")
    await hass.async_block_till_done()

    ent_reg = er.async_get(hass)
    anchor_x = ent_reg.async_get_entity_id("number", DOMAIN, f"{scanner.unique_id}_anchor_x_m")
    anchor_y = ent_reg.async_get_entity_id("number", DOMAIN, f"{scanner.unique_id}_anchor_y_m")
    anchor_z = ent_reg.async_get_entity_id("number", DOMAIN, f"{scanner.unique_id}_anchor_z_m")

    await hass.services.async_call("number", "set_value", {"entity_id": anchor_x, "value": 1.2}, blocking=True)
    await hass.services.async_call("number", "set_value", {"entity_id": anchor_y, "value": 3.4}, blocking=True)
    await hass.services.async_call("number", "set_value", {"entity_id": anchor_z, "value": 5.6}, blocking=True)

    stored = await coordinator.scanner_anchor_store.async_get_coordinates(scanner)
    assert stored == {"anchor_x_m": 1.2, "anchor_y_m": 3.4, "anchor_z_m": 5.6}


async def test_scanner_anchor_numbers_restore_from_storage(hass) -> None:
    """Anchor coordinates should restore from Bermuda storage when restore-state is absent."""
    entry = await setup_integration(hass)
    coordinator = entry.runtime_data.coordinator

    stored_scanner = BermudaDevice("AA:BB:CC:DD:EE:04", coordinator)
    stored_scanner.anchor_x_m = 7.8
    stored_scanner.anchor_y_m = 9.1
    stored_scanner.anchor_z_m = 2.3
    await coordinator.scanner_anchor_store.async_save_scanner(stored_scanner)

    scanner = _create_scanner(coordinator, "AA:BB:CC:DD:EE:04")
    await hass.async_block_till_done()

    assert scanner.anchor_x_m == 7.8
    assert scanner.anchor_y_m == 9.1
    assert scanner.anchor_z_m == 2.3


async def test_scanner_anchor_store_can_hydrate_live_scanners_before_number_restore(hass) -> None:
    """Coordinator should be able to restore scanner anchors before number entities initialise."""
    entry = await setup_integration(hass)
    coordinator = entry.runtime_data.coordinator

    stored_scanner = BermudaDevice("AA:BB:CC:DD:EE:14", coordinator)
    stored_scanner._is_scanner = True  # noqa: SLF001 - test helper
    stored_scanner._is_remote_scanner = False  # noqa: SLF001 - test helper
    stored_scanner.anchor_x_m = 4.4
    stored_scanner.anchor_y_m = 5.5
    stored_scanner.anchor_z_m = 6.6
    await coordinator.scanner_anchor_store.async_save_scanner(stored_scanner)

    scanner = _create_scanner(coordinator, "AA:BB:CC:DD:EE:14")
    scanner.anchor_x_m = None
    scanner.anchor_y_m = None
    scanner.anchor_z_m = None

    await coordinator.scanner_anchor_store.async_ensure_loaded()
    coordinator._restore_scanner_anchors_from_store()

    assert scanner.anchor_x_m == 4.4
    assert scanner.anchor_y_m == 5.5
    assert scanner.anchor_z_m == 6.6


async def test_scanner_anchor_store_can_hydrate_single_scanner_after_late_init(hass) -> None:
    """Late scanner resolution should still pick up stored anchors immediately."""
    entry = await setup_integration(hass)
    coordinator = entry.runtime_data.coordinator

    stored_scanner = BermudaDevice("AA:BB:CC:DD:EE:15", coordinator)
    stored_scanner._is_scanner = True  # noqa: SLF001 - test helper
    stored_scanner._is_remote_scanner = False  # noqa: SLF001 - test helper
    stored_scanner.anchor_x_m = 1.1
    stored_scanner.anchor_y_m = 2.2
    stored_scanner.anchor_z_m = 3.3
    await coordinator.scanner_anchor_store.async_save_scanner(stored_scanner)

    scanner = _create_scanner(coordinator, "AA:BB:CC:DD:EE:15")
    scanner.anchor_x_m = None
    scanner.anchor_y_m = None
    scanner.anchor_z_m = None

    await coordinator.scanner_anchor_store.async_ensure_loaded()
    restored = coordinator._restore_scanner_anchor_from_store(scanner)

    assert restored is True
    assert scanner.anchor_x_m == 1.1
    assert scanner.anchor_y_m == 2.2
    assert scanner.anchor_z_m == 3.3


async def test_anchor_geometry_changed_passes_human_readable_names_to_trilat_repair(hass) -> None:
    """async_handle_anchor_geometry_changed must produce human-readable scanner names.

    Before the fix, it passed raw MAC address strings from scanner_list to
    _async_manage_repair_trilat_without_anchors.  The repair placeholder uses
    these strings verbatim, so users would see addresses instead of scanner
    names, AND any prior state set by _refresh_trilateration (which uses
    "Name [addr]" format) would never match, causing spurious repair churn.
    """
    entry = await setup_integration(hass)
    coordinator = entry.runtime_data.coordinator

    scanner = _create_scanner(coordinator, "AA:BB:CC:DD:EE:20")
    scanner.name = "Hall Proxy"
    # No anchor coordinates set: this scanner has no anchors.
    await hass.async_block_till_done()

    # Simulate what _refresh_trilateration produces: no configured anchor scanners
    # so it records a "Name [addr]" list.
    coordinator._async_manage_repair_trilat_without_anchors(
        [f"{scanner.name} [{scanner.address}]"]
    )
    recorded_list = coordinator._trilat_scanners_without_anchors[:]
    issue = ir.async_get(hass).async_get_issue(DOMAIN, REPAIR_TRILAT_WITHOUT_ANCHORS)
    assert issue is not None
    assert issue.translation_placeholders["scannerlist"] == f"- {recorded_list[0]}\n"

    # Now fire anchor geometry changed (simulating a number restore).
    with (
        patch("custom_components.ble_trilateration.coordinator.ir.async_create_issue") as create_issue,
        patch("custom_components.ble_trilateration.coordinator.ir.async_delete_issue") as delete_issue,
    ):
        await coordinator.async_handle_anchor_geometry_changed(reason="test_restore")

    # The stored list should still be in the same "Name [addr]" format so that
    # the equality check in _async_manage_repair_trilat_without_anchors behaves
    # consistently and no spurious churn occurs.
    create_issue.assert_not_called()
    delete_issue.assert_not_called()
    assert coordinator._trilat_scanners_without_anchors is not None
    assert coordinator._trilat_scanners_without_anchors == recorded_list
    for entry_str in coordinator._trilat_scanners_without_anchors:
        # Each entry must contain a space (i.e. at least "Name [addr]") rather
        # than being a bare MAC address like "aa:bb:cc:dd:ee:20".
        assert " " in entry_str, (
            f"Scanner list entry {entry_str!r} looks like a raw address; "
            "expected 'Name [addr]' format"
        )
