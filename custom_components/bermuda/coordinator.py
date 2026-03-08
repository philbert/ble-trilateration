"""DataUpdateCoordinator for Bermuda bluetooth data."""

from __future__ import annotations

import logging
import math
import re
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, cast

import aiofiles
import voluptuous as vol
import yaml
from bluetooth_data_tools import monotonic_time_coarse
from habluetooth import BaseHaScanner
from homeassistant.components import bluetooth
from homeassistant.components import persistent_notification
from homeassistant.components.bluetooth.api import _get_manager
from homeassistant.const import MAJOR_VERSION as HA_VERSION_MAJ
from homeassistant.const import MINOR_VERSION as HA_VERSION_MIN
from homeassistant.const import Platform
from homeassistant.core import (
    Event,
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import (
    area_registry as ar,
)
from homeassistant.helpers import (
    config_validation as cv,
)
from homeassistant.helpers import (
    device_registry as dr,
)
from homeassistant.helpers import (
    entity_registry as er,
)
from homeassistant.helpers import (
    floor_registry as fr,
)
from homeassistant.helpers import (
    issue_registry as ir,
)
from homeassistant.helpers.device_registry import (
    EVENT_DEVICE_REGISTRY_UPDATED,
    EventDeviceRegistryUpdatedData,
)
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util.dt import get_age, now

from .bermuda_device import BermudaDevice
from .calibration import BermudaCalibrationManager
from .calibration_store import BermudaCalibrationStore
from .bermuda_irk import BermudaIrkManager
from .ranging_model import BermudaRangingModel
from .room_classifier import BermudaRoomClassifier
from .const import (
    _LOGGER,
    _LOGGER_SPAM_LESS,
    _LOGGER_TARGET_SPAM_LESS,
    ADDR_TYPE_PRIVATE_BLE_DEVICE,
    AREA_NAME_UNKNOWN,
    AREA_MAX_AD_AGE,
    BDADDR_TYPE_NOT_MAC48,
    BDADDR_TYPE_RANDOM_RESOLVABLE,
    CONF_ATTENUATION,
    CONF_CONNECTOR_GROUPS,
    CONF_DEVICES,
    CONF_DEVTRACK_TIMEOUT,
    CONF_MAX_RADIUS,
    CONF_MAX_VELOCITY,
    CONF_REF_POWER,
    CONF_RSSI_OFFSETS,
    CONF_TRILAT_CROSS_FLOOR_PENALTY_DB,
    CONF_TRILAT_ENABLED,
    CONF_SMOOTHING_SAMPLES,
    CONF_UPDATE_INTERVAL,
    DEFAULT_ATTENUATION,
    DEFAULT_DEVTRACK_TIMEOUT,
    DEFAULT_MAX_RADIUS,
    DEFAULT_MAX_VELOCITY,
    DEFAULT_SAMPLE_RADIUS_M,
    DEFAULT_REF_POWER,
    DEFAULT_SMOOTHING_SAMPLES,
    DEFAULT_TRILAT_CROSS_FLOOR_PENALTY_DB,
    DEFAULT_TRILAT_ENABLED,
    DEFAULT_UPDATE_INTERVAL,
    DISTANCE_TIMEOUT,
    DOMAIN,
    DOMAIN_PRIVATE_BLE_DEVICE,
    MOBILITY_MOVING,
    MOBILITY_STATIONARY,
    METADEVICE_IBEACON_DEVICE,
    METADEVICE_TYPE_IBEACON_SOURCE,
    METADEVICE_TYPE_PRIVATE_BLE_SOURCE,
    PRUNE_MAX_COUNT,
    PRUNE_TIME_DEFAULT,
    PRUNE_TIME_INTERVAL,
    PRUNE_TIME_KNOWN_IRK,
    PRUNE_TIME_REDACTIONS,
    PRUNE_TIME_UNKNOWN_IRK,
    REPAIR_SCANNER_WITHOUT_AREA,
    REPAIR_TRILAT_WITHOUT_ANCHORS,
    SAVEOUT_COOLDOWN,
    SIGNAL_DEVICE_NEW,
    SIGNAL_SCANNERS_CHANGED,
    UPDATE_INTERVAL,
    debug_device_match,
)
from .trilateration import (
    AnchorMeasurement,
    anchor_centroid,
    anchor_centroid_3d,
    solve_2d_soft_l1,
    solve_3d_soft_l1,
)
from .util import mac_explode_formats, mac_norm

if TYPE_CHECKING:
    from habluetooth import BluetoothServiceInfoBleak
    from homeassistant.components.bluetooth import (
        BluetoothChange,
    )
    from homeassistant.components.bluetooth.manager import HomeAssistantBluetoothManager

    from . import BermudaConfigEntry
    from .bermuda_advert import BermudaAdvert

Cancellable = Callable[[], None]


class _SuppressTimingLogger:
    """Wrapper that hides DEBUG-level availability checks from HA's coordinator."""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def __getattr__(self, name: str):
        """Delegate everything but isEnabledFor/debug to the wrapped logger."""
        return getattr(self._logger, name)

    def isEnabledFor(self, level: int) -> bool:
        """Report DEBUG as disabled so DataUpdateCoordinator skips timing logs."""
        if level == logging.DEBUG:
            return False
        return self._logger.isEnabledFor(level)

    def debug(self, msg, *args, **kwargs):
        """Still emit debug logs for all other code paths."""
        self._logger.debug(msg, *args, **kwargs)


# Using "if" instead of "min/max" triggers PLR1730, but when
# split over two lines, ruff removes it, then complains again.
# so we're just disabling it for the whole file.
# https://github.com/astral-sh/ruff/issues/4244
# ruff: noqa: PLR1730


class BermudaDataUpdateCoordinator(DataUpdateCoordinator):
    """
    Class to manage fetching data from the Bluetooth component.

    Since we are not actually using an external API and only computing local
    data already gathered by the bluetooth integration, the update process is
    very cheap, and the processing process (currently) rather cheap.

    TODO / IDEAS:
    - when we get to establishing a fix, we can apply a path-loss factor to
      a calculated vector based on previously measured losses on that path.
      We could perhaps also fine-tune that with real-time measurements from
      fixed beacons to compensate for environmental factors.
    - An "obstruction map" or "radio map" could provide field strength estimates
      at given locations, and/or hint at attenuation by counting "wall crossings"
      for a given vector/path.

    """

    _BROKEN_PROXY_SYNC_NOTIFICATION_ID = "bermuda_broken_proxy_timestamp_sync"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: BermudaConfigEntry,
    ) -> None:
        """Initialize."""
        self.platforms = []
        self.config_entry = entry

        self.sensor_interval = entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)

        # set some version flags
        self.hass_version_min_2025_2 = HA_VERSION_MAJ > 2025 or (HA_VERSION_MAJ == 2025 and HA_VERSION_MIN >= 2)
        # when habasescanner.discovered_device_timestamps became a public method.
        self.hass_version_min_2025_4 = HA_VERSION_MAJ > 2025 or (HA_VERSION_MAJ == 2025 and HA_VERSION_MIN >= 4)

        # ##### Redaction Data ###
        #
        # match/replacement pairs for redacting addresses
        self.redactions: dict[str, str] = {}
        # Any remaining MAC addresses will be replaced with this. We define it here
        # so we can compile it once. MAC addresses may have [:_-] separators.
        self._redact_generic_re = re.compile(
            r"(?P<start>[0-9A-Fa-f]{2})[:_-]([0-9A-Fa-f]{2}[:_-]){4}(?P<end>[0-9A-Fa-f]{2})"
        )
        self._redact_generic_sub = r"\g<start>:xx:xx:xx:xx:\g<end>"

        self.stamp_redactions_expiry: float | None = None

        self.update_in_progress: bool = False  # A lock to guard against huge backlogs / slow processing
        self.stamp_last_update: float = 0  # Last time we ran an update, from monotonic_time_coarse()
        self.stamp_last_update_started: float = 0
        self.stamp_last_prune: float = 0  # When we last pruned device list

        self.member_uuids = {}
        self.company_uuids = {}

        super().__init__(
            hass,
            _SuppressTimingLogger(_LOGGER),
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )

        self._waitingfor_load_manufacturer_ids = True
        entry.async_create_background_task(
            hass, self.async_load_manufacturer_ids(), "Load Bluetooth IDs", eager_start=True
        )

        self._manager: HomeAssistantBluetoothManager = _get_manager(hass)  # instance of the bluetooth manager
        self._hascanners: set[BaseHaScanner] = set()  # Links to the backend scanners
        self._hascanner_timestamps: dict[str, dict[str, float]] = {}  # scanner_address, device_address, stamp
        self._scanner_list: set[str] = set()
        self._scanners: set[BermudaDevice] = set()  # Set of all in self.devices that is_scanner=True
        self.irk_manager = BermudaIrkManager()

        self.ar = ar.async_get(self.hass)
        self.er = er.async_get(self.hass)
        self.dr = dr.async_get(self.hass)
        self.fr = fr.async_get(self.hass)
        self.have_floors: bool = self.init_floors()

        self._scanners_without_areas: list[str] | None = None  # Tracks any proxies that don't have an area assigned.
        self._area_decision_state: dict[str, BermudaDataUpdateCoordinator.AreaDecisionState] = {}
        self._trilat_decision_state: dict[str, BermudaDataUpdateCoordinator.TrilatDecisionState] = {}
        self._trilat_scanners_without_anchors: list[str] | None = None
        self._broken_timestamp_sync_scanners: list[str] | None = None
        self.calibration_store = BermudaCalibrationStore(hass, entry.entry_id)
        self.calibration = BermudaCalibrationManager(hass, self, self.calibration_store)
        self.ranging_model = BermudaRangingModel(self.calibration)
        self.room_classifier = BermudaRoomClassifier(self.calibration, self.ar)
        self._connector_groups_by_id: dict[str, dict] = {}
        self._connector_area_to_group_id: dict[str, str] = {}
        self._connector_group_floor_ids: dict[str, set[str]] = {}

        # Track the list of Private BLE devices, noting their entity id
        # and current "last address".
        self.pb_state_sources: dict[str, str | None] = {}

        self.metadevices: dict[str, BermudaDevice] = {}

        self._ad_listener_cancel: Cancellable | None = None

        # Tracks the last stamp that we *actually* saved our config entry. Mostly for debugging,
        # we use a request stamp for tracking our add_job request.
        self.last_config_entry_update: float = 0  # Stamp of last *save-out* of config.data

        # We want to delay the first save-out, since it takes a few seconds for things
        # to stabilise. So set the stamp into the future.
        self.last_config_entry_update_request = (
            monotonic_time_coarse() + SAVEOUT_COOLDOWN
        )  # Stamp for save-out requests

        # AJG 2025-04-23 Disabling, see the commented method below for notes.
        # self.config_entry.async_on_unload(self.hass.bus.async_listen(EVENT_STATE_CHANGED, self.handle_state_changes))

        # First time around we freshen the restored scanner info by
        # forcing a scan of the captured info.
        self._scanner_init_pending = True

        self._seed_configured_devices_done = False

        # First time go through the private ble devices to see if there's
        # any there for us to track.
        self._do_private_device_init = True

        # Listen for changes to the device registry and handle them.
        # Primarily for changes to scanners and Private BLE Devices.
        self.config_entry.async_on_unload(
            self.hass.bus.async_listen(EVENT_DEVICE_REGISTRY_UPDATED, self.handle_devreg_changes)
        )

        self.options = {}

        # TODO: This is only here because we haven't set up migration of config
        # entries yet, so some users might not have this defined after an update.
        self.options[CONF_ATTENUATION] = DEFAULT_ATTENUATION
        self.options[CONF_DEVTRACK_TIMEOUT] = DEFAULT_DEVTRACK_TIMEOUT
        self.options[CONF_MAX_RADIUS] = DEFAULT_MAX_RADIUS
        self.options[CONF_MAX_VELOCITY] = DEFAULT_MAX_VELOCITY
        self.options[CONF_REF_POWER] = DEFAULT_REF_POWER
        self.options[CONF_SMOOTHING_SAMPLES] = DEFAULT_SMOOTHING_SAMPLES
        self.options[CONF_UPDATE_INTERVAL] = DEFAULT_UPDATE_INTERVAL
        self.options[CONF_RSSI_OFFSETS] = {}
        self.options[CONF_CONNECTOR_GROUPS] = []
        self.options[CONF_TRILAT_ENABLED] = DEFAULT_TRILAT_ENABLED
        self.options[CONF_TRILAT_CROSS_FLOOR_PENALTY_DB] = DEFAULT_TRILAT_CROSS_FLOOR_PENALTY_DB

        if hasattr(entry, "options"):
            # Firstly, on some calls (specifically during reload after settings changes)
            # we seem to get called with a non-existant config_entry.
            # Anyway... if we DO have one, convert it to a plain dict so we can
            # serialise it properly when it goes into the device and scanner classes.
            for key, val in entry.options.items():
                if key in (
                    CONF_ATTENUATION,
                    CONF_DEVICES,
                    CONF_DEVTRACK_TIMEOUT,
                    CONF_MAX_RADIUS,
                    CONF_MAX_VELOCITY,
                    CONF_REF_POWER,
                    CONF_SMOOTHING_SAMPLES,
                    CONF_RSSI_OFFSETS,
                    CONF_CONNECTOR_GROUPS,
                    CONF_TRILAT_ENABLED,
                    CONF_TRILAT_CROSS_FLOOR_PENALTY_DB,
                ):
                    self.options[key] = val

        self._rebuild_connector_topology()

        self.devices: dict[str, BermudaDevice] = {}
        # self.updaters: dict[str, BermudaPBDUCoordinator] = {}

        # Register the dump_devices service
        hass.services.async_register(
            DOMAIN,
            "dump_devices",
            self.service_dump_devices,
            vol.Schema(
                {
                    vol.Optional("addresses"): cv.string,
                    vol.Optional("configured_devices"): cv.boolean,
                    vol.Optional("redact"): cv.boolean,
                }
            ),
            SupportsResponse.ONLY,
        )
        self.config_entry.async_on_unload(lambda: hass.services.async_remove(DOMAIN, "dump_devices"))

        hass.services.async_register(
            DOMAIN,
            "record_calibration_sample",
            self.service_record_calibration_sample,
            vol.Schema(
                {
                    vol.Required("device_id"): cv.string,
                    vol.Required("room_area_id"): cv.string,
                    vol.Required("x_m"): vol.Coerce(float),
                    vol.Required("y_m"): vol.Coerce(float),
                    vol.Required("z_m"): vol.Coerce(float),
                    vol.Optional("sample_radius_m"): vol.Coerce(float),
                    vol.Optional("room_radius_m"): vol.Coerce(float),
                    vol.Optional("duration_s", default=60): vol.All(vol.Coerce(int), vol.Range(min=1)),
                    vol.Optional("notes", default=""): cv.string,
                }
            ),
            SupportsResponse.OPTIONAL,
        )
        self.config_entry.async_on_unload(lambda: hass.services.async_remove(DOMAIN, "record_calibration_sample"))

        # Register for newly discovered / changed BLE devices
        if self.config_entry is not None:
            self.config_entry.async_on_unload(
                bluetooth.async_register_callback(
                    self.hass,
                    self.async_handle_advert,
                    bluetooth.BluetoothCallbackMatcher(connectable=False),
                    bluetooth.BluetoothScanningMode.ACTIVE,
                )
            )

    @property
    def scanner_list(self):
        return self._scanner_list

    async def async_initialize(self) -> None:
        """Initialize coordinator-owned subsystems after setup."""
        await self.calibration.async_initialize()
        self.calibration.register_change_callback(self.async_handle_calibration_samples_changed)
        await self.async_handle_calibration_samples_changed()

    async def async_shutdown(self) -> None:
        """Tear down coordinator-owned subsystems."""
        await self.calibration.async_shutdown()

    async def async_handle_calibration_samples_changed(self) -> None:
        """Rebuild sample-derived runtime helpers after calibration data changes."""
        await self.ranging_model.async_rebuild()
        await self.room_classifier.async_rebuild()

    @property
    def get_scanners(self) -> set[BermudaDevice]:
        return self._scanners

    def init_floors(self) -> bool:
        """Check if the system has floors configured, and enable sensors."""
        _have_floors: bool = False
        for area in self.ar.async_list_areas():
            if area.floor_id is not None:
                _have_floors = True
                break
        _LOGGER.debug("Have_floors is %s", _have_floors)
        return _have_floors

    def scanner_list_add(self, scanner_device: BermudaDevice):
        self._scanner_list.add(scanner_device.address)
        self._scanners.add(scanner_device)
        async_dispatcher_send(self.hass, SIGNAL_SCANNERS_CHANGED)

    def scanner_list_del(self, scanner_device: BermudaDevice):
        self._scanner_list.remove(scanner_device.address)
        self._scanners.remove(scanner_device)
        async_dispatcher_send(self.hass, SIGNAL_SCANNERS_CHANGED)

    def get_scanner_rssi_offset(self, scanner_address: str) -> float:
        """
        Get RSSI offset for a scanner.

        Priority: scanner device attribute > legacy config > default (0)
        """
        # Check if scanner device has the attribute
        if scanner_address in self.devices:
            device = self.devices[scanner_address]
            if hasattr(device, 'rssi_offset'):
                return device.rssi_offset

        # Fall back to legacy config
        legacy_offsets = self.options.get(CONF_RSSI_OFFSETS, {})
        if scanner_address in legacy_offsets:
            return legacy_offsets[scanner_address]

        # Default
        return 0

    def get_scanner_attenuation(self, scanner_address: str) -> float:
        """
        Get attenuation for a scanner.

        Priority: scanner device attribute > global default
        """
        # Check if scanner device has the attribute
        if scanner_address in self.devices:
            device = self.devices[scanner_address]
            if hasattr(device, 'attenuation'):
                return device.attenuation

        # Fall back to global default
        return self.options.get(CONF_ATTENUATION, DEFAULT_ATTENUATION)

    def get_scanner_max_radius(self, scanner_address: str) -> float:
        """
        Get max radius for a scanner.

        Priority: scanner device attribute > global default
        """
        # Check if scanner device has the attribute
        if scanner_address in self.devices:
            device = self.devices[scanner_address]
            if hasattr(device, 'max_radius'):
                return device.max_radius

        # Fall back to global default
        return self.options.get(CONF_MAX_RADIUS, DEFAULT_MAX_RADIUS)

    def current_anchor_layout_hash(self) -> str:
        """Return the active anchor layout hash."""
        return self.calibration.current_anchor_layout_hash

    def get_registry_id_for_device(self, device: BermudaDevice) -> str | None:
        """Return the HA device registry id for a Bermuda device when available."""
        if ":" in device.address:
            if registry_device := self.dr.async_get_device(connections={(dr.CONNECTION_BLUETOOTH, device.address.upper())}):
                return registry_device.id
        if registry_device := self.dr.async_get_device(connections={(DOMAIN_PRIVATE_BLE_DEVICE, device.address)}):
            return registry_device.id
        if registry_device := self.dr.async_get_device(connections={("ibeacon", device.address)}):
            return registry_device.id
        return None

    def estimate_sampled_range(
        self,
        *,
        scanner_address: str,
        device: BermudaDevice,
        filtered_rssi: float | None,
        live_rssi_dispersion: float | None = None,
    ):
        """Estimate range for one advert using the sample-derived model."""
        return self.ranging_model.estimate_range(
            layout_hash=self.current_anchor_layout_hash(),
            scanner_address=scanner_address,
            device_id=self.get_registry_id_for_device(device),
            filtered_rssi=filtered_rssi,
            live_rssi_dispersion=live_rssi_dispersion,
        )

    def get_scanner_anchor_enabled(self, scanner_address: str) -> bool:
        """Return whether scanner should be used as a trilat anchor."""
        if scanner_address in self.devices:
            return bool(getattr(self.devices[scanner_address], "anchor_enabled", True))
        return False

    def get_scanner_anchor_x(self, scanner_address: str) -> float | None:
        """Return scanner trilat X coordinate in meters."""
        if scanner_address in self.devices:
            return getattr(self.devices[scanner_address], "anchor_x_m", None)
        return None

    def get_scanner_anchor_y(self, scanner_address: str) -> float | None:
        """Return scanner trilat Y coordinate in meters."""
        if scanner_address in self.devices:
            return getattr(self.devices[scanner_address], "anchor_y_m", None)
        return None

    def get_scanner_anchor_z(self, scanner_address: str) -> float | None:
        """Return scanner trilat Z coordinate in meters."""
        if scanner_address in self.devices:
            return getattr(self.devices[scanner_address], "anchor_z_m", None)
        return None

    def trilat_enabled(self) -> bool:
        """Return true if trilateration is enabled."""
        return bool(self.options.get(CONF_TRILAT_ENABLED, DEFAULT_TRILAT_ENABLED))

    def trilat_cross_floor_penalty_db(self) -> float:
        """Return configured cross-floor RSSI penalty for floor evidence."""
        return float(
            self.options.get(
                CONF_TRILAT_CROSS_FLOOR_PENALTY_DB,
                DEFAULT_TRILAT_CROSS_FLOOR_PENALTY_DB,
            )
        )

    def reload_all_advert_configs(self) -> None:
        """
        Reload configuration for all BermudaAdvert instances.

        Called when a scanner Number entity value changes to ensure
        all advert instances pick up the new configuration values.
        """
        for device in self.devices.values():
            if hasattr(device, 'scanners'):
                for scanner_address, advert in device.scanners.items():
                    if hasattr(advert, 'reload_config'):
                        advert.reload_config()

    def get_manufacturer_from_id(self, uuid: int | str) -> tuple[str, bool] | tuple[None, None]:
        """
        An opinionated Bluetooth UUID to Name mapper.

        - uuid must be four hex chars in a string, or an `int`

        Retreives the manufacturer name from the Bluetooth SIG Member UUID listing,
        using a cached copy of https://bitbucket.org/bluetooth-SIG/public/src/main/assigned_numbers/uuids/member_uuids.yaml

        HOWEVER: Bermuda adds some opinionated overrides for the benefit of user clarity:
        - Legal entity names may be overriden with well-known brand names
        - Special-use prefixes may be tagged as such (eg iBeacon etc)
        - Generics can be excluded by setting exclude_generics=True
        """
        if isinstance(uuid, str):
            uuid = int(uuid.replace(":", ""), 16)

        _generic = False
        # Because iBeacon and (soon) GFMD and AppleFindmy etc are common protocols, they
        # don't do a good job of uniquely identifying a manufacturer, so we use them
        # as fallbacks only.
        if uuid == 0x0BA9:
            # allterco robotics, aka...
            _name = "Shelly Devices"
        elif uuid == 0x004C:
            # Apple have *many* UUIDs, but since they don't OEM for others (AFAIK)
            # and only the iBeacon / FindMy adverts seem to be third-partied, match just
            # this one instead of their entire set.
            _name = "Apple Inc."
            _generic = True
        elif uuid == 0x181C:
            _name = "BTHome v1 cleartext"
            _generic = True
        elif uuid == 0x181E:
            _name = "BTHome v1 encrypted"
            _generic = True
        elif uuid == 0xFCD2:
            _name = "BTHome V2"  # Sponsored by Allterco / Shelly
            _generic = True
        elif uuid in self.member_uuids:
            _name = self.member_uuids[uuid]
            # Hardware manufacturers who OEM MAC PHYs etc, or offer the use
            # of their OUIs to third parties (specific known ones can be moved
            # to a case in the above conditions).
            if any(x in _name for x in ["Google", "Realtek"]):
                _generic = True
        elif uuid in self.company_uuids:
            _name = self.company_uuids[uuid]
            _generic = False
        else:
            return (None, None)
        return (_name, _generic)

    async def async_load_manufacturer_ids(self):
        """Import yaml files containing manufacturer name mappings."""
        try:
            # https://bitbucket.org/bluetooth-SIG/public/src/main/assigned_numbers/uuids/member_uuids.yaml
            file_path = self.hass.config.path(
                f"custom_components/{DOMAIN}/manufacturer_identification/member_uuids.yaml"
            )
            async with aiofiles.open(file_path) as f:
                mi_yaml = yaml.safe_load(await f.read())["uuids"]
            self.member_uuids: dict[int, str] = {member["uuid"]: member["name"] for member in mi_yaml}

            # https://bitbucket.org/bluetooth-SIG/public/src/main/assigned_numbers/company_identifiers/company_identifiers.yaml
            file_path = self.hass.config.path(
                f"custom_components/{DOMAIN}/manufacturer_identification/company_identifiers.yaml"
            )
            async with aiofiles.open(file_path) as f:
                ci_yaml = yaml.safe_load(await f.read())["company_identifiers"]
            self.company_uuids: dict[int, str] = {member["value"]: member["name"] for member in ci_yaml}
        finally:
            # Ensure that an issue reading these files (which are optional, really) doesn't stop the whole show.
            self._waitingfor_load_manufacturer_ids = False

    @callback
    def handle_devreg_changes(self, ev: Event[EventDeviceRegistryUpdatedData]):
        """
        Update our scanner list if the device registry is changed.

        This catches area changes (on scanners) and any new/changed
        Private BLE Devices.
        """
        if ev.data["action"] == "update":
            _LOGGER.debug("Device registry UPDATE. ev: %s changes: %s", ev, ev.data["changes"])
        else:
            _LOGGER.debug("Device registry has changed. ev: %s", ev)

        device_id = ev.data.get("device_id")

        if ev.data["action"] in {"create", "update"}:
            if device_id is None:
                _LOGGER.error("Received Device Registry create/update without a device_id. ev.data: %s", ev.data)
                return

            # First look for any of our devices that have a stored id on them, it'll be quicker.
            for device in self.devices.values():
                if device.entry_id == device_id:
                    # We matched, most likely a scanner.
                    if device.is_scanner:
                        self._refresh_scanners(force=True)
                        return
            # Didn't match an existing, work through the connections etc.

            # Pull up the device registry entry for the device_id
            if device_entry := self.dr.async_get(ev.data["device_id"]):
                # Work out if it's a device that interests us and respond appropriately.
                for conn_type, _conn_id in device_entry.connections:
                    if conn_type == "private_ble_device":
                        _LOGGER.debug("Trigger updating of Private BLE Devices")
                        self._do_private_device_init = True
                    elif conn_type == "ibeacon":
                        # this was probably us, nothing else to do
                        pass
                    else:
                        for ident_type, ident_id in device_entry.identifiers:
                            if ident_type == DOMAIN:
                                # One of our sensor devices!
                                try:
                                    if _device := self.devices[ident_id.lower()]:
                                        _device.name_by_user = device_entry.name_by_user
                                        _device.make_name()
                                except KeyError:
                                    pass
                        # might be a scanner, so let's refresh those
                        _LOGGER.debug("Trigger updating of Scanner Listings")
                        self._scanner_init_pending = True
            else:
                _LOGGER.error(
                    "Received DR update/create but device id does not exist: %s",
                    ev.data["device_id"],
                )

        elif ev.data["action"] == "remove":
            device_found = False
            for scanner in self.get_scanners:
                if scanner.entry_id == device_id:
                    _LOGGER.debug(
                        "Scanner %s removed, trigger update of scanners",
                        scanner.name,
                    )
                    self._scanner_init_pending = True
                    device_found = True
            if not device_found:
                # If we save the private ble device's device_id into devices[].entry_id
                # we could check ev.data["device_id"] against it to decide if we should
                # rescan PBLE devices. But right now we don't, so scan 'em anyway.
                _LOGGER.debug("Opportunistic trigger of update for Private BLE Devices")
                self._do_private_device_init = True
        # The co-ordinator will only get updates if we have created entities already.
        # Since this might not always be the case (say, private_ble_device loads after
        # we do), then we trigger an update here with the expectation that we got a
        # device registry update after the private ble device was created. There might
        # be other corner cases where we need to trigger our own update here, so test
        # carefully and completely if you are tempted to remove / alter this. Bermuda
        # will skip an update cycle if it detects one already in progress.
        # FIXME: self._async_update_data_internal()

    @callback
    def async_handle_advert(
        self,
        service_info: BluetoothServiceInfoBleak,
        change: BluetoothChange,
    ) -> None:
        """
        Handle an incoming advert callback from the bluetooth integration.

        These should come in as adverts are received, rather than on our update schedule.
        The data *should* be as fresh as can be, but actually the backend only sends
        these periodically (mainly when the data changes, I think). So it's no good for
        responding to changing rssi values, but it *is* good for seeding our updates in case
        there are no defined sensors yet (or the defined ones are away).
        """
        # _LOGGER.debug(
        #     "New Advert! change: %s, scanner: %s mac: %s name: %s serviceinfo: %s",
        #     change,
        #     service_info.source,
        #     service_info.address,
        #     service_info.name,
        #     service_info,
        # )

        # If there are no active entities created after Bermuda's
        # initial setup, then no updates will be triggered on the co-ordinator.
        # So let's check if we haven't updated recently, and do so...
        if self.stamp_last_update < monotonic_time_coarse() - (UPDATE_INTERVAL * 2):
            self._async_update_data_internal()

    def _check_all_platforms_created(self, address):
        """Checks if all platforms have finished loading a device's entities."""
        dev = self._get_device(address)
        if dev is not None:
            if all(
                [
                    dev.create_sensor_done,
                    dev.create_tracker_done,
                    dev.create_number_done,
                    dev.create_select_done,
                ]
            ):
                dev.create_all_done = True

    def sensor_created(self, address):
        """Allows sensor platform to report back that sensors have been set up."""
        dev = self._get_device(address)
        if dev is not None:
            dev.create_sensor_done = True
            # _LOGGER.debug("Sensor confirmed created for %s", address)
        else:
            _LOGGER.warning("Very odd, we got sensor_created for non-tracked device")
        self._check_all_platforms_created(address)

    def device_tracker_created(self, address):
        """Allows device_tracker platform to report back that sensors have been set up."""
        dev = self._get_device(address)
        if dev is not None:
            dev.create_tracker_done = True
            # _LOGGER.debug("Device_tracker confirmed created for %s", address)
        else:
            _LOGGER.warning("Very odd, we got sensor_created for non-tracked device")
        self._check_all_platforms_created(address)

    def number_created(self, address):
        """Receives report from number platform that sensors have been set up."""
        dev = self._get_device(address)
        if dev is not None:
            dev.create_number_done = True
        self._check_all_platforms_created(address)

    def select_created(self, address):
        """Receives report from select platform that entities have been set up."""
        dev = self._get_device(address)
        if dev is not None:
            dev.create_select_done = True
        self._check_all_platforms_created(address)

    # def button_created(self, address):
    #     """Receives report from number platform that sensors have been set up."""
    #     dev = self._get_device(address)
    #     if dev is not None:
    #         dev.create_button_done = True
    #     self._check_all_platforms_created(address)

    def count_active_devices(self) -> int:
        """
        Returns the number of bluetooth devices that have recent timestamps.

        Useful as a general indicator of health
        """
        stamp = monotonic_time_coarse() - 10  # seconds
        fresh_count = 0
        for device in self.devices.values():
            if device.last_seen > stamp:
                fresh_count += 1
        return fresh_count

    def count_active_scanners(self, max_age=10) -> int:
        """Returns count of scanners that have recently sent updates."""
        stamp = monotonic_time_coarse() - max_age  # seconds
        fresh_count = 0
        for scanner in self.get_active_scanner_summary():
            if scanner.get("last_stamp", 0) > stamp:
                fresh_count += 1
        return fresh_count

    def get_active_scanner_summary(self) -> list[dict]:
        """
        Returns a list of dicts suitable for seeing which scanners
        are configured in the system and how long it has been since
        each has returned an advertisement.
        """
        stamp = monotonic_time_coarse()
        return [
            {
                "name": scannerdev.name,
                "address": scannerdev.address,
                "last_stamp": scannerdev.last_seen,
                "last_stamp_age": stamp - scannerdev.last_seen,
            }
            for scannerdev in self.get_scanners
        ]

    def _get_device(self, address: str) -> BermudaDevice | None:
        """Search for a device entry based on mac address."""
        # mac_norm tries to return a lower-cased, colon-separated mac address.
        # failing that, it returns the original, lower-cased.
        try:
            return self.devices[mac_norm(address)]
        except KeyError:
            return None

    def _get_or_create_device(self, address: str) -> BermudaDevice:
        mac = mac_norm(address)
        try:
            return self.devices[mac]
        except KeyError:
            self.devices[mac] = device = BermudaDevice(mac, self)
            return device

    async def _async_update_data(self):
        """Implementation of DataUpdateCoordinator update_data function."""
        # return False
        self._async_update_data_internal()

    def _async_update_data_internal(self):
        """
        The primary update loop that processes almost all data in Bermuda.

        This works only with local data, so should be cheap to run
        (no network requests made etc). This function takes care of:

        - gathering all bluetooth adverts since last run and saving them into
          Bermuda's device objects
        - Updating all metadata
        - Performing rssi and statistical calculations
        - Making area determinations
        - (periodically) pruning device entries

        """
        if self._waitingfor_load_manufacturer_ids:
            _LOGGER.debug("Waiting for BT data load...")
            return True
        if self.update_in_progress:
            # Eeep!
            _LOGGER_SPAM_LESS.warning("update_still_running", "Previous update still running, skipping this cycle.")
            return False
        self.update_in_progress = True

        try:  # so we can still clean up update_in_progress
            nowstamp = monotonic_time_coarse()

            # The main "get all adverts from the backend" part.
            result_gather_adverts = self._async_gather_advert_data()

            self.update_metadevices()

            # Calculate per-device data
            #
            # Scanner entries have been loaded up with latest data, now we can
            # process data for all devices over all scanners.
            for device in self.devices.values():
                # Recalculate smoothed distances, last_seen etc
                device.calculate_data()

            self._refresh_trilateration()
            if self.trilat_enabled():
                self._refresh_areas_from_trilat()
            else:
                self._refresh_areas_by_min_distance()
            self.calibration.capture_update()

            # We might need to freshen deliberately on first start if no new scanners
            # were discovered in the first scan update. This is likely if nothing has changed
            # since the last time we booted.
            # if self._do_full_scanner_init:
            #     if not self._refresh_scanners():
            #         # _LOGGER.debug("Failed to refresh scanners, likely config entry not ready.")
            #         # don't fail the update, just try again next time.
            #         # self.last_update_success = False
            #         pass

            # If any *configured* devices have not yet been seen, create device
            # entries for them so they will claim the restored sensors in HA
            # (this prevents them from restoring at startup as "Unavailable" if they
            # are not currently visible, and will instead show as "Unknown" for
            # sensors and "Away" for device_trackers).
            #
            # This isn't working right if it runs once. Bodge it for now (cost is low)
            # and sort it out when moving to device-based restoration (ie using DR/ER
            # to decide what devices to track and deprecating CONF_DEVICES)
            #
            # if not self._seed_configured_devices_done:
            for _source_address in self.options.get(CONF_DEVICES, []):
                self._get_or_create_device(_source_address)
            self._seed_configured_devices_done = True

            # Trigger creation of any new entities
            #
            # The devices are all updated now (and any new scanners and beacons seen have been added),
            # so let's ensure any devices that we create sensors for are set up ready to go.
            for address, device in self.devices.items():
                if device.create_sensor:
                    if not device.create_all_done:
                        _LOGGER.debug("Firing device_new for %s (%s)", device.name, address)
                        # Note that the below should be OK thread-wise, debugger indicates this is being
                        # called by _run in events.py, so pretty sure we are "in the event loop".
                        async_dispatcher_send(self.hass, SIGNAL_DEVICE_NEW, address)

            # Device Pruning (only runs periodically)
            self.prune_devices()

        finally:
            # end of async update
            self.update_in_progress = False

        self.stamp_last_update_started = nowstamp
        self.stamp_last_update = monotonic_time_coarse()
        self.last_update_success = True
        return result_gather_adverts

    def _async_gather_advert_data(self):
        """Perform the gathering of backend Bluetooth Data and updating scanners and devices."""
        nowstamp = monotonic_time_coarse()
        _timestamp_cutoff = nowstamp - min(PRUNE_TIME_DEFAULT, PRUNE_TIME_UNKNOWN_IRK)

        # Initialise ha_scanners if we haven't already
        if self._scanner_init_pending:
            self._refresh_scanners(force=True)

        for ha_scanner in self._hascanners:
            # Create / Get the BermudaDevice for this scanner
            scanner_device = self._get_device(ha_scanner.source)

            if scanner_device is None:
                # Looks like a scanner we haven't met, refresh the list.
                self._refresh_scanners(force=True)
                scanner_device = self._get_device(ha_scanner.source)

            if scanner_device is None:
                # Highly unusual. If we can't find an entry for the scanner
                # maybe it's from an integration that's not yet loaded, or
                # perhaps it's an unexpected type that we don't know how to
                # find.
                _LOGGER_SPAM_LESS.error(
                    f"missing_scanner_entry_{ha_scanner.source}",
                    "Failed to find config for scanner %s, this is probably a bug.",
                    ha_scanner.source,
                )
                continue

            scanner_device.async_as_scanner_update(ha_scanner)

            # Now go through the scanner's adverts and send them to our device objects.
            for bledevice, advertisementdata in ha_scanner.discovered_devices_and_advertisement_data.values():
                if adstamp := scanner_device.async_as_scanner_get_stamp(bledevice.address):
                    if adstamp < self.stamp_last_update_started - 3:
                        # skip older adverts that should already have been processed
                        continue
                if advertisementdata.rssi == -127:
                    # BlueZ is pushing bogus adverts for paired but absent devices.
                    continue

                device = self._get_or_create_device(bledevice.address)
                device.process_advertisement(scanner_device, advertisementdata)

        # end of for ha_scanner loop
        return True

    def prune_devices(self, force_pruning=False):
        """
        Scan through all collected devices, and remove those that meet Pruning criteria.

        By default no pruning will be done if it has been performed within the last
        PRUNE_TIME_INTERVAL, unless the force_pruning flag is set to True.
        """
        if self.stamp_last_prune > monotonic_time_coarse() - PRUNE_TIME_INTERVAL and not force_pruning:
            # We ran recently enough, bail out.
            return
        # stamp the run.
        nowstamp = self.stamp_last_prune = monotonic_time_coarse()
        stamp_known_irk = nowstamp - PRUNE_TIME_KNOWN_IRK
        stamp_unknown_irk = nowstamp - PRUNE_TIME_UNKNOWN_IRK

        # Prune redaction data
        if self.stamp_redactions_expiry is not None and self.stamp_redactions_expiry < nowstamp:
            _LOGGER.debug("Clearing redaction data (%d items)", len(self.redactions))
            self.redactions.clear()
            self.stamp_redactions_expiry = None

        # Prune any IRK MACs that have expired
        self.irk_manager.async_prune()

        # Prune devices.
        prune_list: list[str] = []  # list of addresses to be pruned
        prunable_stamps: dict[str, float] = {}  # dict of potential prunees if we need to be more aggressive.

        metadevice_source_keepers = set()
        for metadevice in self.metadevices.values():
            if len(metadevice.metadevice_sources) > 0:
                # Always keep the most recent source, which we keep in index 0.
                # This covers static iBeacon sources, and possibly IRKs that might exceed
                # the spec lifetime but are going stale because they're away for a bit.
                _first = True
                for address in metadevice.metadevice_sources:
                    if _device := self._get_device(address):
                        if _first or _device.last_seen > stamp_known_irk:
                            # The source has been seen within the spec's limits, keep it.
                            metadevice_source_keepers.add(address)
                            _first = False
                        else:
                            # It's too old to be an IRK, and otherwise we'll auto-detect it,
                            # so let's be rid of it.
                            prune_list.append(address)

        for device_address, device in self.devices.items():
            # Prune any devices that haven't been heard from for too long, but only
            # if we aren't actively tracking them and it's a traditional MAC address.
            # We just collect the addresses first, and do the pruning after exiting this iterator
            #
            # Reduced selection criteria - basically if if's not:
            # - a scanner (beacuse we need those!)
            # - any metadevice less than 15 minutes old (PRUNE_TIME_KNOWN_IRK)
            # - a private_ble device (because they will re-create anyway, plus we auto-sensor them
            # - create_sensor
            # then it should be up for pruning. A stale iBeacon that we don't actually track
            # should totally be pruned if it's no longer around.
            if (
                device_address not in metadevice_source_keepers
                and device not in self.metadevices
                and device_address not in self.scanner_list
                and (not device.create_sensor)  # Not if we track the device
                and (not device.is_scanner)  # redundant, but whatevs.
                and device.address_type != BDADDR_TYPE_NOT_MAC48
            ):
                if device.address_type == BDADDR_TYPE_RANDOM_RESOLVABLE:
                    # This is an *UNKNOWN* IRK source address, or a known one which is
                    # well and truly stale (ie, not in keepers).
                    # We prune unknown irk's aggressively because they pile up quickly
                    # in high-density situations, and *we* don't need to hang on to new
                    # enrollments because we'll seed them from PBLE.
                    if device.last_seen < stamp_unknown_irk:
                        _LOGGER.debug(
                            "Marking stale (%ds) Unknown IRK address for pruning: [%s] %s",
                            nowstamp - device.last_seen,
                            device_address,
                            device.name,
                        )
                        prune_list.append(device_address)
                    elif device.last_seen < nowstamp - 200:  # BlueZ cache time
                        # It's not stale, but we will prune it if we can't make our
                        # quota of PRUNE_MAX_COUNT we'll shave these off too.

                        # Note that because BlueZ doesn't give us timestamps, we guess them
                        # based on whether the rssi has changed. If we delete our existing
                        # device we have nothing to compare too and will forever churn them.
                        # This can change if we drop support for BlueZ or we find a way to
                        # make stamps (we could also just keep a separate list but meh)
                        prunable_stamps[device_address] = device.last_seen

                elif device.last_seen < nowstamp - PRUNE_TIME_DEFAULT:
                    # It's a static address, and stale.
                    _LOGGER.debug(
                        "Marking old device entry for pruning: %s",
                        device.name,
                    )
                    prune_list.append(device_address)
                else:
                    # Device is static, not tracked, not so old, but we might have to prune it anyway
                    prunable_stamps[device_address] = device.last_seen

            # Do nothing else at this level without excluding the keepers first.

        prune_quota_shortfall = len(self.devices) - len(prune_list) - PRUNE_MAX_COUNT
        if prune_quota_shortfall > 0:
            # We need to find more addresses to prune. Perhaps we live
            # in a busy train station, or are under some sort of BLE-MAC
            # DOS-attack.
            if len(prunable_stamps) > 0:
                # Sort the prunables by timestamp ascending
                sorted_addresses = sorted([(v, k) for k, v in prunable_stamps.items()])
                cutoff_index = min(len(sorted_addresses), prune_quota_shortfall)

                _LOGGER.debug(
                    "Prune quota short by %d. Pruning %d extra devices (down to age %0.2f seconds)",
                    prune_quota_shortfall,
                    cutoff_index,
                    nowstamp - sorted_addresses[prune_quota_shortfall - 1][0],
                )
                # pylint: disable-next=unused-variable
                for _stamp, address in sorted_addresses[: prune_quota_shortfall - 1]:
                    prune_list.append(address)
            else:
                _LOGGER.warning(
                    "Need to prune another %s devices to make quota, but no extra prunables available",
                    prune_quota_shortfall,
                )
        else:
            _LOGGER.debug(
                "Pruning %d available MACs, we are inside quota by %d.", len(prune_list), prune_quota_shortfall * -1
            )

        # ###############################################
        # Prune_list is now ready to action. It contains no keepers, and is already
        # expanded if necessary to meet quota, as much as we can.

        # Prune the source devices
        for device_address in prune_list:
            _LOGGER.debug("Acting on prune list for %s", device_address)
            del self.devices[device_address]
            self._area_decision_state.pop(device_address, None)
            self._trilat_decision_state.pop(device_address, None)

        # Clean out the scanners dicts in metadevices and scanners
        # (scanners will have entries if they are also beacons, although
        # their addresses should never get stale, but one day someone will
        # have a beacon that uses randomised source addresses for some reason.
        #
        # Just brute-force all devices, because it was getting a bit hairy
        # ensuring we hit the right ones, and the cost is fairly low and periodic.
        for device in self.devices.values():
            # if (
            #     device.is_scanner
            #     or METADEVICE_PRIVATE_BLE_DEVICE in device.metadevice_type
            #     or METADEVICE_IBEACON_DEVICE in device.metadevice_type
            # ):
            # clean out the metadevice_sources field
            for address in prune_list:
                if address in device.metadevice_sources:
                    device.metadevice_sources.remove(address)

            # clean out the device/scanner advert pairs
            for advert_tuple in list(device.adverts.keys()):
                if device.adverts[advert_tuple].device_address in prune_list:
                    _LOGGER.debug(
                        "Pruning metadevice advert %s aged %ds",
                        advert_tuple,
                        nowstamp - device.adverts[advert_tuple].stamp,
                    )
                    del device.adverts[advert_tuple]

    def discover_private_ble_metadevices(self):
        """
        Access the Private BLE Device integration to find metadevices to track.

        This function sets up the skeleton metadevice entry for Private BLE (IRK)
        devices, ready for update_metadevices to manage.
        """
        if self._do_private_device_init:
            self._do_private_device_init = False
            _LOGGER.debug("Refreshing Private BLE Device list")

            # Iterate through the Private BLE Device integration's entities,
            # and ensure for each "device" we create a source device.
            # pb here means "private ble device"
            pb_entries = self.hass.config_entries.async_entries(DOMAIN_PRIVATE_BLE_DEVICE, include_disabled=False)
            for pb_entry in pb_entries:
                pb_entities = self.er.entities.get_entries_for_config_entry_id(pb_entry.entry_id)
                # This will be a list of entities for a given private ble device,
                # let's pull out the device_tracker one, since it has the state
                # info we need.
                for pb_entity in pb_entities:
                    if pb_entity.domain == Platform.DEVICE_TRACKER:
                        # We found a *device_tracker* entity for the private_ble device.
                        _LOGGER.debug(
                            "Found a Private BLE Device Tracker! %s",
                            pb_entity.entity_id,
                        )

                        # Grab the device entry (for the name, mostly)
                        if pb_entity.device_id is not None:
                            pb_device = self.dr.async_get(pb_entity.device_id)
                        else:
                            pb_device = None

                        # Grab the current state (so we can access the source address attrib)
                        pb_state = self.hass.states.get(pb_entity.entity_id)

                        if pb_state:  # in case it's not there yet
                            pb_source_address = pb_state.attributes.get("current_address", None)
                        else:
                            # Private BLE Device hasn't yet found a source device
                            pb_source_address = None

                        # Get the IRK of the device, which we will use as the address
                        # for the metadevice.
                        # As of 2024.4.0b4 Private_ble appends _device_tracker to the
                        # unique_id of the entity, while we really want to know
                        # the actual IRK, so handle either case by splitting it:
                        _irk = pb_entity.unique_id.split("_")[0]

                        # Create our Meta-Device and tag it up...
                        metadevice = self._get_or_create_device(_irk)
                        # Since user has already configured the Private BLE Device, we
                        # always create sensors for them.
                        metadevice.create_sensor = True

                        # Set a nice name
                        if pb_device:
                            metadevice.name_by_user = pb_device.name_by_user
                            metadevice.name_devreg = pb_device.name
                            metadevice.make_name()

                        # Ensure we track this PB entity so we get source address updates.
                        if pb_entity.entity_id not in self.pb_state_sources:
                            self.pb_state_sources[pb_entity.entity_id] = None  # FIXME: why none?

                        # Add metadevice to list so it gets included in update_metadevices
                        if metadevice.address not in self.metadevices:
                            self.metadevices[metadevice.address] = metadevice

                        if pb_source_address is not None:
                            # We've got a source MAC address!
                            pb_source_address = mac_norm(pb_source_address)

                            # Set up and tag the source device entry
                            source_device = self._get_or_create_device(pb_source_address)
                            source_device.metadevice_type.add(METADEVICE_TYPE_PRIVATE_BLE_SOURCE)

                            # Add source address. Don't remove anything, as pruning takes care of that.
                            if pb_source_address not in metadevice.metadevice_sources:
                                metadevice.metadevice_sources.insert(0, pb_source_address)

                            # Update state_sources so we can track when it changes
                            self.pb_state_sources[pb_entity.entity_id] = pb_source_address

                        else:
                            _LOGGER.debug(
                                "No address available for PB Device %s",
                                pb_entity.entity_id,
                            )

    def register_ibeacon_source(self, source_device: BermudaDevice):
        """
        Create or update the meta-device for tracking an iBeacon.

        This should be called each time we discover a new address advertising
        an iBeacon. This might happen only once at startup, but will also
        happen each time a new MAC address is used by a given iBeacon,
        or each time an existing MAC sends a *new* iBeacon(!)

        This does not update the beacon's details (distance etc), that is done
        in the update_metadevices function after all data has been gathered.
        """
        if METADEVICE_TYPE_IBEACON_SOURCE not in source_device.metadevice_type:
            _LOGGER.error(
                "Only IBEACON_SOURCE devices can be used to see a beacon metadevice. %s is not",
                source_device.name,
            )
        if source_device.beacon_unique_id is None:
            _LOGGER.error("Source device %s is not a valid iBeacon!", source_device.name)
        else:
            metadevice = self._get_or_create_device(source_device.beacon_unique_id)
            if len(metadevice.metadevice_sources) == 0:
                # #### NEW METADEVICE #####
                # (do one-off init stuff here)
                if metadevice.address not in self.metadevices:
                    self.metadevices[metadevice.address] = metadevice

                # Copy over the beacon attributes
                metadevice.name_bt_serviceinfo = source_device.name_bt_serviceinfo
                metadevice.name_bt_local_name = source_device.name_bt_local_name
                metadevice.beacon_unique_id = source_device.beacon_unique_id
                metadevice.beacon_major = source_device.beacon_major
                metadevice.beacon_minor = source_device.beacon_minor
                metadevice.beacon_power = source_device.beacon_power
                metadevice.beacon_uuid = source_device.beacon_uuid

                # Check if we should set up sensors for this beacon
                if metadevice.address.upper() in self.options.get(CONF_DEVICES, []):
                    # This is a meta-device we track. Flag it for set-up:
                    metadevice.create_sensor = True

            # #### EXISTING METADEVICE ####
            # (only do things that might have to change when MAC address cycles etc)

            if source_device.address not in metadevice.metadevice_sources:
                # We have a *new* source device.
                # insert this device as a known source
                metadevice.metadevice_sources.insert(0, source_device.address)

                # If we have a new / better name, use that..
                metadevice.name_bt_serviceinfo = metadevice.name_bt_serviceinfo or source_device.name_bt_serviceinfo
                metadevice.name_bt_local_name = metadevice.name_bt_local_name or source_device.name_bt_local_name

    def update_metadevices(self):
        """
        Create or update iBeacon, Private_BLE and other meta-devices from
        the received advertisements.

        This must be run on each update cycle, after the calculations for each source
        device is done, since we will copy their results into the metadevice.

        Area matching and trilateration will be performed *after* this, as they need
        to consider the full collection of sources, not just the ones of a single
        source device.
        """
        # First seed the Private BLE metadevice skeletons. It will only do anything
        # if the self._do_private_device_init flag is set.
        # FIXME: Can we delete this? pble's should create at realtime as they
        # are detected now.
        self.discover_private_ble_metadevices()

        # iBeacon devices should already have their metadevices created, so nothing more to
        # set up for them.

        for metadevice in self.metadevices.values():
            # Find every known source device and copy their adverts in.

            # Keep track of whether we want to recalculate the name fields at the end.
            _want_name_update = False
            _sources_to_remove = []

            for source_address in metadevice.metadevice_sources:
                # Get the BermudaDevice holding those adverts
                # TODO: Verify it's OK to not create here. Problem is that if we do create,
                # it causes a binge/purge cycle during pruning since it has no adverts on it.
                source_device = self._get_device(source_address)
                if source_device is None:
                    # No ads current in the backend for this one. Not an issue, the mac might be old
                    # or now showing up yet.
                    # _LOGGER_SPAM_LESS.debug(
                    #     f"metaNoAdsFor_{metadevice.address}_{source_address}",
                    #     "Metadevice %s: no adverts for source MAC %s found during update_metadevices",
                    #     metadevice.__repr__(),
                    #     source_address,
                    # )
                    continue

                if (
                    METADEVICE_IBEACON_DEVICE in metadevice.metadevice_type
                    and metadevice.beacon_unique_id != source_device.beacon_unique_id
                ):
                    # This source device no longer has the same ibeacon uuid+maj+min as
                    # the metadevice has.
                    # Some iBeacons (specifically Bluecharms) change uuid on movement.
                    #
                    # This source device has changed its uuid, so we won't track it against
                    # this metadevice any more / for now, and we will also remove
                    # the existing scanner entries on the metadevice, to ensure it goes
                    # `unknown` immediately (assuming no other source devices show up)
                    #
                    # Note that this won't quick-away devices that change their MAC at the
                    # same time as changing their uuid (like manually altering the beacon
                    # in an Android 15+), since the old source device will still be a match.
                    # and will be subject to the nomal DEVTRACK_TIMEOUT.
                    #
                    _LOGGER.debug(
                        "Source %s for metadev %s changed iBeacon identifiers, severing", source_device, metadevice
                    )
                    for key_address, key_scanner in list(metadevice.adverts):
                        if key_address == source_device.address:
                            del metadevice.adverts[(key_address, key_scanner)]
                    if source_device.address in metadevice.metadevice_sources:
                        # Remove this source from the list once we're done iterating on it
                        _sources_to_remove.append(source_device.address)
                    continue  # to next metadevice_source

                # Copy every ADVERT_TUPLE into our metadevice
                for advert_tuple in source_device.adverts:
                    metadevice.adverts[advert_tuple] = source_device.adverts[advert_tuple]

                # Update last_seen if the source is newer.
                if metadevice.last_seen < source_device.last_seen:
                    metadevice.last_seen = source_device.last_seen

                # If not done already, set the source device's ref_power from our own. This will cause
                # the source device and all its scanner entries to update their
                # distance measurements. This won't affect Area wins though, because
                # they are "relative", not absolute.

                # FIXME: This has two potential bugs:
                # - if multiple metadevices share a source, they will
                #   "fight" over their preferred ref_power, if different.
                # - The non-meta device (if tracked) will receive distances
                #   based on the meta device's ref_power.
                # - The non-meta device if tracked will have its own ref_power ignored.
                #
                # None of these are terribly awful, but worth fixing.

                # Note we are setting the ref_power on the source_device, not the
                # individual scanner entries (it will propagate to them though)
                if source_device.ref_power != metadevice.ref_power:
                    source_device.set_ref_power(metadevice.ref_power)

                # anything that isn't already set to something interesting, overwrite
                # it with the new device's data.
                for key, val in source_device.items():
                    if val is any(
                        [
                            source_device.name_bt_local_name,
                            source_device.name_bt_serviceinfo,
                            source_device.manufacturer,
                        ]
                    ) and metadevice[key] in [None, False]:
                        metadevice[key] = val
                        _want_name_update = True

                if _want_name_update:
                    metadevice.make_name()

                # Anything that's VERY interesting, overwrite it regardless of what's already there:
                # INTERESTING:
                for key, val in source_device.items():
                    if val is any(
                        [
                            source_device.beacon_major,
                            source_device.beacon_minor,
                            source_device.beacon_power,
                            source_device.beacon_unique_id,
                            source_device.beacon_uuid,
                        ]
                    ):
                        metadevice[key] = val
                        # _want_name_update = True
            # Done iterating sources, remove any to be dropped
            for source in _sources_to_remove:
                metadevice.metadevice_sources.remove(source)
            if _want_name_update:
                metadevice.make_name()

    def dt_mono_to_datetime(self, stamp) -> datetime:
        """Given a monotonic timestamp, convert to datetime object."""
        age = monotonic_time_coarse() - stamp
        return now() - timedelta(seconds=age)

    def dt_mono_to_age(self, stamp) -> str:
        """Convert monotonic timestamp to age (eg: "6 seconds ago")."""
        return get_age(self.dt_mono_to_datetime(stamp))

    def resolve_area_name(self, area_id) -> str | None:
        """
        Given an area_id, return the current area name.

        Will return None if the area id does *not* resolve to a single
        known area name.
        """
        areas = self.ar.async_get_area(area_id)
        if hasattr(areas, "name"):
            return getattr(areas, "name", "invalid_area")
        return None

    def _refresh_areas_by_min_distance(self):
        """Set area for ALL devices based on closest beacon."""
        for device in self.devices.values():
            if (
                # device.is_scanner is not True  # exclude scanners.
                device.create_sensor  # include any devices we are tracking
                # or device.metadevice_type in METADEVICE_SOURCETYPES  # and any source devices for PBLE, ibeacon etc
            ):
                self._refresh_area_by_min_distance(device)

    def _refresh_areas_from_trilat(self) -> None:
        """Set room/area for tracked devices from trilat output."""
        layout_hash = self.current_anchor_layout_hash()
        for device in self.devices.values():
            if not device.create_sensor:
                continue
            self._refresh_area_from_trilat(device, layout_hash)

    def _refresh_area_from_trilat(self, device: BermudaDevice, layout_hash: str) -> None:
        """Resolve one device room from trilat position and trained samples."""
        if (
            device.trilat_status not in {"ok", "low_confidence"}
            or device.trilat_x_m is None
            or device.trilat_y_m is None
        ):
            device.diag_area_switch = "KDE room classification: trilat_unavailable"
            device.apply_position_classification(
                None,
                floor_id=device.trilat_floor_id,
                floor_name=device.trilat_floor_name,
                force_unknown=True,
            )
            return

        if not self.room_classifier.has_trained_rooms(layout_hash, device.trilat_floor_id):
            device.diag_area_switch = "KDE room classification: no_trained_rooms"
            device.apply_position_classification(
                None,
                floor_id=device.trilat_floor_id,
                floor_name=device.trilat_floor_name,
                force_unknown=True,
            )
            return

        classification = self.room_classifier.classify(
            layout_hash=layout_hash,
            floor_id=device.trilat_floor_id,
            x_m=device.trilat_x_m,
            y_m=device.trilat_y_m,
            z_m=device.trilat_z_m,
        )
        device.diag_area_switch = (
            "KDE room classification: "
            f"{classification.reason} "
            f"best={classification.best_area_id or 'none'} "
            f"score={classification.best_score:.2f} "
            f"second={classification.second_score:.2f} "
            f"topk_used={classification.topk_used}"
        )
        if classification.area_id is not None:
            device.apply_position_classification(
                classification.area_id,
                floor_id=device.trilat_floor_id,
                floor_name=device.trilat_floor_name,
            )
            return

        device.apply_position_classification(
            None,
            floor_id=device.trilat_floor_id,
            floor_name=device.trilat_floor_name,
            force_unknown=True,
        )

    @dataclass
    class AreaTests:
        """
        Holds the results of Area-based tests.

        Likely to become a stand-alone class for performing the whole area-selection
        process.
        """

        device: str = ""
        scannername: tuple[str, str] = ("", "")
        areas: tuple[str, str] = ("", "")
        pcnt_diff: float = 0  # distance percentage difference.
        same_area: bool = False  # The old scanner is in the same area as us.
        # last_detection: tuple[float, float] = (0, 0)  # bt manager's last_detection field. Compare with ours.
        last_ad_age: tuple[float, float] = (0, 0)  # seconds since we last got *any* ad from scanner
        this_ad_age: tuple[float, float] = (0, 0)  # how old the *current* advert is on this scanner
        distance: tuple[float, float] = (0, 0)
        hist_min_max: tuple[float, float] = (0, 0)  # min/max distance from history
        # velocity: tuple[float, float] = (0, 0)
        # last_closer: tuple[float, float] = (0, 0)  # since old was closer and how long new has been closer
        reason: str | None = None  # reason/result

        def sensortext(self) -> str:
            """Return a text summary suitable for use in a sensor entity."""
            out = ""
            for var, val in vars(self).items():
                out += f"{var}|"
                if isinstance(val, tuple):
                    for v in val:
                        if isinstance(v, float):
                            out += f"{v:.2f}|"
                        else:
                            out += f"{v}"
                    # out += "\n"
                elif var == "pcnt_diff":
                    out += f"{val:.3f}"
                else:
                    out += f"{val}"
                out += "\n"
            return out[:255]

        def __str__(self) -> str:
            """
            Create string representation for easy debug logging/dumping
            and potentially a sensor for logging Area decisions.
            """
            out = ""
            for var, val in vars(self).items():
                out += f"** {var:20} "
                if isinstance(val, tuple):
                    for v in val:
                        if isinstance(v, float):
                            out += f"{v:.2f} "
                        else:
                            out += f"{v} "
                    out += "\n"
                elif var == "pcnt_diff":
                    out += f"{val:.3f}\n"
                else:
                    out += f"{val}\n"
            return out

    @dataclass
    class AreaCandidate:
        """A valid area contender after stale/radius/area checks."""

        advert: BermudaAdvert
        score: float
        rssi_filtered: float
        dispersion: float
        max_radius: float

    @dataclass
    class MobilityPolicy:
        """Mobility-aware switching policy defaults."""

        mode: str = MOBILITY_MOVING
        fast_ratio: float = 1.6
        dwell_seconds: float = 8.0
        majority_window: int = 9
        majority_need: int = 6
        min_rssi_confidence: float = -94.0
        ambiguity_ratio: float = 1.2
        ambiguity_hold_seconds: float = 8.0
        unknown_exit_ratio: float = 1.35
        unknown_enter_seconds: float = 12.0
        reacquire_seconds: float = 8.0

    @dataclass
    class AreaDecisionState:
        """Per-device rolling state to smooth area decisions."""

        dominant_history: deque[str] = field(default_factory=lambda: deque(maxlen=21))
        challenger_scanner: str | None = None
        challenger_since: float = 0.0
        ambiguous_since: float = 0.0
        unknown_since: float = 0.0

    @dataclass
    class TrilatMobilityPolicy:
        """Mobility-aware policy for floor hysteresis and trilat EWMA."""

        floor_dwell_seconds: float
        floor_switch_margin: float
        trilat_alpha: float

    @dataclass
    class TrilatDecisionState:
        """Per-device state for floor and trilat solve smoothing."""

        floor_id: str | None = None
        floor_challenger_id: str | None = None
        floor_challenger_since: float = 0.0
        floor_ambiguous_since: float = 0.0
        last_anchor_ids: tuple[str, ...] = ()
        last_anchor_ranges: dict[str, float] = field(default_factory=dict)
        last_anchor_z: dict[str, float | None] = field(default_factory=dict)
        last_solution_xy: tuple[float, float] | None = None
        last_solution_z: float | None = None
        last_solver_dimension: str = "2d"
        last_residual_m: float | None = None
        last_mean_sigma_m: float | None = None
        last_status: str = "unknown"

    _TRILAT_MIN_ANCHORS: int = 3
    _TRILAT_MIN_ANCHORS_3D: int = 4
    _TRILAT_MAX_RESIDUAL_M: float = 5.0
    _TRILAT_MAX_ANCHOR_SIGMA_M: float = 6.0
    _TRILAT_FLOOR_AMBIGUITY_RATIO: float = 0.2
    _TRILAT_RANGE_DELTA_EPSILON_M: float = 0.2
    _NON_CONNECTOR_EXTRA_FLOOR_SWITCH_MARGIN: float = 0.18
    _NON_CONNECTOR_EXTRA_FLOOR_DWELL_S: float = 12.0

    @staticmethod
    def _score_rssi(rssi_filtered: float | None) -> float:
        """Convert filtered RSSI to a monotonic confidence score."""
        if rssi_filtered is None:
            return 0.0
        # Keep score bounded and monotonic. ~6-10dB should matter noticeably.
        exponent = (max(min(rssi_filtered, -30.0), -120.0) + 90.0) / 8.0
        exponent = max(min(exponent, 12.0), -12.0)
        return math.exp(exponent)

    @staticmethod
    def _trilat_confidence_band(score: float) -> str:
        """Map a 0..10 confidence score into a coarse label."""
        if score >= 7.0:
            return "high"
        if score >= 4.0:
            return "medium"
        return "low"

    def _set_trilat_confidence(self, device: BermudaDevice, score: float) -> None:
        """Store trilateration confidence on the device."""
        clamped = round(max(0.0, min(10.0, score)), 1)
        device.trilat_confidence = clamped
        device.trilat_confidence_level = self._trilat_confidence_band(clamped)

    def _compute_trilat_confidence(
        self,
        anchor_count: int,
        residual_m: float | None,
        solver_dimension: str,
        floor_ambiguous: bool = False,
        mean_sigma_m: float | None = None,
    ) -> float:
        """Compute a conservative 0..10 confidence score for the current estimate."""
        residual_term = 0.0
        if residual_m is not None:
            residual_term = max(0.0, 1.0 - (residual_m / self._TRILAT_MAX_RESIDUAL_M))
        anchor_target = 6.0 if solver_dimension == "3d" else 4.0
        anchor_term = min(float(anchor_count) / anchor_target, 1.0)
        score = (7.2 * residual_term) + (2.3 * anchor_term) + (0.5 if solver_dimension == "3d" else 0.0)
        if mean_sigma_m is not None:
            sigma_term = max(0.0, 1.0 - (mean_sigma_m / self._TRILAT_MAX_ANCHOR_SIGMA_M))
            score = (score * 0.8) + (2.0 * sigma_term)
        if floor_ambiguous:
            score -= 2.0
        return score

    @staticmethod
    def _mobility_policy(mobility_type: str) -> MobilityPolicy:
        """Return area switching policy for the device mobility mode."""
        if mobility_type == MOBILITY_STATIONARY:
            return BermudaDataUpdateCoordinator.MobilityPolicy(
                mode=MOBILITY_STATIONARY,
                fast_ratio=2.0,
                dwell_seconds=28.0,
                majority_window=17,
                majority_need=12,
                min_rssi_confidence=-95.0,
                ambiguity_ratio=1.2,
                ambiguity_hold_seconds=16.0,
                unknown_exit_ratio=1.45,
                unknown_enter_seconds=30.0,
                reacquire_seconds=20.0,
            )
        return BermudaDataUpdateCoordinator.MobilityPolicy(
            fast_ratio=1.8,
            dwell_seconds=12.0,
            majority_window=11,
            majority_need=8,
            min_rssi_confidence=-96.0,
            ambiguity_ratio=1.15,
            ambiguity_hold_seconds=10.0,
            unknown_exit_ratio=1.35,
            unknown_enter_seconds=12.0,
            reacquire_seconds=8.0,
        )

    @staticmethod
    def _trilat_mobility_policy(mobility_type: str) -> TrilatMobilityPolicy:
        """Return floor/trilat policy for the device mobility mode."""
        if mobility_type == MOBILITY_STATIONARY:
            return BermudaDataUpdateCoordinator.TrilatMobilityPolicy(
                floor_dwell_seconds=24.0,
                floor_switch_margin=0.22,
                trilat_alpha=0.20,
            )
        return BermudaDataUpdateCoordinator.TrilatMobilityPolicy(
            floor_dwell_seconds=8.0,
            floor_switch_margin=0.12,
            trilat_alpha=0.40,
        )

    @staticmethod
    def _majority_wins(history: deque[str], candidate: str, window: int, need: int) -> bool:
        """Return true if candidate owns enough of the recent winner history."""
        if window <= 0 or need <= 0:
            return False
        recent = list(history)[-window:]
        return recent.count(candidate) >= need

    def _get_area_decision_state(self, device: BermudaDevice) -> AreaDecisionState:
        """Return mutable state holder for area-decision smoothing."""
        return self._area_decision_state.setdefault(device.address, self.AreaDecisionState())

    def _get_trilat_decision_state(self, device: BermudaDevice) -> TrilatDecisionState:
        """Return mutable state holder for trilat/floor smoothing."""
        return self._trilat_decision_state.setdefault(device.address, self.TrilatDecisionState())

    def _refresh_area_by_min_distance(self, device: BermudaDevice):
        """Resolve a device area using score-based contenders and mobility-aware smoothing."""
        nowstamp = monotonic_time_coarse()
        tests = self.AreaTests()
        tests.device = device.name
        _debug_this_device = debug_device_match(
            device.name,
            device.prefname,
            device.address,
            device.name_by_user,
            device.name_devreg,
            device.name_bt_local_name,
            device.name_bt_serviceinfo,
        )
        policy = self._mobility_policy(device.get_mobility_type())
        state = self._get_area_decision_state(device)

        contenders: list[BermudaDataUpdateCoordinator.AreaCandidate] = []
        for challenger in device.adverts.values():
            if challenger.stamp < nowstamp - AREA_MAX_AD_AGE:
                continue

            challenger_max_radius = self.get_scanner_max_radius(challenger.scanner_address)
            if challenger.rssi_distance is None or challenger.rssi_distance > challenger_max_radius or challenger.area_id is None:
                if _debug_this_device:
                    _LOGGER_TARGET_SPAM_LESS.debug(
                        f"area_rejected:{device.address}:{challenger.scanner_address}",
                        "Area: %s challenger %s REJECTED: distance=%.2f, max_radius=%.2f, area=%s",
                        device.name,
                        challenger.name,
                        challenger.rssi_distance if challenger.rssi_distance is not None else -1,
                        challenger_max_radius,
                        challenger.area_name or "None",
                    )
                continue

            rssi_for_score = challenger.rssi_filtered
            if rssi_for_score is None and challenger.rssi is not None:
                rssi_for_score = challenger.rssi + challenger.conf_rssi_offset

            score = self._score_rssi(rssi_for_score)
            contenders.append(
                self.AreaCandidate(
                    advert=challenger,
                    score=score,
                    rssi_filtered=rssi_for_score if rssi_for_score is not None else -127.0,
                    dispersion=getattr(challenger, "rssi_dispersion", 0.0),
                    max_radius=challenger_max_radius,
                )
            )

        contenders.sort(key=lambda c: (-c.score, c.advert.rssi_distance or 999.0))
        best = contenders[0] if contenders else None
        second = contenders[1] if len(contenders) > 1 else None
        best_area_id = best.advert.area_id if best is not None else None
        second_area_id = second.advert.area_id if second is not None else None
        contenders_same_area = best is not None and second is not None and best_area_id == second_area_id
        # dominant_history is appended at each exit point below with the resolved
        # outcome (chosen scanner address or AREA_NAME_UNKNOWN) rather than here
        # with the pre-gating best, so Unknown cycles never inflate any scanner's
        # majority vote count.

        incumbent_advert = device.area_advert
        incumbent = next((c for c in contenders if incumbent_advert is not None and c.advert is incumbent_advert), None)
        if incumbent is None and incumbent_advert is not None:
            incumbent = next((c for c in contenders if c.advert.scanner_address == incumbent_advert.scanner_address), None)

        # Unknown/Uncovered gating: weak, ambiguous, or edge-of-radius with tiny separation.
        unknown_reason: str | None = None
        if best is None:
            # Clear the ambiguity timer so it doesn't carry over into the next appearance.
            # If left set, a device that disappears while ambiguous and returns after
            # ambiguity_hold_seconds would fire the ambiguous_ratio unknown immediately
            # without waiting for the grace period.
            state.ambiguous_since = 0.0
            unknown_reason = "no_valid_contender"
        elif best.rssi_filtered < policy.min_rssi_confidence:
            # Same timer-hygiene: a weak-signal exit doesn't clear ambiguous_since, so
            # reset it here to prevent a stale timestamp triggering instant-unknown on return.
            state.ambiguous_since = 0.0
            unknown_reason = f"weak_rssi({best.rssi_filtered:.1f}dBm)"
        elif second is not None:
            ambiguous_ratio = best.score / max(second.score, 1e-9)
            if contenders_same_area:
                # Two top scanners in the same area are not area-ambiguous.
                state.ambiguous_since = 0.0
            else:
                if ambiguous_ratio < policy.ambiguity_ratio:
                    if state.ambiguous_since <= 0:
                        state.ambiguous_since = nowstamp
                    elif nowstamp - state.ambiguous_since >= policy.ambiguity_hold_seconds:
                        unknown_reason = f"ambiguous_ratio({ambiguous_ratio:.2f})"
                else:
                    state.ambiguous_since = 0.0

            if (
                unknown_reason is None
                and not contenders_same_area
                and best.advert.rssi_distance is not None
                and best.advert.rssi_distance > best.max_radius * 0.92
                and ambiguous_ratio < (policy.ambiguity_ratio + 0.1)
            ):
                unknown_reason = "edge_of_radius_ambiguous"
        else:
            state.ambiguous_since = 0.0

        # Hold unknown until evidence becomes clearly dominant.
        if unknown_reason is None and device.area_is_unknown and best is not None:
            # Only require a dominant ratio when there IS a second contender to compare against.
            # When second is None the best scanner is unambiguously the closest visible scanner,
            # which is actually strong evidence — requiring a non-existent competitor's score
            # would permanently trap single-scanner environments in Unknown state.
            if best_area_id is not None and best_area_id == device.area_last_seen_id:
                # Reacquiring the last known area should clear Unknown quickly.
                pass
            elif (
                second is not None
                and best_area_id != second_area_id
                and (best.score / max(second.score, 1e-9)) < policy.unknown_exit_ratio
            ):
                unknown_reason = "hold_unknown_until_clear"

        if unknown_reason is not None:
            if state.unknown_since <= 0:
                state.unknown_since = nowstamp
            unknown_age = nowstamp - state.unknown_since
            incumbent_for_hold = incumbent.advert if incumbent is not None else incumbent_advert

            # Delay Unknown transitions to avoid very short blips.
            if incumbent_for_hold is not None and unknown_age < policy.unknown_enter_seconds:
                tests.reason = f"HOLD incumbent pending_unknown({unknown_reason}) age={unknown_age:.1f}s"
                state.challenger_scanner = None
                state.challenger_since = 0.0
                if _debug_this_device:
                    _LOGGER_TARGET_SPAM_LESS.debug(
                        f"area_pending_unknown:{device.address}:{unknown_reason}",
                        "Area: %s hold incumbent %s while unknown pending (%s, age=%.1fs)",
                        device.name,
                        incumbent_for_hold.name,
                        unknown_reason,
                        unknown_age,
                    )
                state.dominant_history.append(incumbent_for_hold.scanner_address)
                device.apply_scanner_selection(incumbent_for_hold)
                return

            state.challenger_scanner = None
            state.challenger_since = 0.0
            tests.reason = f"UNKNOWN - {unknown_reason}"
            device.diag_area_switch = tests.sensortext()
            if _debug_this_device:
                _LOGGER_TARGET_SPAM_LESS.debug(
                    f"area_unknown:{device.address}:{unknown_reason}",
                    "Area: %s -> Unknown (%s), best=%s second=%s",
                    device.name,
                    unknown_reason,
                    f"{best.advert.name}:{best.score:.3f}" if best is not None else "None",
                    f"{second.advert.name}:{second.score:.3f}" if second is not None else "None",
                )
            state.dominant_history.append(AREA_NAME_UNKNOWN)
            device.apply_scanner_selection(None, force_unknown=True)
            return

        # Evidence is good enough, clear unknown timer.
        state.unknown_since = 0.0
        chosen = best
        if chosen is None:
            state.dominant_history.append(AREA_NAME_UNKNOWN)
            device.apply_scanner_selection(None)
            return

        if incumbent is None:
            if device.area_name is not None and not device.area_is_unknown:
                if state.challenger_scanner != chosen.advert.scanner_address:
                    state.challenger_scanner = chosen.advert.scanner_address
                    state.challenger_since = nowstamp
                if nowstamp - state.challenger_since < policy.reacquire_seconds:
                    tests.reason = (
                        f"HOLD pending_reacquire area={chosen.advert.area_name} "
                        f"age={nowstamp - state.challenger_since:.1f}s"
                    )
                    if device.area_is_unknown:
                        state.dominant_history.append(AREA_NAME_UNKNOWN)
                        device.apply_scanner_selection(None, force_unknown=True)
                    # else: device stays in prior valid area; skip history entry
                    # since no state changed and the new scanner is not yet confirmed.
                    return
            tests.reason = "WIN initial_valid_contender"
            state.challenger_scanner = None
            state.challenger_since = 0.0
        elif chosen.advert.scanner_address == incumbent.advert.scanner_address:
            tests.reason = "HOLD incumbent_best_score"
            state.challenger_scanner = None
            state.challenger_since = 0.0
            chosen = incumbent
        else:
            score_ratio = chosen.score / max(incumbent.score, 1e-9)
            # Adaptive hysteresis from measured jitter.
            max_dispersion = max(chosen.dispersion, incumbent.dispersion)
            noise_scale = min(max((max_dispersion - 3.0) / 5.0, 0.0), 1.0)
            fast_ratio = policy.fast_ratio + (0.25 if policy.mode == MOBILITY_MOVING else 0.45) * noise_scale
            dwell_seconds = policy.dwell_seconds + (6.0 if policy.mode == MOBILITY_MOVING else 12.0) * noise_scale
            majority_window = policy.majority_window + (2 if noise_scale > 0.6 else 0)
            majority_need = min(majority_window, policy.majority_need + (1 if noise_scale > 0.4 else 0))

            if score_ratio >= fast_ratio:
                tests.reason = f"WIN fast_lane ratio={score_ratio:.2f}"
                state.challenger_scanner = None
                state.challenger_since = 0.0
            else:
                # Legacy distance tie-break for tiny score differences.
                if (
                    score_ratio < 1.05
                    and incumbent.advert.rssi_distance is not None
                    and chosen.advert.rssi_distance is not None
                ):
                    _pda = chosen.advert.rssi_distance
                    _pdb = incumbent.advert.rssi_distance
                    tests.pcnt_diff = abs(_pda - _pdb) / ((_pda + _pdb) / 2)
                    if tests.pcnt_diff < 0.15:
                        tests.reason = "HOLD distance_tie_break"
                        chosen = incumbent
                        state.challenger_scanner = None
                        state.challenger_since = 0.0

                if tests.reason is None:
                    if state.challenger_scanner != chosen.advert.scanner_address:
                        state.challenger_scanner = chosen.advert.scanner_address
                        state.challenger_since = nowstamp
                    dwell_ok = nowstamp - state.challenger_since >= dwell_seconds
                    majority_ok = self._majority_wins(
                        state.dominant_history,
                        chosen.advert.scanner_address,
                        majority_window,
                        majority_need,
                    )
                    if dwell_ok or majority_ok:
                        tests.reason = (
                            f"WIN slow_lane {'dwell' if dwell_ok else 'majority'}"
                            f" ratio={score_ratio:.2f} disp={max_dispersion:.2f}"
                        )
                        state.challenger_scanner = None
                        state.challenger_since = 0.0
                    else:
                        tests.reason = (
                            f"HOLD incumbent slow_lane ratio={score_ratio:.2f}"
                            f" disp={max_dispersion:.2f}"
                        )
                        chosen = incumbent

        tests.scannername = (
            incumbent.advert.name if incumbent is not None else "",
            chosen.advert.name,
        )
        tests.areas = (
            incumbent.advert.area_name if incumbent is not None and incumbent.advert.area_name is not None else "",
            chosen.advert.area_name if chosen.advert.area_name is not None else "",
        )
        tests.distance = (
            incumbent.advert.rssi_distance if incumbent is not None and incumbent.advert.rssi_distance is not None else 0,
            chosen.advert.rssi_distance if chosen.advert.rssi_distance is not None else 0,
        )

        if _debug_this_device:
            _LOGGER_TARGET_SPAM_LESS.debug(
                f"area_chosen:{device.address}:{chosen.advert.scanner_address}:{tests.reason}",
                "Area: %s chose %s (%s) score=%.3f rssi_f=%.1f disp=%.2f reason=%s",
                device.name,
                chosen.advert.name,
                chosen.advert.area_name,
                chosen.score,
                chosen.rssi_filtered,
                chosen.dispersion,
                tests.reason,
            )

        if device.area_advert != chosen.advert and tests.reason is not None:
            device.diag_area_switch = tests.sensortext()

        state.dominant_history.append(chosen.advert.scanner_address)
        device.apply_scanner_selection(chosen.advert)

    @staticmethod
    def _scanner_timestamp_sync_state(scanner: BermudaDevice) -> str:
        """Return scanner timestamp-sync state with safe defaults for tests and local scanners."""
        diagnostics = getattr(scanner, "timestamp_sync_diagnostics", None)
        if callable(diagnostics):
            state = diagnostics().get("state")
            if isinstance(state, str):
                return state
        state_method = getattr(scanner, "timestamp_sync_state", None)
        if callable(state_method):
            state = state_method()
            if isinstance(state, str):
                return state
        return "synchronized"

    def _scanner_blocks_trilateration(self, scanner: BermudaDevice) -> bool:
        """Return whether a scanner should be ignored for trilat due to bad timestamps."""
        block_method = getattr(scanner, "blocks_trilateration_for_timestamp_sync", None)
        if callable(block_method):
            return bool(block_method())
        return self._scanner_timestamp_sync_state(scanner) in {"unstable", "broken"}

    def _latest_adverts_by_scanner(self, device: BermudaDevice):
        """Return latest advert per scanner for the device."""
        latest: dict[str, BermudaAdvert] = {}
        for advert in device.adverts.values():
            if self._scanner_blocks_trilateration(advert.scanner_device):
                continue
            prior = latest.get(advert.scanner_address)
            if prior is None or advert.stamp > prior.stamp:
                latest[advert.scanner_address] = advert
        return latest

    def _resolve_floor_name(self, floor_id: str | None) -> str | None:
        """Resolve floor name from floor id."""
        if floor_id is None:
            return None
        floor = self.fr.async_get_floor(floor_id)
        if floor is None:
            return None
        return floor.name

    def _rebuild_connector_topology(self) -> None:
        """Rebuild runtime connector-group lookup tables from config options."""
        self._connector_groups_by_id = {}
        self._connector_area_to_group_id = {}
        self._connector_group_floor_ids = {}
        for raw_group in self.options.get(CONF_CONNECTOR_GROUPS, []):
            group_id = str(raw_group.get("id") or "").strip()
            area_ids = [str(area_id) for area_id in raw_group.get("area_ids", []) if str(area_id).strip()]
            if not group_id or not area_ids:
                continue
            floor_ids: set[str] = set()
            for area_id in area_ids:
                area = self.ar.async_get_area(area_id)
                if area is None or area.floor_id is None:
                    continue
                floor_ids.add(area.floor_id)
                self._connector_area_to_group_id[area_id] = group_id
            self._connector_groups_by_id[group_id] = {
                "id": group_id,
                "name": str(raw_group.get("name") or group_id),
                "area_ids": area_ids,
            }
            self._connector_group_floor_ids[group_id] = floor_ids

    def group_for_area(self, area_id: str | None) -> str | None:
        """Return connector-group id for an area, if any."""
        if area_id is None:
            return None
        return self._connector_area_to_group_id.get(area_id)

    def group_has_floor(self, group_id: str | None, floor_id: str | None) -> bool:
        """Return whether a connector group spans a floor."""
        if group_id is None or floor_id is None:
            return False
        return floor_id in self._connector_group_floor_ids.get(group_id, set())

    def challenger_floor_is_connector_authorized(self, previous_floor_id: str | None, challenger_floor_id: str | None) -> bool:
        """Return whether any connector group links the previous and challenger floors."""
        if previous_floor_id is None or challenger_floor_id is None:
            return False
        if previous_floor_id == challenger_floor_id:
            return True
        for floor_ids in self._connector_group_floor_ids.values():
            if previous_floor_id in floor_ids and challenger_floor_id in floor_ids:
                return True
        return False

    def _stable_area_id_for_topology(self, device: BermudaDevice) -> str | None:
        """Return the most recent stable area id for topology checks."""
        if device.area_id is not None and not device.area_is_unknown:
            return device.area_id
        return device.area_last_seen_id

    def _stable_floor_id_for_topology(self, device: BermudaDevice) -> str | None:
        """Resolve stable floor id from current or last stable area."""
        stable_area_id = self._stable_area_id_for_topology(device)
        if stable_area_id is None:
            return None
        area = self.ar.async_get_area(stable_area_id)
        if area is None:
            return None
        return area.floor_id

    def _async_manage_repair_trilat_without_anchors(self, scannerlist: list[str]):
        """Raise/clear repair when trilat is enabled but no anchors are configured."""
        if self._trilat_scanners_without_anchors != scannerlist:
            self._trilat_scanners_without_anchors = scannerlist
            ir.async_delete_issue(self.hass, DOMAIN, REPAIR_TRILAT_WITHOUT_ANCHORS)
            if self._trilat_scanners_without_anchors and len(self._trilat_scanners_without_anchors) != 0:
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    REPAIR_TRILAT_WITHOUT_ANCHORS,
                    translation_key=REPAIR_TRILAT_WITHOUT_ANCHORS,
                    translation_placeholders={
                        "scannerlist": "".join(f"- {name}\n" for name in self._trilat_scanners_without_anchors),
                    },
                    severity=ir.IssueSeverity.WARNING,
                    is_fixable=False,
                )

    def _async_manage_broken_timestamp_sync_notification(self) -> None:
        """Raise or clear a persistent notification for scanners in broken sync state."""
        broken_scanners: list[str] = []
        for scanner in sorted(self._scanners, key=lambda item: item.name):
            if self._scanner_timestamp_sync_state(scanner) != "broken":
                continue
            diagnostics = scanner.timestamp_sync_diagnostics()
            broken_scanners.append(
                (
                    f"- {scanner.name} [{scanner.address}]"
                    f" last={diagnostics.get('last_scanner_backward_s')!s}s"
                    f" max_recent={diagnostics.get('recent_max_backward_s')!s}s"
                    f" recent_regressions={diagnostics.get('recent_scanner_regressions')}"
                    f" recent_stale_drops={diagnostics.get('recent_stale_advert_drops')}"
                )
            )
        if self._broken_timestamp_sync_scanners == broken_scanners:
            return
        self._broken_timestamp_sync_scanners = broken_scanners
        persistent_notification.async_dismiss(self.hass, self._BROKEN_PROXY_SYNC_NOTIFICATION_ID)
        if not broken_scanners:
            return
        persistent_notification.async_create(
            self.hass,
            "Bermuda has stopped using these scanners for trilateration because their "
            "timestamp sync state is `broken`:\n\n"
            + "\n".join(broken_scanners)
            + "\n\nTry restarting the affected proxy and watch its `Timestamp Sync` sensor.",
            title="Bermuda proxy timestamp sync problem",
            notification_id=self._BROKEN_PROXY_SYNC_NOTIFICATION_ID,
        )

    def _refresh_trilateration(self) -> None:
        """Refresh trilateration diagnostics for all tracked devices."""
        self._async_manage_broken_timestamp_sync_notification()
        if not self.trilat_enabled():
            self._async_manage_repair_trilat_without_anchors([])
            for device in self.devices.values():
                if device.create_sensor:
                    device.set_trilat_unknown("disabled")
                    self._set_trilat_confidence(device, 0.0)
            return

        configured_anchor_scanners: list[str] = []
        for scanner in self._scanners:
            if (
                getattr(scanner, "anchor_enabled", False)
                and self.get_scanner_anchor_x(scanner.address) is not None
                and self.get_scanner_anchor_y(scanner.address) is not None
                and scanner.floor_id is not None
            ):
                configured_anchor_scanners.append(scanner.address)

        if len(configured_anchor_scanners) == 0:
            scannerlist = [f"{scanner.name} [{scanner.address}]" for scanner in sorted(self._scanners, key=lambda s: s.name)]
            self._async_manage_repair_trilat_without_anchors(scannerlist)
        else:
            self._async_manage_repair_trilat_without_anchors([])

        for device in self.devices.values():
            if not device.create_sensor:
                continue
            self._refresh_trilateration_for_device(device)

    def _refresh_trilateration_for_device(self, device: BermudaDevice) -> None:
        """Resolve per-device trilateration diagnostics."""
        nowstamp = monotonic_time_coarse()
        latest = self._latest_adverts_by_scanner(device)
        state = self._get_trilat_decision_state(device)
        policy = self._trilat_mobility_policy(device.get_mobility_type())
        _debug_this_device = debug_device_match(
            device.name,
            device.prefname,
            device.address,
            device.name_by_user,
            device.name_devreg,
            device.name_bt_local_name,
            device.name_bt_serviceinfo,
        )

        fresh_any = any(advert.stamp >= nowstamp - DISTANCE_TIMEOUT for advert in latest.values())
        if not fresh_any:
            device.set_trilat_unknown(
                "stale_inputs",
                floor_id=state.floor_id,
                floor_name=self._resolve_floor_name(state.floor_id),
                anchor_count=0,
            )
            state.last_status = "unknown"
            self._set_trilat_confidence(device, 0.0)
            if _debug_this_device:
                _LOGGER_TARGET_SPAM_LESS.debug(
                    f"trilat_unknown:{device.address}:stale_inputs",
                    "Trilat: %s -> Unknown (stale_inputs)",
                    device.name,
                )
            return

        evidence_inputs: list[tuple[str, float]] = []
        penalty_db = self.trilat_cross_floor_penalty_db()
        for advert in latest.values():
            if advert.stamp < nowstamp - DISTANCE_TIMEOUT:
                continue
            scanner_floor_id = advert.scanner_device.floor_id
            if scanner_floor_id is None:
                continue
            rssi_for_score = advert.rssi_filtered
            if rssi_for_score is None and advert.rssi is not None:
                rssi_for_score = advert.rssi + advert.conf_rssi_offset
            if rssi_for_score is None:
                continue
            evidence_inputs.append((scanner_floor_id, rssi_for_score))

        if not evidence_inputs:
            device.set_trilat_unknown("ambiguous_floor", floor_id=state.floor_id, floor_name=self._resolve_floor_name(state.floor_id))
            state.last_status = "unknown"
            self._set_trilat_confidence(device, 0.0)
            return

        floors = sorted({floor_id for floor_id, _rssi in evidence_inputs})
        floor_evidence: dict[str, float] = {}
        for candidate_floor_id in floors:
            evidence = 0.0
            for scanner_floor_id, rssi_for_score in evidence_inputs:
                adjusted_rssi = (
                    rssi_for_score
                    if scanner_floor_id == candidate_floor_id
                    else rssi_for_score - penalty_db
                )
                evidence += self._score_rssi(adjusted_rssi)
            floor_evidence[candidate_floor_id] = evidence

        ranked_floors = sorted(floor_evidence.items(), key=lambda row: row[1], reverse=True)
        best_floor_id, best_floor_score = ranked_floors[0]
        second_floor_score = ranked_floors[1][1] if len(ranked_floors) > 1 else 0.0
        total_floor_score = sum(floor_evidence.values())
        floor_ambiguity = (
            len(ranked_floors) > 1
            and total_floor_score > 0.0
            and ((best_floor_score - second_floor_score) / total_floor_score) < self._TRILAT_FLOOR_AMBIGUITY_RATIO
        )

        floor_ambiguous_persisted = False
        if floor_ambiguity:
            if state.floor_ambiguous_since <= 0:
                state.floor_ambiguous_since = nowstamp
            elif nowstamp - state.floor_ambiguous_since >= policy.floor_dwell_seconds:
                floor_ambiguous_persisted = True
                if _debug_this_device:
                    _LOGGER_TARGET_SPAM_LESS.debug(
                        f"trilat_low_conf:{device.address}:ambiguous_floor",
                        "Trilat: %s low confidence (ambiguous_floor) best=%s(%.3f) second=%.3f total=%.3f",
                        device.name,
                        best_floor_id,
                        best_floor_score,
                        second_floor_score,
                        total_floor_score,
                    )
        else:
            state.floor_ambiguous_since = 0.0

        prev_floor_id = state.floor_id
        if state.floor_id is None:
            state.floor_id = best_floor_id
            state.floor_challenger_id = None
            state.floor_challenger_since = 0.0
        elif best_floor_id != state.floor_id:
            current_floor_score = floor_evidence.get(state.floor_id, 0.0)
            floor_margin = (best_floor_score - current_floor_score) / max(best_floor_score, 1e-9)
            previous_stable_floor_id = self._stable_floor_id_for_topology(device)
            connector_authorized = True
            if self._connector_groups_by_id and previous_stable_floor_id is not None:
                connector_authorized = self.challenger_floor_is_connector_authorized(
                    previous_stable_floor_id,
                    best_floor_id,
                )
            required_margin = policy.floor_switch_margin
            required_dwell = policy.floor_dwell_seconds
            if (
                self._connector_groups_by_id
                and previous_stable_floor_id is not None
                and not connector_authorized
            ):
                required_margin += self._NON_CONNECTOR_EXTRA_FLOOR_SWITCH_MARGIN
                required_dwell += self._NON_CONNECTOR_EXTRA_FLOOR_DWELL_S

            if floor_margin >= required_margin:
                if state.floor_challenger_id != best_floor_id:
                    state.floor_challenger_id = best_floor_id
                    state.floor_challenger_since = nowstamp
                elif nowstamp - state.floor_challenger_since >= required_dwell:
                    state.floor_id = best_floor_id
                    state.floor_challenger_id = None
                    state.floor_challenger_since = 0.0
            else:
                state.floor_challenger_id = None
                state.floor_challenger_since = 0.0
        else:
            state.floor_challenger_id = None
            state.floor_challenger_since = 0.0

        selected_floor_id = state.floor_id or best_floor_id
        selected_floor_name = self._resolve_floor_name(selected_floor_id)

        if prev_floor_id is not None and selected_floor_id != prev_floor_id:
            for advert in latest.values():
                advert.trilat_range_ewma_m = None
            state.last_anchor_ids = ()
            state.last_anchor_ranges.clear()
            state.last_anchor_z.clear()
            state.last_solution_xy = None
            state.last_solution_z = None
            state.last_solver_dimension = "2d"
            state.last_residual_m = None
            state.last_mean_sigma_m = None

        anchors: list[AnchorMeasurement] = []
        anchor_sigmas_m: list[float] = []
        for advert in latest.values():
            if advert.stamp < nowstamp - DISTANCE_TIMEOUT:
                continue
            scanner = advert.scanner_device
            if scanner.floor_id != selected_floor_id:
                continue
            if not self.get_scanner_anchor_enabled(scanner.address):
                continue

            anchor_x = self.get_scanner_anchor_x(scanner.address)
            anchor_y = self.get_scanner_anchor_y(scanner.address)
            if anchor_x is None or anchor_y is None:
                continue

            if advert.rssi_distance_raw is None:
                continue
            if advert.rssi_distance is None:
                continue
            sigma_m = getattr(advert, "rssi_distance_sigma_m", None)
            if sigma_m is None or sigma_m > self._TRILAT_MAX_ANCHOR_SIGMA_M:
                continue

            if advert.trilat_range_ewma_m is None:
                advert.trilat_range_ewma_m = advert.rssi_distance_raw
            else:
                advert.trilat_range_ewma_m = (policy.trilat_alpha * advert.rssi_distance_raw) + (
                    (1 - policy.trilat_alpha) * advert.trilat_range_ewma_m
                )
            anchor_sigmas_m.append(float(sigma_m))
            anchors.append(
                AnchorMeasurement(
                    scanner_address=scanner.address,
                    x_m=float(anchor_x),
                    y_m=float(anchor_y),
                    range_m=float(advert.trilat_range_ewma_m),
                    z_m=self.get_scanner_anchor_z(scanner.address),
                )
            )

        anchor_count = len(anchors)
        mean_sigma_m = (sum(anchor_sigmas_m) / anchor_count) if anchor_count > 0 else None
        if anchor_count < self._TRILAT_MIN_ANCHORS:
            fallback_xy = state.last_solution_xy
            fallback_z = state.last_solution_z
            if fallback_xy is None:
                fallback_xy = anchor_centroid(anchors) if anchor_count > 0 else (0.0, 0.0)
            if fallback_z is None and anchor_count > 0 and all(anchor.z_m is not None for anchor in anchors):
                fallback_z = anchor_centroid_3d(anchors)[2]

            device.set_trilat_solution(
                x_m=fallback_xy[0],
                y_m=fallback_xy[1],
                z_m=fallback_z,
                floor_id=selected_floor_id,
                floor_name=selected_floor_name,
                anchor_count=anchor_count,
                residual_m=state.last_residual_m if state.last_residual_m is not None else 0.0,
            )
            device.trilat_status = "low_confidence"
            device.trilat_reason = "insufficient_anchors_low_confidence"
            self._set_trilat_confidence(device, max(0.5, float(anchor_count) * 0.8))
            state.last_mean_sigma_m = mean_sigma_m
            state.last_status = "low_confidence"
            if _debug_this_device:
                _LOGGER_TARGET_SPAM_LESS.debug(
                    f"trilat_low_conf:{device.address}:insufficient_anchors",
                    "Trilat: %s low confidence (insufficient_anchors), floor=%s anchors=%d",
                    device.name,
                    selected_floor_name,
                    anchor_count,
                )
            return

        anchors.sort(key=lambda anchor: anchor.scanner_address)
        anchor_ids = tuple(anchor.scanner_address for anchor in anchors)
        anchor_ranges = {anchor.scanner_address: anchor.range_m for anchor in anchors}
        anchor_z = {anchor.scanner_address: anchor.z_m for anchor in anchors}
        can_solve_3d = (
            anchor_count >= self._TRILAT_MIN_ANCHORS_3D
            and all(anchor.z_m is not None for anchor in anchors)
        )
        solver_dimension = "3d" if can_solve_3d else "2d"

        inputs_changed = (
            state.last_anchor_ids != anchor_ids
            or state.last_solution_xy is None
            or state.last_solver_dimension != solver_dimension
            or (solver_dimension == "3d" and state.last_solution_z is None)
            or any(
                abs(anchor_ranges[address] - state.last_anchor_ranges.get(address, 1e9))
                >= self._TRILAT_RANGE_DELTA_EPSILON_M
                for address in anchor_ids
            )
            or any(anchor_z[address] != state.last_anchor_z.get(address) for address in anchor_ids)
        )

        if not inputs_changed and state.last_solution_xy is not None and state.last_residual_m is not None:
            device.set_trilat_solution(
                x_m=state.last_solution_xy[0],
                y_m=state.last_solution_xy[1],
                z_m=state.last_solution_z if state.last_solver_dimension == "3d" else None,
                floor_id=selected_floor_id,
                floor_name=selected_floor_name,
                anchor_count=anchor_count,
                residual_m=state.last_residual_m,
            )
            device.trilat_reason = "skip_unchanged_inputs"
            self._set_trilat_confidence(
                device,
                self._compute_trilat_confidence(
                    anchor_count=anchor_count,
                    residual_m=state.last_residual_m,
                    solver_dimension=state.last_solver_dimension,
                    floor_ambiguous=floor_ambiguous_persisted,
                    mean_sigma_m=state.last_mean_sigma_m,
                ),
            )
            if _debug_this_device:
                _LOGGER_TARGET_SPAM_LESS.debug(
                    f"trilat_skip:{device.address}",
                    "Trilat: %s skipped solve (unchanged inputs), floor=%s anchors=%d residual=%.3f",
                    device.name,
                    selected_floor_name,
                    anchor_count,
                    state.last_residual_m,
                )
            return

        if solver_dimension == "3d":
            centroid_3d = anchor_centroid_3d(anchors)
            initial_guess_3d = centroid_3d
            if (
                state.last_solution_xy is not None
                and state.last_solution_z is not None
                and selected_floor_id == state.floor_id
            ):
                if math.sqrt(
                    ((state.last_solution_xy[0] - centroid_3d[0]) ** 2)
                    + ((state.last_solution_xy[1] - centroid_3d[1]) ** 2)
                    + ((state.last_solution_z - centroid_3d[2]) ** 2)
                ) <= self._TRILAT_MAX_RESIDUAL_M:
                    initial_guess_3d = (
                        state.last_solution_xy[0],
                        state.last_solution_xy[1],
                        state.last_solution_z,
                    )
            solve_result = solve_3d_soft_l1(anchors, initial_guess=initial_guess_3d)
        else:
            centroid = anchor_centroid(anchors)
            initial_guess_2d = centroid
            if state.last_solution_xy is not None and selected_floor_id == state.floor_id:
                if math.hypot(
                    state.last_solution_xy[0] - centroid[0],
                    state.last_solution_xy[1] - centroid[1],
                ) <= self._TRILAT_MAX_RESIDUAL_M:
                    initial_guess_2d = state.last_solution_xy
            solve_result = solve_2d_soft_l1(anchors, initial_guess=initial_guess_2d)

        state.last_anchor_ids = anchor_ids
        state.last_anchor_ranges = anchor_ranges
        state.last_anchor_z = anchor_z
        state.last_solver_dimension = solver_dimension
        state.last_mean_sigma_m = mean_sigma_m

        if (
            not solve_result.ok
            or solve_result.residual_rms_m is None
            or solve_result.residual_rms_m > self._TRILAT_MAX_RESIDUAL_M
            or solve_result.x_m is None
            or solve_result.y_m is None
            or (solver_dimension == "3d" and solve_result.z_m is None)
        ):
            fallback_xy = state.last_solution_xy
            fallback_z = state.last_solution_z
            if solve_result.x_m is not None and solve_result.y_m is not None:
                fallback_xy = (solve_result.x_m, solve_result.y_m)
            elif fallback_xy is None:
                fallback_xy = anchor_centroid(anchors)
            if solver_dimension == "3d":
                if solve_result.z_m is not None:
                    fallback_z = solve_result.z_m
                elif fallback_z is None and all(anchor.z_m is not None for anchor in anchors):
                    fallback_z = anchor_centroid_3d(anchors)[2]

            fallback_residual = solve_result.residual_rms_m
            if fallback_residual is None:
                fallback_residual = state.last_residual_m if state.last_residual_m is not None else (self._TRILAT_MAX_RESIDUAL_M * 1.5)

            device.set_trilat_solution(
                x_m=fallback_xy[0],
                y_m=fallback_xy[1],
                z_m=fallback_z if solver_dimension == "3d" else None,
                floor_id=selected_floor_id,
                floor_name=selected_floor_name,
                anchor_count=anchor_count,
                residual_m=fallback_residual,
            )
            device.trilat_status = "low_confidence"
            device.trilat_reason = "high_residual_low_confidence"
            self._set_trilat_confidence(
                device,
                self._compute_trilat_confidence(
                    anchor_count=anchor_count,
                    residual_m=max(fallback_residual, self._TRILAT_MAX_RESIDUAL_M),
                    solver_dimension=solver_dimension,
                    floor_ambiguous=floor_ambiguous_persisted,
                    mean_sigma_m=mean_sigma_m,
                ),
            )
            state.last_solution_xy = fallback_xy
            state.last_solution_z = fallback_z if solver_dimension == "3d" else None
            state.last_residual_m = fallback_residual
            state.last_status = "low_confidence"
            if _debug_this_device:
                _LOGGER_TARGET_SPAM_LESS.debug(
                    f"trilat_low_conf:{device.address}:high_residual",
                    "Trilat: %s low confidence (high_residual), floor=%s anchors=%d residual=%s reason=%s",
                    device.name,
                    selected_floor_name,
                    anchor_count,
                    f"{solve_result.residual_rms_m:.3f}" if solve_result.residual_rms_m is not None else "None",
                    solve_result.reason,
                )
            return

        device.set_trilat_solution(
            x_m=solve_result.x_m,
            y_m=solve_result.y_m,
            z_m=solve_result.z_m if solver_dimension == "3d" else None,
            floor_id=selected_floor_id,
            floor_name=selected_floor_name,
            anchor_count=anchor_count,
            residual_m=solve_result.residual_rms_m,
        )
        state.last_solution_xy = (solve_result.x_m, solve_result.y_m)
        state.last_solution_z = solve_result.z_m if solver_dimension == "3d" else None
        state.last_residual_m = solve_result.residual_rms_m
        state.last_status = "ok"
        self._set_trilat_confidence(
            device,
            self._compute_trilat_confidence(
                anchor_count=anchor_count,
                residual_m=solve_result.residual_rms_m,
                solver_dimension=solver_dimension,
                floor_ambiguous=floor_ambiguous_persisted,
                mean_sigma_m=mean_sigma_m,
            ),
        )
        if _debug_this_device:
            if solver_dimension == "3d" and solve_result.z_m is not None:
                _LOGGER_TARGET_SPAM_LESS.debug(
                    f"trilat_ok:{device.address}",
                    "Trilat: %s solved floor=%s anchors=%d x=%.3f y=%.3f z=%.3f residual=%.3f",
                    device.name,
                    selected_floor_name,
                    anchor_count,
                    solve_result.x_m,
                    solve_result.y_m,
                    solve_result.z_m,
                    solve_result.residual_rms_m,
                )
            else:
                _LOGGER_TARGET_SPAM_LESS.debug(
                    f"trilat_ok:{device.address}",
                    "Trilat: %s solved floor=%s anchors=%d x=%.3f y=%.3f residual=%.3f",
                    device.name,
                    selected_floor_name,
                    anchor_count,
                    solve_result.x_m,
                    solve_result.y_m,
                    solve_result.residual_rms_m,
                )

    def _refresh_scanners(self, force=False):
        """
        Refresh data on existing scanner objects, and rebuild if scannerlist has changed.

        Called on every update cycle, this handles the *fast* updates (such as updating
        timestamps). If it detects that the list of scanners has changed (or is called
        with force=True) then the full list of scanners will be rebuild by calling
        _rebuild_scanners.
        """
        self._rebuild_scanner_list(force=force)

    def _rebuild_scanner_list(self, force=False):
        """
        Rebuild Bermuda's internal list of scanners.

        Called on every update (via _refresh_scanners) but exits *quickly*
        *unless*:
          - the scanner set has changed or
          - force=True or
          - self._force_full_scanner_init=True
        """
        _new_ha_scanners = set[BaseHaScanner]
        # Using new API in 2025.2
        _new_ha_scanners = set(self._manager.async_current_scanners())

        if _new_ha_scanners is self._hascanners or _new_ha_scanners == self._hascanners:
            # No changes.
            return

        _LOGGER.debug("HA Base Scanner Set has changed, rebuilding.")
        self._hascanners = _new_ha_scanners

        self._async_purge_removed_scanners()

        # So we can raise a single repair listing all area-less scanners:
        _scanners_without_areas: list[str] = []

        # Find active HaBaseScanners in the backend and treat that as our
        # authoritative source of truth.
        #
        for hascanner in self._hascanners:
            scanner_address = mac_norm(hascanner.source)
            bermuda_scanner = self._get_or_create_device(scanner_address)
            bermuda_scanner.async_as_scanner_init(hascanner)

            if bermuda_scanner.area_id is None:
                _scanners_without_areas.append(f"{bermuda_scanner.name} [{bermuda_scanner.address}]")
        self._async_manage_repair_scanners_without_areas(_scanners_without_areas)

    def _async_purge_removed_scanners(self):
        """Demotes any devices that are no longer scanners based on new self.hascanners."""
        _scanners = [device.address for device in self.devices.values() if device.is_scanner]
        for ha_scanner in self._hascanners:
            scanner_address = mac_norm(ha_scanner.source)
            if scanner_address in _scanners:
                # This is still an extant HA Scanner, so we'll keep it.
                _scanners.remove(scanner_address)
        # Whatever's left are presumably no longer scanners.
        for address in _scanners:
            _LOGGER.info("Demoting ex-scanner %s", self.devices[address].name)
            self.devices[address].async_as_scanner_nolonger()

    def _async_manage_repair_scanners_without_areas(self, scannerlist: list[str]):
        """
        Raise a repair for any scanners that lack an area assignment.

        This function will take care of ensuring a repair is (re)raised
        or cleared (if the list is empty) when given a list of area-less scanner names.

        scannerlist should contain a friendly string to name each scanner missing an area.
        """
        if self._scanners_without_areas != scannerlist:
            self._scanners_without_areas = scannerlist
            # Clear any existing repair, because it's either resolved now (empty list) or we need to re-issue
            # the repair in order to update the scanner list (re-calling doesn't update it).
            ir.async_delete_issue(self.hass, DOMAIN, REPAIR_SCANNER_WITHOUT_AREA)

            if self._scanners_without_areas and len(self._scanners_without_areas) != 0:
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    REPAIR_SCANNER_WITHOUT_AREA,
                    translation_key=REPAIR_SCANNER_WITHOUT_AREA,
                    translation_placeholders={
                        "scannerlist": "".join(f"- {name}\n" for name in self._scanners_without_areas),
                    },
                    severity=ir.IssueSeverity.ERROR,
                    is_fixable=False,
                )

    # *** Not required now that we don't reload for scanners.
    # @callback
    # def async_call_update_entry(self, confdata_scanners) -> None:
    #     """
    #     Call in the event loop to update the scanner entries in our config.

    #     We do this via add_job to ensure it runs in the event loop.
    #     """
    #     # Clear the flag for init and update the stamp
    #     self._do_full_scanner_init = False
    #     self.last_config_entry_update = monotonic_time_coarse()
    #     # Apply new config (will cause reload if there are changes)
    #     self.hass.config_entries.async_update_entry(
    #         self.config_entry,
    #         data={
    #             **self.config_entry.data,
    #             CONFDATA_SCANNERS: confdata_scanners,
    #         },
    #     )

    def get_bermuda_device_from_registry_id(self, registry_id: str) -> BermudaDevice | None:
        """Return the matching Bermuda device for a Home Assistant device id."""
        device = self.dr.async_get(registry_id)
        if device is None:
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
            return None
        return self.devices.get(str(device_address).lower())

    async def service_record_calibration_sample(self, call: ServiceCall) -> ServiceResponse:
        """Start an asynchronous calibration sample capture session."""
        sample_radius_m = call.data.get("sample_radius_m")
        if sample_radius_m is None:
            sample_radius_m = call.data.get("room_radius_m", DEFAULT_SAMPLE_RADIUS_M)
        try:
            response = await self.calibration.async_start_session(
                device_id=call.data["device_id"],
                room_area_id=call.data["room_area_id"],
                x_m=call.data["x_m"],
                y_m=call.data["y_m"],
                z_m=call.data["z_m"],
                sample_radius_m=sample_radius_m,
                duration_s=call.data.get("duration_s", 60),
                notes=call.data.get("notes") or None,
            )
        except HomeAssistantError as err:
            raise vol.Invalid(str(err)) from err
        return response

    async def service_dump_devices(self, call: ServiceCall) -> ServiceResponse:  # pylint: disable=unused-argument;
        """Return a dump of beacon advertisements by receiver."""
        out = {}
        addresses_input = call.data.get("addresses", "")
        redact = call.data.get("redact", False)
        configured_devices = call.data.get("configured_devices", False)

        # Choose filter for device/address selection
        addresses = []
        if addresses_input != "":
            # Specific devices
            addresses += addresses_input.upper().split()
        if configured_devices:
            # configured and scanners
            addresses += self.scanner_list
            addresses += self.options.get(CONF_DEVICES, [])
            # known IRK/Private BLE Devices
            addresses += self.pb_state_sources

        # lowercase all the addresses for matching
        addresses = list(map(str.lower, addresses))

        # Build the dict of devices
        for address, device in self.devices.items():
            if len(addresses) == 0 or address.lower() in addresses:
                out[address] = device.to_dict()

        if redact:
            _stamp_redact = monotonic_time_coarse()
            out = cast("ServiceResponse", self.redact_data(out))
            _stamp_redact_elapsed = monotonic_time_coarse() - _stamp_redact
            if _stamp_redact_elapsed > 3:  # It should be fast now.
                _LOGGER.warning("Dump devices redaction took %2f seconds", _stamp_redact_elapsed)
            else:
                _LOGGER.debug("Dump devices redaction took %2f seconds", _stamp_redact_elapsed)
        return out

    def redaction_list_update(self):
        """
        Freshen or create the list of match/replace pairs that we use to
        redact MAC addresses. This gives a set of helpful address replacements
        that still allows identifying device entries without disclosing MAC
        addresses.
        """
        _stamp = monotonic_time_coarse()

        # counter for incrementing replacement names (eg, SCANNER_n). The length
        # of the existing redaction list is a decent enough starting point.
        i = len(self.redactions)

        # SCANNERS
        for non_lower_address in self.scanner_list:
            address = non_lower_address.lower()
            if address not in self.redactions:
                i += 1
                for altmac in mac_explode_formats(address):
                    self.redactions[altmac] = f"{address[:2]}::SCANNER_{i}::{address[-2:]}"
        _LOGGER.debug("Redact scanners: %ss, %d items", monotonic_time_coarse() - _stamp, len(self.redactions))
        # CONFIGURED DEVICES
        for non_lower_address in self.options.get(CONF_DEVICES, []):
            address = non_lower_address.lower()
            if address not in self.redactions:
                i += 1
                if address.count("_") == 2:
                    self.redactions[address] = f"{address[:4]}::CFG_iBea_{i}::{address[32:]}"
                    # Raw uuid in advert
                    self.redactions[address.split("_")[0]] = f"{address[:4]}::CFG_iBea_{i}_{address[32:]}::"
                elif len(address) == 17:
                    for altmac in mac_explode_formats(address):
                        self.redactions[altmac] = f"{address[:2]}::CFG_MAC_{i}::{address[-2:]}"
                else:
                    # Don't know what it is, but not a mac.
                    self.redactions[address] = f"CFG_OTHER_{1}_{address}"
        _LOGGER.debug("Redact confdevs: %ss, %d items", monotonic_time_coarse() - _stamp, len(self.redactions))
        # EVERYTHING ELSE
        for non_lower_address, device in self.devices.items():
            address = non_lower_address.lower()
            if address not in self.redactions:
                # Only add if they are not already there.
                i += 1
                if device.address_type == ADDR_TYPE_PRIVATE_BLE_DEVICE:
                    self.redactions[address] = f"{address[:4]}::IRK_DEV_{i}"
                elif address.count("_") == 2:
                    self.redactions[address] = f"{address[:4]}::OTHER_iBea_{i}::{address[32:]}"
                    # Raw uuid in advert
                    self.redactions[address.split("_")[0]] = f"{address[:4]}::OTHER_iBea_{i}_{address[32:]}::"
                elif len(address) == 17:  # a MAC
                    for altmac in mac_explode_formats(address):
                        self.redactions[altmac] = f"{address[:2]}::OTHER_MAC_{i}::{address[-2:]}"
                else:
                    # Don't know what it is.
                    self.redactions[address] = f"OTHER_{i}_{address}"
        _LOGGER.debug("Redact therest: %ss, %d items", monotonic_time_coarse() - _stamp, len(self.redactions))
        _elapsed = monotonic_time_coarse() - _stamp
        if _elapsed > 0.5:
            _LOGGER.warning("Redaction list update took %.3f seconds, has %d items", _elapsed, len(self.redactions))
        else:
            _LOGGER.debug("Redaction list update took %.3f seconds, has %d items", _elapsed, len(self.redactions))
        self.stamp_redactions_expiry = monotonic_time_coarse() + PRUNE_TIME_REDACTIONS

    def redact_data(self, data, first_recursion=True):
        """
        Wash any collection of data of any MAC addresses.

        Uses the redaction list of substitutions if already created, then
        washes any remaining mac-like addresses. This routine is recursive,
        so if you're changing it bear that in mind!
        """
        if first_recursion:
            # On first/outer call, refresh the redaction list to ensure
            # we don't let any new addresses slip through. Might be expensive
            # on first call, but will be much cheaper for subsequent calls.
            self.redaction_list_update()
            first_recursion = False

        if isinstance(data, str):  # Base Case
            datalower = data.lower()
            # the end of the recursive wormhole, do the actual work:
            if datalower in self.redactions:
                # Full string match, a quick short-circuit
                data = self.redactions[datalower]
            else:
                # Search for any of the redaction strings in the data.
                for find, fix in list(self.redactions.items()):
                    if find in datalower:
                        data = datalower.replace(find, fix)
                        # don't break out because there might be multiple fixes required.
            # redactions done, now replace any remaining MAC addresses
            # We are only looking for xx:xx:xx... format.
            return self._redact_generic_re.sub(self._redact_generic_sub, data)
        elif isinstance(data, dict):
            return {self.redact_data(k, False): self.redact_data(v, False) for k, v in data.items()}
        elif isinstance(data, list):
            return [self.redact_data(v, False) for v in data]
        else:  # Base Case
            return data
