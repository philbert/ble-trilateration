"""Create Bermuda Number entities."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.number import NumberExtraStoredData, NumberMode, RestoreNumber
from homeassistant.const import EntityCategory, UnitOfLength
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers import entity_registry as er

from .const import SIGNAL_DEVICE_NEW, SIGNAL_SCANNERS_CHANGED
from .entity import BermudaEntity

if TYPE_CHECKING:
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import BermudaConfigEntry
    from .coordinator import BermudaDataUpdateCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BermudaConfigEntry,
    async_add_devices: AddEntitiesCallback,
) -> None:
    """Load Number entities for a config entry."""
    coordinator: BermudaDataUpdateCoordinator = entry.runtime_data.coordinator
    _remove_legacy_scanner_numbers(hass, entry.entry_id)

    created_scanner_entities = []  # list of scanner addresses we've created config entities for

    @callback
    def device_new(address: str) -> None:
        """
        Create entities for newly-found device.

        Called from the data co-ordinator when it finds a new device that needs
        to have sensors created. Not called directly, but via the dispatch
        facility from HA.
        Make sure you have a full list of scanners ready before calling this.
        """
        coordinator.number_created(address)
        # Also check for pending scanner anchors, since scanner resolution
        # may now be complete (mirrors sensor.py's create_scanner_entities() call).
        scanners_changed()

    @callback
    def scanners_changed() -> None:
        """Create per-scanner configuration Number entities."""
        entities = []
        for address, device in coordinator.devices.items():
            if not device.is_scanner:
                continue
            if address in created_scanner_entities:
                continue
            # Skip this specific scanner until it has resolved its wifi MAC,
            # so the entity unique_id (based on device.unique_id) is stable.
            if device.is_remote_scanner is None:
                continue
            if device.is_remote_scanner and device.address_wifi_mac is None:
                continue
            entities.append(BermudaScannerAnchorX(coordinator, entry, address))
            entities.append(BermudaScannerAnchorY(coordinator, entry, address))
            entities.append(BermudaScannerAnchorZ(coordinator, entry, address))
            created_scanner_entities.append(address)

        if entities:
            async_add_devices(entities, False)

    # Connect device_new to a signal so the coordinator can call it
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_DEVICE_NEW, device_new))

    # Connect scanners_changed to handle new scanners
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_SCANNERS_CHANGED, scanners_changed))

    # Now we must tell the co-ord to do initial refresh, so that it will call our callback.
    # await coordinator.async_config_entry_first_refresh()


LEGACY_SCANNER_NUMBER_SUFFIXES = (
    "_rssi_offset",
    "_attenuation",
    "_max_radius",
)


def _remove_legacy_scanner_numbers(hass: HomeAssistant, entry_id: str) -> None:
    """Remove entity-registry entries for retired per-scanner calibration numbers."""
    entity_registry = er.async_get(hass)
    for entity_entry in er.async_entries_for_config_entry(entity_registry, entry_id):
        if entity_entry.domain != "number":
            continue
        if entity_entry.unique_id.endswith(LEGACY_SCANNER_NUMBER_SUFFIXES):
            entity_registry.async_remove(entity_entry.entity_id)


class _BermudaScannerAnchorCoordinate(BermudaEntity, RestoreNumber):
    """Base class for scanner anchor coordinate configuration numbers."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = -500
    _attr_native_max_value = 500
    _attr_native_step = 0.1
    _attr_native_unit_of_measurement = UnitOfLength.METERS
    _attr_mode = NumberMode.BOX

    _coord_attr: str = ""
    _coord_suffix: str = ""
    _default_name: str = ""

    def __init__(
        self,
        coordinator: BermudaDataUpdateCoordinator,
        entry: BermudaConfigEntry,
        address: str,
    ) -> None:
        """Initialise the scanner anchor coordinate entity."""
        self.restored_data: NumberExtraStoredData | None = None
        super().__init__(coordinator, entry, address)

    async def async_added_to_hass(self) -> None:
        """Restore values from HA storage on startup."""
        await super().async_added_to_hass()
        self.restored_data = await self.async_get_last_number_data()
        if self.restored_data is not None and self.restored_data.native_value is not None:
            setattr(self.coordinator.devices[self.address], self._coord_attr, self.restored_data.native_value)
            await self.coordinator.scanner_anchor_store.async_save_scanner(self.coordinator.devices[self.address])
            return

        if (
            stored_coords := await self.coordinator.scanner_anchor_store.async_get_coordinates(
                self.coordinator.devices[self.address]
            )
        ) is not None:
            if (stored_value := stored_coords.get(self._coord_attr)) is not None:
                setattr(self.coordinator.devices[self.address], self._coord_attr, stored_value)

    @property
    def native_value(self) -> float | None:
        """Return value of number."""
        return getattr(self.coordinator.devices[self.address], self._coord_attr, None)

    async def async_set_native_value(self, value: float) -> None:
        """Set value."""
        setattr(self.coordinator.devices[self.address], self._coord_attr, value)
        await self.coordinator.scanner_anchor_store.async_save_scanner(self.coordinator.devices[self.address])
        self.async_write_ha_state()

    @property
    def unique_id(self):
        """Uniquely identify this entity."""
        return f"{self._device.unique_id}_{self._coord_suffix}"

    @property
    def name(self):
        """Return the name of the entity."""
        return self._default_name


class BermudaScannerAnchorX(_BermudaScannerAnchorCoordinate):
    """Anchor X coordinate configuration for a scanner device."""

    _coord_attr = "anchor_x_m"
    _coord_suffix = "anchor_x_m"
    _default_name = "Anchor X"


class BermudaScannerAnchorY(_BermudaScannerAnchorCoordinate):
    """Anchor Y coordinate configuration for a scanner device."""

    _coord_attr = "anchor_y_m"
    _coord_suffix = "anchor_y_m"
    _default_name = "Anchor Y"


class BermudaScannerAnchorZ(_BermudaScannerAnchorCoordinate):
    """Anchor Z coordinate configuration for a scanner device."""

    _coord_attr = "anchor_z_m"
    _coord_suffix = "anchor_z_m"
    _default_name = "Anchor Z"
