"""Constants for BLE Trilateration tests."""

from __future__ import annotations

import custom_components.ble_trilateration.const

# from custom_components.ble_trilateration.const import CONF_DEVICES
MOCK_OPTIONS = {
    custom_components.ble_trilateration.const.CONF_MAX_VELOCITY: 3.0,
    custom_components.ble_trilateration.const.CONF_DEVTRACK_TIMEOUT: 30,
    custom_components.ble_trilateration.const.CONF_UPDATE_INTERVAL: 10.0,
    custom_components.ble_trilateration.const.CONF_SMOOTHING_SAMPLES: 20,
    custom_components.ble_trilateration.const.CONF_DEVICES: [],  # ["EE:E8:37:9F:6B:54"],
}

MOCK_OPTIONS_GLOBALS = {
    custom_components.ble_trilateration.const.CONF_MAX_VELOCITY: 3.0,
    custom_components.ble_trilateration.const.CONF_DEVTRACK_TIMEOUT: 30,
    custom_components.ble_trilateration.const.CONF_UPDATE_INTERVAL: 10.0,
    custom_components.ble_trilateration.const.CONF_SMOOTHING_SAMPLES: 20,
}

MOCK_OPTIONS_DEVICES = {
    custom_components.ble_trilateration.const.CONF_DEVICES: [],  # ["EE:E8:37:9F:6B:54"],
}

MOCK_CONFIG = {"source": "user"}


SERVICE_INFOS = [
    {
        "name": "test device",
        "advertisement": {"local_name": "test local name"},
        "device": {"name": "test device name"},
        "address": "EE:E8:37:9F:6B:54",
    },
    {
        "name": "test device2",
        "advertisement": {"local_name": "test local name2"},
        "device": {"name": "test device name2"},
        "address": "EE:E8:37:9F:6B:56",
    },
]
