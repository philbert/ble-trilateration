"""Adds config flow for Bermuda BLE Trilateration."""

from __future__ import annotations

from typing import TYPE_CHECKING

import voluptuous as vol
from bluetooth_data_tools import monotonic_time_coarse
from homeassistant import config_entries
from homeassistant.config_entries import OptionsFlowWithConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)
from uuid import uuid4

from .const import (
    ADDR_TYPE_IBEACON,
    ADDR_TYPE_PRIVATE_BLE_DEVICE,
    BDADDR_TYPE_RANDOM_RESOLVABLE,
    CONF_CONNECTOR_GROUPS,
    CONF_DEVICES,
    DISTANCE_INFINITE,
    DOMAIN,
    NAME,
)
from .util import mac_redact

if TYPE_CHECKING:
    from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
    from homeassistant.config_entries import ConfigFlowResult

    from . import BermudaConfigEntry
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
        self._last_calibration_status: str | None = None
        self._last_topology_status: str | None = None
        self._editing_connector_group_id: str | None = None

    async def async_step_init(self, user_input=None):  # pylint: disable=unused-argument
        """Manage the options."""
        self.coordinator = self.config_entry.runtime_data.coordinator
        devices = self.coordinator.devices

        messages = {}
        active_devices = self.coordinator.count_active_devices()
        active_scanners = self.coordinator.count_active_scanners()

        messages["device_counter_active"] = f"{active_devices}"
        messages["device_counter_devices"] = f"{len(devices)}"
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
                "selectdevices": "Select Devices",
                "calibration_samples": "Calibration Samples",
                "topology": "Topology",
            },
            description_placeholders=messages,
        )

    async def async_step_selectdevices(self, user_input=None):
        """Handle a flow initialized by the user."""
        if user_input is not None:
            self.options.update(user_input)
            return await self._update_options()

        # Grab the co-ordinator's device list so we can build a selector from it.
        devices = self.config_entry.runtime_data.coordinator.devices

        # Where we store the options before building the selector
        options_list = []
        options_metadevices = []  # These will be first in the list
        options_otherdevices = []  # These will be last.
        options_randoms = []  # Random MAC addresses - very last!

        for device in devices.values():
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


    async def async_step_calibration_samples(self, user_input=None):
        """Manage stored calibration samples."""
        summary = self.coordinator.calibration.get_summary()
        description = self._format_calibration_summary(summary)
        if self._last_calibration_status:
            description = f"{self._last_calibration_status}\n\n{description}"
            self._last_calibration_status = None

        menu_options = {"calibration_samples_summary": "Sample Summary"}
        if summary["sample_count"] > 0:
            menu_options["calibration_samples_delete_one"] = "Delete One Sample"
            menu_options["calibration_samples_clear_device"] = "Clear Samples For Device"
            menu_options["calibration_samples_clear_room"] = "Clear Samples For Room"
            menu_options["calibration_samples_clear_current_layout"] = "Clear Samples For Current Anchor Layout"
            menu_options["calibration_samples_clear_all"] = "Clear All Samples"

        return self.async_show_menu(
            step_id="calibration_samples",
            menu_options=menu_options,
            description_placeholders={"summary": description},
        )

    async def async_step_calibration_samples_summary(self, user_input=None):
        """Show calibration sample summary details."""
        if user_input is not None:
            return await self.async_step_calibration_samples()
        summary = self.coordinator.calibration.get_summary()
        return self.async_show_form(
            step_id="calibration_samples_summary",
            data_schema=vol.Schema({}),
            description_placeholders={"summary": self._format_calibration_summary(summary, include_recent=True)},
        )

    async def async_step_calibration_samples_delete_one(self, user_input=None):
        """Delete one persisted calibration sample."""
        samples = self._get_samples_newest_first()
        options = [
            SelectOptionDict(value=sample["id"], label=self._format_sample_label(sample))
            for sample in samples
        ]
        if user_input is not None:
            deleted = await self.coordinator.calibration.async_delete_sample(user_input["sample_id"])
            self._last_calibration_status = (
                "Deleted calibration sample." if deleted else "Calibration sample was not found."
            )
            return await self.async_step_calibration_samples()

        return self.async_show_form(
            step_id="calibration_samples_delete_one",
            data_schema=vol.Schema(
                {
                    vol.Required("sample_id"): SelectSelector(
                        SelectSelectorConfig(options=options, multiple=False, mode=SelectSelectorMode.DROPDOWN)
                    )
                }
            ),
            description_placeholders={"summary": "Choose one saved calibration sample to delete."},
        )

    async def async_step_calibration_samples_clear_device(self, user_input=None):
        """Delete all samples for one device."""
        devices = self.coordinator.calibration.get_device_samples()
        options = [
            SelectOptionDict(
                value=device_id,
                label=f"{details['name']} [{details['address']}]" if details["address"] else details["name"],
            )
            for device_id, details in sorted(devices.items(), key=lambda item: item[1]["name"])
        ]
        if user_input is not None:
            removed = await self.coordinator.calibration.async_clear_device(user_input["device_id"])
            self._last_calibration_status = f"Deleted {removed} calibration sample(s) for the selected device."
            return await self.async_step_calibration_samples()

        return self.async_show_form(
            step_id="calibration_samples_clear_device",
            data_schema=vol.Schema(
                {
                    vol.Required("device_id"): SelectSelector(
                        SelectSelectorConfig(options=options, multiple=False, mode=SelectSelectorMode.DROPDOWN)
                    )
                }
            ),
            description_placeholders={"summary": "Delete all saved calibration samples for one device."},
        )

    async def async_step_calibration_samples_clear_room(self, user_input=None):
        """Delete all samples for one room."""
        rooms = self.coordinator.calibration.get_room_samples()
        options = [
            SelectOptionDict(
                value=room_area_id,
                label=f"{details['name']} ({details['count']})",
            )
            for room_area_id, details in sorted(rooms.items(), key=lambda item: str(item[1]["name"]))
        ]
        if user_input is not None:
            removed = await self.coordinator.calibration.async_clear_room(user_input["room_area_id"])
            self._last_calibration_status = f"Deleted {removed} calibration sample(s) for the selected room."
            return await self.async_step_calibration_samples()

        return self.async_show_form(
            step_id="calibration_samples_clear_room",
            data_schema=vol.Schema(
                {
                    vol.Required("room_area_id"): SelectSelector(
                        SelectSelectorConfig(options=options, multiple=False, mode=SelectSelectorMode.DROPDOWN)
                    )
                }
            ),
            description_placeholders={"summary": "Delete all saved calibration samples for one room."},
        )

    async def async_step_calibration_samples_clear_current_layout(self, user_input=None):
        """Delete all samples for the current anchor layout."""
        current_hash = self.coordinator.calibration.current_anchor_layout_hash
        if user_input is not None:
            if not user_input["confirm"]:
                self._last_calibration_status = "Current-anchor-layout deletion was not confirmed."
                return await self.async_step_calibration_samples()
            removed = await self.coordinator.calibration.async_clear_current_anchor_layout()
            self._last_calibration_status = (
                f"Deleted {removed} calibration sample(s) for the current anchor layout."
            )
            return await self.async_step_calibration_samples()

        return self.async_show_form(
            step_id="calibration_samples_clear_current_layout",
            data_schema=vol.Schema({vol.Required("confirm", default=False): vol.Coerce(bool)}),
            description_placeholders={
                "summary": f"Current anchor layout hash: `{current_hash[:8]}`. Confirm to delete samples for this layout."
            },
        )

    async def async_step_calibration_samples_clear_all(self, user_input=None):
        """Delete all persisted calibration samples."""
        if user_input is not None:
            if not user_input["confirm"]:
                self._last_calibration_status = "Delete-all was not confirmed."
                return await self.async_step_calibration_samples()
            removed = await self.coordinator.calibration.async_clear_all()
            self._last_calibration_status = f"Deleted {removed} calibration sample(s)."
            return await self.async_step_calibration_samples()

        return self.async_show_form(
            step_id="calibration_samples_clear_all",
            data_schema=vol.Schema({vol.Required("confirm", default=False): vol.Coerce(bool)}),
            description_placeholders={"summary": "Confirm to delete all saved calibration samples."},
        )

    def _get_samples_newest_first(self) -> list[dict]:
        """Return stored calibration samples newest first."""
        return sorted(
            self.coordinator.calibration.samples(),
            key=lambda sample: sample.get("created_at", ""),
            reverse=True,
        )

    def _format_sample_label(self, sample: dict) -> str:
        """Create a compact label for one calibration sample."""
        status = sample.get("quality", {}).get("status", "unknown")
        created_at = sample.get("created_at", "")
        room_name = sample.get("room_name", sample.get("room_area_id", "Unknown"))
        device_name = sample.get("device_name", sample.get("device_id", "Unknown"))
        return f"{created_at} | {room_name} | {device_name} | {status}"

    def _format_calibration_summary(self, summary: dict, include_recent: bool = False) -> str:
        """Build markdown summary for calibration samples."""
        lines = [
            f"Total samples: `{summary['sample_count']}`",
            f"Current anchor layout hash: `{summary['current_layout_hash'][:8]}`",
            f"Samples for current anchor layout: `{summary['current_layout_count']}`",
        ]
        if summary["sample_count"] > summary["warn_threshold"]:
            lines.append(
                f"Warning: sample count exceeds the soft warning threshold of `{summary['warn_threshold']}`."
            )
        if summary["by_room"]:
            lines.append("")
            lines.append("By room:")
            for room_name, count in sorted(summary["by_room"].items()):
                lines.append(f"- {room_name}: `{count}`")
        if summary["by_device"]:
            lines.append("")
            lines.append("By device:")
            for device_name, count in sorted(summary["by_device"].items()):
                lines.append(f"- {device_name}: `{count}`")
        if include_recent and summary["recent"]:
            lines.append("")
            lines.append("Recent samples:")
            for sample in summary["recent"]:
                lines.append(f"- {self._format_sample_label(sample)}")
        return "\n".join(lines)

    async def async_step_topology(self, user_input=None):
        """Manage connector-group topology."""
        summary = self._format_topology_summary()
        description = summary
        if self._last_topology_status:
            description = f"{self._last_topology_status}\n\n{description}"
            self._last_topology_status = None

        menu_options = {
            "topology_summary": "Topology Summary",
            "topology_add_group": "Add Connector Group",
        }
        if self._connector_groups():
            menu_options["topology_edit_select"] = "Edit Connector Group"
            menu_options["topology_delete_group"] = "Delete Connector Group"

        return self.async_show_menu(
            step_id="topology",
            menu_options=menu_options,
            description_placeholders={"summary": description},
        )

    async def async_step_topology_summary(self, user_input=None):
        """Show connector-group topology summary."""
        if user_input is not None:
            return await self.async_step_topology()
        return self.async_show_form(
            step_id="topology_summary",
            data_schema=vol.Schema({}),
            description_placeholders={"summary": self._format_topology_summary(include_groups=True)},
        )

    async def async_step_topology_add_group(self, user_input=None):
        """Create a connector group."""
        return await self._async_step_topology_group_form(step_id="topology_add_group", user_input=user_input)

    async def async_step_topology_edit_select(self, user_input=None):
        """Choose connector group to edit."""
        groups = self._connector_groups()
        options = [
            SelectOptionDict(value=group["id"], label=self._format_connector_group_label(group))
            for group in groups
        ]
        if user_input is not None:
            self._editing_connector_group_id = user_input["group_id"]
            return await self.async_step_topology_edit_group()

        return self.async_show_form(
            step_id="topology_edit_select",
            data_schema=vol.Schema(
                {
                    vol.Required("group_id"): SelectSelector(
                        SelectSelectorConfig(options=options, multiple=False, mode=SelectSelectorMode.DROPDOWN)
                    )
                }
            ),
            description_placeholders={"summary": "Choose one connector group to edit."},
        )

    async def async_step_topology_edit_group(self, user_input=None):
        """Edit a connector group."""
        return await self._async_step_topology_group_form(
            step_id="topology_edit_group",
            user_input=user_input,
            group_id=self._editing_connector_group_id,
        )

    async def async_step_topology_delete_group(self, user_input=None):
        """Delete a connector group."""
        groups = self._connector_groups()
        options = [
            SelectOptionDict(value=group["id"], label=self._format_connector_group_label(group))
            for group in groups
        ]
        if user_input is not None:
            group_id = user_input["group_id"]
            groups = [group for group in groups if group["id"] != group_id]
            self.options[CONF_CONNECTOR_GROUPS] = groups
            self._last_topology_status = "Deleted connector group."
            return await self._update_options()

        return self.async_show_form(
            step_id="topology_delete_group",
            data_schema=vol.Schema(
                {
                    vol.Required("group_id"): SelectSelector(
                        SelectSelectorConfig(options=options, multiple=False, mode=SelectSelectorMode.DROPDOWN)
                    )
                }
            ),
            description_placeholders={"summary": "Choose one connector group to delete."},
        )

    async def _async_step_topology_group_form(self, step_id: str, user_input=None, group_id: str | None = None):
        """Add or edit a connector group."""
        groups = self._connector_groups()
        group = next((item for item in groups if item["id"] == group_id), None)
        status = ""
        if user_input is not None:
            area_ids = list(user_input["area_ids"])
            valid, status = self._validate_connector_group(area_ids, editing_group_id=group_id)
            if valid:
                payload = {
                    "id": group_id or f"connector_{uuid4().hex[:8]}",
                    "name": str(user_input.get("name") or "").strip(),
                    "area_ids": area_ids,
                }
                next_groups = [item for item in groups if item["id"] != payload["id"]]
                next_groups.append(payload)
                next_groups.sort(key=lambda item: (str(item.get("name") or ""), item["id"]))
                self.options[CONF_CONNECTOR_GROUPS] = next_groups
                self._last_topology_status = "Saved connector group."
                self._editing_connector_group_id = None
                return await self._update_options()

        area_options = self._topology_area_options()
        description = self._format_topology_summary(include_groups=True)
        if status:
            description = f"{status}\n\n{description}"

        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema(
                {
                    vol.Optional("name", default="" if group is None else group.get("name", "")): str,
                    vol.Required(
                        "area_ids",
                        default=[] if group is None else group.get("area_ids", []),
                    ): SelectSelector(SelectSelectorConfig(options=area_options, multiple=True)),
                }
            ),
            description_placeholders={"summary": description},
        )

    def _connector_groups(self) -> list[dict]:
        """Return configured connector groups."""
        return list(self.options.get(CONF_CONNECTOR_GROUPS, []))

    def _topology_area_options(self) -> list[SelectOptionDict]:
        """Return all Home Assistant areas with floor labels."""
        options = []
        for area in sorted(self.coordinator.ar.async_list_areas(), key=lambda item: item.name):
            floor_name = "No floor"
            if area.floor_id is not None:
                floor = self.coordinator.fr.async_get_floor(area.floor_id)
                if floor is not None:
                    floor_name = floor.name
            options.append(SelectOptionDict(value=area.id, label=f"{area.name} ({floor_name})"))
        return options

    def _format_connector_group_label(self, group: dict) -> str:
        """Format connector group label for selectors."""
        name = str(group.get("name") or group["id"])
        area_names = []
        for area_id in group.get("area_ids", []):
            area = self.coordinator.ar.async_get_area(area_id)
            area_names.append(area.name if area is not None else area_id)
        return f"{name} | {', '.join(area_names)}"

    def _format_topology_summary(self, include_groups: bool = False) -> str:
        """Build a compact connector-topology summary."""
        groups = self._connector_groups()
        lines = [f"Connector groups: `{len(groups)}`"]
        if not groups:
            lines.append("")
            lines.append("No connector groups are configured.")
            return "\n".join(lines)
        if include_groups:
            lines.append("")
            lines.append("Groups:")
            for group in groups:
                lines.append(f"- {self._format_connector_group_label(group)}")
        return "\n".join(lines)

    def _validate_connector_group(self, area_ids: list[str], editing_group_id: str | None = None) -> tuple[bool, str]:
        """Validate connector-group area selection."""
        unique_area_ids = list(dict.fromkeys(area_ids))
        if len(unique_area_ids) < 2:
            return False, "Connector groups must include at least two areas."

        floors: set[str] = set()
        for area_id in unique_area_ids:
            area = self.coordinator.ar.async_get_area(area_id)
            if area is None:
                return False, f"Area `{area_id}` no longer exists."
            if area.floor_id is None:
                return False, f"Area `{area.name}` must be assigned to a floor."
            floors.add(area.floor_id)

        if len(floors) < 2:
            return False, "Connector groups must span at least two floors."

        for group in self._connector_groups():
            if group["id"] == editing_group_id:
                continue
            overlap = set(unique_area_ids).intersection(group.get("area_ids", []))
            if overlap:
                overlap_area_id = sorted(overlap)[0]
                area = self.coordinator.ar.async_get_area(overlap_area_id)
                area_name = area.name if area is not None else overlap_area_id
                return False, f"Area `{area_name}` is already used by another connector group."

        return True, ""

    async def _update_options(self):
        """Update config entry options."""
        return self.async_create_entry(title=NAME, data=self.options)
