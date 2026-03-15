"""
Tests for BermudaAdvert class in bermuda_advert.py.
"""

import pytest
from unittest.mock import MagicMock, patch
from custom_components.ble_trilateration.bermuda_advert import BermudaAdvert
from custom_components.ble_trilateration.bermuda_device import BermudaDevice
from custom_components.ble_trilateration.const import (
    CONF_MAX_VELOCITY,
    CONF_SMOOTHING_SAMPLES,
)
from bleak.backends.scanner import AdvertisementData


@pytest.fixture
def mock_coordinator():
    """Provide a coordinator with per-scanner helpers."""
    coordinator = MagicMock()
    coordinator.estimate_sampled_range.return_value = None
    return coordinator


@pytest.fixture
def mock_parent_device(mock_coordinator):
    """Fixture for mocking the parent BermudaDevice."""
    device = MagicMock(spec=BermudaDevice)
    device.address = "aa:bb:cc:dd:ee:ff"
    device.name = "mock parent name"
    device.prefname = "mock parent name"
    device.name_bt_local_name = None
    device.name_by_user = None
    device.name_devreg = None
    device.name_bt_serviceinfo = None
    device.get_mobility_type.return_value = "moving"
    device._coordinator = mock_coordinator
    return device


@pytest.fixture
def mock_scanner_device():
    """Fixture for mocking the scanner BermudaDevice."""
    scanner = MagicMock(spec=BermudaDevice)
    scanner.address = "11:22:33:44:55:66"
    scanner.name = "Mock Scanner"
    scanner.area_id = "server_room"
    scanner.area_name = "server room"
    scanner.is_remote_scanner = True
    scanner.last_seen = 0.0
    scanner.stamps = {"AA:BB:CC:DD:EE:FF": 123.45}
    scanner.async_as_scanner_get_stamp.return_value = 123.45
    return scanner


@pytest.fixture
def mock_advertisement_data():
    """Fixture for mocking AdvertisementData."""
    advert = MagicMock(spec=AdvertisementData)
    advert.rssi = -70
    advert.tx_power = -20
    advert.local_name = "Mock advert Local Name"
    advert.name = "Mock advert name"
    advert.manufacturer_data = {76: b"\x02\x15"}
    advert.service_data = {"0000abcd-0000-1000-8000-00805f9b34fb": b"\x01\x02"}
    advert.service_uuids = ["0000abcd-0000-1000-8000-00805f9b34fb"]
    return advert


@pytest.fixture
def bermuda_advert(mock_parent_device, mock_advertisement_data, mock_scanner_device):
    """Fixture for creating a BermudaAdvert instance."""
    options = {
        CONF_MAX_VELOCITY: 3.0,
        CONF_SMOOTHING_SAMPLES: 5,
    }
    ba = BermudaAdvert(
        parent_device=mock_parent_device,
        advertisementdata=mock_advertisement_data,
        options=options,
        scanner_device=mock_scanner_device,
    )
    ba.name = "foo name"
    return ba


def test_bermuda_advert_initialization(bermuda_advert):
    """Test BermudaAdvert initialization."""
    assert bermuda_advert.device_address == "aa:bb:cc:dd:ee:ff"
    assert bermuda_advert.scanner_address == "11:22:33:44:55:66"
    assert bermuda_advert.stamp == 123.45
    assert bermuda_advert.rssi == -70


def test_apply_new_scanner(bermuda_advert, mock_scanner_device):
    """Test apply_new_scanner method."""
    bermuda_advert.apply_new_scanner(mock_scanner_device)
    assert bermuda_advert.scanner_device == mock_scanner_device
    assert bermuda_advert.scanner_sends_stamps is True


def test_update_advertisement(bermuda_advert, mock_advertisement_data, mock_scanner_device):
    """Test update_advertisement method."""
    bermuda_advert.update_advertisement(mock_advertisement_data, mock_scanner_device)
    assert bermuda_advert.rssi == -70
    assert bermuda_advert.tx_power == -20
    assert bermuda_advert.local_name[0][0] == "Mock advert Local Name"
    assert bermuda_advert.manufacturer_data[0][76] == b"\x02\x15"
    assert bermuda_advert.service_data[0]["0000abcd-0000-1000-8000-00805f9b34fb"] == b"\x01\x02"


def test_calculate_data_device_arrived(bermuda_advert):
    """Test calculate_data method when device arrives."""
    bermuda_advert.new_stamp = 123.45
    bermuda_advert.rssi_distance_raw = 5.0
    bermuda_advert.calculate_data()
    assert bermuda_advert.rssi_distance == 5.0


def test_calculate_data_device_away(bermuda_advert):
    """Test calculate_data method when device is away."""
    bermuda_advert.stamp = 0.0
    bermuda_advert.new_stamp = None
    bermuda_advert.calculate_data()
    assert bermuda_advert.rssi_distance is None


def test_to_dict(bermuda_advert):
    """Test to_dict method."""
    advert_dict = bermuda_advert.to_dict()
    assert isinstance(advert_dict, dict)
    assert advert_dict["device_address"] == "aa:bb:cc:dd:ee:ff"
    assert advert_dict["scanner_address"] == "11:22:33:44:55:66"


def test_repr(bermuda_advert):
    """Test __repr__ method."""
    repr_str = repr(bermuda_advert)
    assert repr_str == "aa:bb:cc:dd:ee:ff__Mock Scanner"


def test_rssi_outlier_is_clamped(bermuda_advert):
    """Spiky RSSI samples should be clamped close to the rolling median."""
    bermuda_advert.rssi_filtered = -70.0
    bermuda_advert.hist_rssi_adjusted = [-70.0] * 9
    bermuda_advert.hist_rssi_filtered = [-70.0] * 9
    filtered = bermuda_advert._update_filtered_rssi(-15.0)
    assert filtered < -60.0
    assert filtered > -75.0


def test_winsorize_outlier_preserves_retreat_direction(bermuda_advert, mock_parent_device):
    """A large genuine retreat should shift the filter toward the new value, not stay stuck."""
    mock_parent_device.get_mobility_type.return_value = "moving"
    # Stable history at -60 dBm (moving window=9, alpha=0.45).
    bermuda_advert.rssi_filtered = -60.0
    bermuda_advert.hist_rssi_adjusted = [-60.0] * 9
    bermuda_advert.hist_rssi_filtered = [-60.0] * 9

    # Large genuine retreat: -90 dBm is 30 dBm below median; threshold ~12 dBm.
    # Old clamp-to-median behaviour would leave filtered at -60.0.
    # Winsorize to med-threshold = -72 dBm, then EMA: alpha*-72 + (1-alpha)*-60 ≈ -65.4.
    filtered = bermuda_advert._update_filtered_rssi(-90.0)

    assert filtered < -60.0, "filter should move toward the retreat, not stay at old median"
    assert filtered > -90.0, "filter should not jump all the way to the new reading in one step"


def test_mobility_changes_ema_responsiveness(bermuda_advert, mock_parent_device):
    """Moving mode should react faster than stationary mode to the same RSSI step."""
    bermuda_advert.rssi_filtered = -90.0
    bermuda_advert.hist_rssi_adjusted = [-90.0] * 9
    bermuda_advert.hist_rssi_filtered = [-90.0] * 9

    mock_parent_device.get_mobility_type.return_value = "moving"
    moving = bermuda_advert._update_filtered_rssi(-70.0)

    bermuda_advert.rssi_filtered = -90.0
    bermuda_advert.hist_rssi_adjusted = [-90.0] * 13
    bermuda_advert.hist_rssi_filtered = [-90.0] * 13

    mock_parent_device.get_mobility_type.return_value = "stationary"
    stationary = bermuda_advert._update_filtered_rssi(-70.0)

    assert moving > stationary


def test_time_window_ignores_old_history(bermuda_advert, mock_parent_device):
    """Very old RSSI history should not contaminate the current window median."""
    mock_parent_device.get_mobility_type.return_value = "stationary"
    bermuda_advert.rssi_filtered = -70.0
    bermuda_advert.hist_rssi_adjusted = [-70.0, -70.0, -70.0, -40.0]
    bermuda_advert.hist_rssi_filtered = [-70.0, -70.0, -70.0, -40.0]
    bermuda_advert.hist_stamp = [99.5, 99.0, 98.5, 90.0]

    bermuda_advert._update_filtered_rssi(-70.0, sample_stamp=100.0)

    assert bermuda_advert.rssi_window_packet_count == 4
    assert bermuda_advert.rssi_window_median == pytest.approx(-70.0)
    assert bermuda_advert.rssi_dispersion == pytest.approx(0.0)


def test_stationary_window_keeps_more_history_than_moving(bermuda_advert, mock_parent_device):
    """Stationary mode should aggregate over a longer time horizon than moving mode."""
    bermuda_advert.rssi_filtered = -70.0
    bermuda_advert.hist_rssi_adjusted = [-68.0, -69.0, -70.0]
    bermuda_advert.hist_rssi_filtered = [-68.0, -69.0, -70.0]
    bermuda_advert.hist_stamp = [96.0, 95.0, 94.0]

    mock_parent_device.get_mobility_type.return_value = "moving"
    bermuda_advert._update_filtered_rssi(-70.0, sample_stamp=100.0)
    moving_count = bermuda_advert.rssi_window_packet_count

    bermuda_advert.rssi_filtered = -70.0
    bermuda_advert.hist_rssi_adjusted = [-68.0, -69.0, -70.0]
    bermuda_advert.hist_rssi_filtered = [-68.0, -69.0, -70.0]
    bermuda_advert.hist_stamp = [96.0, 95.0, 94.0]

    mock_parent_device.get_mobility_type.return_value = "stationary"
    bermuda_advert._update_filtered_rssi(-70.0, sample_stamp=100.0)
    stationary_count = bermuda_advert.rssi_window_packet_count

    assert stationary_count > moving_count


def test_missing_learned_range_stays_unavailable(bermuda_advert, mock_coordinator):
    """Without a learned sample-derived range, Bermuda should not fall back to RSSI math."""
    mock_coordinator.estimate_sampled_range.return_value = None
    bermuda_advert.rssi = -68

    distance = bermuda_advert._update_raw_distance(reading_is_new=False)

    assert distance is None
    assert bermuda_advert.rssi_distance_raw is None
    assert bermuda_advert.rssi_distance_sigma_m is None
    assert bermuda_advert.ranging_source == "unavailable"


def test_sampled_range_estimate_takes_priority(bermuda_advert, mock_coordinator):
    """A learned sample-derived range should populate the advert distance."""
    mock_coordinator.estimate_sampled_range.return_value = MagicMock(
        range_m=2.75,
        sigma_m=0.6,
        source="learned",
    )
    bermuda_advert.rssi = -68

    distance = bermuda_advert._update_raw_distance(reading_is_new=False)

    assert distance == pytest.approx(2.75)
    assert bermuda_advert.rssi_distance_raw == pytest.approx(2.75)
    assert bermuda_advert.rssi_distance_sigma_m == pytest.approx(0.6)
    assert bermuda_advert.ranging_source == "learned"
