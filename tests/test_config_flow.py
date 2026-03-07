"""Test Bermuda BLE Trilateration config flow."""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant import data_entry_flow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import floor_registry as fr

# from homeassistant.core import HomeAssistant  # noqa: F401
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bermuda.const import DOMAIN
from custom_components.bermuda.const import NAME

# from .const import MOCK_OPTIONS
from .const import MOCK_CONFIG

# Here we simiulate a successful config flow from the backend.
# Note that we use the `bypass_get_data` fixture here because
# we want the config flow validation to succeed during the test.
async def test_successful_config_flow(hass, bypass_get_data):
    """Test a successful config flow."""
    # Initialize a config flow
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})

    # Check that the config flow shows the user form as the first step
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"

    # If a user were to enter `test_username` for username and `test_password`
    # for password, it would result in this function call
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input=MOCK_CONFIG)

    # Check that the config flow is complete and a new entry is created with
    # the input data
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == NAME
    assert result["data"] == {"source": "user"}
    assert result["options"] == {}
    assert result["result"]


# In this case, we want to simulate a failure during the config flow.
# We use the `error_on_get_data` mock instead of `bypass_get_data`
# (note the function parameters) to raise an Exception during
# validation of the input config.
async def test_failed_config_flow(hass, error_on_get_data):
    """Test a failed config flow due to credential validation failure."""

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input=MOCK_CONFIG)

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result.get("errors") is None


# Our config flow also has an options flow, so we must test it as well.
async def test_options_flow(hass: HomeAssistant, setup_bermuda_entry: MockConfigEntry):
    """Test the slimmed options flow menu."""
    result = await hass.config_entries.options.async_init(setup_bermuda_entry.entry_id)

    assert result.get("type") == FlowResultType.MENU
    assert result.get("step_id") == "init"
    assert result.get("menu_options") == {
        "selectdevices": "Select Devices",
        "calibration_samples": "Calibration Samples",
        "topology": "Topology",
    }


async def test_topology_options_flow_add_edit_delete_group(hass: HomeAssistant, setup_bermuda_entry: MockConfigEntry):
    """Topology options flow should add, edit and delete connector groups."""
    floors = fr.async_get(hass)
    ground = floors.async_create("Ground floor", level=0)
    upper = floors.async_create("Upper floor", level=1)
    areas = ar.async_get(hass)
    living = areas.async_create("Living Room", floor_id=ground.floor_id)
    landing = areas.async_create("Landing", floor_id=upper.floor_id)

    result = await hass.config_entries.options.async_init(setup_bermuda_entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], user_input={"next_step_id": "topology"})
    assert result["type"] == FlowResultType.MENU
    assert result["step_id"] == "topology"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={"next_step_id": "topology_add_group"},
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "topology_add_group"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={"name": "Stairs", "area_ids": [living.id, landing.id]},
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert len(result["data"]["connector_groups"]) == 1

    hass.config_entries.async_update_entry(setup_bermuda_entry, options=result["data"])

    result = await hass.config_entries.options.async_init(setup_bermuda_entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], user_input={"next_step_id": "topology"})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={"next_step_id": "topology_edit_select"},
    )
    group_id = setup_bermuda_entry.options["connector_groups"][0]["id"]
    result = await hass.config_entries.options.async_configure(result["flow_id"], user_input={"group_id": group_id})
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "topology_edit_group"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={"name": "Main stairs", "area_ids": [living.id, landing.id]},
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"]["connector_groups"][0]["name"] == "Main stairs"

    hass.config_entries.async_update_entry(setup_bermuda_entry, options=result["data"])

    result = await hass.config_entries.options.async_init(setup_bermuda_entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], user_input={"next_step_id": "topology"})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={"next_step_id": "topology_delete_group"},
    )
    result = await hass.config_entries.options.async_configure(result["flow_id"], user_input={"group_id": group_id})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"]["connector_groups"] == []


async def test_topology_options_flow_rejects_invalid_groups(hass: HomeAssistant, setup_bermuda_entry: MockConfigEntry):
    """Topology options flow should reject groups without floors, cross-floor span or unique areas."""
    floors = fr.async_get(hass)
    ground = floors.async_create("Ground floor", level=0)
    upper = floors.async_create("Upper floor", level=1)
    areas = ar.async_get(hass)
    living = areas.async_create("Living Room", floor_id=ground.floor_id)
    kitchen = areas.async_create("Kitchen", floor_id=ground.floor_id)
    landing = areas.async_create("Landing", floor_id=upper.floor_id)
    attic = areas.async_create("Attic")

    result = await hass.config_entries.options.async_init(setup_bermuda_entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], user_input={"next_step_id": "topology"})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={"next_step_id": "topology_add_group"},
    )

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={"name": "Invalid", "area_ids": [living.id, attic.id]},
    )
    assert result["type"] == FlowResultType.FORM
    assert "must be assigned to a floor" in result["description_placeholders"]["summary"]

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={"name": "Same floor", "area_ids": [living.id, kitchen.id]},
    )
    assert result["type"] == FlowResultType.FORM
    assert "span at least two floors" in result["description_placeholders"]["summary"]

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={"name": "Valid", "area_ids": [living.id, landing.id]},
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY

    hass.config_entries.async_update_entry(setup_bermuda_entry, options=result["data"])

    result = await hass.config_entries.options.async_init(setup_bermuda_entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], user_input={"next_step_id": "topology"})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={"next_step_id": "topology_add_group"},
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={"name": "Duplicate", "area_ids": [living.id, kitchen.id, landing.id]},
    )
    assert result["type"] == FlowResultType.FORM
    assert "already used by another connector group" in result["description_placeholders"]["summary"]
