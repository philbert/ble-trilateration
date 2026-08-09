"""Sensor platform for BLE Trilateration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.components.sensor.const import SensorDeviceClass, SensorStateClass
from homeassistant.const import (
    STATE_UNAVAILABLE,
    EntityCategory,
    UnitOfLength,
    UnitOfSpeed,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect

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
            entities.append(BermudaSensorArea(coordinator, entry, address))
            entities.append(BermudaSensorMobilityMode(coordinator, entry, address))
            entities.append(BermudaSensorTrilatX(coordinator, entry, address))
            entities.append(BermudaSensorTrilatY(coordinator, entry, address))
            entities.append(BermudaSensorTrilatZ(coordinator, entry, address))
            entities.append(BermudaSensorPositionUncertaintyXBand(coordinator, entry, address))
            entities.append(BermudaSensorPositionUncertaintyYBand(coordinator, entry, address))
            entities.append(BermudaSensorTrilatFloor(coordinator, entry, address))
            entities.append(BermudaSensorTrilatAnchorCount(coordinator, entry, address))
            entities.append(BermudaSensorPositionConfidence(coordinator, entry, address))
            entities.append(BermudaSensorTrackingConfidence(coordinator, entry, address))
            entities.append(BermudaSensorGeometryQuality(coordinator, entry, address))
            entities.append(BermudaSensorResidualConsistency(coordinator, entry, address))
            entities.append(BermudaSensorRoomDecisionReason(coordinator, entry, address))
            entities.append(BermudaSensorRoomCandidate(coordinator, entry, address))
            entities.append(BermudaSensorRoomChallenger(coordinator, entry, address))
            entities.append(BermudaSensorRoomChallengerEvidence(coordinator, entry, address))
            entities.append(BermudaSensorRoomScoreMargin(coordinator, entry, address))
            entities.append(BermudaSensorGeometryRoomScore(coordinator, entry, address))
            entities.append(BermudaSensorFingerprintRoomScore(coordinator, entry, address))
            entities.append(BermudaSensorFingerprintBestRoom(coordinator, entry, address))
            entities.append(BermudaSensorFingerprintMargin(coordinator, entry, address))
            entities.append(BermudaSensorFingerprintCoverage(coordinator, entry, address))
            entities.append(BermudaSensorFingerprintBlendWeight(coordinator, entry, address))
            entities.append(BermudaSensorRoomSampleCount(coordinator, entry, address))
            entities.append(BermudaSensorRoomHoldReason(coordinator, entry, address))
            entities.append(BermudaSensorGeometryGdop(coordinator, entry, address))
            entities.append(BermudaSensorGeometryConditionNumber(coordinator, entry, address))
            entities.append(BermudaSensorNormalizedResidualRms(coordinator, entry, address))
            entities.append(BermudaSensorResidualRms(coordinator, entry, address))
            entities.append(BermudaSensorBroadcastInterval(coordinator, entry, address))
            entities.append(BermudaSensorAdvertIntervalDrift(coordinator, entry, address))
            entities.append(BermudaSensorValidAnchorCount(coordinator, entry, address))
            entities.append(BermudaSensorStaleAnchorCount(coordinator, entry, address))
            entities.append(BermudaSensorNoAdvertAnchorCount(coordinator, entry, address))
            entities.append(BermudaSensorValidOtherFloorAnchorCount(coordinator, entry, address))
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

    # Catch up on devices/scanners that already existed before this platform finished
    # wiring its dispatcher callbacks. Without this pass, restored per-scanner BLE
    # status sensors can stay unavailable until a later dispatcher event happens.
    for address, device in list(coordinator.devices.items()):
        if device.create_sensor:
            device_new(address)
    create_scanner_entities()


RETIRED_SENSOR_UNIQUE_ID_SUFFIXES = (
    "_floor",
    "_scanner",
    "_rssi",
    "_range",
    "_range_raw",
    "_area_last_seen",
    "_area_switch_reason",
)


def _level_from_thresholds(value: float | None, thresholds: tuple[tuple[float, str], ...]) -> str | None:
    """Return a coarse diagnostic level from ordered upper-bound thresholds."""
    if value is None:
        return None
    numeric_value = float(value)
    for upper_bound, level in thresholds:
        if numeric_value <= upper_bound:
            return level
    return thresholds[-1][1] if thresholds else None


def _diagnostic_guidance_attributes(
    *,
    meaning: str,
    better: str,
    expected_range: str,
    bands: str,
    level_value: float | None = None,
    level_thresholds: tuple[tuple[float, str], ...] = (),
) -> dict[str, Any]:
    """Build stable explanatory attributes for numeric diagnostic sensors."""
    attrs: dict[str, Any] = {
        "meaning": meaning,
        "better": better,
        "expected_range": expected_range,
        "bands": bands,
    }
    level = _level_from_thresholds(level_value, level_thresholds)
    if level is not None:
        attrs["level"] = level
    return attrs


def _remove_retired_sensor_entities(hass: HomeAssistant, entry_id: str) -> None:
    """Remove entity-registry entries for retired legacy sensor entities."""
    entity_registry = er.async_get(hass)
    for entity_entry in er.async_entries_for_config_entry(entity_registry, entry_id):
        if entity_entry.domain != "sensor":
            continue
        if entity_entry.unique_id.endswith(RETIRED_SENSOR_UNIQUE_ID_SUFFIXES):
            entity_registry.async_remove(entity_entry.entity_id)


# Main Sensor Class with overrides that go everywhere.
class BermudaSensor(BermudaEntity, SensorEntity):
    """bermuda Sensor class."""

    @property
    def entity_registry_enabled_default(self) -> bool:
        """Declare if entity should be automatically enabled on adding."""
        return False

    @property
    def has_entity_name(self) -> bool:
        """
        Indicate that our name() method only returns the entity's name,
        so that HA should prepend the device name for the user.
        """
        return True

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
        if self.name in ["Area", "Trilat Floor"]:
            attribs["area_id"] = self._device.area_id
            attribs["area_name"] = self._device.area_name
            attribs["floor_id"] = self._device.floor_id
            attribs["floor_name"] = self._device.floor_name
            attribs["floor_level"] = self._device.floor_level
        attribs["current_mac"] = current_mac

        return attribs


# Class for Default sesnors for devices.
class BermudaDefaultSensor(BermudaSensor):
    """bermuda Sensor class which are enabled by default."""

    @property
    def entity_registry_enabled_default(self) -> bool:
        """Declare if entity should be automatically enabled on adding."""
        return True


# Class for Distance sensors.
class BermudaDistanceSensor(BermudaSensor):
    """bermuda Sensor class which set common distance properties."""

    @property
    def device_class(self):
        return SensorDeviceClass.DISTANCE

    @property
    def native_unit_of_measurement(self):
        """Results are in metres."""
        return UnitOfLength.METERS

    @property
    def state_class(self):
        """Measurement should result in graphed results."""
        return SensorStateClass.MEASUREMENT


# Area / Floor
class BermudaSensorArea(BermudaDefaultSensor):
    """Sensor for name of current area."""

    @property
    def icon(self):
        """Provide a custom icon for particular entities."""
        return self._device.area_icon

    @property
    def name(self):
        """Return the name of the sensor."""
        return "Area"

    @property
    def native_value(self):
        """Return the state of the sensor."""
        return self._device.area_name

    @property
    def state(self) -> str:
        """Return the state of the device."""
        return self._device.area_name

    @property
    def unique_id(self):
        """
        "Uniquely identify this sensor so that it gets stored in the entity_registry,
        and can be maintained / renamed etc by the user.
        """
        return f"{self._device.unique_id}_area"


class BermudaSensorTrilatFloor(BermudaDefaultSensor):
    """Diagnostic sensor for chosen trilat floor."""

    @property
    def name(self):
        return "Trilat Floor"

    @property
    def native_value(self):
        return getattr(self._device, "trilat_floor_name", None)

    @property
    def state(self):
        return getattr(self._device, "trilat_floor_name", None)

    @property
    def unique_id(self):
        return f"{self._device.unique_id}_trilat_floor"

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        # The floor_evidence list and trilat_floor_diagnostics dict were removed:
        # both are rebuilt from scored floats on every coordinator tick, which
        # generated a fresh state_attributes row every time even when the winning
        # floor didn't change. Detailed evidence is still available via diagnostics.
        floor_id = getattr(self._device, "trilat_floor_id", None)
        if floor_id is None:
            return None
        return {"floor_id": floor_id}


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
        return False

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        # Deliberately avoid unpacking the full status_entry dict here: it contains
        # timestamps/rssi/distance that change on every scanner cycle and would
        # create a new state_attributes row for every state write. Stable metadata only.
        return {
            "scanner_name": self._scanner.name,
            "scanner_address": self._scanner.address,
        }


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
        return False

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        # Only stable identifying metadata. The full status_entry contains
        # per-cycle telemetry (timestamps, rssi, distance) that would churn
        # state_attributes rows on every coordinator tick.
        return {
            "tracked_device_name": self._tracked_device.name,
            "tracked_device_address": self._tracked_device.address,
            "scanner_name": self._device.name,
            "scanner_address": self._device.address,
        }


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


class BermudaSensorTrilatX(BermudaDistanceSensor):
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
        # 1cm precision + deadband: write immediately if moved >=20cm (responsive to real
        # movement), suppress writes when BLE noise keeps the value within the deadband.
        # Time-based fallback (bermuda_update_interval) still fires so HA stays current.
        return self._cached_ratelimit(round(x_val, 2), fast_falling=False, fast_rising=False, deadband=0.20)


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
        return self._cached_ratelimit(round(y_val, 2), fast_falling=False, fast_rising=False, deadband=0.20)


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
        return self._cached_ratelimit(round(z_val, 2), fast_falling=False, fast_rising=False)


class BermudaSensorPositionUncertaintyXBand(BermudaSensorTrilatX):
    """Diagnostic sensor for empirical X uncertainty band width."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def unique_id(self):
        return f"{self._device.unique_id}_position_uncertainty_x_band"

    @property
    def name(self):
        return "Position Uncertainty X Band"

    @property
    def native_value(self):
        band = getattr(self._device, "position_uncertainty_x_band_m", None)
        if band is None:
            return None
        return self._cached_ratelimit(round(float(band), 2), fast_falling=False, fast_rising=False)

    @property
    def entity_registry_enabled_default(self) -> bool:
        return False

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        # `source` is a discrete enum-like string and is stable between updates.
        # Raw/correction float values were dropped — they changed on every tick
        # and are available via diagnostics download when needed.
        source = getattr(self._device, "position_uncertainty_source", None)
        if source is None:
            return None
        return {"source": source}


class BermudaSensorPositionUncertaintyYBand(BermudaSensorPositionUncertaintyXBand):
    """Diagnostic sensor for empirical Y uncertainty band width."""

    @property
    def unique_id(self):
        return f"{self._device.unique_id}_position_uncertainty_y_band"

    @property
    def name(self):
        return "Position Uncertainty Y Band"

    @property
    def native_value(self):
        band = getattr(self._device, "position_uncertainty_y_band_m", None)
        if band is None:
            return None
        return self._cached_ratelimit(round(float(band), 2), fast_falling=False, fast_rising=False)


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
        # The floor_evidence list and trilat_floor_diagnostics dict were removed:
        # both are rebuilt from scored floats on every coordinator tick, which
        # generated a fresh state_attributes row every time even when the winning
        # floor didn't change. Detailed evidence is still available via diagnostics.
        floor_id = getattr(self._device, "trilat_floor_id", None)
        if floor_id is None:
            return None
        return {"floor_id": floor_id}


class BermudaSensorTrilatAnchorCount(BermudaSensor):
    """Diagnostic sensor for active trilat anchor count."""

    @property
    def unique_id(self):
        return f"{self._device.unique_id}_trilat_anchor_count"

    @property
    def name(self):
        return "Anchor - Used Count"

    @property
    def native_value(self):
        return getattr(self._device, "trilat_anchor_count", 0)

    @property
    def state_class(self):
        return SensorStateClass.MEASUREMENT

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        # Per-anchor diagnostic strings (previously exposed as numbered keys
        # "1", "2", ...) were removed: they include live residual/distance text
        # that changed every tick and caused state_attributes churn proportional
        # to the anchor count. The integer summary below is stable.
        return {
            "cross_floor_candidate_count": getattr(self._device, "trilat_cross_floor_anchor_count", 0),
        }


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
        # fast_falling=True: report confidence drops immediately (loss of signal is actionable).
        return self._cached_ratelimit(round(confidence, 1), fast_falling=True, fast_rising=False)

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
        return self._cached_ratelimit(round(confidence, 1), fast_falling=True, fast_rising=False)

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
        return "Geometry - Quality"

    @property
    def native_value(self):
        return self._cached_ratelimit(
            round(getattr(self._device, "trilat_geometry_quality", 0.0), 1), fast_falling=False, fast_rising=False
        )

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        level_value = self.bermuda_last_state if self.bermuda_last_stamp > 0 else self.native_value
        return _diagnostic_guidance_attributes(
            meaning="Combined 0-10 quality score for active anchor geometry.",
            better="higher",
            expected_range="0..10",
            bands="0-2 weak; 2-4 usable; 4-7 good; 7-10 excellent",
            level_value=level_value,
            level_thresholds=((2.0, "weak"), (4.0, "usable"), (7.0, "good"), (10.0, "excellent")),
        )


class BermudaSensorResidualConsistency(BermudaSensorPositionConfidence):
    """Diagnostic sensor for per-anchor residual consistency."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def unique_id(self):
        return f"{self._device.unique_id}_residual_consistency"

    @property
    def name(self):
        return "Residual - Consistency"

    @property
    def entity_registry_enabled_default(self) -> bool:
        return False

    @property
    def native_value(self):
        return self._cached_ratelimit(
            round(getattr(self._device, "trilat_residual_consistency", 0.0), 1), fast_falling=False, fast_rising=False
        )

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        level_value = self.bermuda_last_state if self.bermuda_last_stamp > 0 else self.native_value
        return _diagnostic_guidance_attributes(
            meaning="How well active anchor ranges agree with the solved position.",
            better="higher",
            expected_range="0..10",
            bands="0-2 poor; 2-5 weak; 5-8 good; 8-10 excellent",
            level_value=level_value,
            level_thresholds=((2.0, "poor"), (5.0, "weak"), (8.0, "good"), (10.0, "excellent")),
        )


class BermudaSensorRateLimitedDiagnostic(BermudaSensor):
    """Base class for compact disabled-by-default diagnostic sensors."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _diag_suffix = ""
    _diag_name = ""

    @property
    def unique_id(self):
        return f"{self._device.unique_id}_{self._diag_suffix}"

    @property
    def name(self):
        return self._diag_name

    @property
    def entity_registry_enabled_default(self) -> bool:
        return False


class BermudaSensorStringDiagnostic(BermudaSensorRateLimitedDiagnostic):
    """Rate-limited diagnostic sensor for enum/string values."""

    _device_attr = ""
    _none_state = "none"

    @property
    def native_value(self):
        value = getattr(self._device, self._device_attr, None)
        return self._cached_ratelimit(value or self._none_state, fast_falling=False, fast_rising=False)


class BermudaSensorNumericDiagnostic(BermudaSensorRateLimitedDiagnostic):
    """Rate-limited diagnostic sensor for numeric values."""

    _device_attr = ""
    _round_digits = 3
    _meaning = ""
    _better = ""
    _expected_range = ""
    _bands = ""
    _level_thresholds: tuple[tuple[float, str], ...] = ()

    @property
    def native_value(self):
        value = getattr(self._device, self._device_attr, None)
        if value is None:
            return None
        return self._cached_ratelimit(round(float(value), self._round_digits), fast_falling=False, fast_rising=False)

    @property
    def state_class(self):
        return SensorStateClass.MEASUREMENT

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        if not (self._meaning and self._better and self._expected_range and self._bands):
            return None
        level_value = self.bermuda_last_state if self.bermuda_last_stamp > 0 else self.native_value
        return _diagnostic_guidance_attributes(
            meaning=self._meaning,
            better=self._better,
            expected_range=self._expected_range,
            bands=self._bands,
            level_value=level_value,
            level_thresholds=self._level_thresholds,
        )


class BermudaSensorIntegerDiagnostic(BermudaSensorNumericDiagnostic):
    """Rate-limited diagnostic sensor for integer values."""

    @property
    def native_value(self):
        value = getattr(self._device, self._device_attr, None)
        if value is None:
            return None
        return self._cached_ratelimit(int(value), fast_falling=False, fast_rising=False)


class BermudaSensorAnchorStatusCount(BermudaSensorIntegerDiagnostic):
    """Count per-anchor diagnostic statuses without exposing all volatile details."""

    _anchor_status = ""

    @property
    def native_value(self):
        statuses = getattr(self._device, "trilat_anchor_statuses", {})
        count = sum(1 for entry in statuses.values() if entry.get("status") == self._anchor_status)
        return self._cached_ratelimit(count, fast_falling=False, fast_rising=False)


class BermudaSensorRoomDecisionReason(BermudaSensorStringDiagnostic):
    """Diagnostic sensor for the latest room-classification reason."""

    _diag_suffix = "room_decision_reason"
    _diag_name = "Room - Decision Reason"
    _device_attr = "room_decision_reason"


class BermudaSensorRoomCandidate(BermudaSensorStringDiagnostic):
    """Diagnostic sensor for the current best room candidate."""

    _diag_suffix = "room_candidate"
    _diag_name = "Room - Candidate"
    _device_attr = "room_candidate_name"


class BermudaSensorRoomChallenger(BermudaSensorStringDiagnostic):
    """Diagnostic sensor for the room challenger accumulating switch evidence."""

    _diag_suffix = "room_challenger"
    _diag_name = "Room - Challenger"
    _device_attr = "room_challenger_name"


class BermudaSensorRoomChallengerEvidence(BermudaSensorNumericDiagnostic):
    """Diagnostic sensor for accumulated same-floor room-switch evidence."""

    _diag_suffix = "room_challenger_evidence"
    _diag_name = "Room - Challenger Evidence"
    _device_attr = "room_challenger_evidence"
    _round_digits = 2
    _meaning = "Accumulated evidence toward switching from the stable room to the challenger."
    _better = "contextual: higher means closer to switching room"
    _expected_range = "0..0.45 before switch"
    _bands = "0 none; 0-0.15 weak; 0.15-0.30 building; 0.30-0.45 near switch; >=0.45 switch-ready"
    _level_thresholds = (
        (0.0, "none"),
        (0.15, "weak"),
        (0.30, "building"),
        (0.45, "near_switch"),
        (999999.0, "switch_ready"),
    )


class BermudaSensorRoomScoreMargin(BermudaSensorNumericDiagnostic):
    """Diagnostic sensor for blended room best-vs-second score margin."""

    _diag_suffix = "room_score_margin"
    _diag_name = "Room - Score Margin"
    _device_attr = "room_score_margin"
    _meaning = "Difference between the best and second-best blended room scores."
    _better = "higher"
    _expected_range = "0..1"
    _bands = "0-0.05 ambiguous; 0.05-0.15 weak; 0.15-0.35 moderate; >0.35 strong"
    _level_thresholds = ((0.05, "ambiguous"), (0.15, "weak"), (0.35, "moderate"), (999999.0, "strong"))


class BermudaSensorGeometryRoomScore(BermudaSensorNumericDiagnostic):
    """Diagnostic sensor for geometry-only support of the selected room."""

    _diag_suffix = "geometry_room_score"
    _diag_name = "Geometry - Room Score"
    _device_attr = "room_geometry_score"
    _meaning = "How strongly solved x/y/z position alone supports the selected room."
    _better = "higher"
    _expected_range = "0..1"
    _bands = "0-0.15 weak; 0.15-0.35 low; 0.35-0.60 moderate; 0.60-0.80 strong; >0.80 excellent"
    _level_thresholds = ((0.15, "weak"), (0.35, "low"), (0.60, "moderate"), (0.80, "strong"), (999999.0, "excellent"))


class BermudaSensorFingerprintRoomScore(BermudaSensorNumericDiagnostic):
    """Diagnostic sensor for fingerprint support of the selected room."""

    _diag_suffix = "fingerprint_room_score"
    _diag_name = "Fingerprint - Room Score"
    _device_attr = "room_fingerprint_score"
    _meaning = "How strongly RSSI fingerprinting supports the selected room."
    _better = "higher"
    _expected_range = "0..1"
    _bands = "0-0.15 weak; 0.15-0.35 low; 0.35-0.60 moderate; 0.60-0.80 strong; >0.80 excellent"
    _level_thresholds = ((0.15, "weak"), (0.35, "low"), (0.60, "moderate"), (0.80, "strong"), (999999.0, "excellent"))


class BermudaSensorFingerprintBestRoom(BermudaSensorStringDiagnostic):
    """Diagnostic sensor for the top RSSI-fingerprint room."""

    _diag_suffix = "fingerprint_best_room"
    _diag_name = "Fingerprint - Best Room"
    _device_attr = "room_fingerprint_best_name"


class BermudaSensorFingerprintMargin(BermudaSensorNumericDiagnostic):
    """Diagnostic sensor for RSSI-fingerprint best-vs-second score margin."""

    _diag_suffix = "fingerprint_margin"
    _diag_name = "Fingerprint - Margin"
    _device_attr = "room_fingerprint_margin"
    _meaning = "Difference between the best and second-best fingerprint-only room scores."
    _better = "higher"
    _expected_range = "0..1"
    _bands = "0-0.03 ambiguous; 0.03-0.05 weak; 0.05-0.15 moderate; 0.15-0.30 strong; >0.30 decisive"
    _level_thresholds = (
        (0.03, "ambiguous"),
        (0.05, "weak"),
        (0.15, "moderate"),
        (0.30, "strong"),
        (999999.0, "decisive"),
    )


class BermudaSensorFingerprintCoverage(BermudaSensorNumericDiagnostic):
    """Diagnostic sensor for current live-scanner coverage of candidate room samples."""

    _diag_suffix = "fingerprint_coverage"
    _diag_name = "Fingerprint - Coverage"
    _device_attr = "room_fingerprint_coverage"
    _meaning = "Fraction of candidate-room calibration samples that have enough live scanner overlap to score."
    _better = "higher"
    _expected_range = "0..1"
    _bands = "0-0.25 poor; 0.25-0.50 weak; 0.50-0.75 usable; 0.75-0.90 good; >0.90 excellent"
    _level_thresholds = ((0.25, "poor"), (0.50, "weak"), (0.75, "usable"), (0.90, "good"), (999999.0, "excellent"))


class BermudaSensorFingerprintBlendWeight(BermudaSensorNumericDiagnostic):
    """Diagnostic sensor for how strongly room allocation weights fingerprinting."""

    _diag_suffix = "fingerprint_blend_weight"
    _diag_name = "Fingerprint - Blend Weight"
    _device_attr = "room_fingerprint_blend_weight"
    _meaning = "Weight given to fingerprinting in the final hybrid room score; the remainder is geometry."
    _better = "contextual: higher trusts fingerprinting more, lower trusts geometry more"
    _expected_range = "0..1"
    _bands = "around 0.65 normal; 0.65-0.75 elevated; 0.75-0.85 fingerprint-heavy"
    _level_thresholds = ((0.65, "normal"), (0.75, "elevated"), (0.85, "fingerprint_heavy"), (999999.0, "unexpected"))


class BermudaSensorRoomSampleCount(BermudaSensorIntegerDiagnostic):
    """Diagnostic sensor for sample count in the current candidate room."""

    _diag_suffix = "room_sample_count"
    _diag_name = "Room - Sample Count"
    _device_attr = "room_sample_count"
    _meaning = "Number of calibration samples for the current candidate room."
    _better = "higher until the room is well covered"
    _expected_range = "0+"
    _bands = "0 none; 1 sparse; 2 limited; 3-4 usable; >=5 good"
    _level_thresholds = ((0.0, "none"), (1.0, "sparse"), (2.0, "limited"), (4.0, "usable"), (999999.0, "good"))


class BermudaSensorRoomHoldReason(BermudaSensorStringDiagnostic):
    """Diagnostic sensor for the latest reason a room switch was held."""

    _diag_suffix = "room_hold_reason"
    _diag_name = "Room - Hold Reason"
    _device_attr = "room_hold_reason"


class BermudaSensorGeometryGdop(BermudaSensorNumericDiagnostic):
    """Diagnostic sensor for trilateration geometric dilution of precision."""

    _diag_suffix = "geometry_gdop"
    _diag_name = "Geometry - GDOP"
    _device_attr = "trilat_geometry_gdop"
    _round_digits = 2
    _meaning = "How much active anchor layout amplifies range error in the position solve."
    _better = "lower"
    _expected_range = "1+"
    _bands = "1-1.5 excellent; 1.5-2 good; 2-3 usable; 3-5 weak; >5 poor"
    _level_thresholds = ((1.5, "excellent"), (2.0, "good"), (3.0, "usable"), (5.0, "weak"), (999999.0, "poor"))


class BermudaSensorGeometryConditionNumber(BermudaSensorNumericDiagnostic):
    """Diagnostic sensor for trilateration geometry condition number."""

    _diag_suffix = "geometry_condition_number"
    _diag_name = "Geometry - Condition Number"
    _device_attr = "trilat_geometry_condition"
    _round_digits = 2
    _meaning = "Numerical conditioning of the active anchor geometry; high values mean stretched or one-sided geometry."
    _better = "lower"
    _expected_range = "1+"
    _bands = "1-3 excellent; 3-10 good; 10-30 usable; 30-100 weak; >100 poor"
    _level_thresholds = ((3.0, "excellent"), (10.0, "good"), (30.0, "usable"), (100.0, "weak"), (999999.0, "poor"))


class BermudaSensorNormalizedResidualRms(BermudaSensorNumericDiagnostic):
    """Diagnostic sensor for normalized residual RMS."""

    _diag_suffix = "normalized_residual_rms"
    _diag_name = "Residual - Normalized RMS"
    _device_attr = "trilat_normalized_residual_rms"
    _round_digits = 3
    _meaning = "Range residual size relative to each anchor's expected uncertainty."
    _better = "lower"
    _expected_range = "0+"
    _bands = "0-0.5 excellent; 0.5-1 good; 1-2 usable; 2-3 weak; >3 poor"
    _level_thresholds = ((0.5, "excellent"), (1.0, "good"), (2.0, "usable"), (3.0, "weak"), (999999.0, "poor"))


class BermudaSensorResidualRms(BermudaSensorNumericDiagnostic):
    """Diagnostic sensor for residual RMS in metres."""

    _diag_suffix = "residual_rms"
    _diag_name = "Residual - RMS"
    _device_attr = "trilat_residual_m"
    _round_digits = 2
    _meaning = "Metre-level RMS mismatch between active anchor ranges and the solved position."
    _better = "lower"
    _expected_range = "0+ m"
    _bands = "0-1 m excellent; 1-2 m good; 2-3 m usable; 3-5 m weak; >5 m poor"
    _level_thresholds = ((1.0, "excellent"), (2.0, "good"), (3.0, "usable"), (5.0, "weak"), (999999.0, "poor"))

    @property
    def device_class(self):
        return SensorDeviceClass.DISTANCE

    @property
    def native_unit_of_measurement(self):
        return UnitOfLength.METERS


class BermudaSensorBroadcastInterval(BermudaSensorNumericDiagnostic):
    """Diagnostic sensor estimating the device's advert broadcast interval."""

    _diag_suffix = "broadcast_interval"
    _diag_name = "Advert - Broadcast Interval"
    _device_attr = "broadcast_interval_estimate_s"
    _round_digits = 2
    _meaning = (
        "Estimated seconds between this device's advertisements, taken from the "
        "best-receiving scanner's median inter-advert gap. Missed packets can only "
        "stretch this estimate, never shrink it."
    )
    _better = "lower"
    _expected_range = "0.1..30 s"
    _bands = (
        "0-2 s excellent; 2-10 s good; 10-15 s usable; 15-30 s risky; "
        ">30 s device will drop out between adverts (30 s away-timeout)"
    )
    _level_thresholds = (
        (2.0, "excellent"),
        (10.0, "good"),
        (15.0, "usable"),
        (30.0, "risky"),
        (999999.0, "beyond_away_timeout"),
    )

    @property
    def device_class(self):
        return SensorDeviceClass.DURATION

    @property
    def native_unit_of_measurement(self):
        return UnitOfTime.SECONDS


class BermudaSensorAdvertIntervalDrift(BermudaSensorNumericDiagnostic):
    """Diagnostic sensor for advert-interval drift against the device's healthy baseline."""

    _diag_suffix = "advert_interval_drift"
    _diag_name = "Advert - Interval Drift"
    _device_attr = "broadcast_interval_drift"
    _round_digits = 2
    _meaning = (
        "Current advert interval divided by this device's fastest sustained cadence. "
        "For a beacon that has not moved, a sustained rise often means its battery is weakening."
    )
    _better = "lower"
    _expected_range = "1+"
    _bands = "1-1.3 nominal; 1.3-2 elevated; 2-4 degraded - check battery; >4 failing or out of range"
    _level_thresholds = (
        (1.3, "nominal"),
        (2.0, "elevated"),
        (4.0, "degraded_check_battery"),
        (999999.0, "failing"),
    )

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        attrs = dict(super().extra_state_attributes or {})
        baseline = getattr(self._device, "broadcast_interval_baseline_s", None)
        if baseline is not None:
            attrs["baseline_interval_s"] = round(float(baseline), 2)
        return attrs


class BermudaSensorValidAnchorCount(BermudaSensorAnchorStatusCount):
    """Diagnostic sensor for same-floor anchors currently used by trilat."""

    _diag_suffix = "valid_anchor_count"
    _diag_name = "Anchor - Valid Count"
    _anchor_status = "valid"
    _meaning = "Number of same-floor anchors currently usable for the position solve."
    _better = "higher"
    _expected_range = "0+"
    _bands = "0-2 insufficient; 3 minimum; 4-5 good; >=6 strong"
    _level_thresholds = ((2.0, "insufficient"), (3.0, "minimum"), (5.0, "good"), (999999.0, "strong"))


class BermudaSensorStaleAnchorCount(BermudaSensorAnchorStatusCount):
    """Diagnostic sensor for anchors rejected because their advert is stale."""

    _diag_suffix = "stale_anchor_count"
    _diag_name = "Anchor - Stale Count"
    _anchor_status = "rejected_stale"
    _meaning = "Number of anchors rejected because the latest advert aged out."
    _better = "lower"
    _expected_range = "0+"
    _bands = "0 none; 1-2 minor; 3-5 high; >5 severe"
    _level_thresholds = ((0.0, "none"), (2.0, "minor"), (5.0, "high"), (999999.0, "severe"))


class BermudaSensorNoAdvertAnchorCount(BermudaSensorAnchorStatusCount):
    """Diagnostic sensor for configured anchors with no advert from this device."""

    _diag_suffix = "no_advert_anchor_count"
    _diag_name = "Anchor - No Advert Count"
    _anchor_status = "no_advert"
    _meaning = "Number of configured anchors that have no recent advert for this tracked device."
    _better = "lower"
    _expected_range = "0+"
    _bands = "0 none; 1-2 minor; 3-5 high; >5 severe"
    _level_thresholds = ((0.0, "none"), (2.0, "minor"), (5.0, "high"), (999999.0, "severe"))


class BermudaSensorValidOtherFloorAnchorCount(BermudaSensorAnchorStatusCount):
    """Diagnostic sensor for live anchors seen on floors other than the selected floor."""

    _diag_suffix = "valid_other_floor_anchor_count"
    _diag_name = "Anchor - Valid Other-Floor Count"
    _anchor_status = "valid_other_floor"
    _meaning = "Number of live anchors from floors other than the currently selected floor."
    _better = "lower for a same-floor solve; contextual during real floor transitions"
    _expected_range = "0+"
    _bands = "0 none; 1-2 some; >2 many"
    _level_thresholds = ((0.0, "none"), (2.0, "some"), (999999.0, "many"))


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
        # fast_rising=True: report movement onset immediately.
        return self._cached_ratelimit(round(speed, 2), fast_falling=False, fast_rising=True)

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
        return self._cached_ratelimit(round(speed, 2), fast_falling=False, fast_rising=True)


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
        return False

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        # Deliberately compact: the full rolling diagnostics dict was the largest
        # per-scanner contributor to state_attributes growth, so only slow-moving
        # values are exposed (counters move only while problems occur, and the
        # accepted-advert age is bucketed so "proxy silent" vs "packets rejected"
        # is visible without per-second churn). Full detail remains available in
        # the HA diagnostics download.
        diagnostics = self._device.timestamp_sync_diagnostics()
        age_s = diagnostics.get("last_accepted_advert_age_s")
        if age_s is None:
            accepted_bucket = "never"
        elif age_s < 60.0:
            accepted_bucket = "under_1m"
        elif age_s < 300.0:
            accepted_bucket = "1m_to_5m"
        elif age_s < 1800.0:
            accepted_bucket = "5m_to_30m"
        else:
            accepted_bucket = "over_30m"
        return {
            "last_accepted_advert": accepted_bucket,
            "recent_regressions": diagnostics["recent_scanner_regressions"],
            "recent_stale_drops": diagnostics["recent_stale_advert_drops"],
            "recent_rebases": diagnostics["recent_stamp_rebases"],
            "recent_future_clamps": diagnostics["recent_future_stamp_clamps"],
            "recent_max_backward_s": diagnostics["recent_max_backward_s"],
            "replay_suspects_total": getattr(self._device, "scanner_replay_suspect_count", 0),
        }


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
