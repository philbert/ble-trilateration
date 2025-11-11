"""Adds config flow for Bermuda BLE Trilateration."""

from __future__ import annotations

from typing import TYPE_CHECKING

import voluptuous as vol
from bluetooth_data_tools import monotonic_time_coarse
from homeassistant import config_entries
from homeassistant.config_entries import OptionsFlowWithConfigEntry
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.selector import (
    DeviceSelector,
    DeviceSelectorConfig,
    ObjectSelector,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    ADDR_TYPE_IBEACON,
    ADDR_TYPE_PRIVATE_BLE_DEVICE,
    BDADDR_TYPE_RANDOM_RESOLVABLE,
    CONF_ATTENUATION,
    CONF_DEVICES,
    CONF_DEVTRACK_TIMEOUT,
    CONF_MAX_RADIUS,
    CONF_MAX_VELOCITY,
    CONF_REF_POWER,
    CONF_RSSI_OFFSETS,
    CONF_SAVE_AND_CLOSE,
    CONF_SCANNER_ATTENUATION,
    CONF_SCANNER_INFO,
    CONF_SCANNER_MAX_RADIUS,
    CONF_SCANNERS,
    CONF_SMOOTHING_SAMPLES,
    CONF_UPDATE_INTERVAL,
    DEFAULT_ATTENUATION,
    DEFAULT_DEVTRACK_TIMEOUT,
    DEFAULT_MAX_RADIUS,
    DEFAULT_MAX_VELOCITY,
    DEFAULT_REF_POWER,
    DEFAULT_SMOOTHING_SAMPLES,
    DEFAULT_UPDATE_INTERVAL,
    DISTANCE_INFINITE,
    DOMAIN,
    DOMAIN_PRIVATE_BLE_DEVICE,
    NAME,
)
from .util import mac_norm, mac_redact, rssi_to_metres

if TYPE_CHECKING:
    from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
    from homeassistant.config_entries import ConfigFlowResult

    from . import BermudaConfigEntry
    from .bermuda_device import BermudaDevice
    from .coordinator import BermudaDataUpdateCoordinator

# from homeassistant import data_entry_flow

# from homeassistant.helpers.aiohttp_client import async_create_clientsession


class BermudaFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for bermuda."""

    VERSION = 1
    # CONNECTION_CLASS = config_entries.CONN_CLASS_CLOUD_POLL

    def __init__(self) -> None:
        """Initialize."""
        self._errors = {}

    async def async_step_bluetooth(self, discovery_info: BluetoothServiceInfoBleak) -> ConfigFlowResult:
        """
        Support automatic initiation of setup through bluetooth discovery.
        (we still show a confirmation form to the user, though)
        This is triggered by discovery matchers set in manifest.json,
        and since we track any BLE advert, we're being a little cheeky by listing any.
        """
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        # Create a unique ID so that we don't get multiple discoveries appearing.
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        return self.async_show_form(step_id="user", description_placeholders={"name": NAME})

    async def async_step_user(self, user_input=None):
        """
        Handle a flow initialized by the user.

        We don't need any config for base setup, so we just activate
        (but only for one instance)
        """
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        if user_input is not None:
            # create the integration!
            return self.async_create_entry(title=NAME, data={"source": "user"}, description=NAME)

        return self.async_show_form(step_id="user", description_placeholders={"name": NAME})

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return BermudaOptionsFlowHandler(config_entry)

    # async def _show_config_form(self, user_input):  # pylint: disable=unused-argument
    #     """Show the configuration form to edit location data."""
    #     return self.async_show_form(
    #         step_id="user",
    #         data_schema=vol.Schema(
    #             {vol.Required(CONF_USERNAME): str, vol.Required(CONF_PASSWORD): str}
    #         ),
    #         errors=self._errors,
    #     )


class BermudaOptionsFlowHandler(OptionsFlowWithConfigEntry):
    """Config flow options handler for bermuda."""

    def __init__(self, config_entry: BermudaConfigEntry) -> None:
        """Initialize HACS options flow."""
        super().__init__(config_entry)
        self.coordinator: BermudaDataUpdateCoordinator
        self.devices: dict[str, BermudaDevice]
        self._last_ref_power = None
        self._last_device = None
        self._last_scanner = None
        self._last_attenuation = None
        self._last_scanner_info = None

    async def async_step_init(self, user_input=None):  # pylint: disable=unused-argument
        """Manage the options."""
        self.coordinator = self.config_entry.runtime_data.coordinator
        self.devices = self.coordinator.devices

        messages = {}
        active_devices = self.coordinator.count_active_devices()
        active_scanners = self.coordinator.count_active_scanners()

        messages["device_counter_active"] = f"{active_devices}"
        messages["device_counter_devices"] = f"{len(self.devices)}"
        messages["scanner_counter_active"] = f"{active_scanners}"
        messages["scanner_counter_scanners"] = f"{len(self.coordinator.scanner_list)}"

        if len(self.coordinator.scanner_list) == 0:
            messages["status"] = (
                "You need to configure some bluetooth scanners before Bermuda will have anything to work with. "
                "Any one of esphome bluetooth_proxy, Shelly bluetooth proxy or local bluetooth adaptor should get "
                "you started."
            )
        elif active_devices == 0:
            messages["status"] = (
                "No bluetooth devices are actively being reported from your scanners. "
                "You will need to solve this before Bermuda can be of much help."
            )
        else:
            messages["status"] = "You have at least some active devices, this is good."

        # Build a markdown table of scanners so the user can see what's up.
        scanner_table = "\n\nStatus of scanners:\n\n|Scanner|Address|Last advertisement|\n|---|---|---:|\n"
        # Use emoji to indicate if age is "good"
        for scanner in self.coordinator.get_active_scanner_summary():
            age = int(scanner.get("last_stamp_age", 999))
            if age < 2:
                status = '<ha-icon icon="mdi:check-circle-outline"></ha-icon>'
            elif age < 10:
                status = '<ha-icon icon="mdi:alert-outline"></ha-icon>'
            else:
                status = '<ha-icon icon="mdi:skull-crossbones"></ha-icon>'
            # Remove centre octets from mac for condensed, privatised display
            shortmac = mac_redact(scanner.get("address", "ERR"))
            scanner_table += (
                f"| {scanner.get('name', 'NAME_ERR')}| [{shortmac}]"
                f"| {status} {(scanner.get('last_stamp_age', DISTANCE_INFINITE)):.2f} seconds ago.|\n"
            )
        messages["status"] += scanner_table

        # return await self.async_step_globalopts()
        return self.async_show_menu(
            step_id="init",
            menu_options={
                "globalopts": "Global Options",
                "selectdevices": "Select Devices",
                "calibration1_global": "Calibration 1: Global Settings",
                "calibration2_scanners": "Calibration 2: Per-Scanner Configuration",
            },
            description_placeholders=messages,
        )

    async def async_step_globalopts(self, user_input=None):
        """Handle global options flow."""
        if user_input is not None:
            self.options.update(user_input)
            return await self._update_options()

        data_schema = {
            vol.Required(
                CONF_MAX_RADIUS,
                default=self.options.get(CONF_MAX_RADIUS, DEFAULT_MAX_RADIUS),
            ): vol.Coerce(float),
            vol.Required(
                CONF_MAX_VELOCITY,
                default=self.options.get(CONF_MAX_VELOCITY, DEFAULT_MAX_VELOCITY),
            ): vol.Coerce(float),
            vol.Required(
                CONF_DEVTRACK_TIMEOUT,
                default=self.options.get(CONF_DEVTRACK_TIMEOUT, DEFAULT_DEVTRACK_TIMEOUT),
            ): vol.Coerce(int),
            vol.Required(
                CONF_UPDATE_INTERVAL,
                default=self.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL),
            ): vol.Coerce(float),
            vol.Required(
                CONF_SMOOTHING_SAMPLES,
                default=self.options.get(CONF_SMOOTHING_SAMPLES, DEFAULT_SMOOTHING_SAMPLES),
            ): vol.Coerce(int),
            vol.Required(
                CONF_ATTENUATION,
                default=self.options.get(CONF_ATTENUATION, DEFAULT_ATTENUATION),
            ): vol.Coerce(float),
            vol.Required(
                CONF_REF_POWER,
                default=self.options.get(CONF_REF_POWER, DEFAULT_REF_POWER),
            ): vol.Coerce(float),
        }

        return self.async_show_form(step_id="globalopts", data_schema=vol.Schema(data_schema))

    async def async_step_selectdevices(self, user_input=None):
        """Handle a flow initialized by the user."""
        if user_input is not None:
            self.options.update(user_input)
            return await self._update_options()

        # Grab the co-ordinator's device list so we can build a selector from it.
        self.devices = self.config_entry.runtime_data.coordinator.devices

        # Where we store the options before building the selector
        options_list = []
        options_metadevices = []  # These will be first in the list
        options_otherdevices = []  # These will be last.
        options_randoms = []  # Random MAC addresses - very last!

        for device in self.devices.values():
            # Iterate through all the discovered devices to build the options list

            name = device.name

            if device.is_scanner:
                # We don't "track" scanner devices, per se
                continue
            if device.address_type == ADDR_TYPE_PRIVATE_BLE_DEVICE:
                # Private BLE Devices get configured automagically, skip
                continue
            if device.address_type == ADDR_TYPE_IBEACON:
                # This is an iBeacon meta-device
                if len(device.metadevice_sources) > 0:
                    source_mac = f"[{device.metadevice_sources[0].upper()}]"
                else:
                    source_mac = ""

                options_metadevices.append(
                    SelectOptionDict(
                        value=device.address.upper(),
                        label=f"iBeacon: {device.address.upper()} {source_mac} "
                        f"{name if device.address.upper() != name.upper() else ''}",
                    )
                )
                continue

            if device.address_type == BDADDR_TYPE_RANDOM_RESOLVABLE:
                # This is a random MAC, we should tag it as such

                if device.last_seen < monotonic_time_coarse() - (60 * 60 * 2):  # two hours
                    # A random MAC we haven't seen for a while is not much use, skip
                    continue

                options_randoms.append(
                    SelectOptionDict(
                        value=device.address.upper(),
                        label=f"[{device.address.upper()}] {name} (Random MAC)",
                    )
                )
                continue

            # Default, unremarkable devices, just pop them in the list.
            options_otherdevices.append(
                SelectOptionDict(
                    value=device.address.upper(),
                    label=f"[{device.address.upper()}] {name}",
                )
            )

        # build the final list with "preferred" devices first.
        options_metadevices.sort(key=lambda item: item["label"])
        options_otherdevices.sort(key=lambda item: item["label"])
        options_randoms.sort(key=lambda item: item["label"])
        options_list.extend(options_metadevices)
        options_list.extend(options_otherdevices)
        options_list.extend(options_randoms)

        for address in self.options.get(CONF_DEVICES, []):
            # Now check for any configured devices that weren't discovered, and add them
            if not next(
                (item for item in options_list if item["value"] == address.upper()),
                False,
            ):
                options_list.append(SelectOptionDict(value=address.upper(), label=f"[{address}] (saved)"))

        data_schema = {
            vol.Optional(
                CONF_DEVICES,
                default=self.options.get(CONF_DEVICES, []),
            ): SelectSelector(SelectSelectorConfig(options=options_list, multiple=True)),
        }

        return self.async_show_form(step_id="selectdevices", data_schema=vol.Schema(data_schema))

    async def async_step_calibration1_global(self, user_input=None):
        # FIXME: This is ridiculous. But I can't yet find a better way.
        _ugly_token_hack = {
            # These are work-arounds for (broken?) placeholder substitutions.
            # I've not been able to find out why, but just having <details> in the
            # en.json will cause placeholders to break, due to *something* treating
            # the html elements as placeholders.
            "details": "<details>",
            "details_end": "</details>",
            "summary": "<summary>",
            "summary_end": "</summary>",
        }

        if user_input is not None:
            if user_input[CONF_SAVE_AND_CLOSE]:
                # Update the running options (this propagates to coordinator etc)
                self.options.update(
                    {
                        CONF_ATTENUATION: user_input[CONF_ATTENUATION],
                        CONF_REF_POWER: user_input[CONF_REF_POWER],
                    }
                )
                # Ideally, we'd like to just save out the config entry and return to the main menu.
                # Unfortunately, doing so seems to break the chosen device (for at least 15 seconds or so)
                # until it gets re-invigorated. My guess is that the link between coordinator and the
                # sensor entity might be getting broken, but not entirely sure.
                # For now disabling the return-to-menu and instead we finish out the flow.

                # Previous block for returning to menu:
                # # Let's update the options - but we don't want to call create entry as that will close the flow.
                # # This will save out the config entry:
                # self.hass.config_entries.async_update_entry(self.config_entry, options=self.options)
                # Reset last device so that the next step doesn't think it exists.
                # self._last_device = None
                # return await self.async_step_init()

                # Current block for finishing the flow:
                return await self._update_options()

            self._last_ref_power = user_input[CONF_REF_POWER]
            self._last_attenuation = user_input[CONF_ATTENUATION]
            self._last_device = user_input[CONF_DEVICES]
            self._last_scanner = user_input[CONF_SCANNERS]

        # TODO: Switch this to be a device selector when devices are made for scanners
        scanner_options = [
            SelectOptionDict(
                value=scanner,
                label=self.coordinator.devices[scanner].name if scanner in self.coordinator.devices else scanner,
            )
            for scanner in self.coordinator.scanner_list
        ]
        data_schema = {
            vol.Required(
                CONF_DEVICES,
                default=self._last_device if self._last_device is not None else vol.UNDEFINED,
            ): DeviceSelector(DeviceSelectorConfig(integration=DOMAIN)),
            vol.Required(
                CONF_SCANNERS,
                default=self._last_scanner if self._last_scanner is not None else vol.UNDEFINED,
            ): SelectSelector(
                SelectSelectorConfig(
                    options=scanner_options,
                    multiple=False,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required(
                CONF_REF_POWER,
                default=self._last_ref_power
                if self._last_ref_power is not None
                else self.options.get(CONF_REF_POWER, DEFAULT_REF_POWER),
            ): vol.Coerce(float),
            vol.Required(
                CONF_ATTENUATION,
                default=self._last_attenuation
                if self._last_attenuation is not None
                else self.options.get(CONF_ATTENUATION, DEFAULT_ATTENUATION),
            ): vol.Coerce(float),
            vol.Optional(CONF_SAVE_AND_CLOSE, default=False): vol.Coerce(bool),
        }
        if user_input is None:
            return self.async_show_form(
                step_id="calibration1_global",
                data_schema=vol.Schema(data_schema),
                description_placeholders=_ugly_token_hack
                | {"suffix": "After you click Submit, the new distances will be shown here."},
            )
        results_str = ""
        device = self._get_bermuda_device_from_registry(user_input[CONF_DEVICES])
        if device is not None:
            scanner = device.get_scanner(user_input[CONF_SCANNERS])
            if scanner is None:
                return self.async_show_form(
                    step_id="calibration1_global",
                    errors={"err_scanner_no_record": "The selected scanner hasn't (yet) seen this device."},
                    data_schema=vol.Schema(data_schema),
                    description_placeholders=_ugly_token_hack
                    | {"suffix": "After you click Submit, the new distances will be shown here."},
                )

            distances = [
                rssi_to_metres(historical_rssi, self._last_ref_power, self._last_attenuation)
                for historical_rssi in scanner.hist_rssi
            ]

            # Build a markdown table showing distance and rssi history for the
            # selected device / scanner combination
            results_str = f"| {device.name} |"
            # Limit the number of columns to what's available up to a max of 5.
            cols = min(5, len(distances), len(scanner.hist_rssi))
            for i in range(cols):
                results_str += f" {i} |"
            results_str += "\n|---|"
            for i in range(cols):  # noqa for unused var i
                results_str += "---:|"

            results_str += "\n| Estimate (m) |"
            for i in range(cols):
                results_str += f" `{distances[i]:>5.2f}`|"
            results_str += "\n| RSSI Actual |"
            for i in range(cols):
                results_str += f" `{scanner.hist_rssi[i]:>5}`|"
            results_str += "\n"

        return self.async_show_form(
            step_id="calibration1_global",
            data_schema=vol.Schema(data_schema),
            description_placeholders=_ugly_token_hack
            | {
                "suffix": (
                    f"Recent distances, calculated using `ref_power = {self._last_ref_power}` "
                    f"and `attenuation = {self._last_attenuation}` (values from new...old):\n\n{results_str}"
                ),
            },
        )

    async def async_step_calibration2_scanners(self, user_input=None):
        """
        Per-scanner configuration.

        Configure individual settings for each BLE scanner/proxy:
        - RSSI Offset: Fine-tune signal strength readings (advanced)
        - Attenuation: Environmental absorption factor (lower=open space, higher=thick walls)
        - Max Radius: Maximum tracking distance for this scanner (in meters)

        Select a device to see real-time distance estimates for calibration.

        NOTE: Attenuation and Max Radius settings are currently UI-only and not yet applied
        to calculations. This will be enabled in a future update.
        """
        if user_input is not None:
            if user_input[CONF_SAVE_AND_CLOSE]:
                # Convert the name-based nested dict to address-based dicts
                rssi_offset_by_address = {}
                attenuation_by_address = {}
                max_radius_by_address = {}

                # Get global defaults for fallback
                global_attenuation = self.options.get(CONF_ATTENUATION, DEFAULT_ATTENUATION)
                global_max_radius = self.options.get(CONF_MAX_RADIUS, DEFAULT_MAX_RADIUS)

                for address in self.coordinator.scanner_list:
                    scanner_name = self.coordinator.devices[address].name
                    scanner_data = user_input[CONF_SCANNER_INFO].get(scanner_name, {})

                    # RSSI Offset - clip to sensible range, fixes #497
                    rssi_val = scanner_data.get("rssi_offset", 0)
                    rssi_offset_by_address[address] = max(min(rssi_val, 127), -127)

                    # Attenuation - store if different from global default
                    atten_val = scanner_data.get("attenuation", global_attenuation)
                    if atten_val != global_attenuation:
                        attenuation_by_address[address] = max(min(float(atten_val), 10.0), 1.0)

                    # Max Radius - store if different from global default
                    radius_val = scanner_data.get("max_radius", global_max_radius)
                    if radius_val != global_max_radius:
                        max_radius_by_address[address] = max(min(float(radius_val), 100.0), 1.0)

                self.options.update({
                    CONF_RSSI_OFFSETS: rssi_offset_by_address,
                    CONF_SCANNER_ATTENUATION: attenuation_by_address,
                    CONF_SCANNER_MAX_RADIUS: max_radius_by_address,
                })

                return await self._update_options()

            # User clicked Submit without Save - refresh display with current values
            self._last_scanner_info = user_input[CONF_SCANNER_INFO]
            self._last_device = user_input.get(CONF_DEVICES)

        # Load saved values and global defaults
        saved_rssi_offsets = self.options.get(CONF_RSSI_OFFSETS, {})
        saved_attenuations = self.options.get(CONF_SCANNER_ATTENUATION, {})
        saved_max_radii = self.options.get(CONF_SCANNER_MAX_RADIUS, {})
        global_attenuation = self.options.get(CONF_ATTENUATION, DEFAULT_ATTENUATION)
        global_max_radius = self.options.get(CONF_MAX_RADIUS, DEFAULT_MAX_RADIUS)

        # For now, always show all scanners - we'll filter later once we understand the data
        scanners_to_show = self.coordinator.scanner_list
        selected_device = None
        debug_info = []

        if self._last_device:
            debug_info.append(f"registry_id: {self._last_device}")

            devreg = dr.async_get(self.hass)
            ha_device = devreg.async_get(self._last_device)
            debug_info.append(f"ha_device found: {ha_device is not None}")

            if ha_device:
                debug_info.append(f"ha_device.name: {ha_device.name}")
                debug_info.append(f"connections: {ha_device.connections}")

                for conn in ha_device.connections:
                    debug_info.append(f"  checking connection: {conn}")
                    if conn[0] in {"private_ble_device", "bluetooth", "ibeacon"}:
                        address = conn[1]
                        normalized = mac_norm(address)
                        debug_info.append(f"  address: {address}, normalized: {normalized}")
                        debug_info.append(f"  in coordinator.devices? {normalized in self.coordinator.devices}")

                        if normalized in self.coordinator.devices:
                            selected_device = self.coordinator.devices[normalized]
                            debug_info.append(f"  GOT DEVICE: {selected_device.name}")
                            break
                        else:
                            debug_info.append(f"  NOT FOUND IN COORDINATOR")

        # Build nested dict for scanners to display
        scanner_config_dict = {}
        for scanner in scanners_to_show:
            scanner_name = self.coordinator.devices[scanner].name
            scanner_config_dict[scanner_name] = {
                "rssi_offset": saved_rssi_offsets.get(scanner, 0),
                "attenuation": saved_attenuations.get(scanner, global_attenuation),
                "max_radius": saved_max_radii.get(scanner, global_max_radius),
            }

        data_schema = {
            vol.Optional(
                CONF_DEVICES,
                default=self._last_device if self._last_device is not None else vol.UNDEFINED,
            ): DeviceSelector(DeviceSelectorConfig(integration=DOMAIN)),
            vol.Required(
                CONF_SCANNER_INFO,
                default=scanner_config_dict if not self._last_scanner_info else self._last_scanner_info,
            ): ObjectSelector(),
            vol.Optional(CONF_SAVE_AND_CLOSE, default=False): vol.Coerce(bool),
        }

        # Build description with helpful information
        description = (
            "**Configure per-scanner settings:**\n\n"
            "- **rssi_offset** (Advanced): Adjust signal strength, typically -10 to +10\n"
            "- **attenuation**: Environmental factor, 2.0-2.5 for open space, 4.0-5.0 for thick walls/concrete\n"
            "- **max_radius**: Maximum tracking distance in meters for this scanner\n\n"
            f"*Global defaults: attenuation={global_attenuation}, max_radius={global_max_radius}m*\n\n"
        )

        # Debug device lookup
        if debug_info:
            description += "---\n\n## 🔍 LOOKUP DEBUG:\n\n"
            for line in debug_info:
                description += f"{line}\n\n"
            description += f"selected_device is None? {selected_device is None}\n\n"
            description += f"selected_device value: {selected_device}\n\n"

        # If a device is selected, dump EVERYTHING about it
        if selected_device:
            description += "---\n\n## 📋 DEVICE DEBUG DUMP\n\n"
            description += f"**name:** {selected_device.name}\n\n"
            description += f"**address:** {selected_device.address}\n\n"
            description += f"**area_name:** {selected_device.area_name}\n\n"
            description += f"**area_distance:** {selected_device.area_distance}\n\n"
            description += f"**area_rssi:** {selected_device.area_rssi}\n\n"
            description += f"**area_advert:** {selected_device.area_advert}\n\n"

            if selected_device.area_advert:
                description += "**area_advert details:**\n\n"
                description += f"- scanner_address: {selected_device.area_advert.scanner_address}\n"
                description += f"- rssi: {selected_device.area_advert.rssi}\n"
                description += f"- rssi_distance: {selected_device.area_advert.rssi_distance}\n"
                description += f"- area_id: {selected_device.area_advert.area_id}\n"
                description += f"- area_name: {selected_device.area_advert.area_name}\n\n"

            description += f"**adverts (all scanners seeing this device):**\n\n"
            for key, advert in selected_device.adverts.items():
                description += f"- Scanner: {advert.scanner_address}\n"
                description += f"  - distance: {advert.rssi_distance}\n"
                description += f"  - rssi: {advert.rssi}\n\n"

        elif self._last_device:
            # Device ID provided but couldn't find the Bermuda device
            # Show debug info to help troubleshoot
            devreg = dr.async_get(self.hass)
            ha_device = devreg.async_get(self._last_device)

            description += "\n⚠️ **Device Lookup Failed - Debug Info:**\n\n"

            if ha_device:
                description += f"- HA Device: {ha_device.name}\n"
                description += f"- Connections: {list(ha_device.connections)}\n"

                # Try to find what address we extracted
                device_address = None
                for connection in ha_device.connections:
                    if connection[0] in {DOMAIN_PRIVATE_BLE_DEVICE, dr.CONNECTION_BLUETOOTH, "ibeacon"}:
                        device_address = connection[1]
                        break

                if device_address:
                    normalized = mac_norm(device_address)
                    description += f"- Extracted address: `{device_address}`\n"
                    description += f"- Normalized: `{normalized}`\n"
                    description += f"- In coordinator.devices? {normalized in self.coordinator.devices}\n"

                    if normalized in self.coordinator.devices:
                        bermuda_dev = self.coordinator.devices[normalized]
                        description += f"- create_sensor flag: {bermuda_dev.create_sensor}\n"
                        description += f"- Is scanner: {bermuda_dev._is_scanner}\n"

                    description += "\n"
                    # Show some coordinator device keys for comparison
                    sample_keys = list(self.coordinator.devices.keys())[:5]
                    description += f"- Sample coordinator keys: {sample_keys}\n"
                else:
                    description += "- Could not extract bluetooth address from connections\n"
            else:
                description += "- HA device not found in registry\n"

            description += (
                "\n*This device may not be configured in 'Select Devices' or may be using "
                "a different address format (iBeacon UUID, IRK, etc.)*\n"
            )
        else:
            # No device selected - show all scanners for manual configuration
            description += (
                "\n💡 **Calibration Workflow:**\n"
                "1. Place a tracked device near the scanner you want to calibrate\n"
                "2. Select that device from the dropdown above\n"
                "3. Click **Submit** - you'll see only that scanner's settings\n"
                "4. Adjust settings until the distance is accurate\n"
                "5. Check **Save and Close** when done\n"
                "6. Repeat for other scanners\n\n"
                "*Or edit all scanner settings manually below:*\n"
            )

        description += (
            "\n\n⚠️ **Note:** Attenuation and max_radius are currently saved but not yet applied to tracking. "
            "Only rssi_offset is active."
        )

        return self.async_show_form(
            step_id="calibration2_scanners",
            data_schema=vol.Schema(data_schema),
            description_placeholders={"suffix": description},
        )

    def _get_bermuda_device_from_registry(self, registry_id: str) -> BermudaDevice | None:
        """
        Given a device registry device id, return the associated BermudaDevice.

        Returns None if the id can not be resolved to a tracked device.
        """
        from .const import _LOGGER

        devreg = dr.async_get(self.hass)
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
            normalized_address in self.coordinator.devices,
        )

        if normalized_address in self.coordinator.devices:
            result = self.coordinator.devices[normalized_address]
            _LOGGER.debug("_get_bermuda_device: Found! Returning device %s", result.name)
            return result

        # Try lowercase as fallback
        if device_address.lower() in self.coordinator.devices:
            result = self.coordinator.devices[device_address.lower()]
            _LOGGER.debug("_get_bermuda_device: Found via lowercase! Returning device %s", result.name)
            return result

        # We couldn't match the HA device id to a bermuda device mac.
        _LOGGER.warning("_get_bermuda_device: Address %s not found in coordinator.devices", normalized_address)
        return None

    async def _update_options(self):
        """Update config entry options."""
        return self.async_create_entry(title=NAME, data=self.options)
