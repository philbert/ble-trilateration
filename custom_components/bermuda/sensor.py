"""Sensor platform for Bermuda BLE Trilateration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.components.sensor.const import SensorDeviceClass, SensorStateClass
from homeassistant.const import (
    STATE_UNAVAILABLE,
    EntityCategory,
    UnitOfLength,
    UnitOfSpeed,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers import entity_registry as er

from .const import (
    _LOGGER,
    ADDR_TYPE_IBEACON,
    ADDR_TYPE_PRIVATE_BLE_DEVICE,
    SIGNAL_DEVICE_NEW,
    SIGNAL_SCANNERS_CHANGED,
)
from .entity import BermudaEntity, BermudaGlobalEntity

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import BermudaConfigEntry
    from .coordinator import BermudaDataUpdateCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BermudaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Setup sensor platform."""
    coordinator: BermudaDataUpdateCoordinator = entry.runtime_data.coordinator
    _remove_retired_sensor_entities(hass, entry.entry_id)

    created_devices: list[str] = []  # list of already-created devices
    created_scanners: dict[str, list[str]] = {}  # list of scanner:address for created entities
    created_scanner_devices: list[str] = []

    @callback
    def device_new(address: str) -> None:
        """
        Create entities for newly-found device.

        Called from the data co-ordinator when it finds a new device that needs
        to have sensors created. Not called directly, but via the dispatch
        facility from HA.
        """
        # if len(scanners) == 0:
        #     # Bail out until we get called with some scanners to work with!
        #     return
        # for scanner in scanners:
        #     if (
        #         coordinator.devices[scanner]._is_remote_scanner is None  # usb/HCI scanner's are fine.
        #         or (
        #             coordinator.devices[scanner]._is_remote_scanner  # usb/HCI scanner's are fine.
        #             and coordinator.devices[scanner].address_wifi_mac is None
        #         )
        #     ):
        #         # This scanner doesn't have a wifi mac yet, bail out
        #         # until they are all filled out.
        #         return

        if address not in created_devices:
            entities = []
            entities.append(BermudaSensor(coordinator, entry, address))
            entities.append(BermudaSensorMobilityMode(coordinator, entry, address))
            entities.append(BermudaSensorTrilatX(coordinator, entry, address))
            entities.append(BermudaSensorTrilatY(coordinator, entry, address))
            entities.append(BermudaSensorTrilatZ(coordinator, entry, address))
            entities.append(BermudaSensorTrilatFloor(coordinator, entry, address))
            entities.append(BermudaSensorTrilatAnchorCount(coordinator, entry, address))
            entities.append(BermudaSensorPositionConfidence(coordinator, entry, address))
            entities.append(BermudaSensorTrackingConfidence(coordinator, entry, address))
            entities.append(BermudaSensorGeometryQuality(coordinator, entry, address))
            entities.append(BermudaSensorResidualConsistency(coordinator, entry, address))
            entities.append(BermudaSensorHorizontalSpeed(coordinator, entry, address))
            entities.append(BermudaSensorVerticalSpeed(coordinator, entry, address))

            # _LOGGER.debug("Sensor received new_device signal for %s", address)
            # We set update before add to False because we are being
            # call(back(ed)) from the update, so causing it to call another would be... bad.
            async_add_entities(entities, False)
            created_devices.append(address)
        else:
            # We've already created this one.
            # _LOGGER.debug("Ignoring duplicate creation request for %s", address)
            pass
        # Get the per-scanner entities set up to match
        create_scanner_entities()
        # tell the co-ord we've done it.
        coordinator.sensor_created(address)

    def create_scanner_entities():
        # These are per-proxy entities on each device, and scanners may come and
        # go over time. So we need to maintain our matrix of which ones we have already
        # spun-up so we don't duplicate any.

        entities = []
        for scanner in coordinator.scanner_list:
            # Skip this specific scanner until its unique_id is stable (wifi MAC resolved),
            # to avoid orphaned entity registry entries if unique_id changes.
            scanner_device = coordinator.devices.get(scanner)
            if scanner_device is None:
                continue
            if scanner_device.is_remote_scanner is None:
                continue
            if scanner_device.is_remote_scanner and scanner_device.address_wifi_mac is None:
                continue
            if scanner not in created_scanner_devices:
                entities.append(BermudaSensorScannerTimestampSync(coordinator, entry, scanner))
                created_scanner_devices.append(scanner)
            for address in created_devices:
                if address not in created_scanners.get(scanner, []):
                    _LOGGER.debug(
                        "Creating Scanner %s entities for %s",
                        scanner,
                        address,
                    )
                    entities.append(BermudaSensorScannerAdvertStatus(coordinator, entry, address, scanner))
                    entities.append(BermudaSensorTrackedDeviceAdvertStatus(coordinator, entry, address, scanner))
                    created_entry = created_scanners.setdefault(scanner, [])
                    created_entry.append(address)
        # _LOGGER.debug("Sensor received new_device signal for %s", address)
        # We set update before add to False because we are being
        # call(back(ed)) from the update, so causing it to call another would be... bad.
        async_add_entities(entities, False)

    @callback
    def scanners_changed() -> None:
        """Callback for event from coordinator advising that the roster of scanners has changed."""
        create_scanner_entities()

    # Connect device_new to a signal so the coordinator can call it
    _LOGGER.debug("Registering device_new and scanners_changed callbacks")
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_DEVICE_NEW, device_new))
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_SCANNERS_CHANGED, scanners_changed))

    # Create Global Bermuda entities
    async_add_entities(
        (
            BermudaTotalProxyCount(coordinator, entry),
            BermudaActiveProxyCount(coordinator, entry),
            BermudaTotalDeviceCount(coordinator, entry),
            BermudaVisibleDeviceCount(coordinator, entry),
        )
    )


RETIRED_SENSOR_UNIQUE_ID_SUFFIXES = (
    "_floor",
    "_scanner",
    "_rssi",
    "_range",
    "_range_raw",
    "_area_last_seen",
    "_area_switch_reason",
)


def _remove_retired_sensor_entities(hass: HomeAssistant, entry_id: str) -> None:
    """Remove entity-registry entries for retired legacy sensor entities."""
    entity_registry = er.async_get(hass)
    for entity_entry in er.async_entries_for_config_entry(entity_registry, entry_id):
        if entity_entry.domain != "sensor":
            continue
        if entity_entry.unique_id.endswith(RETIRED_SENSOR_UNIQUE_ID_SUFFIXES):
            entity_registry.async_remove(entity_entry.entity_id)


class BermudaSensor(BermudaEntity, SensorEntity):
    """bermuda Sensor class."""

    @property
    def unique_id(self):
        """
        "Uniquely identify this sensor so that it gets stored in the entity_registry,
        and can be maintained / renamed etc by the user.
        """
        return self._device.unique_id

    @property
    def has_entity_name(self) -> bool:
        """
        Indicate that our name() method only returns the entity's name,
        so that HA should prepend the device name for the user.
        """
        return True

    @property
    def name(self):
        """Return the name of the sensor."""
        return "Area"

    @property
    def native_value(self):
        """Return the state of the sensor."""
        # return self.coordinator.data.get("body")
        return self._device.area_name

    @property
    def icon(self):
        """Provide a custom icon for particular entities."""
        # TODO: This is ugly doing a check on name, and is a kludge
        # because I originally was a bit reckless with the multiple
        # inheritance here. So all the sensors should be restructured
        # a bit to clean up this and other properties.
        if self.name == "Area":
            return self._device.area_icon
        return super().icon
        # return "mdi:floor-plan" or "mdi:map-marker-distance" or "mdi:signal-distance-variant"

    @property
    def entity_registry_enabled_default(self) -> bool:
        """Declare if entity should be automatically enabled on adding."""
        return self.name == "Area"

    @property
    def device_class(self):
        """Return de device class of the sensor."""
        # There isn't one for "Area Names" so we'll arbitrarily define our own.
        if self.name == "Area":
            return "bermuda__custom_device_class"
        return None

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Provide state_attributes for the sensor entity."""
        # By default, it's the device's MAC
        current_mac = self._device.address
        # But metadevices have source_devices
        if self._device.address_type in [
            ADDR_TYPE_IBEACON,
            ADDR_TYPE_PRIVATE_BLE_DEVICE,
        ]:
            # Check the current sources and find the latest
            current_mac: str = STATE_UNAVAILABLE
            _best_stamp = 0
            for source_ad in self._device.adverts.values():
                if source_ad.stamp > _best_stamp:  # It's a valid ad
                    current_mac = source_ad.device_address
                    _best_stamp = source_ad.stamp

        # Limit how many attributes we list - prefer new sensors instead
        # since oft-changing attribs cause more db writes than sensors
        # "last_seen": self.coordinator.dt_mono_to_datetime(self._device.last_seen),
        attribs = {}
        if self.name == "Area":
            attribs["area_id"] = self._device.area_id
            attribs["area_name"] = self._device.area_name
            attribs["floor_id"] = self._device.floor_id
            attribs["floor_name"] = self._device.floor_name
            attribs["floor_level"] = self._device.floor_level
        attribs["current_mac"] = current_mac

        return attribs


class BermudaSensorScannerAdvertStatus(BermudaSensor):
    """Tracked-device-side status of how a scanner advert was treated."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: BermudaDataUpdateCoordinator,
        config_entry,
        address: str,
        scanner_address: str,
    ) -> None:
        super().__init__(coordinator, config_entry, address)
        self._scanner = coordinator.devices[scanner_address]

    def _status_entry(self) -> Mapping[str, Any] | None:
        statuses = getattr(self._device, "trilat_anchor_statuses", {})
        return statuses.get(self._scanner.address.lower())

    @property
    def unique_id(self):
        return f"{self._device.unique_id}_{self._scanner.address_wifi_mac or self._scanner.address}_ble_status"

    @property
    def name(self):
        return f"BLE Status to {self._scanner.name}"

    @property
    def native_value(self):
        status_entry = self._status_entry()
        if status_entry is None:
            return "no_advert"
        return status_entry.get("status")

    @property
    def entity_registry_enabled_default(self) -> bool:
        return True

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        status_entry = dict(self._status_entry() or {})
        status_entry.setdefault("scanner_name", self._scanner.name)
        status_entry.setdefault("scanner_address", self._scanner.address)
        return status_entry


class BermudaSensorTrackedDeviceAdvertStatus(BermudaSensor):
    """Scanner-side mirror of tracked-device advert handling status."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: BermudaDataUpdateCoordinator,
        config_entry,
        tracked_address: str,
        scanner_address: str,
    ) -> None:
        super().__init__(coordinator, config_entry, scanner_address)
        self._tracked_device = coordinator.devices[tracked_address]
        # Track the tracked device's name separately so we can detect renames.
        # This entity lives on the scanner device, so the parent's _lastname only
        # covers scanner renames; we need this for tracked-device renames.
        self._tracked_lastname = self._tracked_device.name

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle coordinator update, detecting both scanner and tracked-device renames."""
        super()._handle_coordinator_update()
        if self._tracked_device.name != self._tracked_lastname:
            old_tracked_name = self._tracked_lastname
            self._tracked_lastname = self._tracked_device.name
            self._async_rename_entity_id(old_tracked_name, self._tracked_device.name)
        # Fix stale scanner prefix: if the entity_id was renamed when the scanner
        # device had a temporarily wrong name, correct it to match the current scanner name.
        self._async_fix_stale_entity_id(self._device.name)

    def _status_entry(self) -> Mapping[str, Any] | None:
        statuses = getattr(self._tracked_device, "trilat_anchor_statuses", {})
        return statuses.get(self._device.address.lower())

    @property
    def unique_id(self):
        return f"{self._device.unique_id}_{self._tracked_device.unique_id}_tracked_ble_status"

    @property
    def name(self):
        return f"{self._tracked_device.name} BLE Status"

    @property
    def native_value(self):
        status_entry = self._status_entry()
        if status_entry is None:
            return "no_advert"
        return status_entry.get("status")

    @property
    def entity_registry_enabled_default(self) -> bool:
        return True

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        status_entry = dict(self._status_entry() or {})
        status_entry.setdefault("tracked_device_name", self._tracked_device.name)
        status_entry.setdefault("tracked_device_address", self._tracked_device.address)
        status_entry.setdefault("scanner_name", self._device.name)
        status_entry.setdefault("scanner_address", self._device.address)
        return status_entry


class BermudaSensorMobilityMode(BermudaSensor):
    """Diagnostic sensor exposing effective mobility mode for this device."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def unique_id(self):
        return f"{self._device.unique_id}_mobility_mode"

    @property
    def name(self):
        return "Mobility Mode"

    @property
    def native_value(self):
        return self._device.get_mobility_type()


class BermudaSensorTrilatX(BermudaSensor):
    """Diagnostic sensor for trilat X coordinate."""

    @property
    def unique_id(self):
        return f"{self._device.unique_id}_trilat_x"

    @property
    def name(self):
        return "Trilat X"

    @property
    def native_value(self):
        x_val = getattr(self._device, "trilat_x_m", None)
        if x_val is None:
            return None
        return round(x_val, 3)

    @property
    def device_class(self):
        return SensorDeviceClass.DISTANCE

    @property
    def native_unit_of_measurement(self):
        return UnitOfLength.METERS


class BermudaSensorTrilatY(BermudaSensorTrilatX):
    """Diagnostic sensor for trilat Y coordinate."""

    @property
    def unique_id(self):
        return f"{self._device.unique_id}_trilat_y"

    @property
    def name(self):
        return "Trilat Y"

    @property
    def native_value(self):
        y_val = getattr(self._device, "trilat_y_m", None)
        if y_val is None:
            return None
        return round(y_val, 3)


class BermudaSensorTrilatZ(BermudaSensorTrilatX):
    """Diagnostic sensor for trilat Z coordinate."""

    @property
    def unique_id(self):
        return f"{self._device.unique_id}_trilat_z"

    @property
    def name(self):
        return "Trilat Z"

    @property
    def native_value(self):
        z_val = getattr(self._device, "trilat_z_m", None)
        if z_val is None:
            return None
        return round(z_val, 3)


class BermudaSensorTrilatFloor(BermudaSensor):
    """Diagnostic sensor for chosen trilat floor."""

    @property
    def unique_id(self):
        return f"{self._device.unique_id}_trilat_floor"

    @property
    def name(self):
        return "Trilat Floor"

    @property
    def native_value(self):
        return getattr(self._device, "trilat_floor_name", None)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        attrs = dict(getattr(self._device, "trilat_floor_diagnostics", {}))
        floor_evidence = getattr(self._device, "trilat_floor_evidence", {})
        floor_evidence_names = getattr(self._device, "trilat_floor_evidence_names", {})
        if floor_evidence:
            attrs["floor_evidence"] = [
                {
                    "floor_id": floor_id,
                    "floor_name": floor_evidence_names.get(floor_id),
                    "score": round(score, 3),
                }
                for floor_id, score in sorted(
                    floor_evidence.items(),
                    key=lambda row: row[1],
                    reverse=True,
                )
            ]
        return attrs


class BermudaSensorTrilatAnchorCount(BermudaSensor):
    """Diagnostic sensor for active trilat anchor count."""

    @property
    def unique_id(self):
        return f"{self._device.unique_id}_trilat_anchor_count"

    @property
    def name(self):
        return "Trilat Anchor Count"

    @property
    def native_value(self):
        return getattr(self._device, "trilat_anchor_count", 0)

    @property
    def state_class(self):
        return SensorStateClass.MEASUREMENT

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        anchor_lines = list(getattr(self._device, "trilat_anchor_diagnostics", []))
        attrs: dict[str, Any] = {
            "used_anchors": getattr(self._device, "trilat_anchor_count", 0),
            "cross_floor_candidate_count": getattr(self._device, "trilat_cross_floor_anchor_count", 0),
        }
        cross_floor_lines = list(getattr(self._device, "trilat_cross_floor_anchor_diagnostics", []))
        if cross_floor_lines:
            attrs["cross_floor_candidates"] = cross_floor_lines
        for index, line in enumerate(anchor_lines, start=1):
            attrs[str(index)] = line
        return attrs


class BermudaSensorPositionConfidence(BermudaSensor):
    """Diagnostic sensor for trilat confidence score."""

    @property
    def unique_id(self):
        return f"{self._device.unique_id}_position_confidence"

    @property
    def name(self):
        return "Position Confidence"

    @property
    def native_value(self):
        confidence = getattr(self._device, "trilat_confidence", 0.0)
        return round(confidence, 1)

    @property
    def state_class(self):
        return SensorStateClass.MEASUREMENT

    @property
    def entity_registry_enabled_default(self) -> bool:
        return True

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        return {"level": getattr(self._device, "trilat_confidence_level", "low")}


class BermudaSensorTrackingConfidence(BermudaSensorPositionConfidence):
    """Diagnostic sensor for filtered tracked-position confidence."""

    @property
    def unique_id(self):
        return f"{self._device.unique_id}_tracking_confidence"

    @property
    def name(self):
        return "Tracking Confidence"

    @property
    def native_value(self):
        confidence = getattr(self._device, "trilat_tracking_confidence", 0.0)
        return round(confidence, 1)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        return {"level": getattr(self._device, "trilat_tracking_confidence_level", "low")}


class BermudaSensorGeometryQuality(BermudaSensorPositionConfidence):
    """Diagnostic sensor for trilat anchor-geometry quality."""

    @property
    def unique_id(self):
        return f"{self._device.unique_id}_geometry_quality"

    @property
    def name(self):
        return "Geometry Quality"

    @property
    def native_value(self):
        return round(getattr(self._device, "trilat_geometry_quality", 0.0), 1)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        return {
            "gdop": getattr(self._device, "trilat_geometry_gdop", None),
            "condition_number": getattr(self._device, "trilat_geometry_condition", None),
        }


class BermudaSensorResidualConsistency(BermudaSensorPositionConfidence):
    """Diagnostic sensor for per-anchor residual consistency."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def unique_id(self):
        return f"{self._device.unique_id}_residual_consistency"

    @property
    def name(self):
        return "Residual Consistency"

    @property
    def native_value(self):
        return round(getattr(self._device, "trilat_residual_consistency", 0.0), 1)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        return {
            "normalized_residual_rms": getattr(self._device, "trilat_normalized_residual_rms", None),
            "residual_m": getattr(self._device, "trilat_residual_m", None),
        }


class BermudaSensorHorizontalSpeed(BermudaSensor):
    """Diagnostic sensor for filtered horizontal speed."""

    @property
    def unique_id(self):
        return f"{self._device.unique_id}_horizontal_speed"

    @property
    def name(self):
        return "Speed Horizontal"

    @property
    def native_value(self):
        speed = getattr(self._device, "trilat_horizontal_speed_mps", None)
        if speed is None:
            return None
        return round(speed, 3)

    @property
    def device_class(self):
        return SensorDeviceClass.SPEED

    @property
    def native_unit_of_measurement(self):
        return UnitOfSpeed.METERS_PER_SECOND

    @property
    def state_class(self):
        return SensorStateClass.MEASUREMENT


class BermudaSensorVerticalSpeed(BermudaSensorHorizontalSpeed):
    """Diagnostic sensor for filtered vertical speed."""

    @property
    def unique_id(self):
        return f"{self._device.unique_id}_vertical_speed"

    @property
    def name(self):
        return "Speed Vertical"

    @property
    def native_value(self):
        speed = getattr(self._device, "trilat_vertical_speed_mps", None)
        if speed is None:
            return None
        return round(speed, 3)


class BermudaSensorScannerTimestampSync(BermudaSensor):
    """Diagnostic sensor for scanner timestamp synchronization health."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def unique_id(self):
        return f"{self._device.unique_id}_timestamp_sync"

    @property
    def name(self):
        return "Timestamp Sync"

    @property
    def native_value(self):
        return self._device.timestamp_sync_diagnostics()["state"]

    @property
    def entity_registry_enabled_default(self) -> bool:
        return True

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        diagnostics = dict(self._device.timestamp_sync_diagnostics())
        diagnostics.pop("state", None)
        return diagnostics


class BermudaGlobalSensor(BermudaGlobalEntity, SensorEntity):
    """bermuda Global Sensor class."""

    _attr_has_entity_name = True

    @property
    def name(self):
        """Return the name of the sensor."""
        return "Area"

    @property
    def device_class(self):
        """Return de device class of the sensor."""
        return "bermuda__custom_device_class"


class BermudaTotalProxyCount(BermudaGlobalSensor):
    """Counts the total number of proxies we have access to."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def unique_id(self):
        """
        "Uniquely identify this sensor so that it gets stored in the entity_registry,
        and can be maintained / renamed etc by the user.
        """
        return "BERMUDA_GLOBAL_PROXY_COUNT"

    @property
    def native_value(self) -> int:
        """Gets the number of proxies we have access to."""
        return self._cached_ratelimit(len(self.coordinator.scanner_list)) or 0

    @property
    def name(self):
        """Gets the name of the sensor."""
        return "Total proxy count"


class BermudaActiveProxyCount(BermudaGlobalSensor):
    """Counts the number of proxies that are active."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def unique_id(self):
        """
        "Uniquely identify this sensor so that it gets stored in the entity_registry,
        and can be maintained / renamed etc by the user.
        """
        return "BERMUDA_GLOBAL_ACTIVE_PROXY_COUNT"

    @property
    def native_value(self) -> int:
        """Gets the number of proxies we have access to."""
        return self._cached_ratelimit(self.coordinator.count_active_scanners()) or 0

    @property
    def name(self):
        """Gets the name of the sensor."""
        return "Active proxy count"


class BermudaTotalDeviceCount(BermudaGlobalSensor):
    """Counts the total number of devices we can see."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def unique_id(self):
        """
        "Uniquely identify this sensor so that it gets stored in the entity_registry,
        and can be maintained / renamed etc by the user.
        """
        return "BERMUDA_GLOBAL_DEVICE_COUNT"

    @property
    def native_value(self) -> int:
        """Gets the amount of devices we have seen."""
        return self._cached_ratelimit(len(self.coordinator.devices)) or 0

    @property
    def name(self):
        """Gets the name of the sensor."""
        return "Total device count"


class BermudaVisibleDeviceCount(BermudaGlobalSensor):
    """Counts the number of devices that are active."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def unique_id(self):
        """
        "Uniquely identify this sensor so that it gets stored in the entity_registry,
        and can be maintained / renamed etc by the user.
        """
        return "BERMUDA_GLOBAL_VISIBLE_DEVICE_COUNT"

    @property
    def native_value(self) -> int:
        """Gets the amount of devices that are active."""
        return self._cached_ratelimit(self.coordinator.count_active_devices()) or 0

    @property
    def name(self):
        """Gets the name of the sensor."""
        return "Visible device count"
