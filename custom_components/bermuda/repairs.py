"""Repairs flows for Bermuda."""

from __future__ import annotations

from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import SelectSelector, SelectSelectorConfig, SelectSelectorMode
import voluptuous as vol

from .const import DOMAIN, REPAIR_CALIBRATION_LAYOUT_MISMATCH


class CalibrationLayoutMismatchRepairFlow(RepairsFlow):
    """Offer resolution choices for calibration/layout mismatch issues."""

    async def async_step_init(self, user_input: dict[str, str] | None = None) -> FlowResult:
        """Handle the first step of the fix flow."""
        return await self.async_step_confirm(user_input)

    async def async_step_confirm(self, user_input: dict[str, str] | None = None) -> FlowResult:
        """Handle the confirmation step."""
        coordinator = self._get_coordinator()

        if user_input is not None:
            action = user_input["action"]
            if action == "physical_layout_changed":
                await coordinator.calibration.async_acknowledge_current_layout_mismatch()
            elif action == "update_sample_geometry":
                await coordinator.calibration.async_update_samples_to_current_geometry()
            await coordinator.async_handle_calibration_samples_changed()
            return self.async_create_entry(data={"action": action})

        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema(
                {
                    vol.Required("action"): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                {
                                    "value": "physical_layout_changed",
                                    "label": "Physical layout changed",
                                },
                                {
                                    "value": "update_sample_geometry",
                                    "label": "Update stored sample geometry",
                                },
                            ],
                            mode=SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
        )

    def _get_coordinator(self):
        """Return Bermuda's single coordinator instance."""
        entry = self.hass.config_entries.async_entries(DOMAIN)[0]
        return entry.runtime_data.coordinator


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Create fix flow for Bermuda repair issues."""
    del hass, data
    if issue_id == REPAIR_CALIBRATION_LAYOUT_MISMATCH:
        return CalibrationLayoutMismatchRepairFlow()
    raise ValueError(f"Unknown repair issue_id: {issue_id}")
