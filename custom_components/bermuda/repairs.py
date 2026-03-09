"""Repairs flows for Bermuda."""

from __future__ import annotations

from typing import cast

from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
import voluptuous as vol

from .const import DOMAIN, REPAIR_CALIBRATION_LAYOUT_MISMATCH


class CalibrationLayoutMismatchRepairFlow(RepairsFlow):
    """Repair stored sample geometry after anchor-coordinate corrections."""

    def __init__(self, entry_id: str, issue_id: str) -> None:
        """Create the repair flow."""
        self.entry_id = entry_id
        self.issue_id = issue_id
        super().__init__()

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        """Handle the fix flow."""
        if user_input is not None:
            if not user_input.get("update_stored_sample_geometry", False):
                return self.async_show_form(
                    step_id="init",
                    data_schema=vol.Schema(
                        {
                            vol.Required("update_stored_sample_geometry"): bool,
                        }
                    ),
                    errors={"base": "confirm_required"},
                    description_placeholders=self._description_placeholders(),
                )
            coordinator = self._get_coordinator()
            if coordinator is None:
                return self.async_abort(reason="entry_not_found")
            await coordinator.calibration.async_update_samples_to_current_geometry()
            await coordinator.async_handle_calibration_samples_changed()
            return self.async_create_entry(data={})

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required("update_stored_sample_geometry"): bool,
                }
            ),
            description_placeholders=self._description_placeholders(),
        )

    def _description_placeholders(self) -> dict[str, str] | None:
        """Return issue translation placeholders for the repair dialog."""
        issue_registry = ir.async_get(self.hass)
        if issue := issue_registry.async_get_issue(DOMAIN, self.issue_id):
            return issue.translation_placeholders
        return None

    def _get_coordinator(self):
        """Return Bermuda's single coordinator instance."""
        entry = self.hass.config_entries.async_get_entry(self.entry_id)
        if entry is None:
            return None
        runtime_data = getattr(entry, "runtime_data", None)
        if runtime_data is None:
            return None
        return getattr(runtime_data, "coordinator", None)


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Create fix flow for Bermuda repair issues."""
    if issue_id == REPAIR_CALIBRATION_LAYOUT_MISMATCH:
        if data is None or (entry_id := data.get("entry_id")) is None:
            entry = next(iter(hass.config_entries.async_entries(DOMAIN)), None)
            if entry is None:
                raise ValueError("No Bermuda config entry available for repair flow")
            entry_id = entry.entry_id
        return CalibrationLayoutMismatchRepairFlow(cast(str, entry_id), issue_id)
    raise ValueError(f"Unknown repair issue_id: {issue_id}")
