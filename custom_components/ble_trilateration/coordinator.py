"""DataUpdateCoordinator for Bermuda bluetooth data."""

from __future__ import annotations

import logging
import math
import re
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
from .floor_config_store import FloorConfigStore
from .transition_zone_store import BermudaTransitionZoneStore, TransitionZone
from .reachability_gate import ReachabilityGate, ReachabilityDecision
from .bermuda_irk import BermudaIrkManager
from .ranging_model import BermudaRangingModel
from .room_classifier import BermudaRoomClassifier, GlobalFingerprintResult
from .scanner_anchor_store import BermudaScannerAnchorStore
from .trilat_bootstrap_store import BermudaTrilatBootstrapStore, TrilatBootstrapRecord
from .const import (
    _LOGGER,
    _LOGGER_SPAM_LESS,
    _LOGGER_TARGET_SPAM_LESS,
    ADDR_TYPE_PRIVATE_BLE_DEVICE,
    BDADDR_TYPE_NOT_MAC48,
    BDADDR_TYPE_RANDOM_RESOLVABLE,
    CONF_DEVICES,
    CONF_DEVTRACK_TIMEOUT,
    CONF_MAX_VELOCITY,
    CONF_TRILAT_CROSS_FLOOR_PENALTY_DB,
    CONF_TRILAT_SOFT_INCLUDE_OTHER_FLOOR_ANCHORS,
    CONF_TRILAT_REACHABILITY_GATE,
    CONF_SMOOTHING_SAMPLES,
    CONF_UPDATE_INTERVAL,
    DEFAULT_DEVTRACK_TIMEOUT,
    DEFAULT_MAX_VELOCITY,
    DEFAULT_SAMPLE_RADIUS_M,
    DEFAULT_SMOOTHING_SAMPLES,
    DEFAULT_TRILAT_CROSS_FLOOR_PENALTY_DB,
    DEFAULT_TRILAT_SOFT_INCLUDE_OTHER_FLOOR_ANCHORS,
    DEFAULT_TRILAT_REACHABILITY_GATE,
    DEFAULT_UPDATE_INTERVAL,
    DISTANCE_TIMEOUT,
    DOMAIN,
    DOMAIN_PRIVATE_BLE_DEVICE,
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
    REPAIR_CALIBRATION_LAYOUT_MISMATCH,
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
    SolveQualityMetrics,
    SolvePrior2D,
    SolvePrior3D,
    anchor_centroid,
    anchor_centroid_3d,
    solve_quality_metrics_2d,
    solve_quality_metrics_3d,
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
        self._trilat_decision_state: dict[str, BermudaDataUpdateCoordinator.TrilatDecisionState] = {}
        self._trilat_scanners_without_anchors: list[str] | None = None
        self._calibration_layout_mismatch_signature: str | None = None
        self.calibration_store = BermudaCalibrationStore(hass, entry.entry_id)
        self.scanner_anchor_store = BermudaScannerAnchorStore(hass)
        self._transition_zone_store = BermudaTransitionZoneStore(hass)
        self._floor_config_store = FloorConfigStore(hass)
        self._trilat_bootstrap_store = BermudaTrilatBootstrapStore(hass)
        self._reachability_gate = ReachabilityGate()
        self.calibration = BermudaCalibrationManager(hass, self, self.calibration_store)
        self.ranging_model = BermudaRangingModel(self.calibration)
        self.room_classifier = BermudaRoomClassifier(self.calibration, self.ar)

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
        self.options[CONF_DEVTRACK_TIMEOUT] = DEFAULT_DEVTRACK_TIMEOUT
        self.options[CONF_MAX_VELOCITY] = DEFAULT_MAX_VELOCITY
        self.options[CONF_SMOOTHING_SAMPLES] = DEFAULT_SMOOTHING_SAMPLES
        self.options[CONF_UPDATE_INTERVAL] = DEFAULT_UPDATE_INTERVAL
        self.options[CONF_TRILAT_CROSS_FLOOR_PENALTY_DB] = DEFAULT_TRILAT_CROSS_FLOOR_PENALTY_DB
        self.options[CONF_TRILAT_SOFT_INCLUDE_OTHER_FLOOR_ANCHORS] = DEFAULT_TRILAT_SOFT_INCLUDE_OTHER_FLOOR_ANCHORS

        if hasattr(entry, "options"):
            # Firstly, on some calls (specifically during reload after settings changes)
            # we seem to get called with a non-existant config_entry.
            # Anyway... if we DO have one, convert it to a plain dict so we can
            # serialise it properly when it goes into the device and scanner classes.
            for key, val in entry.options.items():
                if key in (
                    CONF_DEVICES,
                    CONF_DEVTRACK_TIMEOUT,
                    CONF_MAX_VELOCITY,
                    CONF_SMOOTHING_SAMPLES,
                    CONF_TRILAT_CROSS_FLOOR_PENALTY_DB,
                    CONF_TRILAT_SOFT_INCLUDE_OTHER_FLOOR_ANCHORS,
                ):
                    self.options[key] = val

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
                    vol.Optional("x_y_z_m"): cv.string,
                    vol.Optional("x_m"): vol.Coerce(float),
                    vol.Optional("y_m"): vol.Coerce(float),
                    vol.Optional("z_m"): vol.Coerce(float),
                    vol.Optional("sample_radius_m"): vol.Coerce(float),
                    vol.Optional("room_radius_m"): vol.Coerce(float),
                    vol.Optional("duration_s", default=60): vol.All(vol.Coerce(int), vol.Range(min=1)),
                    vol.Optional("notes", default=""): cv.string,
                }
            ),
            SupportsResponse.OPTIONAL,
        )
        self.config_entry.async_on_unload(lambda: hass.services.async_remove(DOMAIN, "record_calibration_sample"))

        hass.services.async_register(
            DOMAIN,
            "record_transition_sample",
            self.service_record_transition_sample,
            vol.Schema(
                {
                    vol.Required("device_id"): cv.string,
                    vol.Required("room_area_id"): cv.string,
                    vol.Required("transition_name"): cv.string,
                    vol.Required("transition_floor_ids"): vol.All(cv.ensure_list, [cv.string]),
                    vol.Optional("x_y_z_m"): cv.string,
                    vol.Optional("x_m"): vol.Coerce(float),
                    vol.Optional("y_m"): vol.Coerce(float),
                    vol.Optional("z_m"): vol.Coerce(float),
                    vol.Optional("sample_radius_m", default=DEFAULT_SAMPLE_RADIUS_M): vol.Coerce(float),
                    vol.Optional("capture_duration_s", default=60): vol.All(vol.Coerce(int), vol.Range(min=1)),
                }
            ),
            SupportsResponse.OPTIONAL,
        )
        self.config_entry.async_on_unload(lambda: hass.services.async_remove(DOMAIN, "record_transition_sample"))

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
        await self.scanner_anchor_store.async_ensure_loaded()
        await self._transition_zone_store.async_load()
        await self._floor_config_store.async_load()
        await self._trilat_bootstrap_store.async_load()
        await self.calibration.async_initialize()
        migrated = await self.calibration.async_migrate_transition_samples_to_zones(
            self._transition_zone_store
        )
        if migrated:
            _LOGGER.debug("Migrated %d transition sample group(s) to TransitionZone store", migrated)
        self.calibration.register_change_callback(self.async_handle_calibration_samples_changed)
        self._restore_scanner_anchors_from_store()
        await self.async_handle_calibration_samples_changed()
        self._refresh_trilateration()
        self._refresh_areas_from_trilat()

    async def async_shutdown(self) -> None:
        """Tear down coordinator-owned subsystems."""
        await self._trilat_bootstrap_store.async_save()
        await self.calibration.async_shutdown()

    async def async_handle_calibration_samples_changed(self) -> None:
        """Rebuild sample-derived runtime helpers after calibration data changes."""
        await self.ranging_model.async_rebuild()
        await self.room_classifier.async_rebuild()
        self._async_manage_repair_calibration_layout_mismatch()

    async def async_handle_anchor_geometry_changed(self) -> None:
        """Re-evaluate repairs that depend on configured anchor geometry."""
        self._async_manage_repair_calibration_layout_mismatch()
        self._async_manage_repair_trilat_without_anchors(list(self.scanner_list))

    def _restore_scanner_anchor_from_store(self, scanner: BermudaDevice) -> bool:
        """Hydrate one scanner's anchor coordinates from Bermuda storage when available."""
        if not scanner.is_scanner:
            return False
        stored_coords = self.scanner_anchor_store.get_coordinates_if_loaded(scanner)
        if stored_coords is None:
            return False
        scanner.anchor_x_m = stored_coords.get("anchor_x_m")
        scanner.anchor_y_m = stored_coords.get("anchor_y_m")
        scanner.anchor_z_m = stored_coords.get("anchor_z_m")
        return True

    def _restore_scanner_anchors_from_store(self) -> None:
        """Hydrate scanner anchor coordinates from Bermuda storage into live scanner devices."""
        for scanner_address in list(self.scanner_list):
            scanner = self.devices.get(scanner_address)
            if scanner is None:
                continue
            self._restore_scanner_anchor_from_store(scanner)

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
        live_packet_count: int | None = None,
    ):
        """Estimate range for one advert using the sample-derived model."""
        scanner = self.devices.get(scanner_address)
        timestamp_health_penalty = self._timestamp_health_penalty(scanner)
        return self.ranging_model.estimate_range(
            layout_hash=self.current_anchor_layout_hash(),
            scanner_address=scanner_address,
            device_id=self.get_registry_id_for_device(device),
            filtered_rssi=filtered_rssi,
            live_rssi_dispersion=live_rssi_dispersion,
            live_packet_count=live_packet_count,
            timestamp_health_penalty=timestamp_health_penalty,
        )

    @staticmethod
    def _timestamp_health_penalty(scanner: BermudaDevice | None) -> float:
        """Map scanner timestamp health into an uncertainty inflation factor."""
        if scanner is None or not getattr(scanner, "is_scanner", False):
            return 0.0
        sync_state = scanner.timestamp_sync_diagnostics().get("state")
        if sync_state in {"local", "synchronized"}:
            return 0.0
        if sync_state == "recovered":
            return 0.08
        if sync_state == "drifting":
            return 0.25
        if sync_state == "unstable":
            return 0.75
        if sync_state == "broken":
            return 1.25
        return 0.0

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

    def trilat_cross_floor_penalty_db(self) -> float:
        """Return configured cross-floor RSSI penalty for floor evidence."""
        return float(
            self.options.get(
                CONF_TRILAT_CROSS_FLOOR_PENALTY_DB,
                DEFAULT_TRILAT_CROSS_FLOOR_PENALTY_DB,
            )
        )

    def trilat_soft_include_other_floor_anchors_enabled(self) -> bool:
        """Return whether Phase-2 other-floor soft inclusion is enabled."""
        return bool(
            self.options.get(
                CONF_TRILAT_SOFT_INCLUDE_OTHER_FLOOR_ANCHORS,
                DEFAULT_TRILAT_SOFT_INCLUDE_OTHER_FLOOR_ANCHORS,
            )
        )

    def trilat_reachability_gate_enabled(self) -> bool:
        return bool(self.options.get(CONF_TRILAT_REACHABILITY_GATE, DEFAULT_TRILAT_REACHABILITY_GATE))

    def get_floor_z_m(self, floor_id: str | None) -> float | None:
        """Return the configured floor surface Z height in metres, or None if unconfigured."""
        cfg = self._floor_config_store.get(floor_id)
        return cfg.floor_z_m if cfg is not None else None

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
            self._refresh_areas_from_trilat()
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

    def _refresh_areas_from_trilat(self) -> None:
        """Set room/area for tracked devices from trilat output."""
        layout_hash = self.current_anchor_layout_hash()
        for device in self.devices.values():
            if not device.create_sensor:
                continue
            state = self._get_trilat_decision_state(device)
            self._refresh_area_from_trilat(device, layout_hash)
            self._refresh_transition_sample_diagnostics(device, layout_hash)
            self._schedule_trilat_bootstrap_save(device, state, layout_hash=layout_hash)

    def _refresh_area_from_trilat(self, device: BermudaDevice, layout_hash: str) -> None:
        """Resolve one device room from trilat position and trained samples."""
        state = self._get_trilat_decision_state(device)
        nowstamp = monotonic_time_coarse()
        debug_this_device = debug_device_match(
            device.name,
            device.address,
            getattr(device, "prefname", None),
        )

        def _log_target_room_diag(*, stable_area_id: str | None, candidate_area_id: str | None) -> None:
            """Emit targeted room-classifier diagnostics for selected devices."""
            if not debug_this_device or device.diag_area_switch is None:
                return
            _LOGGER_TARGET_SPAM_LESS.debug(
                f"trilat_room_diag:{device.address}",
                "Trilat room diag: %s floor=%s stable=%s challenger=%s candidate=%s resolved=%s %s",
                device.name,
                device.trilat_floor_id,
                stable_area_id,
                state.room_challenger_id,
                candidate_area_id,
                device.area_id,
                device.diag_area_switch,
            )

        if (
            device.trilat_status not in {"ok", "low_confidence"}
            or device.trilat_x_m is None
            or device.trilat_y_m is None
        ):
            state.room_challenger_id = None
            state.room_challenger_since = 0.0
            device.diag_area_switch = "Hybrid room classification: trilat_unavailable"
            device.apply_position_classification(
                None,
                floor_id=device.trilat_floor_id,
                floor_name=device.trilat_floor_name,
                force_unknown=True,
            )
            _log_target_room_diag(stable_area_id=self._stable_area_id(device), candidate_area_id=None)
            return

        if not self.room_classifier.has_trained_rooms(layout_hash, device.trilat_floor_id):
            state.room_challenger_id = None
            state.room_challenger_since = 0.0
            device.diag_area_switch = "Hybrid room classification: no_trained_rooms"
            device.apply_position_classification(
                None,
                floor_id=device.trilat_floor_id,
                floor_name=device.trilat_floor_name,
                force_unknown=True,
            )
            _log_target_room_diag(stable_area_id=self._stable_area_id(device), candidate_area_id=None)
            return

        live_rssi_by_scanner: dict[str, float] = {}
        for advert in self._latest_adverts_by_scanner(device).values():
            if advert.stamp < nowstamp - DISTANCE_TIMEOUT:
                continue
            scanner = advert.scanner_device
            if scanner.floor_id != device.trilat_floor_id:
                continue
            window_rssi = getattr(advert, "rssi_window_median", None)
            if window_rssi is None:
                window_rssi = advert.rssi_filtered
            if window_rssi is None:
                continue
            live_rssi_by_scanner[advert.scanner_address.lower()] = float(window_rssi)

        classification = self.room_classifier.classify(
            layout_hash=layout_hash,
            floor_id=device.trilat_floor_id,
            x_m=device.trilat_x_m,
            y_m=device.trilat_y_m,
            z_m=device.trilat_z_m,
            live_rssi_by_scanner=live_rssi_by_scanner,
        )
        device.diag_area_switch = (
            "Hybrid room classification: "
            f"{classification.reason} "
            f"best={classification.best_area_id or 'none'} "
            f"score={classification.best_score:.2f} "
            f"geom={classification.geometry_score:.2f} "
            f"fp={classification.fingerprint_score:.2f} "
            f"second={classification.second_score:.2f} "
            f"topk_used={classification.topk_used}"
        )
        stable_area_id = self._stable_area_id(device)
        if classification.area_id is not None:
            if stable_area_id is not None and stable_area_id != classification.area_id:
                transition_strength = self._room_transition_strength(
                    layout_hash=layout_hash,
                    floor_id=device.trilat_floor_id,
                    from_area_id=stable_area_id,
                    to_area_id=classification.area_id,
                )
                required_dwell = self._room_switch_dwell_seconds(
                    classification,
                    transition_strength=transition_strength,
                )
                device.diag_area_switch += f" transition={transition_strength:.2f}"
                if state.room_challenger_id != classification.area_id:
                    state.room_challenger_id = classification.area_id
                    state.room_challenger_since = nowstamp
                    device.diag_area_switch += f" hold=room_switch_dwell({required_dwell:.1f}s)"
                    device.apply_position_classification(
                        stable_area_id,
                        floor_id=device.trilat_floor_id,
                        floor_name=device.trilat_floor_name,
                    )
                    _log_target_room_diag(
                        stable_area_id=stable_area_id,
                        candidate_area_id=classification.area_id,
                    )
                    return
                if nowstamp - state.room_challenger_since < required_dwell:
                    device.diag_area_switch += f" hold=room_switch_dwell({required_dwell:.1f}s)"
                    device.apply_position_classification(
                        stable_area_id,
                        floor_id=device.trilat_floor_id,
                        floor_name=device.trilat_floor_name,
                    )
                    _log_target_room_diag(
                        stable_area_id=stable_area_id,
                        candidate_area_id=classification.area_id,
                    )
                    return
            state.room_challenger_id = None
            state.room_challenger_since = 0.0
            device.apply_position_classification(
                classification.area_id,
                floor_id=device.trilat_floor_id,
                floor_name=device.trilat_floor_name,
            )
            _log_target_room_diag(
                stable_area_id=stable_area_id,
                candidate_area_id=classification.area_id,
            )
            return

        if stable_area_id is not None and classification.reason in {"weak_room_evidence", "room_ambiguity"}:
            state.room_challenger_id = None
            state.room_challenger_since = 0.0
            device.diag_area_switch += " hold=weak_evidence"
            device.apply_position_classification(
                stable_area_id,
                floor_id=device.trilat_floor_id,
                floor_name=device.trilat_floor_name,
            )
            _log_target_room_diag(stable_area_id=stable_area_id, candidate_area_id=classification.best_area_id)
            return

        state.room_challenger_id = None
        state.room_challenger_since = 0.0
        device.apply_position_classification(
            None,
            floor_id=device.trilat_floor_id,
            floor_name=device.trilat_floor_name,
            force_unknown=True,
        )
        _log_target_room_diag(stable_area_id=stable_area_id, candidate_area_id=classification.best_area_id)

    @staticmethod
    def _room_switch_dwell_seconds(classification, *, transition_strength: float = 1.0) -> float:
        """Return an evidence-based dwell for room switches."""
        margin = max(0.0, classification.best_score - classification.second_score)
        if classification.best_score >= 0.60 and margin >= 0.35:
            base = 1.5
        elif classification.best_score >= 0.40 and margin >= 0.20:
            base = 2.5
        else:
            base = 4.0

        if transition_strength >= 0.65:
            return base
        if transition_strength >= 0.35:
            return base + 1.5
        return base + 3.0

    def _room_transition_strength(
        self,
        *,
        layout_hash: str,
        floor_id: str | None,
        from_area_id: str | None,
        to_area_id: str | None,
    ) -> float:
        """Return soft plausibility for a room-to-room transition."""
        classifier = self.room_classifier
        strength_fn = getattr(classifier, "transition_strength", None)
        if strength_fn is None:
            return 1.0
        return float(
            strength_fn(
                layout_hash=layout_hash,
                floor_id=floor_id,
                from_area_id=from_area_id,
                to_area_id=to_area_id,
            )
        )

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
        last_floor_change_at: float = 0.0
        last_floor_change_from_id: str | None = None
        last_anchor_floor_roles: dict[str, str] = field(default_factory=dict)
        last_anchor_ids: tuple[str, ...] = ()
        last_anchor_ranges: dict[str, float] = field(default_factory=dict)
        last_anchor_z: dict[str, float | None] = field(default_factory=dict)
        last_solution_xy: tuple[float, float] | None = None
        last_solution_z: float | None = None
        velocity_x_mps: float = 0.0
        velocity_y_mps: float = 0.0
        velocity_z_mps: float = 0.0
        last_filter_stamp: float = 0.0
        last_solver_dimension: str = "2d"
        last_residual_m: float | None = None
        last_mean_sigma_m: float | None = None
        last_geometry_quality_01: float = 0.0
        last_residual_consistency_01: float = 0.0
        last_geometry_gdop: float | None = None
        last_geometry_condition: float | None = None
        last_normalized_residual_rms: float | None = None
        last_status: str = "unknown"
        room_challenger_id: str | None = None
        room_challenger_since: float = 0.0
        recent_transition_name: str | None = None
        recent_transition_room_area_id: str | None = None
        recent_transition_floor_ids: tuple[str, ...] = ()
        recent_transition_support_01: float = 0.0
        recent_transition_seen_at: float = 0.0
        # Last known position recorded while geometry quality was acceptable (updated every cycle).
        # Used as the challenger reference — more reliable than the position at challenger onset,
        # which may already be degraded by the time the challenger appears.
        last_good_position: tuple[float, float, float] | None = None
        last_good_position_at: float = 0.0
        # Challenger reference position (frozen from last_good_position at challenger onset)
        challenger_reference_position: tuple[float, float, float] | None = None
        challenger_onset_time: float = 0.0
        challenger_motion_budget_m: float = 0.0
        # Floor confidence for gate bypass logic
        floor_confidence: float = 0.0
        # Per-zone traversal tracking: zone_id -> (entry_at, exit_at); exit_at=0.0 means in-zone
        zone_traversal_history: dict = field(default_factory=dict)
        zone_entry_scores: dict = field(default_factory=dict)
        # Timestamp of the last cycle where fingerprint_switch_veto fired.  Used to
        # persist the veto for a short hold window so that a single cycle of weak
        # fp_conf cannot sneak a floor switch through.
        last_fingerprint_veto_at: float = 0.0
        bootstrap_applied: bool = False
        bootstrap_restored_at: float = 0.0
        bootstrap_hold_until: float = 0.0

    _TRILAT_MIN_ANCHORS: int = 3
    _TRILAT_MIN_ANCHORS_3D: int = 4
    _TRILAT_MAX_RESIDUAL_M: float = 5.0
    _TRILAT_MAX_ANCHOR_SIGMA_M: float = 6.0
    _TRILAT_DEFAULT_ANCHOR_SIGMA_M: float = 8.0
    _TRILAT_DIAGNOSTIC_OTHER_FLOOR_SIGMA_MULTIPLIER: float = 4.0
    _TRILAT_FLOOR_AMBIGUITY_RATIO: float = 0.2
    _TRILAT_FINGERPRINT_FLOOR_CONFIDENCE_HIGH: float = 0.70
    _TRILAT_FINGERPRINT_FLOOR_CONFIDENCE_MODERATE: float = 0.55
    _TRILAT_FINGERPRINT_FLOOR_SCORE_RATIO_HOLD: float = 1.25
    # After the fingerprint veto fires, hold the veto active for this many seconds
    # even if fp_conf momentarily drops below the support threshold on a single cycle.
    _FINGERPRINT_VETO_HOLD_S: float = 8.0
    _TRILAT_TRANSITION_SUPPORT_REQUIRED: float = 0.60
    _TRILAT_RECENT_TRANSITION_WINDOW_S: float = 20.0
    _TRILAT_FLOOR_SWITCH_PRIOR_WINDOW_S: float = 12.0
    _TRILAT_FLOOR_SWITCH_PRIOR_SIGMA_MULTIPLIER: float = 2.5
    _TRILAT_RANGE_DELTA_EPSILON_M: float = 0.2
    _TRILAT_MAX_POSITION_SPEED_MPS: float = 5.0
    _TRILAT_MAX_VERTICAL_SPEED_MPS: float = 1.5
    _TRILAT_MAX_FILTER_DT_S: float = 5.0
    _CHALLENGER_REFERENCE_QUALITY_THRESHOLD: float = 0.30
    _CHALLENGER_UNCERTAINTY_BUDGET_M: float = 1.5
    _CHALLENGER_MAX_MOTION_BUDGET_M: float = 3.0
    _FLOOR_CONFIDENCE_HIGH_THRESHOLD: float = 0.65
    _FLOOR_CONFIDENCE_GATE_THRESHOLD: float = 0.50
    _ZONE_ENTRY_THRESHOLD: float = 0.45
    _ZONE_EXIT_THRESHOLD: float = 0.20
    _ZONE_TRAVERSAL_RECENCY_S: float = 30.0
    _TRILAT_BOOTSTRAP_MAX_AGE_S: float = 6 * 3600.0
    _TRILAT_BOOTSTRAP_HOLD_S: float = 60.0

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
    def _trilat_age_sigma_multiplier(advert_age_s: float) -> float:
        """Softly inflate anchor uncertainty as the underlying advert gets older."""
        if advert_age_s <= 0.5:
            return 1.0
        return 1.0 + min(2.0, math.sqrt(max(0.0, advert_age_s - 0.5) / 4.0))

    @staticmethod
    def _trilat_confidence_band(score: float) -> str:
        """Map a 0..10 confidence score into a coarse label."""
        if score >= 7.0:
            return "high"
        if score >= 4.0:
            return "medium"
        return "low"

    @staticmethod
    def _quality_score_to_sensor_value(score_01: float) -> float:
        """Convert a 0..1 quality score into a 0..10 diagnostic value."""
        return round(max(0.0, min(1.0, score_01)) * 10.0, 1)

    def _set_trilat_quality_metrics(
        self,
        device: BermudaDevice,
        *,
        geometry_quality_01: float,
        residual_consistency_01: float,
        gdop: float | None,
        condition_number: float | None,
        normalized_residual_rms: float | None,
    ) -> None:
        """Store solve-quality metrics on the device for diagnostics and history."""
        device.trilat_geometry_quality = self._quality_score_to_sensor_value(geometry_quality_01)
        device.trilat_residual_consistency = self._quality_score_to_sensor_value(residual_consistency_01)
        device.trilat_geometry_gdop = gdop
        device.trilat_geometry_condition = condition_number
        device.trilat_normalized_residual_rms = normalized_residual_rms

    def _clear_trilat_quality_metrics(self, device: BermudaDevice) -> None:
        """Reset solve-quality diagnostics on the device."""
        self._set_trilat_quality_metrics(
            device,
            geometry_quality_01=0.0,
            residual_consistency_01=0.0,
            gdop=None,
            condition_number=None,
            normalized_residual_rms=None,
        )

    @staticmethod
    def _compute_trilat_quality_metrics(
        anchors: list[AnchorMeasurement],
        *,
        solver_dimension: str,
        x_m: float | None,
        y_m: float | None,
        z_m: float | None,
    ) -> SolveQualityMetrics:
        """Compute geometry and residual-consistency metrics for a solved point."""
        if x_m is None or y_m is None:
            return SolveQualityMetrics(
                geometry_quality_01=0.0,
                residual_consistency_01=0.0,
                gdop=None,
                condition_number=None,
                normalized_residual_rms=None,
            )
        if solver_dimension == "3d" and z_m is not None:
            return solve_quality_metrics_3d(x_m, y_m, z_m, anchors)
        return solve_quality_metrics_2d(x_m, y_m, anchors)

    def _set_trilat_confidence(self, device: BermudaDevice, score: float) -> None:
        """Store trilateration confidence on the device."""
        clamped = round(max(0.0, min(10.0, score)), 1)
        device.trilat_confidence = clamped
        device.trilat_confidence_level = self._trilat_confidence_band(clamped)

    def _set_tracking_confidence(self, device: BermudaDevice, score: float) -> None:
        """Store tracked-position confidence on the device."""
        clamped = round(max(0.0, min(10.0, score)), 1)
        device.trilat_tracking_confidence = clamped
        device.trilat_tracking_confidence_level = self._trilat_confidence_band(clamped)

    def _compute_trilat_confidence(
        self,
        anchor_count: int,
        residual_m: float | None,
        solver_dimension: str,
        geometry_quality_01: float = 0.0,
        residual_consistency_01: float = 0.0,
        floor_ambiguous: bool = False,
        mean_sigma_m: float | None = None,
    ) -> float:
        """Compute a conservative 0..10 confidence score for the current estimate."""
        residual_term = 0.0
        if residual_m is not None:
            residual_ref = max(1.0, (mean_sigma_m or 1.0) * (2.0 if solver_dimension == "2d" else 1.6))
            residual_term = 1.0 / (1.0 + ((residual_m / residual_ref) ** 2))
        anchor_target = 6.0 if solver_dimension == "3d" else 4.0
        anchor_term = min(float(anchor_count) / anchor_target, 1.0)
        score = (
            (3.2 * residual_term)
            + (1.4 * anchor_term)
            + (2.7 * max(0.0, min(1.0, geometry_quality_01)))
            + (2.7 * max(0.0, min(1.0, residual_consistency_01)))
            + (0.5 if solver_dimension == "3d" else 0.0)
        )
        if mean_sigma_m is not None:
            sigma_term = 1.0 / (1.0 + max(0.0, mean_sigma_m - 1.0) / 2.5)
            score = (score * 0.85) + (1.5 * sigma_term)
        if floor_ambiguous:
            score -= 1.5
        return score

    def _compute_tracking_confidence(
        self,
        *,
        raw_score: float,
        state: TrilatDecisionState,
        mobility_type: str,
        used_prior: bool,
        mean_anchor_range_delta_m: float | None,
        geometry_quality_01: float = 0.0,
        residual_consistency_01: float = 0.0,
        floor_ambiguous: bool = False,
    ) -> float:
        """Estimate confidence in the filtered tracked position rather than the raw solve."""
        raw_component = 0.35 * max(0.0, min(10.0, raw_score))
        quality_component = 2.1 * max(0.0, min(1.0, geometry_quality_01))
        quality_component += 2.1 * max(0.0, min(1.0, residual_consistency_01))

        horizontal_speed = math.hypot(state.velocity_x_mps, state.velocity_y_mps)
        vertical_speed = abs(state.velocity_z_mps)

        if mobility_type == MOBILITY_STATIONARY:
            horizontal_ref = 0.35
            vertical_ref = 0.15
        else:
            horizontal_ref = 1.50
            vertical_ref = 0.60

        horizontal_stability = max(0.0, 1.0 - min(1.0, horizontal_speed / horizontal_ref))
        vertical_stability = max(0.0, 1.0 - min(1.0, vertical_speed / vertical_ref))
        stability_component = (3.2 * horizontal_stability) + (1.3 * vertical_stability)

        continuity_component = 1.8
        if mean_anchor_range_delta_m is not None:
            continuity_component *= max(0.0, 1.0 - min(1.0, mean_anchor_range_delta_m / 3.0))

        prior_component = 1.0 if used_prior else 0.0
        score = raw_component + quality_component + stability_component + continuity_component + prior_component
        if floor_ambiguous:
            score -= 1.0
        return score

    @staticmethod
    def _format_anchor_status_entry(entry: dict[str, object]) -> str:
        """Render one scanner advert-status line for HA attributes."""
        line = f"{entry['scanner_name']}: {entry['status']}"
        details: list[str] = []
        sync_state = entry.get("sync_state")
        if sync_state not in (None, "synchronized", "local", "not_scanner"):
            details.append(f"sync={sync_state}")
        if entry.get("status") == "rejected_wrong_floor":
            selected_floor_id = entry.get("selected_floor_id")
            scanner_floor_id = entry.get("scanner_floor_id")
            if selected_floor_id is not None:
                details.append(f"selected={selected_floor_id}")
            if scanner_floor_id is not None:
                details.append(f"scanner={scanner_floor_id}")
        soft_sigma_m = entry.get("soft_include_sigma_m")
        if soft_sigma_m is not None:
            details.append(f"soft_sigma={float(soft_sigma_m):.2f}m")
        if entry.get("soft_include_active"):
            details.append("soft_included")
        if details:
            line += f" ({', '.join(details)})"
        return line

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

    def _get_trilat_decision_state(self, device: BermudaDevice) -> TrilatDecisionState:
        """Return mutable state holder for trilat/floor smoothing."""
        state = self._trilat_decision_state.get(device.address)
        if state is None:
            state = self.TrilatDecisionState()
            self._trilat_decision_state[device.address] = state
            self._restore_trilat_bootstrap_state(device, state)
        return state

    def _restore_trilat_bootstrap_state(
        self,
        device: BermudaDevice,
        state: TrilatDecisionState,
    ) -> None:
        """Warm-start floor/geometry state from the last trusted trilat solution."""
        if state.bootstrap_applied:
            return
        record = self._trilat_bootstrap_store.get(device.address)
        if record is None:
            state.bootstrap_applied = True
            return
        layout_hash = self.current_anchor_layout_hash()
        layout_matches = not (record.layout_hash and layout_hash and record.layout_hash != layout_hash)
        try:
            saved_at = datetime.fromisoformat(record.saved_at)
        except ValueError:
            state.bootstrap_applied = True
            return
        record_age_s = (now() - saved_at).total_seconds()
        if record_age_s < 0.0 or record_age_s > self._TRILAT_BOOTSTRAP_MAX_AGE_S:
            state.bootstrap_applied = True
            return
        state.bootstrap_applied = True
        nowstamp = monotonic_time_coarse()
        state.floor_id = record.floor_id
        # Restore the previous floor as a startup prior, but leave confidence at 0 so
        # the reachability gate does not hard-lock a legitimate move that happened while
        # Home Assistant was down.
        state.floor_confidence = 0.0
        state.bootstrap_restored_at = nowstamp
        state.bootstrap_hold_until = nowstamp + self._TRILAT_BOOTSTRAP_HOLD_S
        if layout_matches:
            state.last_filter_stamp = nowstamp
            state.last_solution_xy = (record.x_m, record.y_m)
            state.last_solution_z = record.z_m
            if record.z_m is not None:
                state.last_good_position = (record.x_m, record.y_m, record.z_m)
                state.last_good_position_at = nowstamp
                state.last_solver_dimension = "3d"
            else:
                state.last_solver_dimension = "2d"
            if record.area_id:
                device.area_last_seen_id = record.area_id
                resolved_area_name = self.resolve_area_name(record.area_id)
                if resolved_area_name:
                    device.area_last_seen = resolved_area_name

    def _schedule_trilat_bootstrap_save(
        self,
        device: BermudaDevice,
        state: TrilatDecisionState,
        *,
        layout_hash: str,
    ) -> None:
        """Persist the last trusted trilat state for restart bootstrap."""
        if (
            device.trilat_status != "ok"
            or device.trilat_floor_id is None
            or device.trilat_x_m is None
            or device.trilat_y_m is None
        ):
            return
        geometry_quality_01 = max(0.0, min(1.0, float(device.trilat_geometry_quality or 0.0) / 10.0))
        if geometry_quality_01 < self._CHALLENGER_REFERENCE_QUALITY_THRESHOLD:
            return
        floor_diag = getattr(device, "trilat_floor_diagnostics", {})
        fingerprint_floor_id = floor_diag.get("fingerprint_floor_id")
        fingerprint_floor_conf = float(floor_diag.get("fingerprint_floor_confidence") or 0.0)
        if fingerprint_floor_id:
            if fingerprint_floor_id != device.trilat_floor_id:
                return
            if fingerprint_floor_conf < self._TRILAT_FINGERPRINT_FLOOR_CONFIDENCE_MODERATE:
                return
        stable_area_id = self._stable_area_id(device)
        self._trilat_bootstrap_store.schedule_save(
            device.address,
            TrilatBootstrapRecord(
                saved_at=now().isoformat(),
                floor_id=device.trilat_floor_id,
                area_id=stable_area_id,
                x_m=float(device.trilat_x_m),
                y_m=float(device.trilat_y_m),
                z_m=float(device.trilat_z_m) if device.trilat_z_m is not None else None,
                layout_hash=layout_hash,
                floor_confidence=float(state.floor_confidence),
                geometry_quality_01=geometry_quality_01,
            ),
        )

    def _apply_trilat_motion_filter(
        self,
        state: TrilatDecisionState,
        *,
        nowstamp: float,
        mobility_type: str,
        measurement_xy: tuple[float, float],
        measurement_z: float | None,
        anchor_z_bounds: tuple[float, float] | None,
        residual_m: float | None,
        mean_sigma_m: float | None,
    ) -> tuple[tuple[float, float], float | None]:
        """Apply a motion-constrained filter to raw trilat output."""
        if state.last_solution_xy is None or state.last_filter_stamp <= 0.0:
            state.velocity_x_mps = 0.0
            state.velocity_y_mps = 0.0
            state.velocity_z_mps = 0.0
            state.last_filter_stamp = nowstamp
            filtered_z = measurement_z if measurement_z is not None else state.last_solution_z
            return measurement_xy, filtered_z

        dt = nowstamp - state.last_filter_stamp
        if dt <= 0.0:
            state.velocity_x_mps = 0.0
            state.velocity_y_mps = 0.0
            state.velocity_z_mps = 0.0
            state.last_filter_stamp = nowstamp
            filtered_z = measurement_z if measurement_z is not None else state.last_solution_z
            return measurement_xy, filtered_z
        if dt > self._TRILAT_MAX_FILTER_DT_S:
            dt = self._TRILAT_MAX_FILTER_DT_S
            state.velocity_x_mps = 0.0
            state.velocity_y_mps = 0.0
            state.velocity_z_mps = 0.0

        residual_factor = 1.0
        if residual_m is not None:
            residual_factor = max(0.0, 1.0 - (residual_m / self._TRILAT_MAX_RESIDUAL_M))
        sigma_factor = 1.0
        if mean_sigma_m is not None:
            sigma_factor = max(0.0, 1.0 - (mean_sigma_m / self._TRILAT_MAX_ANCHOR_SIGMA_M))
        gain_scale = (0.2 + (0.8 * residual_factor)) * (0.5 + (0.5 * sigma_factor))

        if mobility_type == MOBILITY_STATIONARY:
            alpha_xy = 0.18 * gain_scale
            beta_xy = 0.06 * gain_scale
            alpha_z = 0.08 * gain_scale
            beta_z = 0.03 * gain_scale
        else:
            alpha_xy = 0.35 * gain_scale
            beta_xy = 0.12 * gain_scale
            alpha_z = 0.16 * gain_scale
            beta_z = 0.05 * gain_scale

        alpha_xy = max(0.05, min(alpha_xy, 0.85))
        beta_xy = max(0.01, min(beta_xy, 0.30))
        alpha_z = max(0.03, min(alpha_z, 0.50))
        beta_z = max(0.01, min(beta_z, 0.20))

        prev_x, prev_y = state.last_solution_xy
        pred_x = prev_x + (state.velocity_x_mps * dt)
        pred_y = prev_y + (state.velocity_y_mps * dt)
        dx = measurement_xy[0] - pred_x
        dy = measurement_xy[1] - pred_y
        distance_to_prediction = math.hypot(dx, dy)
        max_xy_step = self._TRILAT_MAX_POSITION_SPEED_MPS * dt
        if distance_to_prediction > max_xy_step and distance_to_prediction > 0.0:
            scale = max_xy_step / distance_to_prediction
            dx *= scale
            dy *= scale
        innovation_x = dx
        innovation_y = dy
        filtered_x = pred_x + (alpha_xy * innovation_x)
        filtered_y = pred_y + (alpha_xy * innovation_y)
        velocity_x = state.velocity_x_mps + ((beta_xy * innovation_x) / dt)
        velocity_y = state.velocity_y_mps + ((beta_xy * innovation_y) / dt)
        xy_speed = math.hypot(velocity_x, velocity_y)
        if xy_speed > self._TRILAT_MAX_POSITION_SPEED_MPS and xy_speed > 0.0:
            scale = self._TRILAT_MAX_POSITION_SPEED_MPS / xy_speed
            velocity_x *= scale
            velocity_y *= scale

        filtered_z = state.last_solution_z
        velocity_z = state.velocity_z_mps * 0.5
        if measurement_z is not None:
            measurement_z = self._apply_soft_vertical_prior(measurement_z, anchor_z_bounds)
            if state.last_solution_z is None:
                filtered_z = measurement_z
                velocity_z = 0.0
            else:
                pred_z = state.last_solution_z + (state.velocity_z_mps * dt)
                dz = measurement_z - pred_z
                max_z_step = self._TRILAT_MAX_VERTICAL_SPEED_MPS * dt
                if abs(dz) > max_z_step:
                    dz = math.copysign(max_z_step, dz)
                filtered_z = pred_z + (alpha_z * dz)
                velocity_z = state.velocity_z_mps + ((beta_z * dz) / dt)
                velocity_z = max(
                    -self._TRILAT_MAX_VERTICAL_SPEED_MPS,
                    min(self._TRILAT_MAX_VERTICAL_SPEED_MPS, velocity_z),
                )
        elif filtered_z is not None and anchor_z_bounds is not None:
            filtered_z = self._apply_soft_vertical_prior(filtered_z, anchor_z_bounds)

        state.velocity_x_mps = velocity_x
        state.velocity_y_mps = velocity_y
        state.velocity_z_mps = velocity_z
        state.last_filter_stamp = nowstamp
        return (filtered_x, filtered_y), filtered_z

    # Phone-height band: a phone is almost always held between floor level and 1.2 m above it.
    # The prior centre is mid-band (floor_z + 0.6 m); sigma is tight (0.4 m) so it meaningfully
    # constrains Z without preventing the solve from going outside the band under strong evidence.
    _PHONE_HEIGHT_Z_CENTER_M: float = 0.6
    _PHONE_HEIGHT_Z_SIGMA_M: float = 0.4

    def _build_trilat_solve_prior(
        self,
        state: TrilatDecisionState,
        *,
        nowstamp: float,
        mobility_type: str,
        solver_dimension: str,
        selected_floor_id: str | None,
        mean_sigma_m: float | None,
        mean_anchor_range_delta_m: float | None,
        floor_z_m: float | None = None,
        layout_hash: str = "",
    ) -> SolvePrior2D | SolvePrior3D | None:
        """Build a soft predicted-position prior for the next trilat solve.

        When *floor_z_m* is provided (Step 10) the phone-height band prior
        [floor_z_m, floor_z_m + 1.2 m] is combined with the motion-based Z
        estimate via a Gaussian product.  When there is no prior Z history but
        floor_z_m is known, the phone-height prior seeds the initial estimate.

        When *layout_hash* is provided (Step 12) the predicted XY is soft-
        clamped to the per-floor calibration envelope so the solver is always
        anchored within the known calibrated space.
        """
        # --- XY motion prior ------------------------------------------------
        # Require a previous solution and recent stamp to build an XY prior.
        has_xy_prior = (
            state.last_solution_xy is not None
            and state.last_filter_stamp > 0.0
            and (selected_floor_id is not None)
            and (state.floor_id is not None)
            and (selected_floor_id == state.floor_id)
        )
        if not has_xy_prior:
            # No XY prior, but we may still inject a Z-only phone-height prior
            # for 3D solves when floor_z_m is known.
            if solver_dimension == "3d" and floor_z_m is not None:
                return SolvePrior3D(
                    x_m=0.0,
                    y_m=0.0,
                    z_m=floor_z_m + self._PHONE_HEIGHT_Z_CENTER_M,
                    sigma_x_m=1e6,  # effectively unconstrained XY
                    sigma_y_m=1e6,
                    sigma_z_m=self._PHONE_HEIGHT_Z_SIGMA_M,
                )
            return None

        raw_dt = nowstamp - state.last_filter_stamp
        if raw_dt > self._TRILAT_MAX_FILTER_DT_S:
            return None
        if mean_anchor_range_delta_m is not None and mean_anchor_range_delta_m > (self._TRILAT_MAX_RESIDUAL_M * 2.0):
            return None

        dt = max(0.0, min(raw_dt, self._TRILAT_MAX_FILTER_DT_S))

        predicted_x = state.last_solution_xy[0] + (state.velocity_x_mps * dt)
        predicted_y = state.last_solution_xy[1] + (state.velocity_y_mps * dt)

        # Step 12: soft-clamp predicted XY to the per-floor calibration envelope.
        if layout_hash and selected_floor_id:
            room_classifier = getattr(self, "room_classifier", None)
            if room_classifier is not None:
                envelope = room_classifier.floor_xy_envelope(layout_hash, selected_floor_id)
                if envelope is not None:
                    x_min, x_max, y_min, y_max = envelope
                    predicted_x = max(x_min, min(x_max, predicted_x))
                    predicted_y = max(y_min, min(y_max, predicted_y))

        residual_term = min(self._TRILAT_MAX_RESIDUAL_M, max(0.0, state.last_residual_m or 0.0))
        sigma_term = min(self._TRILAT_DEFAULT_ANCHOR_SIGMA_M, max(0.0, mean_sigma_m or state.last_mean_sigma_m or 0.0))
        range_delta_term = min(12.0, max(0.0, mean_anchor_range_delta_m or 0.0))
        speed_xy = math.hypot(state.velocity_x_mps, state.velocity_y_mps)
        status_multiplier = 1.0 if state.last_status == "ok" else 1.7

        if mobility_type == MOBILITY_STATIONARY:
            base_xy_sigma = 0.45
            base_z_sigma = 0.60
            dt_xy_term = 0.18 * dt
            dt_z_term = 0.10 * dt
        else:
            base_xy_sigma = 0.95
            base_z_sigma = 1.15
            dt_xy_term = 0.35 * dt
            dt_z_term = 0.18 * dt

        sigma_xy = (
            base_xy_sigma
            + dt_xy_term
            + min(2.0, speed_xy * dt * 0.35)
            + (0.22 * residual_term)
            + (0.10 * sigma_term)
            + (0.28 * range_delta_term)
        ) * status_multiplier
        sigma_xy *= self._floor_switch_prior_sigma_scale(
            state,
            nowstamp=nowstamp,
            mobility_type=mobility_type,
        )
        sigma_xy = max(0.25, sigma_xy)

        if solver_dimension == "3d":
            if state.last_solution_z is None:
                if floor_z_m is not None:
                    # Bootstrap: no motion Z, but floor height is known — seed with phone-height.
                    return SolvePrior3D(
                        x_m=predicted_x,
                        y_m=predicted_y,
                        z_m=floor_z_m + self._PHONE_HEIGHT_Z_CENTER_M,
                        sigma_x_m=sigma_xy,
                        sigma_y_m=sigma_xy,
                        sigma_z_m=self._PHONE_HEIGHT_Z_SIGMA_M,
                    )
                return None
            predicted_z = state.last_solution_z + (state.velocity_z_mps * dt)
            sigma_z = (
                base_z_sigma
                + dt_z_term
                + min(1.5, abs(state.velocity_z_mps) * dt * 0.30)
                + (0.15 * residual_term)
                + (0.08 * sigma_term)
                + (0.12 * range_delta_term)
            ) * status_multiplier
            sigma_z *= self._floor_switch_prior_sigma_scale(
                state,
                nowstamp=nowstamp,
                mobility_type=mobility_type,
            )
            sigma_z = max(0.35, sigma_z)

            # Step 10: combine motion Z prior with phone-height band prior (Gaussian product).
            if floor_z_m is not None:
                phone_z = floor_z_m + self._PHONE_HEIGHT_Z_CENTER_M
                sigma_phone = self._PHONE_HEIGHT_Z_SIGMA_M
                inv_var_motion = 1.0 / (sigma_z * sigma_z)
                inv_var_phone = 1.0 / (sigma_phone * sigma_phone)
                total_inv_var = inv_var_motion + inv_var_phone
                predicted_z = (predicted_z * inv_var_motion + phone_z * inv_var_phone) / total_inv_var
                sigma_z = math.sqrt(1.0 / total_inv_var)

            return SolvePrior3D(
                x_m=predicted_x,
                y_m=predicted_y,
                z_m=predicted_z,
                sigma_x_m=sigma_xy,
                sigma_y_m=sigma_xy,
                sigma_z_m=sigma_z,
            )

        return SolvePrior2D(
            x_m=predicted_x,
            y_m=predicted_y,
            sigma_x_m=sigma_xy,
            sigma_y_m=sigma_xy,
        )

    def _floor_switch_prior_sigma_scale(
        self,
        state: TrilatDecisionState,
        *,
        nowstamp: float,
        mobility_type: str,
    ) -> float:
        """Weaken the carried prior briefly after a floor switch instead of dropping it."""
        if state.last_floor_change_at <= 0.0:
            return 1.0
        age_s = max(0.0, nowstamp - state.last_floor_change_at)
        window_s = self._TRILAT_FLOOR_SWITCH_PRIOR_WINDOW_S
        if mobility_type == MOBILITY_STATIONARY:
            window_s *= 1.5
        if age_s >= window_s:
            return 1.0
        remaining = 1.0 - (age_s / max(window_s, 1e-9))
        return 1.0 + (
            (self._TRILAT_FLOOR_SWITCH_PRIOR_SIGMA_MULTIPLIER - 1.0)
            * remaining
        )

    @staticmethod
    def _apply_soft_vertical_prior(
        z_value: float,
        anchor_z_bounds: tuple[float, float] | None,
    ) -> float:
        """Softly pull z toward the anchor-height band instead of hard-clamping it."""
        if anchor_z_bounds is None:
            return z_value

        min_anchor_z, max_anchor_z = anchor_z_bounds
        z_span = max(0.0, max_anchor_z - min_anchor_z)
        z_margin = max(0.5, z_span * 0.5)
        comfort_low = min_anchor_z - z_margin
        comfort_high = max_anchor_z + z_margin

        if comfort_low <= z_value <= comfort_high:
            return z_value

        nearest_edge = comfort_low if z_value < comfort_low else comfort_high
        excess = abs(z_value - nearest_edge)
        sigma = max(0.75, z_margin * 1.5)
        pull_weight = 1.0 - math.exp(-0.5 * ((excess / sigma) ** 2))
        return z_value + (pull_weight * (nearest_edge - z_value))

    @staticmethod
    def _set_trilat_speed_diagnostics(device: BermudaDevice, state: TrilatDecisionState) -> None:
        """Expose current filtered motion estimates on the device."""
        device.trilat_horizontal_speed_mps = math.hypot(state.velocity_x_mps, state.velocity_y_mps)
        device.trilat_vertical_speed_mps = abs(state.velocity_z_mps)

    @staticmethod
    def _latest_adverts_by_scanner(device: BermudaDevice):
        """Return latest advert per scanner for the device."""
        latest: dict[str, BermudaAdvert] = {}
        for advert in device.adverts.values():
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

    def _async_manage_repair_calibration_layout_mismatch(self) -> None:
        """Raise or clear repair when stored calibration samples don't match the current anchor layout."""
        mismatch = self.calibration.get_layout_mismatch_summary()
        if mismatch is None:
            if self._calibration_layout_mismatch_signature is not None:
                ir.async_delete_issue(self.hass, DOMAIN, REPAIR_CALIBRATION_LAYOUT_MISMATCH)
                self._calibration_layout_mismatch_signature = None
                # Mismatch was preventing calibration data from loading; rebuild now that
                # the layout hash has stabilised so the room classifier can find samples.
                self.hass.async_create_task(self.room_classifier.async_rebuild())
            return

        signature = "|".join(
            [
                mismatch["current_layout_hash"],
                mismatch["dominant_layout_hash"],
                str(mismatch["sample_count"]),
                mismatch["changed_anchor_lines"],
            ]
        )
        if signature == self._calibration_layout_mismatch_signature:
            return

        _LOGGER.warning(
            "Calibration layout mismatch detected; %s saved sample(s) match layout %s but current anchors are %s. "
            "Anchor coordinate changes:\n%s",
            mismatch["sample_count"],
            mismatch["dominant_layout_hash"][:8],
            mismatch["current_layout_hash"][:8],
            mismatch["changed_anchor_lines"],
        )
        ir.async_delete_issue(self.hass, DOMAIN, REPAIR_CALIBRATION_LAYOUT_MISMATCH)
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            REPAIR_CALIBRATION_LAYOUT_MISMATCH,
            data={"entry_id": self.config_entry.entry_id},
            is_fixable=True,
            is_persistent=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key=REPAIR_CALIBRATION_LAYOUT_MISMATCH,
            translation_placeholders={
                "sample_count": str(mismatch["sample_count"]),
                "current_layout_hash": mismatch["current_layout_hash"][:8],
                "dominant_layout_hash": mismatch["dominant_layout_hash"][:8],
                "dominant_layout_count": str(mismatch["dominant_layout_count"]),
                "changed_anchor_lines": mismatch["changed_anchor_lines"],
            },
        )
        self._calibration_layout_mismatch_signature = signature

    def _stable_area_id(self, device: BermudaDevice) -> str | None:
        """Return the most recent stable area id."""
        if device.area_id is not None and not device.area_is_unknown:
            return device.area_id
        return device.area_last_seen_id

    def _refresh_transition_sample_diagnostics(self, device: BermudaDevice, layout_hash: str) -> None:
        """Attach transition-sample proximity diagnostics to the current floor state."""
        if not hasattr(self.calibration, "transition_support_diagnostics"):
            return
        stable_area_id = self._stable_area_id(device)
        state = self._get_trilat_decision_state(device)
        transition_diag = self.calibration.transition_support_diagnostics(
            layout_hash=layout_hash,
            x_m=device.trilat_x_m,
            y_m=device.trilat_y_m,
            z_m=device.trilat_z_m,
            room_area_id=stable_area_id,
            challenger_floor_id=state.floor_challenger_id,
            geometry_quality_01=max(0.0, min(1.0, float(device.trilat_geometry_quality or 0.0) / 10.0)),
        )
        transition_diag["transition_room_context_name"] = self.resolve_area_name(stable_area_id)
        challenger_floor_id = transition_diag.get("transition_challenger_floor_id")
        transition_diag["transition_challenger_floor_name"] = self._resolve_floor_name(
            str(challenger_floor_id) if challenger_floor_id else None
        )
        best_room_area_id = transition_diag.get("transition_best_room_area_id")
        transition_diag["transition_best_room_name"] = self.resolve_area_name(best_room_area_id)
        transition_diag["transition_best_floor_names"] = [
            self._resolve_floor_name(str(floor_id))
            for floor_id in transition_diag.get("transition_best_floor_ids", [])
        ]
        device.trilat_floor_diagnostics.update(transition_diag)

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

    def _refresh_trilateration(self) -> None:
        """Refresh trilateration diagnostics for all tracked devices."""
        self._async_manage_repair_calibration_layout_mismatch()
        configured_anchor_scanners: list[str] = []
        for scanner in self._scanners:
            if (
                self.get_scanner_anchor_x(scanner.address) is not None
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

    def _update_floor_confidence(
        self,
        state: "BermudaDataUpdateCoordinator.TrilatDecisionState",
        *,
        selected_floor_id: str | None,
        floor_evidence: dict,
        floor_ambiguity: bool,
    ) -> None:
        """Update floor_confidence after each inference cycle."""
        if selected_floor_id is None or floor_ambiguity:
            state.floor_confidence = max(0.0, state.floor_confidence - 0.05)
            return
        total = sum(floor_evidence.values())
        if total <= 0.0:
            state.floor_confidence = max(0.0, state.floor_confidence - 0.05)
            return
        best = floor_evidence.get(selected_floor_id, 0.0)
        rssi_ratio = best / total
        if rssi_ratio >= self._FLOOR_CONFIDENCE_HIGH_THRESHOLD:
            state.floor_confidence = min(1.0, state.floor_confidence + 0.04)
        else:
            state.floor_confidence = max(0.0, state.floor_confidence - 0.02)

    def _update_zone_traversal_tracker(
        self,
        state: "BermudaDataUpdateCoordinator.TrilatDecisionState",
        *,
        nowstamp: float,
        x_m: float,
        y_m: float,
        z_m: float,
        geometry_quality_01: float,
        layout_hash: str,
    ) -> None:
        """Track zone entry/exit for background traversal detection. High-quality solves only."""
        if geometry_quality_01 < self._CHALLENGER_REFERENCE_QUALITY_THRESHOLD:
            return
        # Update last known good position — used as challenger reference, not the onset position
        state.last_good_position = (x_m, y_m, z_m)
        state.last_good_position_at = nowstamp
        for zone in self._transition_zone_store.zones:
            if zone.anchor_layout_hash != layout_hash:
                continue
            score = zone.score(x_m, y_m, z_m)
            prev_score = state.zone_entry_scores.get(zone.zone_id, 0.0)
            state.zone_entry_scores[zone.zone_id] = score
            history = state.zone_traversal_history.get(zone.zone_id, (0.0, 0.0))
            entry_at, exit_at = history
            # Entry: crossed above threshold
            if prev_score < self._ZONE_ENTRY_THRESHOLD <= score:
                state.zone_traversal_history[zone.zone_id] = (nowstamp, 0.0)
            # Exit: crossed below threshold, completing a traversal
            elif prev_score >= self._ZONE_EXIT_THRESHOLD > score and entry_at > 0.0 and exit_at == 0.0:
                state.zone_traversal_history[zone.zone_id] = (entry_at, nowstamp)
        # Prune stale completed traversals
        cutoff = nowstamp - self._ZONE_TRAVERSAL_RECENCY_S * 2
        state.zone_traversal_history = {
            zid: (e, x) for zid, (e, x) in state.zone_traversal_history.items()
            if x == 0.0 or x > cutoff
        }

    def _refresh_trilateration_for_device(self, device: BermudaDevice) -> None:
        """Resolve per-device trilateration diagnostics."""
        nowstamp = monotonic_time_coarse()
        latest = self._latest_adverts_by_scanner(device)
        state = self._get_trilat_decision_state(device)
        policy = self._trilat_mobility_policy(device.get_mobility_type())
        soft_include_other_floor_anchors = self.trilat_soft_include_other_floor_anchors_enabled()
        _debug_this_device = debug_device_match(
            device.name,
            device.prefname,
            device.address,
            device.name_by_user,
            device.name_devreg,
            device.name_bt_local_name,
            device.name_bt_serviceinfo,
        )
        device.trilat_floor_evidence = {}
        device.trilat_floor_evidence_names = {}
        device.trilat_floor_diagnostics = {}
        device.trilat_cross_floor_anchor_count = 0
        device.trilat_cross_floor_anchor_diagnostics = []
        prev_floor_id = state.floor_id
        bootstrap_restored = state.bootstrap_restored_at > 0.0
        bootstrap_hold_active = state.bootstrap_hold_until > nowstamp
        bootstrap_hold_remaining_s = (
            max(0.0, state.bootstrap_hold_until - nowstamp)
            if bootstrap_hold_active
            else 0.0 if bootstrap_restored else None
        )

        def _anchor_effective_sigma_m(
            advert: BermudaAdvert,
            *,
            other_floor: bool = False,
        ) -> float | None:
            if advert.rssi_distance_raw is None or advert.rssi_distance is None:
                return None
            sigma_m = getattr(advert, "rssi_distance_sigma_m", None)
            effective_sigma_m = float(sigma_m) if sigma_m is not None else self._TRILAT_DEFAULT_ANCHOR_SIGMA_M
            advert_age_s = max(0.0, nowstamp - advert.stamp)
            effective_sigma_m *= self._trilat_age_sigma_multiplier(advert_age_s)
            if other_floor:
                effective_sigma_m *= self._TRILAT_DIAGNOSTIC_OTHER_FLOOR_SIGMA_MULTIPLIER
            return effective_sigma_m

        def _apply_floor_diagnostics(
            *,
            reason: str,
            selected_floor_id: str | None,
            floor_evidence: dict[str, float] | None = None,
            best_floor_id: str | None = None,
            best_floor_score: float | None = None,
            second_floor_score: float | None = None,
            total_floor_score: float | None = None,
            current_floor_score: float | None = None,
            floor_ambiguity: bool = False,
            floor_ambiguous_persisted: bool = False,
            challenger_margin: float | None = None,
            effective_required_dwell_s: float | None = None,
            challenger_effective_dwell_s: float | None = None,
            fingerprint_result: GlobalFingerprintResult | None = None,
            fingerprint_switch_veto_active: bool = False,
            transition_support_01: float = 0.0,
            transition_immediate_support_01: float = 0.0,
            transition_recent_support_01: float = 0.0,
            transition_recent_age_s: float | None = None,
            transition_recent_name: str | None = None,
            transition_recent_floor_ids: tuple[str, ...] = (),
            bootstrap_restored: bool = False,
            bootstrap_hold_active: bool = False,
            bootstrap_hold_remaining_s: float | None = None,
        ) -> None:
            floor_evidence = floor_evidence or {}
            fingerprint_result = fingerprint_result or GlobalFingerprintResult(
                area_id=None,
                floor_id=None,
                reason="no_trained_rooms",
            )
            device.trilat_floor_evidence = dict(floor_evidence)
            device.trilat_floor_evidence_names = {
                floor_id: self._resolve_floor_name(floor_id)
                for floor_id in floor_evidence
            }
            challenger_dwell_s = None
            if state.floor_challenger_id is not None and state.floor_challenger_since > 0.0:
                challenger_dwell_s = max(0.0, nowstamp - state.floor_challenger_since)
            ambiguity_ratio = None
            if total_floor_score is not None and total_floor_score > 0.0 and best_floor_score is not None:
                ambiguity_ratio = (best_floor_score - (second_floor_score or 0.0)) / total_floor_score
            floor_switch_age_s = None
            if state.last_floor_change_at > 0.0:
                floor_switch_age_s = max(0.0, nowstamp - state.last_floor_change_at)
            device.trilat_floor_diagnostics = {
                "reason": reason,
                "selected_floor_id": selected_floor_id,
                "selected_floor_name": self._resolve_floor_name(selected_floor_id),
                "previous_floor_id": prev_floor_id,
                "previous_floor_name": self._resolve_floor_name(prev_floor_id),
                "best_floor_id": best_floor_id,
                "best_floor_name": self._resolve_floor_name(best_floor_id),
                "challenger_floor_id": state.floor_challenger_id,
                "challenger_floor_name": self._resolve_floor_name(state.floor_challenger_id),
                "best_floor_score": best_floor_score,
                "current_floor_score": current_floor_score,
                "second_floor_score": second_floor_score,
                "total_floor_score": total_floor_score,
                "ambiguity_ratio": ambiguity_ratio,
                "floor_ambiguity": floor_ambiguity,
                "floor_ambiguous_persisted": floor_ambiguous_persisted,
                "challenger_margin": challenger_margin,
                "required_margin": policy.floor_switch_margin,
                "challenger_dwell_s": challenger_dwell_s,
                "required_dwell_s": policy.floor_dwell_seconds,
                "challenger_effective_dwell_s": challenger_effective_dwell_s,
                "effective_required_dwell_s": effective_required_dwell_s,
                "fingerprint_area_id": fingerprint_result.area_id,
                "fingerprint_floor_id": fingerprint_result.floor_id,
                "fingerprint_floor_name": self._resolve_floor_name(fingerprint_result.floor_id),
                "fingerprint_reason": fingerprint_result.reason,
                "fingerprint_floor_confidence": fingerprint_result.floor_confidence,
                "fingerprint_room_confidence": fingerprint_result.room_confidence,
                "fingerprint_best_score": fingerprint_result.best_score,
                "fingerprint_second_score": fingerprint_result.second_score,
                "fingerprint_floor_scores": dict(fingerprint_result.floor_scores),
                "fingerprint_current_floor_score": (
                    fingerprint_result.floor_scores.get(selected_floor_id, 0.0) if selected_floor_id else 0.0
                ),
                "fingerprint_challenger_floor_score": (
                    fingerprint_result.floor_scores.get(state.floor_challenger_id, 0.0)
                    if state.floor_challenger_id
                    else 0.0
                ),
                "fingerprint_switch_veto_active": fingerprint_switch_veto_active,
                "transition_support_01": transition_support_01,
                "transition_immediate_support_01": transition_immediate_support_01,
                "transition_recent_support_01": transition_recent_support_01,
                "transition_recent_age_s": transition_recent_age_s,
                "transition_recent_name": transition_recent_name,
                "transition_recent_floor_ids": list(transition_recent_floor_ids),
                "transition_recent_floor_names": [
                    self._resolve_floor_name(floor_id) for floor_id in transition_recent_floor_ids
                ],
                "bootstrap_restored": bootstrap_restored,
                "bootstrap_hold_active": bootstrap_hold_active,
                "bootstrap_hold_remaining_s": bootstrap_hold_remaining_s,
                "soft_include_other_floor_anchors_enabled": soft_include_other_floor_anchors,
                "cross_floor_anchor_count": device.trilat_cross_floor_anchor_count,
                "floor_switch_count": getattr(device, "trilat_floor_switch_count", 0),
                "floor_switch_last_at": getattr(device, "trilat_floor_switch_last_at", None),
                "floor_switch_last_from_floor_id": getattr(
                    device,
                    "trilat_floor_switch_last_from_floor_id",
                    None,
                ),
                "floor_switch_last_to_floor_id": getattr(
                    device,
                    "trilat_floor_switch_last_to_floor_id",
                    None,
                ),
                "floor_switch_last_from_name": getattr(
                    device,
                    "trilat_floor_switch_last_from_name",
                    None,
                ),
                "floor_switch_last_to_name": getattr(
                    device,
                    "trilat_floor_switch_last_to_name",
                    None,
                ),
                "floor_switch_age_s": floor_switch_age_s,
                "floor_switch_reset_count": getattr(device, "trilat_floor_switch_reset_count", 0),
                "floor_switch_reset_last_at": getattr(device, "trilat_floor_switch_reset_last_at", None),
                "floor_switch_reset_last_from_floor_id": getattr(
                    device,
                    "trilat_floor_switch_reset_last_from_floor_id",
                    None,
                ),
                "floor_switch_reset_last_to_floor_id": getattr(
                    device,
                    "trilat_floor_switch_reset_last_to_floor_id",
                    None,
                ),
                "floor_switch_reset_last_from_name": getattr(
                    device,
                    "trilat_floor_switch_reset_last_from_name",
                    None,
                ),
                "floor_switch_reset_last_to_name": getattr(
                    device,
                    "trilat_floor_switch_reset_last_to_name",
                    None,
                ),
            }

        def _anchor_status_entries(selected_floor_id: str | None = None) -> list[dict[str, object]]:
            entries: list[dict[str, object]] = []
            for scanner in sorted(self._scanners, key=lambda sc: (sc.name, sc.address)):
                advert = latest.get(scanner.address)
                scanner_name = getattr(scanner, "name", scanner.address)
                anchor_x = self.get_scanner_anchor_x(scanner.address)
                anchor_y = self.get_scanner_anchor_y(scanner.address)
                sync_state = (
                    scanner.timestamp_sync_diagnostics().get("state")
                    if getattr(scanner, "is_scanner", False)
                    else None
                )
                if advert is None:
                    status = "no_advert"
                elif advert.stamp < nowstamp - DISTANCE_TIMEOUT:
                    status = "rejected_stale"
                elif selected_floor_id is not None and scanner.floor_id != selected_floor_id:
                    status = "rejected_wrong_floor"
                elif (
                    anchor_x is None
                    or anchor_y is None
                ):
                    status = "rejected_missing_anchor"
                elif advert.rssi_distance_raw is None or advert.rssi_distance is None:
                    status = "rejected_no_range"
                else:
                    status = "valid"
                soft_include_sigma_m = None
                soft_include_eligible = False
                soft_include_active = False
                if (
                    status == "rejected_wrong_floor"
                    and advert is not None
                    and anchor_x is not None
                    and anchor_y is not None
                ):
                    soft_include_sigma_m = _anchor_effective_sigma_m(advert, other_floor=True)
                    soft_include_eligible = soft_include_sigma_m is not None
                    soft_include_active = soft_include_other_floor_anchors and soft_include_eligible
                entries.append(
                    {
                        "scanner_address": scanner.address,
                        "scanner_name": scanner_name,
                        "scanner_floor_id": scanner.floor_id,
                        "selected_floor_id": selected_floor_id,
                        "status": status,
                        "sync_state": sync_state,
                        "affects_position": (status == "valid") or soft_include_active,
                        "soft_include_sigma_m": soft_include_sigma_m,
                        "soft_include_eligible": soft_include_eligible,
                        "soft_include_active": soft_include_active,
                    }
                )
            return entries

        def _apply_anchor_status_entries(selected_floor_id: str | None = None) -> None:
            entries = _anchor_status_entries(selected_floor_id)
            device.trilat_anchor_statuses = {
                str(entry["scanner_address"]).lower(): entry
                for entry in entries
            }
            device.trilat_anchor_diagnostics = [self._format_anchor_status_entry(entry) for entry in entries]
            cross_floor_entries = [entry for entry in entries if entry.get("soft_include_eligible")]
            device.trilat_cross_floor_anchor_count = len(cross_floor_entries)
            device.trilat_cross_floor_anchor_diagnostics = [
                self._format_anchor_status_entry(entry)
                for entry in cross_floor_entries
            ]

        def _anchor_status_count_summary() -> str:
            counts: dict[str, int] = {}
            for entry in device.trilat_anchor_statuses.values():
                status = str(entry.get("status", "unknown"))
                counts[status] = counts.get(status, 0) + 1
            return ", ".join(f"{status}={counts[status]}" for status in sorted(counts)) or "none"

        fresh_any = any(advert.stamp >= nowstamp - DISTANCE_TIMEOUT for advert in latest.values())
        if not fresh_any:
            _apply_anchor_status_entries()
            _apply_floor_diagnostics(
                reason="stale_inputs",
                selected_floor_id=state.floor_id,
                bootstrap_restored=bootstrap_restored,
                bootstrap_hold_active=bootstrap_hold_active,
                bootstrap_hold_remaining_s=bootstrap_hold_remaining_s,
            )
            device.set_trilat_unknown(
                "stale_inputs",
                floor_id=state.floor_id,
                floor_name=self._resolve_floor_name(state.floor_id),
                anchor_count=0,
            )
            state.last_status = "unknown"
            state.last_geometry_quality_01 = 0.0
            state.last_residual_consistency_01 = 0.0
            state.last_geometry_gdop = None
            state.last_geometry_condition = None
            state.last_normalized_residual_rms = None
            self._set_trilat_confidence(device, 0.0)
            self._set_tracking_confidence(device, 0.0)
            self._clear_trilat_quality_metrics(device)
            if _debug_this_device:
                _LOGGER_TARGET_SPAM_LESS.debug(
                    f"trilat_unknown:{device.address}:stale_inputs",
                    "Trilat: %s -> Unknown (stale_inputs)",
                    device.name,
                )
            return

        evidence_inputs: list[tuple[str, float]] = []
        global_live_rssi_by_scanner: dict[str, float] = {}
        penalty_db = self.trilat_cross_floor_penalty_db()
        for advert in latest.values():
            if advert.stamp < nowstamp - DISTANCE_TIMEOUT:
                continue
            scanner_floor_id = advert.scanner_device.floor_id
            if scanner_floor_id is None:
                continue
            rssi_for_score = getattr(advert, "rssi_window_median", None)
            if rssi_for_score is None:
                rssi_for_score = advert.rssi_filtered
            if rssi_for_score is None and advert.rssi is not None:
                rssi_for_score = advert.rssi + advert.conf_rssi_offset
            if rssi_for_score is None:
                continue
            evidence_inputs.append((scanner_floor_id, rssi_for_score))
            global_live_rssi_by_scanner[advert.scanner_address.lower()] = float(rssi_for_score)

        layout_hash = self.current_anchor_layout_hash() if getattr(self, "calibration", None) is not None else ""
        fingerprint_result = GlobalFingerprintResult(area_id=None, floor_id=None, reason="no_trained_rooms")
        room_classifier = getattr(self, "room_classifier", None)
        if layout_hash and room_classifier is not None and hasattr(room_classifier, "fingerprint_global"):
            fingerprint_result = room_classifier.fingerprint_global(
                layout_hash=layout_hash,
                live_rssi_by_scanner=global_live_rssi_by_scanner,
            )

        geometry_quality_01 = max(0.0, min(1.0, float(device.trilat_geometry_quality or 0.0) / 10.0))

        def _refresh_recent_transition_context() -> dict[str, object]:
            if not layout_hash or not hasattr(self.calibration, "transition_support_diagnostics"):
                return {}
            transition_diag = self.calibration.transition_support_diagnostics(
                layout_hash=layout_hash,
                x_m=device.trilat_x_m,
                y_m=device.trilat_y_m,
                z_m=device.trilat_z_m,
                room_area_id=None,
                challenger_floor_id=None,
                geometry_quality_01=geometry_quality_01,
            )
            best_floor_ids = tuple(str(floor_id) for floor_id in (transition_diag.get("transition_best_floor_ids") or []) if floor_id)
            best_within_radius = bool(transition_diag.get("transition_best_within_radius"))
            if best_within_radius and best_floor_ids:
                support_01 = 1.0 if geometry_quality_01 >= 0.30 else 0.5
                state.recent_transition_name = str(transition_diag.get("transition_best_name") or "") or None
                state.recent_transition_room_area_id = (
                    str(transition_diag.get("transition_best_room_area_id") or "") or None
                )
                state.recent_transition_floor_ids = best_floor_ids
                state.recent_transition_support_01 = support_01
                state.recent_transition_seen_at = nowstamp
            elif (
                state.recent_transition_seen_at > 0.0
                and (nowstamp - state.recent_transition_seen_at) > self._TRILAT_RECENT_TRANSITION_WINDOW_S
            ):
                state.recent_transition_name = None
                state.recent_transition_room_area_id = None
                state.recent_transition_floor_ids = ()
                state.recent_transition_support_01 = 0.0
                state.recent_transition_seen_at = 0.0
            return transition_diag

        def _recent_transition_support_for_challenger(challenger_floor_id: str | None) -> tuple[float, float | None]:
            if (
                challenger_floor_id is None
                or state.recent_transition_seen_at <= 0.0
                or challenger_floor_id not in state.recent_transition_floor_ids
            ):
                return 0.0, None
            age_s = max(0.0, nowstamp - state.recent_transition_seen_at)
            if age_s > self._TRILAT_RECENT_TRANSITION_WINDOW_S:
                state.recent_transition_name = None
                state.recent_transition_room_area_id = None
                state.recent_transition_floor_ids = ()
                state.recent_transition_support_01 = 0.0
                state.recent_transition_seen_at = 0.0
                return 0.0, None
            return state.recent_transition_support_01, age_s

        transition_context_diag = _refresh_recent_transition_context()

        def _transition_support_for_challenger(challenger_floor_id: str | None) -> float:
            if (
                not layout_hash
                or challenger_floor_id is None
                or not hasattr(self.calibration, "transition_support_diagnostics")
            ):
                return 0.0
            transition_diag = self.calibration.transition_support_diagnostics(
                layout_hash=layout_hash,
                x_m=device.trilat_x_m,
                y_m=device.trilat_y_m,
                z_m=device.trilat_z_m,
                room_area_id=self._stable_area_id(device),
                challenger_floor_id=challenger_floor_id,
                geometry_quality_01=geometry_quality_01,
            )
            return float(transition_diag.get("transition_support_01", 0.0) or 0.0)

        if not evidence_inputs:
            _apply_anchor_status_entries()
            _apply_floor_diagnostics(
                reason="ambiguous_floor",
                selected_floor_id=state.floor_id,
                fingerprint_result=fingerprint_result,
                bootstrap_restored=bootstrap_restored,
                bootstrap_hold_active=bootstrap_hold_active,
                bootstrap_hold_remaining_s=bootstrap_hold_remaining_s,
            )
            device.set_trilat_unknown("ambiguous_floor", floor_id=state.floor_id, floor_name=self._resolve_floor_name(state.floor_id))
            state.last_status = "unknown"
            state.last_geometry_quality_01 = 0.0
            state.last_residual_consistency_01 = 0.0
            state.last_geometry_gdop = None
            state.last_geometry_condition = None
            state.last_normalized_residual_rms = None
            self._set_trilat_confidence(device, 0.0)
            self._set_tracking_confidence(device, 0.0)
            self._clear_trilat_quality_metrics(device)
            return

        floors = sorted({floor_id for floor_id, _rssi in evidence_inputs})

        # Phase 3: Signal priority reorder — fingerprint (primary), RSSI (secondary), Z hint (tertiary).
        # RSSI floor evidence (secondary signal)
        rssi_floor_evidence: dict[str, float] = {}
        for candidate_floor_id in floors:
            evidence = 0.0
            for scanner_floor_id, rssi_for_score in evidence_inputs:
                adjusted_rssi = (
                    rssi_for_score
                    if scanner_floor_id == candidate_floor_id
                    else rssi_for_score - penalty_db
                )
                evidence += self._score_rssi(adjusted_rssi)
            rssi_floor_evidence[candidate_floor_id] = evidence

        # Fingerprint floor scores (primary signal)
        fingerprint_has_floor_signal = fingerprint_result.reason in {"ok", "room_ambiguity"}

        # Z-derived floor hint (tertiary) — uses phone-height band per floor when trilat_z_m is solved
        z_floor_scores: dict[str, float] = {}
        if device.trilat_z_m is not None:
            for fid in floors:
                fz_m = self.get_floor_z_m(fid)
                if fz_m is not None:
                    phone_z = fz_m + self._PHONE_HEIGHT_Z_CENTER_M
                    z_diff = device.trilat_z_m - phone_z
                    z_floor_scores[fid] = math.exp(-0.5 * (z_diff / self._PHONE_HEIGHT_Z_SIGMA_M) ** 2)

        # Combine signals: normalise each to [0,1] fraction, then weight-blend.
        # Falls back to pure RSSI when fingerprint and Z evidence are absent.
        total_rssi = sum(rssi_floor_evidence.values()) or 1e-9
        rssi_norm = {fid: v / total_rssi for fid, v in rssi_floor_evidence.items()}
        _fp_norm: dict[str, float] = {}
        _z_norm: dict[str, float] = {}
        if fingerprint_has_floor_signal and fingerprint_result.floor_scores:
            total_fp = sum(fingerprint_result.floor_scores.values()) or 1e-9
            _fp_norm = {fid: fingerprint_result.floor_scores.get(fid, 0.0) / total_fp for fid in floors}
        if z_floor_scores:
            total_z = sum(z_floor_scores.values()) or 1e-9
            _z_norm = {fid: z_floor_scores.get(fid, 0.0) / total_z for fid in floors}
        if _fp_norm and _z_norm:
            floor_evidence: dict[str, float] = {
                fid: _fp_norm.get(fid, 0.0) * 0.55 + rssi_norm.get(fid, 0.0) * 0.30 + _z_norm.get(fid, 0.0) * 0.15
                for fid in floors
            }
        elif _fp_norm:
            floor_evidence = {
                fid: _fp_norm.get(fid, 0.0) * 0.65 + rssi_norm.get(fid, 0.0) * 0.35
                for fid in floors
            }
        elif _z_norm:
            floor_evidence = {
                fid: rssi_norm.get(fid, 0.0) * 0.70 + _z_norm.get(fid, 0.0) * 0.30
                for fid in floors
            }
        else:
            floor_evidence = rssi_floor_evidence

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

        current_floor_score: float | None = None
        floor_margin: float | None = None
        effective_required_dwell_s = float(policy.floor_dwell_seconds)
        challenger_effective_dwell_s: float | None = None
        fingerprint_switch_veto_active = False
        transition_support_01 = 0.0
        transition_immediate_support_01 = 0.0
        transition_recent_support_01 = 0.0
        transition_recent_age_s: float | None = None
        _gate_result_pre: ReachabilityDecision | None = None
        if state.floor_id is None:
            state.floor_id = best_floor_id
            state.floor_challenger_id = None
            state.floor_challenger_since = 0.0
        elif best_floor_id != state.floor_id:
            current_floor_score = floor_evidence.get(state.floor_id, 0.0)
            floor_margin = (best_floor_score - current_floor_score) / max(best_floor_score, 1e-9)
            required_margin = policy.floor_switch_margin
            effective_required_dwell_s = float(policy.floor_dwell_seconds)

            if floor_margin >= required_margin:
                if bootstrap_hold_active and not fingerprint_has_floor_signal:
                    state.floor_challenger_id = None
                    state.floor_challenger_since = 0.0
                    state.challenger_reference_position = None
                    state.challenger_onset_time = 0.0
                    state.challenger_motion_budget_m = 0.0
                    state.last_fingerprint_veto_at = 0.0
                else:
                    # Phase 3: Reachability gate runs BEFORE challenger forms.
                    # Evaluate the gate immediately to decide whether to allow
                    # evidence competition for this floor pair.
                    _gate_blocked = False
                    if self.trilat_reachability_gate_enabled():
                        _is_new_challenger = state.floor_challenger_id != best_floor_id
                        if _is_new_challenger:
                            # For a new challenger, use last_good_position as reference.
                            candidate_ref = (
                                state.last_good_position
                                if (
                                    state.last_good_position is not None
                                    and (nowstamp - state.last_good_position_at) < self._ZONE_TRAVERSAL_RECENCY_S * 2
                                )
                                else None
                            )
                            candidate_budget_m = 0.0
                        else:
                            # For an ongoing challenger, use stored reference and accumulated budget.
                            candidate_ref = state.challenger_reference_position
                            candidate_budget_m = state.challenger_motion_budget_m
                        _gate_result_pre = self._reachability_gate.evaluate(
                            from_floor_id=state.floor_id,
                            to_floor_id=best_floor_id,
                            floor_confidence=state.floor_confidence,
                            floor_confidence_threshold=self._FLOOR_CONFIDENCE_GATE_THRESHOLD,
                            reference_position=candidate_ref,
                            motion_budget_m=candidate_budget_m,
                            zones=self._transition_zone_store.zones,
                            zone_traversal_history=state.zone_traversal_history,
                            nowstamp=nowstamp,
                            traversal_recency_s=self._ZONE_TRAVERSAL_RECENCY_S,
                            layout_hash=layout_hash,
                        )
                        _gate_blocked = not _gate_result_pre.allowed

                    if _gate_blocked:
                        # Gate blocks before challenger can form or accumulate dwell.
                        state.floor_challenger_id = None
                        state.floor_challenger_since = 0.0
                        state.challenger_reference_position = None
                        state.challenger_onset_time = 0.0
                        state.challenger_motion_budget_m = 0.0
                        state.last_fingerprint_veto_at = 0.0
                    else:
                        # Gate allows (or not enabled): form or continue challenger.
                        if state.floor_challenger_id != best_floor_id:
                            state.floor_challenger_id = best_floor_id
                            state.floor_challenger_since = nowstamp
                            state.last_fingerprint_veto_at = 0.0
                            # Use last known good position as reference — more reliable than the
                            # current position at challenger onset, which may already be degraded.
                            # Only use it if it was recorded recently (within 2x the traversal window).
                            if (
                                state.last_good_position is not None
                                and (nowstamp - state.last_good_position_at) < self._ZONE_TRAVERSAL_RECENCY_S * 2
                            ):
                                state.challenger_reference_position = state.last_good_position
                            else:
                                state.challenger_reference_position = None
                            state.challenger_onset_time = nowstamp
                            state.challenger_motion_budget_m = 0.0

                        current_floor_fp_score = fingerprint_result.floor_scores.get(state.floor_id, 0.0) if state.floor_id else 0.0
                        challenger_floor_fp_score = (
                            fingerprint_result.floor_scores.get(state.floor_challenger_id, 0.0)
                            if state.floor_challenger_id
                            else 0.0
                        )
                        current_floor_fp_ratio = (
                            current_floor_fp_score / max(challenger_floor_fp_score, 1e-9)
                            if current_floor_fp_score > 0.0
                            else 0.0
                        )
                        fingerprint_supports_current_floor = (
                            fingerprint_has_floor_signal
                            and fingerprint_result.floor_id == state.floor_id
                            and (
                                fingerprint_result.floor_confidence >= self._TRILAT_FINGERPRINT_FLOOR_CONFIDENCE_HIGH
                                or (
                                    fingerprint_result.floor_confidence >= self._TRILAT_FINGERPRINT_FLOOR_CONFIDENCE_MODERATE
                                    and current_floor_fp_ratio >= self._TRILAT_FINGERPRINT_FLOOR_SCORE_RATIO_HOLD
                                )
                            )
                        )

                        transition_immediate_support_01 = _transition_support_for_challenger(state.floor_challenger_id)
                        transition_recent_support_01, transition_recent_age_s = _recent_transition_support_for_challenger(
                            state.floor_challenger_id
                        )
                        transition_support_01 = max(transition_immediate_support_01, transition_recent_support_01)

                        # Update motion budget while challenger is active
                        if state.floor_challenger_id is not None and state.challenger_onset_time > 0.0:
                            elapsed_s = max(0.0, nowstamp - state.challenger_onset_time)
                            budget_from_elapsed = elapsed_s * self._TRILAT_MAX_POSITION_SPEED_MPS
                            state.challenger_motion_budget_m = min(
                                budget_from_elapsed + self._CHALLENGER_UNCERTAINTY_BUDGET_M,
                                self._CHALLENGER_MAX_MOTION_BUDGET_M,
                            )

                        challenger_effective_dwell_s = max(
                            0.0,
                            nowstamp - state.floor_challenger_since,
                        )

                        # Hysteresis is the last line of defence (Phase 3 + 4 priority order).
                        veto_hold_active = (
                            nowstamp - state.last_fingerprint_veto_at
                        ) < self._FINGERPRINT_VETO_HOLD_S
                        if challenger_effective_dwell_s >= effective_required_dwell_s:
                            if fingerprint_supports_current_floor:
                                # Safety net: combined evidence already de-prioritises this
                                # challenger via fingerprint weighting, but veto if it still
                                # somehow reaches dwell with fp strongly favouring current.
                                fingerprint_switch_veto_active = True
                                state.last_fingerprint_veto_at = nowstamp
                            elif veto_hold_active:
                                # Veto hold window: fp_conf briefly dropped but the veto was
                                # active recently — do not allow a single-cycle dip to switch.
                                fingerprint_switch_veto_active = True
                            else:
                                state.floor_id = best_floor_id
                                state.floor_challenger_id = None
                                state.floor_challenger_since = 0.0
                                state.challenger_reference_position = None
                                state.challenger_onset_time = 0.0
                                state.challenger_motion_budget_m = 0.0
                                state.last_fingerprint_veto_at = 0.0
            else:
                _gate_result_pre = None
                state.floor_challenger_id = None
                state.floor_challenger_since = 0.0
                state.challenger_reference_position = None
                state.challenger_onset_time = 0.0
                state.challenger_motion_budget_m = 0.0
                state.last_fingerprint_veto_at = 0.0
        else:
            current_floor_score = floor_evidence.get(state.floor_id, 0.0)
            floor_margin = 0.0
            state.floor_challenger_id = None
            state.floor_challenger_since = 0.0
            state.challenger_reference_position = None
            state.challenger_onset_time = 0.0
            state.challenger_motion_budget_m = 0.0

        bootstrap_restored = state.bootstrap_restored_at > 0.0
        bootstrap_hold_active = state.bootstrap_hold_until > nowstamp
        bootstrap_hold_remaining_s = (
            max(0.0, state.bootstrap_hold_until - nowstamp)
            if bootstrap_hold_active
            else 0.0 if bootstrap_restored else None
        )
        selected_floor_id = state.floor_id or best_floor_id
        self._update_floor_confidence(
            state,
            selected_floor_id=selected_floor_id,
            floor_evidence=floor_evidence,
            floor_ambiguity=floor_ambiguity,
        )
        selected_floor_name = self._resolve_floor_name(selected_floor_id)
        _apply_anchor_status_entries(selected_floor_id)
        _apply_floor_diagnostics(
            reason="ok",
            selected_floor_id=selected_floor_id,
            floor_evidence=floor_evidence,
            best_floor_id=best_floor_id,
            best_floor_score=best_floor_score,
            second_floor_score=second_floor_score,
            total_floor_score=total_floor_score,
            current_floor_score=current_floor_score,
            floor_ambiguity=floor_ambiguity,
            floor_ambiguous_persisted=floor_ambiguous_persisted,
            challenger_margin=floor_margin,
            effective_required_dwell_s=effective_required_dwell_s,
            challenger_effective_dwell_s=challenger_effective_dwell_s,
            fingerprint_result=fingerprint_result,
            fingerprint_switch_veto_active=fingerprint_switch_veto_active,
            transition_support_01=transition_support_01,
            transition_immediate_support_01=transition_immediate_support_01,
            transition_recent_support_01=transition_recent_support_01,
            transition_recent_age_s=transition_recent_age_s,
            transition_recent_name=state.recent_transition_name,
            transition_recent_floor_ids=state.recent_transition_floor_ids,
            bootstrap_restored=bootstrap_restored,
            bootstrap_hold_active=bootstrap_hold_active,
            bootstrap_hold_remaining_s=bootstrap_hold_remaining_s,
        )
        # Augment diagnostics with Phase 3 signal breakdown and gate result.
        device.trilat_floor_diagnostics["rssi_floor_evidence"] = rssi_floor_evidence
        device.trilat_floor_diagnostics["z_floor_scores"] = z_floor_scores
        device.trilat_floor_diagnostics["fingerprint_has_floor_signal"] = fingerprint_has_floor_signal
        if _gate_result_pre is not None:
            device.trilat_floor_diagnostics["reachability_gate_allowed"] = _gate_result_pre.allowed
            device.trilat_floor_diagnostics["reachability_gate_reason"] = _gate_result_pre.reason
            device.trilat_floor_diagnostics["reachability_gate_budget_m"] = _gate_result_pre.motion_budget_m
            device.trilat_floor_diagnostics["reachability_gate_nearest_m"] = _gate_result_pre.nearest_zone_distance_m

        if prev_floor_id is not None and selected_floor_id != prev_floor_id:
            state.last_floor_change_at = nowstamp
            state.last_floor_change_from_id = prev_floor_id
            device.trilat_floor_switch_count = getattr(device, "trilat_floor_switch_count", 0) + 1
            device.trilat_floor_switch_last_at = nowstamp
            device.trilat_floor_switch_last_from_floor_id = prev_floor_id
            device.trilat_floor_switch_last_to_floor_id = selected_floor_id
            device.trilat_floor_switch_last_from_name = self._resolve_floor_name(prev_floor_id)
            device.trilat_floor_switch_last_to_name = selected_floor_name
            _apply_floor_diagnostics(
                reason="floor_switch_preserved_state",
                selected_floor_id=selected_floor_id,
                floor_evidence=floor_evidence,
                best_floor_id=best_floor_id,
                best_floor_score=best_floor_score,
                second_floor_score=second_floor_score,
                total_floor_score=total_floor_score,
                current_floor_score=current_floor_score,
                floor_ambiguity=floor_ambiguity,
                floor_ambiguous_persisted=floor_ambiguous_persisted,
                challenger_margin=floor_margin,
                effective_required_dwell_s=effective_required_dwell_s,
                bootstrap_restored=bootstrap_restored,
                bootstrap_hold_active=bootstrap_hold_active,
                bootstrap_hold_remaining_s=bootstrap_hold_remaining_s,
                challenger_effective_dwell_s=challenger_effective_dwell_s,
                fingerprint_result=fingerprint_result,
                fingerprint_switch_veto_active=fingerprint_switch_veto_active,
                transition_support_01=transition_support_01,
                transition_immediate_support_01=transition_immediate_support_01,
                transition_recent_support_01=transition_recent_support_01,
                transition_recent_age_s=transition_recent_age_s,
                transition_recent_name=state.recent_transition_name,
                transition_recent_floor_ids=state.recent_transition_floor_ids,
            )

        if _debug_this_device:
            evidence_str = ", ".join(
                f"{floor_id}={score:.3f}"
                for floor_id, score in sorted(
                    device.trilat_floor_evidence.items(),
                    key=lambda row: row[1],
                    reverse=True,
                )
            ) or "none"
            _LOGGER_TARGET_SPAM_LESS.debug(
                f"trilat_floor_diag:{device.address}",
                (
                    "Trilat floor diag: %s selected=%s challenger=%s margin=%s "
                    "cross_floor=%d switches=%d resets=%d fp_floor=%s fp_conf=%s "
                    "fp_reason=%s fp_veto=%s effective_dwell=%s/%s "
                    "transition_support=%s transition_immediate=%s transition_recent=%s "
                    "transition_recent_age=%s transition_recent_name=%s evidence=[%s]"
                ),
                device.name,
                selected_floor_id,
                state.floor_challenger_id,
                f"{floor_margin:.3f}" if floor_margin is not None else "n/a",
                device.trilat_cross_floor_anchor_count,
                getattr(device, "trilat_floor_switch_count", 0),
                getattr(device, "trilat_floor_switch_reset_count", 0),
                fingerprint_result.floor_id,
                f"{fingerprint_result.floor_confidence:.3f}",
                fingerprint_result.reason,
                fingerprint_switch_veto_active,
                f"{challenger_effective_dwell_s:.3f}" if challenger_effective_dwell_s is not None else "n/a",
                f"{effective_required_dwell_s:.3f}" if effective_required_dwell_s is not None else "n/a",
                f"{transition_support_01:.3f}",
                f"{transition_immediate_support_01:.3f}",
                f"{transition_recent_support_01:.3f}",
                f"{transition_recent_age_s:.3f}" if transition_recent_age_s is not None else "n/a",
                state.recent_transition_name,
                evidence_str,
            )

        anchors: list[AnchorMeasurement] = []
        confidence_anchor_sigmas_m: list[float] = []
        same_floor_known_anchor_z: list[float] = []
        current_anchor_floor_roles: dict[str, str] = {}
        included_other_floor_anchor_count = 0
        for advert in latest.values():
            if advert.stamp < nowstamp - DISTANCE_TIMEOUT:
                continue
            scanner = advert.scanner_device
            other_floor = scanner.floor_id != selected_floor_id
            if other_floor and not soft_include_other_floor_anchors:
                continue
            anchor_x = self.get_scanner_anchor_x(scanner.address)
            anchor_y = self.get_scanner_anchor_y(scanner.address)
            if anchor_x is None or anchor_y is None:
                continue

            if advert.rssi_distance_raw is None:
                continue
            if advert.rssi_distance is None:
                continue
            effective_sigma_m = _anchor_effective_sigma_m(advert, other_floor=other_floor)
            if effective_sigma_m is None:
                continue

            current_role = "other_floor" if other_floor else "same_floor"
            previous_role = state.last_anchor_floor_roles.get(scanner.address)
            if previous_role in ("same_floor", "other_floor") and previous_role != current_role:
                advert.trilat_range_ewma_m = None

            if advert.trilat_range_ewma_m is None:
                advert.trilat_range_ewma_m = advert.rssi_distance_raw
            else:
                advert.trilat_range_ewma_m = (policy.trilat_alpha * advert.rssi_distance_raw) + (
                    (1 - policy.trilat_alpha) * advert.trilat_range_ewma_m
                )
            anchor_z_m = self.get_scanner_anchor_z(scanner.address)
            current_anchor_floor_roles[scanner.address] = current_role
            if not other_floor:
                confidence_anchor_sigmas_m.append(effective_sigma_m)
                if anchor_z_m is not None:
                    same_floor_known_anchor_z.append(float(anchor_z_m))
            anchor_measurement = AnchorMeasurement(
                scanner_address=scanner.address,
                x_m=float(anchor_x),
                y_m=float(anchor_y),
                range_m=float(advert.trilat_range_ewma_m),
                z_m=anchor_z_m,
                sigma_m=effective_sigma_m,
            )
            anchors.append(anchor_measurement)
            if other_floor:
                included_other_floor_anchor_count += 1

        anchor_count = len(anchors)
        state.last_anchor_floor_roles = current_anchor_floor_roles
        mean_sigma_m = (
            (sum(confidence_anchor_sigmas_m) / len(confidence_anchor_sigmas_m))
            if confidence_anchor_sigmas_m
            else None
        )
        anchor_z_bounds = (
            (min(same_floor_known_anchor_z), max(same_floor_known_anchor_z))
            if same_floor_known_anchor_z
            else None
        )
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
            raw_confidence = max(0.5, float(anchor_count) * 0.8)
            self._set_trilat_confidence(device, raw_confidence)
            self._clear_trilat_quality_metrics(device)
            self._set_tracking_confidence(
                device,
                self._compute_tracking_confidence(
                    raw_score=raw_confidence,
                    state=state,
                    mobility_type=device.get_mobility_type(),
                    used_prior=False,
                    mean_anchor_range_delta_m=None,
                    geometry_quality_01=0.0,
                    residual_consistency_01=0.0,
                    floor_ambiguous=floor_ambiguous_persisted,
                ),
            )
            state.last_mean_sigma_m = mean_sigma_m
            state.last_status = "low_confidence"
            state.last_geometry_quality_01 = 0.0
            state.last_residual_consistency_01 = 0.0
            state.last_geometry_gdop = None
            state.last_geometry_condition = None
            state.last_normalized_residual_rms = None
            if _debug_this_device:
                _LOGGER_TARGET_SPAM_LESS.debug(
                    f"trilat_low_conf:{device.address}:insufficient_anchors",
                    "Trilat: %s low confidence (insufficient_anchors), floor=%s anchors=%d status_counts=[%s]",
                    device.name,
                    selected_floor_name,
                    anchor_count,
                    _anchor_status_count_summary(),
                )
            return

        anchors.sort(key=lambda anchor: anchor.scanner_address)
        anchor_ids = tuple(anchor.scanner_address for anchor in anchors)
        anchor_ranges = {anchor.scanner_address: anchor.range_m for anchor in anchors}
        anchor_z = {anchor.scanner_address: anchor.z_m for anchor in anchors}
        common_anchor_deltas = [
            abs(anchor_ranges[address] - state.last_anchor_ranges[address])
            for address in anchor_ids
            if address in state.last_anchor_ranges
        ]
        mean_anchor_range_delta_m = (
            sum(common_anchor_deltas) / len(common_anchor_deltas)
            if common_anchor_deltas
            else None
        )
        # Step 11: Always run 3D when all anchors carry Z coordinates, regardless of
        # whether cross-floor anchors are included.  Cross-floor anchors already have
        # inflated sigma so the 3D solve naturally down-weights their Z contribution.
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
                z_m=state.last_solution_z,
                floor_id=selected_floor_id,
                floor_name=selected_floor_name,
                anchor_count=anchor_count,
                residual_m=state.last_residual_m,
            )
            device.trilat_reason = "skip_unchanged_inputs"
            self._set_trilat_quality_metrics(
                device,
                geometry_quality_01=state.last_geometry_quality_01,
                residual_consistency_01=state.last_residual_consistency_01,
                gdop=state.last_geometry_gdop,
                condition_number=state.last_geometry_condition,
                normalized_residual_rms=state.last_normalized_residual_rms,
            )
            raw_confidence = self._compute_trilat_confidence(
                anchor_count=anchor_count,
                residual_m=state.last_residual_m,
                solver_dimension=state.last_solver_dimension,
                geometry_quality_01=state.last_geometry_quality_01,
                residual_consistency_01=state.last_residual_consistency_01,
                floor_ambiguous=floor_ambiguous_persisted,
                mean_sigma_m=state.last_mean_sigma_m,
            )
            self._set_trilat_confidence(device, raw_confidence)
            self._set_tracking_confidence(
                device,
                self._compute_tracking_confidence(
                    raw_score=raw_confidence,
                    state=state,
                    mobility_type=device.get_mobility_type(),
                    used_prior=False,
                    mean_anchor_range_delta_m=mean_anchor_range_delta_m,
                    geometry_quality_01=state.last_geometry_quality_01,
                    residual_consistency_01=state.last_residual_consistency_01,
                    floor_ambiguous=floor_ambiguous_persisted,
                ),
            )
            self._set_trilat_speed_diagnostics(device, state)
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

        # Step 10: look up the configured floor surface height for the phone-height Z prior.
        floor_z_m = self.get_floor_z_m(selected_floor_id)

        if solver_dimension == "3d":
            centroid_3d = anchor_centroid_3d(anchors)
            initial_guess_3d = centroid_3d
            solve_prior = self._build_trilat_solve_prior(
                state,
                nowstamp=nowstamp,
                mobility_type=device.get_mobility_type(),
                solver_dimension="3d",
                selected_floor_id=selected_floor_id,
                mean_sigma_m=mean_sigma_m,
                mean_anchor_range_delta_m=mean_anchor_range_delta_m,
                floor_z_m=floor_z_m,
                layout_hash=layout_hash,
            )
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
            if solve_prior is not None and (mean_anchor_range_delta_m is None or mean_anchor_range_delta_m <= self._TRILAT_MAX_RESIDUAL_M):
                initial_guess_3d = (solve_prior.x_m, solve_prior.y_m, solve_prior.z_m)
            used_prior = solve_prior is not None
            solve_result = solve_3d_soft_l1(anchors, initial_guess=initial_guess_3d, prior=solve_prior)
        else:
            centroid = anchor_centroid(anchors)
            initial_guess_2d = centroid
            solve_prior = self._build_trilat_solve_prior(
                state,
                nowstamp=nowstamp,
                mobility_type=device.get_mobility_type(),
                solver_dimension="2d",
                selected_floor_id=selected_floor_id,
                mean_sigma_m=mean_sigma_m,
                mean_anchor_range_delta_m=mean_anchor_range_delta_m,
                floor_z_m=floor_z_m,
                layout_hash=layout_hash,
            )
            if state.last_solution_xy is not None and selected_floor_id == state.floor_id:
                if math.hypot(
                    state.last_solution_xy[0] - centroid[0],
                    state.last_solution_xy[1] - centroid[1],
                ) <= self._TRILAT_MAX_RESIDUAL_M:
                    initial_guess_2d = state.last_solution_xy
            if solve_prior is not None and (mean_anchor_range_delta_m is None or mean_anchor_range_delta_m <= self._TRILAT_MAX_RESIDUAL_M):
                initial_guess_2d = (solve_prior.x_m, solve_prior.y_m)
            used_prior = solve_prior is not None
            solve_result = solve_2d_soft_l1(anchors, initial_guess=initial_guess_2d, prior=solve_prior)

        quality_metrics = self._compute_trilat_quality_metrics(
            anchors,
            solver_dimension=solver_dimension,
            x_m=solve_result.x_m,
            y_m=solve_result.y_m,
            z_m=solve_result.z_m,
        )

        state.last_anchor_ids = anchor_ids
        state.last_anchor_ranges = anchor_ranges
        state.last_anchor_z = anchor_z
        state.last_solver_dimension = solver_dimension
        state.last_mean_sigma_m = mean_sigma_m
        state.last_geometry_quality_01 = quality_metrics.geometry_quality_01
        state.last_residual_consistency_01 = quality_metrics.residual_consistency_01
        state.last_geometry_gdop = quality_metrics.gdop
        state.last_geometry_condition = quality_metrics.condition_number
        state.last_normalized_residual_rms = quality_metrics.normalized_residual_rms

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

            filtered_xy, filtered_z = self._apply_trilat_motion_filter(
                state,
                nowstamp=nowstamp,
                mobility_type=device.get_mobility_type(),
                measurement_xy=fallback_xy,
                measurement_z=(fallback_z if solver_dimension == "3d" else None),
                anchor_z_bounds=anchor_z_bounds,
                residual_m=fallback_residual,
                mean_sigma_m=mean_sigma_m,
            )

            device.set_trilat_solution(
                x_m=filtered_xy[0],
                y_m=filtered_xy[1],
                z_m=filtered_z if solver_dimension == "3d" or filtered_z is not None else None,
                floor_id=selected_floor_id,
                floor_name=selected_floor_name,
                anchor_count=anchor_count,
                residual_m=fallback_residual,
            )
            device.trilat_status = "low_confidence"
            device.trilat_reason = "high_residual_low_confidence"
            self._set_trilat_quality_metrics(
                device,
                geometry_quality_01=quality_metrics.geometry_quality_01,
                residual_consistency_01=quality_metrics.residual_consistency_01,
                gdop=quality_metrics.gdop,
                condition_number=quality_metrics.condition_number,
                normalized_residual_rms=quality_metrics.normalized_residual_rms,
            )
            raw_confidence = self._compute_trilat_confidence(
                anchor_count=anchor_count,
                residual_m=max(fallback_residual, self._TRILAT_MAX_RESIDUAL_M),
                solver_dimension=solver_dimension,
                geometry_quality_01=quality_metrics.geometry_quality_01,
                residual_consistency_01=quality_metrics.residual_consistency_01,
                floor_ambiguous=floor_ambiguous_persisted,
                mean_sigma_m=mean_sigma_m,
            )
            self._set_trilat_confidence(device, raw_confidence)
            self._set_tracking_confidence(
                device,
                self._compute_tracking_confidence(
                    raw_score=raw_confidence,
                    state=state,
                    mobility_type=device.get_mobility_type(),
                    used_prior=used_prior,
                    mean_anchor_range_delta_m=mean_anchor_range_delta_m,
                    geometry_quality_01=quality_metrics.geometry_quality_01,
                    residual_consistency_01=quality_metrics.residual_consistency_01,
                    floor_ambiguous=floor_ambiguous_persisted,
                ),
            )
            self._set_trilat_speed_diagnostics(device, state)
            state.last_solution_xy = filtered_xy
            state.last_solution_z = filtered_z if solver_dimension == "3d" or filtered_z is not None else None
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

        filtered_xy, filtered_z = self._apply_trilat_motion_filter(
            state,
            nowstamp=nowstamp,
            mobility_type=device.get_mobility_type(),
            measurement_xy=(solve_result.x_m, solve_result.y_m),
            measurement_z=(solve_result.z_m if solver_dimension == "3d" else None),
            anchor_z_bounds=anchor_z_bounds,
            residual_m=solve_result.residual_rms_m,
            mean_sigma_m=mean_sigma_m,
        )

        device.set_trilat_solution(
            x_m=filtered_xy[0],
            y_m=filtered_xy[1],
            z_m=filtered_z if solver_dimension == "3d" or filtered_z is not None else None,
            floor_id=selected_floor_id,
            floor_name=selected_floor_name,
            anchor_count=anchor_count,
            residual_m=solve_result.residual_rms_m,
        )
        self._set_trilat_quality_metrics(
            device,
            geometry_quality_01=quality_metrics.geometry_quality_01,
            residual_consistency_01=quality_metrics.residual_consistency_01,
            gdop=quality_metrics.gdop,
            condition_number=quality_metrics.condition_number,
            normalized_residual_rms=quality_metrics.normalized_residual_rms,
        )
        state.last_solution_xy = filtered_xy
        state.last_solution_z = filtered_z if solver_dimension == "3d" or filtered_z is not None else None
        state.last_residual_m = solve_result.residual_rms_m
        state.last_status = "ok"
        raw_confidence = self._compute_trilat_confidence(
            anchor_count=anchor_count,
            residual_m=solve_result.residual_rms_m,
            solver_dimension=solver_dimension,
            geometry_quality_01=quality_metrics.geometry_quality_01,
            residual_consistency_01=quality_metrics.residual_consistency_01,
            floor_ambiguous=floor_ambiguous_persisted,
            mean_sigma_m=mean_sigma_m,
        )
        self._set_trilat_confidence(device, raw_confidence)
        self._set_tracking_confidence(
            device,
            self._compute_tracking_confidence(
                raw_score=raw_confidence,
                state=state,
                mobility_type=device.get_mobility_type(),
                used_prior=used_prior,
                mean_anchor_range_delta_m=mean_anchor_range_delta_m,
                geometry_quality_01=quality_metrics.geometry_quality_01,
                residual_consistency_01=quality_metrics.residual_consistency_01,
                floor_ambiguous=floor_ambiguous_persisted,
            ),
        )
        self._set_trilat_speed_diagnostics(device, state)
        if layout_hash and device.trilat_x_m is not None and device.trilat_y_m is not None:
            self._update_zone_traversal_tracker(
                state,
                nowstamp=nowstamp,
                x_m=device.trilat_x_m,
                y_m=device.trilat_y_m,
                z_m=device.trilat_z_m if device.trilat_z_m is not None else 0.0,
                geometry_quality_01=quality_metrics.geometry_quality_01,
                layout_hash=layout_hash,
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
            self._restore_scanner_anchor_from_store(bermuda_scanner)

            if bermuda_scanner.area_id is None:
                _scanners_without_areas.append(f"{bermuda_scanner.name} [{bermuda_scanner.address}]")
        self._async_manage_repair_scanners_without_areas(_scanners_without_areas)
        self._async_manage_repair_calibration_layout_mismatch()

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

    @staticmethod
    def _parse_calibration_position(call_data: dict) -> tuple[float, float, float]:
        """Return calibration sample coordinates from packed or split service inputs."""
        packed_xyz = call_data.get("x_y_z_m")
        if packed_xyz is not None:
            parts = [part.strip() for part in str(packed_xyz).split(",")]
            if len(parts) != 3 or any(part == "" for part in parts):
                raise vol.Invalid("x_y_z_m must be provided as 'x,y,z' in metres.")
            try:
                return tuple(float(part) for part in parts)
            except ValueError as err:
                raise vol.Invalid("x_y_z_m must contain numeric x, y, and z values.") from err

        missing = [field for field in ("x_m", "y_m", "z_m") if field not in call_data]
        if missing:
            raise vol.Invalid("Provide either x_y_z_m or all of x_m, y_m, and z_m.")

        return (float(call_data["x_m"]), float(call_data["y_m"]), float(call_data["z_m"]))

    async def service_record_calibration_sample(self, call: ServiceCall) -> ServiceResponse:
        """Start an asynchronous calibration sample capture session."""
        sample_radius_m = call.data.get("sample_radius_m")
        if sample_radius_m is None:
            sample_radius_m = call.data.get("room_radius_m", DEFAULT_SAMPLE_RADIUS_M)
        x_m, y_m, z_m = self._parse_calibration_position(call.data)
        try:
            response = await self.calibration.async_start_session(
                device_id=call.data["device_id"],
                room_area_id=call.data["room_area_id"],
                x_m=x_m,
                y_m=y_m,
                z_m=z_m,
                sample_radius_m=sample_radius_m,
                duration_s=call.data.get("duration_s", 60),
                notes=call.data.get("notes") or None,
            )
        except HomeAssistantError as err:
            raise vol.Invalid(str(err)) from err
        return response

    async def service_record_transition_sample(self, call: ServiceCall) -> ServiceResponse:
        """Start a timed Bermuda-native transition sample capture."""
        x_m, y_m, z_m = self._parse_calibration_position(call.data)
        try:
            response = await self.calibration.async_start_transition_session(
                device_id=call.data["device_id"],
                room_area_id=call.data["room_area_id"],
                transition_name=call.data["transition_name"],
                x_m=x_m,
                y_m=y_m,
                z_m=z_m,
                sample_radius_m=call.data.get("sample_radius_m", DEFAULT_SAMPLE_RADIUS_M),
                capture_duration_s=call.data.get("capture_duration_s", 60),
                transition_floor_ids=list(call.data["transition_floor_ids"]),
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
