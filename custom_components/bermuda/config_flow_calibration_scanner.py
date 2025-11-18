"""Per-scanner calibration flow handlers for Bermuda config flow."""

from __future__ import annotations

from typing import TYPE_CHECKING

import voluptuous as vol
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.selector import (
    DeviceSelector,
    DeviceSelectorConfig,
    ObjectSelector,
)

from .const import (
    get_logger,
    CONF_ATTENUATION,
    CONF_DEVICES,
    CONF_MAX_RADIUS,
    CONF_RSSI_OFFSETS,
    CONF_SCANNER_ATTENUATION,
    CONF_SCANNER_INFO,
    CONF_SCANNER_MAX_RADIUS,
    DEFAULT_ATTENUATION,
    DEFAULT_MAX_RADIUS,
    DOMAIN,
)
from .util import mac_norm

_LOGGER = get_logger(__name__)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigFlowResult


class BermudaCalibrationScannerFlowMixin:
    """Mixin class for Bermuda per-scanner calibration flow."""

    async def async_step_calibration2_scanners(self, user_input=None) -> ConfigFlowResult:
        """
        Per-scanner configuration.

        Configure individual settings for each BLE scanner/proxy:
        - RSSI Offset: Fine-tune signal strength readings (advanced)
        - Attenuation: Environmental absorption factor (lower=open space, higher=thick walls)
        - Max Radius: Maximum tracking distance for this scanner (in meters)

        Select a device to see real-time distance estimates for calibration.
        """
        if user_input is not None:
            # Always save on submit - merge submitted values with existing saved values
            # Load existing saved values
            saved_rssi_offsets = self.options.get(CONF_RSSI_OFFSETS, {})
            saved_attenuations = self.options.get(CONF_SCANNER_ATTENUATION, {})
            saved_max_radii = self.options.get(CONF_SCANNER_MAX_RADIUS, {})

            # Get global defaults for fallback
            global_attenuation = self.options.get(CONF_ATTENUATION, DEFAULT_ATTENUATION)
            global_max_radius = self.options.get(CONF_MAX_RADIUS, DEFAULT_MAX_RADIUS)

            # Track which scanners had their config modified for targeted reload
            modified_scanners = set()

            # Process the submitted scanner info (may be filtered to just one scanner)
            for scanner_name, scanner_data in user_input[CONF_SCANNER_INFO].items():
                # Find the scanner address from the name
                scanner_address = None
                for address in self.coordinator.scanner_list:
                    if self.coordinator.devices[address].name == scanner_name:
                        scanner_address = address
                        break

                if scanner_address:
                    # Track that this scanner was in the submitted form
                    modified_scanners.add(scanner_address)

                    _LOGGER.debug("Processing scanner '%s' address=%s",
                                  scanner_name, scanner_address)

                    # RSSI Offset - clip to sensible range, fixes #497
                    rssi_val = scanner_data.get("rssi_offset", 0)
                    saved_rssi_offsets[scanner_address] = max(min(rssi_val, 127), -127)

                    # Attenuation - store if different from global default
                    atten_val = scanner_data.get("attenuation", global_attenuation)
                    if atten_val != global_attenuation:
                        saved_attenuations[scanner_address] = max(min(float(atten_val), 10.0), 1.0)
                        _LOGGER.debug("Saved attenuation=%s for %s (global=%s)",
                                      saved_attenuations[scanner_address], scanner_address, global_attenuation)
                    elif scanner_address in saved_attenuations:
                        # Value matches global default, remove override
                        del saved_attenuations[scanner_address]
                        _LOGGER.debug("Removed attenuation override for %s (matches global)",
                                      scanner_address)

                    # Max Radius - store if different from global default
                    radius_val = scanner_data.get("max_radius", global_max_radius)
                    if radius_val != global_max_radius:
                        saved_max_radii[scanner_address] = max(min(float(radius_val), 100.0), 1.0)
                    elif scanner_address in saved_max_radii:
                        # Value matches global default, remove override
                        del saved_max_radii[scanner_address]

            # Save the merged values
            self.options.update({
                CONF_RSSI_OFFSETS: saved_rssi_offsets,
                CONF_SCANNER_ATTENUATION: saved_attenuations,
                CONF_SCANNER_MAX_RADIUS: saved_max_radii,
            })

            # Save without closing - update the config entry
            self.hass.config_entries.async_update_entry(self.config_entry, options=self.options)

            # Update coordinator's options and reload advert configs for immediate effect
            # Only reload adverts for the scanners that were actually modified (more efficient)
            _LOGGER.debug("About to reload configs for scanners: %s", modified_scanners)
            _LOGGER.debug("Updated options - CONF_SCANNER_ATTENUATION: %s",
                          self.options.get(CONF_SCANNER_ATTENUATION))
            self.coordinator.options.update(self.options)
            self.coordinator.reload_advert_configs(scanner_addresses=modified_scanners)

            # Update state and refresh display
            new_device = user_input.get(CONF_DEVICES)

            # If a device is selected, always clear scanner info so we rebuild based on
            # the CURRENT nearest scanner (which may have changed)
            if new_device:
                self._last_scanner_info = None
            else:
                # No device selected - keep user's edits to all scanners
                self._last_scanner_info = user_input[CONF_SCANNER_INFO]

            self._last_device = new_device

        # Load saved values and global defaults
        saved_rssi_offsets = self.options.get(CONF_RSSI_OFFSETS, {})
        saved_attenuations = self.options.get(CONF_SCANNER_ATTENUATION, {})
        saved_max_radii = self.options.get(CONF_SCANNER_MAX_RADIUS, {})
        global_attenuation = self.options.get(CONF_ATTENUATION, DEFAULT_ATTENUATION)
        global_max_radius = self.options.get(CONF_MAX_RADIUS, DEFAULT_MAX_RADIUS)

        # Will be set to nearest scanner if device selected, otherwise all scanners
        scanners_to_show = self.coordinator.scanner_list
        selected_device = None

        # Look up the Bermuda device from the selected HA device
        if self._last_device:
            devreg = dr.async_get(self.hass)
            ha_device = devreg.async_get(self._last_device)

            if ha_device:
                for conn in ha_device.connections:
                    if conn[0] in {"private_ble_device", "bluetooth", "ibeacon"}:
                        address = conn[1]
                        normalized = mac_norm(address)

                        if normalized in self.coordinator.devices:
                            selected_device = self.coordinator.devices[normalized]
                            break

        # Same hack as calibration1_global to work around placeholder issues with <details> tags
        _ugly_token_hack = {
            "details": "<details>",
            "details_end": "</details>",
            "summary": "<summary>",
            "summary_end": "</summary>",
        }

        # Start building the dynamic suffix content (calibration info will be added below)
        description = ""

        # If a device is selected, filter to nearest scanner and show calibration info
        if selected_device is not None:
            _LOGGER.debug("Device selected for calibration: %s (address: %s)",
                         selected_device.name, selected_device.address)
            try:
                from homeassistant.helpers import entity_registry as er

                # Get entity registry to read Bermuda sensor states (live/updating values)
                entity_reg = er.async_get(self.hass)
                entities = er.async_entries_for_device(entity_reg, self._last_device, include_disabled_entities=True)
                bermuda_entities = [e for e in entities if e.platform == DOMAIN]

                # Find the sensor values we need from entity states
                nearest_scanner_name = None
                distance = None
                rssi = None

                for entity in bermuda_entities:
                    state = self.hass.states.get(entity.entity_id)
                    if state:
                        if entity.original_name == "Nearest Scanner":
                            nearest_scanner_name = state.state
                        elif entity.original_name == "Distance":
                            distance = state.state
                        elif entity.original_name == "Nearest RSSI":
                            rssi = state.state

                _LOGGER.debug("Entity states - scanner=%s, distance=%s, rssi=%s",
                              nearest_scanner_name, distance, rssi)

                if nearest_scanner_name and nearest_scanner_name != "unavailable":
                    # Build calibration info display from entity states
                    description += "---\n\n## 📍 Calibration Info\n\n"
                    description += f"**Nearest Scanner:** {nearest_scanner_name}\n"

                    # Get area from the selected device
                    if selected_device.area_name:
                        description += f"**Area:** {selected_device.area_name}\n"

                    if distance and distance != "unavailable":
                        description += f"**Distance:** {distance}m\n"
                    if rssi and rssi != "unavailable":
                        description += f"**RSSI:** {rssi} dBm\n"

                    description += "\n*💡 Click **Submit** to refresh these readings*\n\n"

                    # Find the scanner address for this scanner name for filtering
                    nearest_scanner_address = None
                    for address in self.coordinator.scanner_list:
                        scanner_device = self.coordinator.devices.get(address)
                        if scanner_device is not None and scanner_device.name == nearest_scanner_name:
                            nearest_scanner_address = address
                            break

                    if nearest_scanner_address:
                        _LOGGER.debug("Found scanner address %s for name %s",
                                      nearest_scanner_address, nearest_scanner_name)
                        # Filter to show only the nearest scanner's settings
                        scanners_to_show = [nearest_scanner_address]
                    else:
                        _LOGGER.warning("Could not find scanner address for name '%s'",
                                       nearest_scanner_name)
                else:
                    _LOGGER.debug("No nearest scanner from entity states")
                    description += "---\n\n⚠️ Device not currently detected by any scanner\n\n"

            except Exception as e:
                _LOGGER.warning("Error loading calibration info: %s", e, exc_info=True)
                description += f"⚠️ Could not load calibration info: {e}\n\n"
        else:
            _LOGGER.debug("No device selected for calibration (last_device: %s)",
                         self._last_device)

        _LOGGER.debug("Final description length: %d characters, scanners_to_show: %s",
                      len(description), scanners_to_show)

        # Build nested dict for scanners to display (after filtering to nearest scanner if applicable)
        scanner_config_dict = {}
        for scanner in scanners_to_show:
            scanner_name = self.coordinator.devices[scanner].name
            scanner_config_dict[scanner_name] = {
                "rssi_offset": saved_rssi_offsets.get(scanner, 0),
                "attenuation": saved_attenuations.get(scanner, global_attenuation),
                "max_radius": saved_max_radii.get(scanner, global_max_radius),
            }

        # If we have previous user input, filter it to only include scanners we want to show
        if self._last_scanner_info:
            scanner_names_to_show = set(scanner_config_dict.keys())
            filtered_scanner_info = {
                name: values
                for name, values in self._last_scanner_info.items()
                if name in scanner_names_to_show
            }
            # Use filtered user input if it has any scanners, otherwise use the default dict
            default_scanner_info = filtered_scanner_info if filtered_scanner_info else scanner_config_dict
        else:
            default_scanner_info = scanner_config_dict

        data_schema = {
            vol.Optional(
                CONF_DEVICES,
                default=self._last_device if self._last_device is not None else vol.UNDEFINED,
            ): DeviceSelector(DeviceSelectorConfig(integration=DOMAIN)),
            vol.Required(
                CONF_SCANNER_INFO,
                default=default_scanner_info,
            ): ObjectSelector(),
        }

        return self.async_show_form(
            step_id="calibration2_scanners",
            data_schema=vol.Schema(data_schema),
            description_placeholders=_ugly_token_hack | {"suffix": description},
        )
