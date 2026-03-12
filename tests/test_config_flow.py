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

from custom_components.bermuda.config_flow import BermudaOptionsFlowHandler
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


async def test_calibration_samples_options_flow_clear_room(hass: HomeAssistant, setup_bermuda_entry: MockConfigEntry):
    """Calibration samples flow should offer room clearing and delete samples for one room."""
    coordinator = setup_bermuda_entry.runtime_data.coordinator
    living = ar.async_get(hass).async_create("Living Room")
    kitchen = ar.async_get(hass).async_create("Kitchen")

    await coordinator.calibration_store.async_add_sample(
        {
            "id": "sample_living_1",
            "created_at": "2026-03-06T12:00:00+00:00",
            "device_id": "device_one",
            "device_name": "Device One",
            "device_address": "aa:bb:cc:dd:ee:01",
            "room_area_id": living.id,
            "room_name": living.name,
            "position": {"x_m": 1.0, "y_m": 2.0, "z_m": 1.0},
            "sample_radius_m": 1.0,
            "anchor_layout_hash": coordinator.calibration.current_anchor_layout_hash,
            "anchors": {},
            "quality": {"status": "accepted", "eligible_anchor_count": 3, "reason": None},
        }
    )
    await coordinator.calibration_store.async_add_sample(
        {
            "id": "sample_living_2",
            "created_at": "2026-03-06T13:00:00+00:00",
            "device_id": "device_one",
            "device_name": "Device One",
            "device_address": "aa:bb:cc:dd:ee:01",
            "room_area_id": living.id,
            "room_name": living.name,
            "position": {"x_m": 2.0, "y_m": 3.0, "z_m": 1.0},
            "sample_radius_m": 1.0,
            "anchor_layout_hash": coordinator.calibration.current_anchor_layout_hash,
            "anchors": {},
            "quality": {"status": "accepted", "eligible_anchor_count": 3, "reason": None},
        }
    )
    await coordinator.calibration_store.async_add_sample(
        {
            "id": "sample_kitchen_1",
            "created_at": "2026-03-06T14:00:00+00:00",
            "device_id": "device_one",
            "device_name": "Device One",
            "device_address": "aa:bb:cc:dd:ee:01",
            "room_area_id": kitchen.id,
            "room_name": kitchen.name,
            "position": {"x_m": 4.0, "y_m": 5.0, "z_m": 1.0},
            "sample_radius_m": 1.0,
            "anchor_layout_hash": coordinator.calibration.current_anchor_layout_hash,
            "anchors": {},
            "quality": {"status": "accepted", "eligible_anchor_count": 3, "reason": None},
        }
    )
    result = await hass.config_entries.options.async_init(setup_bermuda_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "calibration_samples"}
    )
    assert result["type"] == FlowResultType.MENU
    assert result["step_id"] == "calibration_samples"
    assert "calibration_samples_clear_room" in result["menu_options"]

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "calibration_samples_clear_room"}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "calibration_samples_clear_room"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"room_area_id": living.id}
    )
    assert result["type"] == FlowResultType.MENU
    assert result["step_id"] == "calibration_samples"
    assert "Deleted 2 calibration sample(s) for the selected room." in result["description_placeholders"]["summary"]

    remaining_rooms = [sample["room_area_id"] for sample in coordinator.calibration.samples()]
    assert remaining_rooms == [kitchen.id]


async def test_calibration_samples_summary_shows_quality_and_delete_labels_are_human_sorted(
    hass: HomeAssistant, setup_bermuda_entry: MockConfigEntry
):
    """Calibration sample UI should show by-quality summary and human-friendly labels."""
    coordinator = setup_bermuda_entry.runtime_data.coordinator
    office = ar.async_get(hass).async_create("Ana's Office")
    living = ar.async_get(hass).async_create("Living Room")

    await coordinator.calibration_store.async_add_sample(
        {
            "id": "sample_living_mid",
            "created_at": "2026-03-12T10:46:04.742880+01:00",
            "device_id": "device_b",
            "device_name": "Phil's iPhone",
            "device_address": "aa:bb:cc:dd:ee:02",
            "room_area_id": living.id,
            "room_name": living.name,
            "position": {"x_m": 15.8, "y_m": 8.0, "z_m": 3.3},
            "sample_radius_m": 1.0,
            "anchor_layout_hash": coordinator.calibration.current_anchor_layout_hash,
            "anchors": {},
            "quality": {
                "status": "accepted",
                "level": "medium",
                "eligible_anchor_count": 3,
                "reason": None,
            },
        }
    )
    await coordinator.calibration_store.async_add_sample(
        {
            "id": "sample_office_high",
            "created_at": "2026-03-12T10:44:40.220352+01:00",
            "device_id": "device_a",
            "device_name": "Phil's iPhone",
            "device_address": "aa:bb:cc:dd:ee:01",
            "room_area_id": office.id,
            "room_name": office.name,
            "position": {"x_m": 12.5, "y_m": 0.5, "z_m": 3.7},
            "sample_radius_m": 1.0,
            "anchor_layout_hash": coordinator.calibration.current_anchor_layout_hash,
            "anchors": {},
            "quality": {
                "status": "accepted",
                "level": "high",
                "eligible_anchor_count": 4,
                "reason": None,
            },
        }
    )
    await coordinator.calibration_store.async_add_sample(
        {
            "id": "sample_office_low",
            "created_at": "2026-03-12T10:46:05.742880+01:00",
            "device_id": "device_a",
            "device_name": "Phil's iPhone",
            "device_address": "aa:bb:cc:dd:ee:01",
            "room_area_id": office.id,
            "room_name": office.name,
            "position": {"x_m": 12.5, "y_m": 0.5, "z_m": 3.7},
            "sample_radius_m": 1.0,
            "anchor_layout_hash": coordinator.calibration.current_anchor_layout_hash,
            "anchors": {},
            "quality": {
                "status": "poor_quality",
                "level": "low",
                "eligible_anchor_count": 2,
                "reason": "insufficient_anchors",
            },
        }
    )

    result = await hass.config_entries.options.async_init(setup_bermuda_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "calibration_samples"}
    )
    assert result["type"] == FlowResultType.MENU
    assert "By quality:" in result["description_placeholders"]["summary"]
    assert "- high: `1`" in result["description_placeholders"]["summary"]
    assert "- medium: `1`" in result["description_placeholders"]["summary"]
    assert "- low: `1`" in result["description_placeholders"]["summary"]

    flow = BermudaOptionsFlowHandler(setup_bermuda_entry)
    flow.coordinator = coordinator
    labels = [flow._format_sample_label(sample) for sample in flow._get_samples_for_selection()]
    assert labels == [
        "Ana's Office | 12.5,0.5,3.7 | Phil's iPhone | high | 2026-03-12 10:44:40",
        "Ana's Office | 12.5,0.5,3.7 | Phil's iPhone | low | 2026-03-12 10:46:05",
        "Living Room | 15.8,8.0,3.3 | Phil's iPhone | medium | 2026-03-12 10:46:04",
    ]


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
