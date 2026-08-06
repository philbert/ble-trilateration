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
    scanner.resolve_advert_replay_candidate.return_value = "pending"
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
        CONF_MAX_VELOCITY: 1.8,
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


def test_future_stamp_clamped_and_recorded(bermuda_advert, mock_advertisement_data, mock_scanner_device):
    """Stamps from the future are clamped to arrival time and counted, not stored raw."""
    from bluetooth_data_tools import monotonic_time_coarse

    nowstamp = monotonic_time_coarse()
    mock_scanner_device.async_as_scanner_get_stamp.return_value = nowstamp + 3600.0
    bermuda_advert.update_advertisement(mock_advertisement_data, mock_scanner_device)

    assert bermuda_advert.stamp <= monotonic_time_coarse()
    clamp_delta = mock_scanner_device.record_future_stamp_clamp.call_args[0][0]
    assert clamp_delta == pytest.approx(3600.0, abs=5.0)

    # A later, correct stamp must still be accepted (self.stamp was not poisoned).
    mock_scanner_device.async_as_scanner_get_stamp.return_value = monotonic_time_coarse()
    bermuda_advert.update_advertisement(mock_advertisement_data, mock_scanner_device)
    assert not mock_scanner_device.record_stale_advert_drop.called


def test_backward_stamps_rebase_after_streak(bermuda_advert, mock_advertisement_data, mock_scanner_device):
    """Consistent backwards stamps adopt the new clock base instead of dropping forever."""
    mock_advertisement_data.rssi = -71  # changed RSSI so the fixture-to-baseline jump is accepted
    mock_scanner_device.async_as_scanner_get_stamp.return_value = 1000.0
    bermuda_advert.update_advertisement(mock_advertisement_data, mock_scanner_device)
    assert bermuda_advert.stamp == 1000.0

    # The scanner's time base shifts backwards ~500s (reboot); stamps keep
    # progressing in the new base.
    for backward_stamp in (500.0, 500.5):
        mock_scanner_device.async_as_scanner_get_stamp.return_value = backward_stamp
        bermuda_advert.update_advertisement(mock_advertisement_data, mock_scanner_device)
        assert bermuda_advert.stamp == 1000.0  # still dropping
    assert mock_scanner_device.record_stale_advert_drop.call_count == 2

    mock_scanner_device.async_as_scanner_get_stamp.return_value = 501.0
    bermuda_advert.update_advertisement(mock_advertisement_data, mock_scanner_device)

    assert mock_scanner_device.record_stamp_rebase.called
    assert bermuda_advert.stamp == 501.0
    assert bermuda_advert.backward_drop_streak == 0
    # History spanning the discontinuity was discarded; only the fresh reading remains.
    assert bermuda_advert.hist_stamp == [501.0]

    # Subsequent stamps in the new base flow normally.
    mock_scanner_device.async_as_scanner_get_stamp.return_value = 502.0
    bermuda_advert.update_advertisement(mock_advertisement_data, mock_scanner_device)
    assert bermuda_advert.stamp == 502.0
    assert mock_scanner_device.record_stale_advert_drop.call_count == 2


def test_isolated_backward_stamp_still_dropped(bermuda_advert, mock_advertisement_data, mock_scanner_device):
    """A single out-of-order packet is still rejected; rebase needs a consistent streak."""
    mock_advertisement_data.rssi = -71  # changed RSSI so the fixture-to-baseline jump is accepted
    mock_scanner_device.async_as_scanner_get_stamp.return_value = 1000.0
    bermuda_advert.update_advertisement(mock_advertisement_data, mock_scanner_device)

    mock_scanner_device.async_as_scanner_get_stamp.return_value = 999.0
    bermuda_advert.update_advertisement(mock_advertisement_data, mock_scanner_device)
    assert bermuda_advert.stamp == 1000.0
    assert mock_scanner_device.record_stale_advert_drop.call_count == 1

    # An in-order packet resets the streak.
    mock_scanner_device.async_as_scanner_get_stamp.return_value = 1001.0
    bermuda_advert.update_advertisement(mock_advertisement_data, mock_scanner_device)
    assert bermuda_advert.stamp == 1001.0
    assert bermuda_advert.backward_drop_streak == 0
    assert not mock_scanner_device.record_stamp_rebase.called


def test_broken_scanner_falls_back_to_stampless_ingestion(bermuda_advert, mock_advertisement_data, mock_scanner_device):
    """A broken-stamp scanner keeps ingesting via the RSSI-change heuristic."""
    from bluetooth_data_tools import monotonic_time_coarse

    mock_scanner_device.timestamp_sync_state.return_value = "broken"
    stamp_calls_before = mock_scanner_device.async_as_scanner_get_stamp.call_count

    mock_advertisement_data.rssi = -65  # changed reading arrives while broken
    bermuda_advert.update_advertisement(mock_advertisement_data, mock_scanner_device)

    assert bermuda_advert.rssi == -65
    assert bermuda_advert.stamp_is_synthetic is True
    assert bermuda_advert.stamp == pytest.approx(monotonic_time_coarse() - 3.0, abs=2.0)
    # Stamps were never consulted while broken.
    assert mock_scanner_device.async_as_scanner_get_stamp.call_count == stamp_calls_before

    # Unchanged RSSI while broken means nothing provably new: no update.
    previous_stamp = bermuda_advert.stamp
    bermuda_advert.update_advertisement(mock_advertisement_data, mock_scanner_device)
    assert bermuda_advert.stamp == previous_stamp


def test_recovery_from_stampless_fallback_rebases_once(bermuda_advert, mock_advertisement_data, mock_scanner_device):
    """Returning to trusted stamps after fallback rebases instead of drop-storming."""
    from bluetooth_data_tools import monotonic_time_coarse

    mock_scanner_device.timestamp_sync_state.return_value = "broken"
    mock_advertisement_data.rssi = -65
    bermuda_advert.update_advertisement(mock_advertisement_data, mock_scanner_device)
    assert bermuda_advert.stamp_is_synthetic is True

    # Scanner recovers; its real per-device stamp is older than our synthetic one.
    mock_scanner_device.timestamp_sync_state.return_value = "synchronized"
    real_stamp = monotonic_time_coarse() - 10.0
    mock_scanner_device.async_as_scanner_get_stamp.return_value = real_stamp
    bermuda_advert.update_advertisement(mock_advertisement_data, mock_scanner_device)

    # One rebase, no stale-drop storm, stamp domain is trusted again.
    assert mock_scanner_device.record_stamp_rebase.called
    assert not mock_scanner_device.record_stale_advert_drop.called
    assert bermuda_advert.stamp == real_stamp
    assert bermuda_advert.stamp_is_synthetic is False


def test_accepted_adverts_update_scanner_acceptance_stamp(bermuda_advert, mock_advertisement_data, mock_scanner_device):
    """Every accepted advert refreshes the scanner's last-accepted tracker."""
    from bluetooth_data_tools import monotonic_time_coarse

    mock_scanner_device.async_as_scanner_get_stamp.return_value = monotonic_time_coarse()
    bermuda_advert.update_advertisement(mock_advertisement_data, mock_scanner_device)
    accepted_at = mock_scanner_device.scanner_last_accepted_advert_at
    assert accepted_at == pytest.approx(monotonic_time_coarse(), abs=2.0)


def test_isolated_replay_shaped_comeback_is_accepted_after_window(
    bermuda_advert, mock_advertisement_data, mock_scanner_device
):
    """An isolated one-off comeback is delayed, not discarded as a replay."""
    mock_advertisement_data.rssi = -71  # changed RSSI so the fixture-to-baseline jump is accepted
    mock_scanner_device.async_as_scanner_get_stamp.return_value = 1000.0
    bermuda_advert.update_advertisement(mock_advertisement_data, mock_scanner_device)
    bermuda_advert.calculate_data()
    assert bermuda_advert.stamp == 1000.0

    # A replay-shaped packet is held while a follow-up or burst is still possible.
    mock_scanner_device.async_as_scanner_get_stamp.return_value = 2083.0
    bermuda_advert.update_advertisement(mock_advertisement_data, mock_scanner_device)
    assert bermuda_advert.stamp == 1000.0
    assert not mock_scanner_device.record_advert_replay_suspect.called
    mock_scanner_device.register_advert_replay_candidate.assert_called_once()

    # Once scanner-wide classification says it remained isolated, accept the
    # saved packet rather than losing a legitimate slow/one-off advertisement.
    mock_scanner_device.resolve_advert_replay_candidate.return_value = "accept"
    bermuda_advert.calculate_data()
    assert bermuda_advert.stamp == 2083.0
    assert not mock_scanner_device.record_advert_replay_suspect.called


def test_unconfirmed_replay_burst_member_is_quarantined(bermuda_advert, mock_advertisement_data, mock_scanner_device):
    """Only a candidate classified as part of a scanner-wide burst is dropped."""
    mock_advertisement_data.rssi = -71
    mock_scanner_device.async_as_scanner_get_stamp.return_value = 1000.0
    bermuda_advert.update_advertisement(mock_advertisement_data, mock_scanner_device)
    bermuda_advert.calculate_data()

    mock_scanner_device.async_as_scanner_get_stamp.return_value = 2083.0
    bermuda_advert.update_advertisement(mock_advertisement_data, mock_scanner_device)
    mock_scanner_device.resolve_advert_replay_candidate.return_value = "reject"
    bermuda_advert.calculate_data()

    assert bermuda_advert.stamp == 1000.0
    mock_scanner_device.record_advert_replay_suspect.assert_called_once_with(bermuda_advert.device_address)


def test_real_comeback_confirmed_by_second_packet(bermuda_advert, mock_advertisement_data, mock_scanner_device):
    """A second packet arriving shortly after a quarantined one confirms a genuine return."""
    mock_advertisement_data.rssi = -71  # changed RSSI so the fixture-to-baseline jump is accepted
    mock_scanner_device.async_as_scanner_get_stamp.return_value = 1000.0
    bermuda_advert.update_advertisement(mock_advertisement_data, mock_scanner_device)
    bermuda_advert.calculate_data()

    # First comeback packet after long silence: quarantined even though genuine.
    mock_scanner_device.async_as_scanner_get_stamp.return_value = 2083.0
    bermuda_advert.update_advertisement(mock_advertisement_data, mock_scanner_device)
    assert bermuda_advert.stamp == 1000.0

    # A real device keeps advertising: the next packet confirms within seconds.
    mock_scanner_device.async_as_scanner_get_stamp.return_value = 2085.0
    bermuda_advert.update_advertisement(mock_advertisement_data, mock_scanner_device)
    assert bermuda_advert.stamp == 2085.0
    # The proxy is never accused for a comeback that confirmed itself.
    assert not mock_scanner_device.record_advert_replay_suspect.called
    mock_scanner_device.clear_advert_replay_candidate.assert_called_once()


def test_comeback_with_changed_payload_accepted_immediately(
    bermuda_advert, mock_advertisement_data, mock_scanner_device
):
    """Identical RSSI but a changed payload rules out a replayed cache frame."""
    mock_advertisement_data.rssi = -71  # changed RSSI so the fixture-to-baseline jump is accepted
    mock_scanner_device.async_as_scanner_get_stamp.return_value = 1000.0
    bermuda_advert.update_advertisement(mock_advertisement_data, mock_scanner_device)

    # Same RSSI after long silence, but the device's payload has moved on.
    mock_advertisement_data.manufacturer_data = {76: b"\x02\x16"}
    mock_scanner_device.async_as_scanner_get_stamp.return_value = 2083.0
    bermuda_advert.update_advertisement(mock_advertisement_data, mock_scanner_device)
    assert bermuda_advert.stamp == 2083.0
    assert not mock_scanner_device.record_advert_replay_suspect.called


def test_comeback_with_changed_rssi_accepted_immediately(bermuda_advert, mock_advertisement_data, mock_scanner_device):
    """A comeback packet with a different RSSI is not replay-like and passes at once."""
    mock_advertisement_data.rssi = -71  # changed RSSI so the fixture-to-baseline jump is accepted
    mock_scanner_device.async_as_scanner_get_stamp.return_value = 1000.0
    bermuda_advert.update_advertisement(mock_advertisement_data, mock_scanner_device)

    mock_advertisement_data.rssi = -75  # replays carry the identical cached frame
    mock_scanner_device.async_as_scanner_get_stamp.return_value = 2083.0
    bermuda_advert.update_advertisement(mock_advertisement_data, mock_scanner_device)
    assert bermuda_advert.stamp == 2083.0
    assert not mock_scanner_device.record_advert_replay_suspect.called


def test_short_gap_with_identical_rssi_not_quarantined(bermuda_advert, mock_advertisement_data, mock_scanner_device):
    """Ordinary packet loss well under the replay gap threshold flows through untouched."""
    mock_advertisement_data.rssi = -71  # changed RSSI so the fixture-to-baseline jump is accepted
    mock_scanner_device.async_as_scanner_get_stamp.return_value = 1000.0
    bermuda_advert.update_advertisement(mock_advertisement_data, mock_scanner_device)

    mock_scanner_device.async_as_scanner_get_stamp.return_value = 1060.0
    bermuda_advert.update_advertisement(mock_advertisement_data, mock_scanner_device)
    assert bermuda_advert.stamp == 1060.0
    assert not mock_scanner_device.record_advert_replay_suspect.called
