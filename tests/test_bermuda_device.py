"""
Tests for BermudaDevice class in bermuda_device.py.
"""

import pytest
from unittest.mock import MagicMock, patch
from homeassistant.components.bluetooth import BaseHaScanner, BaseHaRemoteScanner
from custom_components.ble_trilateration.bermuda_device import BermudaDevice
from custom_components.ble_trilateration.const import ICON_DEFAULT_AREA, ICON_DEFAULT_FLOOR


@pytest.fixture
def mock_coordinator():
    """Fixture for mocking BermudaDataUpdateCoordinator."""
    coordinator = MagicMock()
    coordinator.options = {}
    coordinator.hass_version_min_2025_4 = True
    return coordinator


@pytest.fixture
def mock_scanner():
    """Fixture for mocking BaseHaScanner."""
    scanner = MagicMock(spec=BaseHaScanner)
    scanner.time_since_last_detection.return_value = 5.0
    scanner.source = "mock_source"
    return scanner


@pytest.fixture
def mock_remote_scanner():
    """Fixture for mocking BaseHaRemoteScanner."""
    scanner = MagicMock(spec=BaseHaRemoteScanner)
    scanner.time_since_last_detection.return_value = 5.0
    scanner.source = "mock_source"
    return scanner


@pytest.fixture
def bermuda_device(mock_coordinator):
    """Fixture for creating a BermudaDevice instance."""
    return BermudaDevice(address="AA:BB:CC:DD:EE:FF", coordinator=mock_coordinator)


@pytest.fixture
def bermuda_scanner(mock_coordinator):
    """Fixture for creating a BermudaDevice Scanner instance."""
    return BermudaDevice(address="11:22:33:44:55:66", coordinator=mock_coordinator)


def test_bermuda_device_initialization(bermuda_device):
    """Test BermudaDevice initialization."""
    assert bermuda_device.address == "aa:bb:cc:dd:ee:ff"
    assert bermuda_device.name.startswith("ble_trilateration_")
    assert bermuda_device.area_icon == ICON_DEFAULT_AREA
    assert bermuda_device.floor_icon == ICON_DEFAULT_FLOOR
    assert bermuda_device.zone == "not_home"
    assert bermuda_device.get_mobility_type() == "moving"


def test_async_as_scanner_init(bermuda_scanner, mock_scanner):
    """Test async_as_scanner_init method."""
    bermuda_scanner.async_as_scanner_init(mock_scanner)
    assert bermuda_scanner._hascanner == mock_scanner
    assert bermuda_scanner.is_scanner is True
    assert bermuda_scanner.is_remote_scanner is False


def test_async_as_scanner_init_resolves_identity_before_scanner_list_add(bermuda_scanner, mock_remote_scanner):
    """Scanner registration should happen after identity resolution."""
    call_order: list[str] = []

    def record_resolve() -> None:
        call_order.append("resolve")

    def record_add(device) -> None:
        assert device is bermuda_scanner
        call_order.append("add")

    bermuda_scanner.async_as_scanner_resolve_device_entries = MagicMock(side_effect=record_resolve)
    bermuda_scanner._coordinator.scanner_list_add = MagicMock(side_effect=record_add)

    bermuda_scanner.async_as_scanner_init(mock_remote_scanner)

    bermuda_scanner.async_as_scanner_resolve_device_entries.assert_called_once_with()
    bermuda_scanner._coordinator.scanner_list_add.assert_called_once_with(bermuda_scanner)
    assert call_order == ["resolve", "add"]


def test_async_as_scanner_update(bermuda_scanner, mock_scanner):
    """Test async_as_scanner_update method."""
    bermuda_scanner.async_as_scanner_update(mock_scanner)
    assert bermuda_scanner.last_seen > 0


def test_timestamp_sync_diagnostics_record_regressions(bermuda_scanner):
    """Scanner timestamp diagnostics should summarize regressions and dropped adverts."""
    bermuda_scanner._is_scanner = True  # noqa: SLF001 - test helper
    bermuda_scanner._is_remote_scanner = True  # noqa: SLF001 - test helper

    bermuda_scanner.record_scanner_timestamp_regression(3.2)
    bermuda_scanner.record_stale_advert_drop(1.1)

    diagnostics = bermuda_scanner.timestamp_sync_diagnostics()

    assert diagnostics["state"] == "unstable"
    assert diagnostics["recent_scanner_regressions"] == 1
    assert diagnostics["recent_stale_advert_drops"] == 1
    assert diagnostics["max_scanner_backward_s"] == 3.2
    assert diagnostics["max_stale_advert_drop_s"] == 1.1


def test_timestamp_sync_recovered_returns_to_synchronized_after_cooldown(bermuda_scanner):
    """Recovered scanners should return to synchronized after 15 quiet minutes."""
    bermuda_scanner._is_scanner = True  # noqa: SLF001 - test helper
    bermuda_scanner._is_remote_scanner = True  # noqa: SLF001 - test helper

    with patch("custom_components.ble_trilateration.bermuda_device.monotonic_time_coarse", return_value=1000.0):
        bermuda_scanner.record_scanner_timestamp_regression(1.2)

    with patch("custom_components.ble_trilateration.bermuda_device.monotonic_time_coarse", return_value=1600.0):
        diagnostics = bermuda_scanner.timestamp_sync_diagnostics()
        assert diagnostics["state"] == "recovered"

    with patch("custom_components.ble_trilateration.bermuda_device.monotonic_time_coarse", return_value=1901.0):
        diagnostics = bermuda_scanner.timestamp_sync_diagnostics()
        assert diagnostics["state"] == "synchronized"


def test_replay_candidates_reject_only_a_mature_multi_device_burst(bermuda_scanner):
    """Every member must remain unconfirmed for a full window before rejection."""
    bermuda_scanner.register_advert_replay_candidate("device-a", 100.0)
    bermuda_scanner.register_advert_replay_candidate("device-b", 108.0)

    assert bermuda_scanner.resolve_advert_replay_candidate("device-a", 100.0, 128.0, 20.0) == "pending"
    assert bermuda_scanner.resolve_advert_replay_candidate("device-a", 100.0, 129.0, 20.0) == "reject"
    assert bermuda_scanner.resolve_advert_replay_candidate("device-b", 108.0, 129.0, 20.0) == "reject"


def test_replay_candidate_accepts_an_isolated_one_off(bermuda_scanner):
    """A lone unconfirmed packet is ambiguous and must not be discarded."""
    bermuda_scanner.register_advert_replay_candidate("device-a", 100.0)

    assert bermuda_scanner.resolve_advert_replay_candidate("device-a", 100.0, 120.0, 20.0) == "pending"
    assert bermuda_scanner.resolve_advert_replay_candidate("device-a", 100.0, 121.0, 20.0) == "accept"


def test_confirmed_replay_candidate_leaves_no_burst_evidence(bermuda_scanner):
    """A later packet removes the candidate before scanner-wide classification."""
    bermuda_scanner.register_advert_replay_candidate("device-a", 100.0)
    bermuda_scanner.clear_advert_replay_candidate("device-a", 100.0)

    assert bermuda_scanner._scanner_replay_candidates == {}  # noqa: SLF001
    assert bermuda_scanner._scanner_replay_rejected == {}  # noqa: SLF001


def test_async_as_scanner_get_stamp(bermuda_scanner, mock_scanner, mock_remote_scanner):
    """Test async_as_scanner_get_stamp method."""
    bermuda_scanner.async_as_scanner_init(mock_scanner)
    bermuda_scanner.stamps = {"AA:BB:CC:DD:EE:FF": 123.45}

    stamp = bermuda_scanner.async_as_scanner_get_stamp("AA:bb:CC:DD:EE:FF")
    assert stamp is None

    bermuda_scanner.async_as_scanner_init(mock_remote_scanner)

    stamp = bermuda_scanner.async_as_scanner_get_stamp("AA:bb:CC:DD:EE:FF")
    assert stamp == 123.45

    stamp = bermuda_scanner.async_as_scanner_get_stamp("AA:BB:CC:DD:E1:FF")
    assert stamp is None


def test_make_name(bermuda_device):
    """Test make_name method."""
    bermuda_device.name_by_user = "Custom Name"
    name = bermuda_device.make_name()
    assert name == "Custom Name"
    assert bermuda_device.name == "Custom Name"


def test_process_advertisement(bermuda_device, bermuda_scanner):
    """Test process_advertisement method."""
    advertisement_data = MagicMock()
    bermuda_device.process_advertisement(bermuda_scanner, advertisement_data)
    assert len(bermuda_device.adverts) == 1


# def test_process_manufacturer_data(bermuda_device):
#     """Test process_manufacturer_data method."""
#     mock_advert = MagicMock()
#     mock_advert.service_uuids = ["0000abcd-0000-1000-8000-00805f9b34fb"]
#     mock_advert.manufacturer_data = [{"004C": b"\x02\x15"}]
#     bermuda_device.process_manufacturer_data(mock_advert)
#     assert bermuda_device.manufacturer == "Apple Inc."


def test_to_dict(bermuda_device):
    """Test to_dict method."""
    device_dict = bermuda_device.to_dict()
    assert isinstance(device_dict, dict)
    assert device_dict["address"] == "aa:bb:cc:dd:ee:ff"


def test_repr(bermuda_device):
    """Test __repr__ method."""
    repr_str = repr(bermuda_device)
    assert repr_str == f"{bermuda_device.name} [{bermuda_device.address}]"


def test_timestamp_sync_single_reboot_glitch_decays_quickly(bermuda_scanner):
    """One huge backward jump must not mark the scanner broken for the whole window."""
    bermuda_scanner._is_scanner = True  # noqa: SLF001 - test helper
    bermuda_scanner._is_remote_scanner = True  # noqa: SLF001 - test helper

    with patch("custom_components.ble_trilateration.bermuda_device.monotonic_time_coarse", return_value=1000.0):
        bermuda_scanner.record_scanner_timestamp_regression(3600.0)
        # A single hard event, even a massive one, is a reboot glitch: unstable, not broken.
        assert bermuda_scanner.timestamp_sync_state() == "unstable"

    with patch("custom_components.ble_trilateration.bermuda_device.monotonic_time_coarse", return_value=1061.0):
        # Quiet for longer than the ongoing window: decay to drifting.
        assert bermuda_scanner.timestamp_sync_state() == "drifting"

    with patch("custom_components.ble_trilateration.bermuda_device.monotonic_time_coarse", return_value=1400.0):
        # Outside the rolling window entirely: recovered.
        assert bermuda_scanner.timestamp_sync_state() == "recovered"


def test_timestamp_sync_repeated_large_events_are_broken_while_ongoing(bermuda_scanner):
    """Repeated large backward events mean actively broken, until the stream goes quiet."""
    bermuda_scanner._is_scanner = True  # noqa: SLF001 - test helper
    bermuda_scanner._is_remote_scanner = True  # noqa: SLF001 - test helper

    with patch("custom_components.ble_trilateration.bermuda_device.monotonic_time_coarse", return_value=1000.0):
        bermuda_scanner.record_scanner_timestamp_regression(120.0)
        bermuda_scanner.record_stale_advert_drop(90.0)
        assert bermuda_scanner.timestamp_sync_state() == "broken"

    with patch("custom_components.ble_trilateration.bermuda_device.monotonic_time_coarse", return_value=1061.0):
        assert bermuda_scanner.timestamp_sync_state() == "drifting"


def test_timestamp_sync_soft_events_count_toward_instability(bermuda_scanner):
    """Rebases and future clamps signal a moving clock base without implying broken."""
    bermuda_scanner._is_scanner = True  # noqa: SLF001 - test helper
    bermuda_scanner._is_remote_scanner = True  # noqa: SLF001 - test helper

    with patch("custom_components.ble_trilateration.bermuda_device.monotonic_time_coarse", return_value=1000.0):
        bermuda_scanner.record_future_stamp_clamp(500.0)
        assert bermuda_scanner.timestamp_sync_state() == "drifting"
        for _ in range(4):
            bermuda_scanner.record_stamp_rebase(45.0)
        # Five soft events while ongoing: unstable, but never broken (data was salvaged).
        assert bermuda_scanner.timestamp_sync_state() == "unstable"

        diagnostics = bermuda_scanner.timestamp_sync_diagnostics()
        assert diagnostics["recent_future_stamp_clamps"] == 1
        assert diagnostics["recent_stamp_rebases"] == 4

    # Lifetime counters survive the rolling window.
    assert bermuda_scanner.timestamp_sync_diagnostics()["lifetime_stamp_rebases"] == 4


def test_timestamp_sync_diagnostics_reports_accepted_advert_age(bermuda_scanner):
    """Diagnostics expose how long ago this scanner last had an advert accepted."""
    bermuda_scanner._is_scanner = True  # noqa: SLF001 - test helper
    bermuda_scanner._is_remote_scanner = True  # noqa: SLF001 - test helper

    assert bermuda_scanner.timestamp_sync_diagnostics()["last_accepted_advert_age_s"] is None

    with patch("custom_components.ble_trilateration.bermuda_device.monotonic_time_coarse", return_value=2000.0):
        bermuda_scanner.scanner_last_accepted_advert_at = 1993.5
        assert bermuda_scanner.timestamp_sync_diagnostics()["last_accepted_advert_age_s"] == 6.5
