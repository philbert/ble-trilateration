# Debug Logging Improvement Plan

## Objective

Enable filtering debug logs by friendly device names (e.g., `if device.name == "Phil's iPhone"`) instead of seeing unfriendly names like `apple_device_ab_cd_ef_12_34_56` in logs.

## Root Cause

In `bermuda_advert.py`, the `update_advertisement()` method calls distance calculation BEFORE name resolution:

**Current order**:
1. Capture RSSI from advertisement
2. Call `_update_raw_distance()` → logs fire with unfriendly name
3. Process advertisement data, extract `local_name`
4. Call `make_name()` → friendly name resolved (too late)

The logs fire **before** the advertisement's local_name has been processed and `make_name()` called.

## Solution

### Part 1: Fix Name Resolution Timing

Reorder the code in `bermuda_advert.py` to extract and apply the device name BEFORE calculating distance:

- Extract `local_name` from advertisement data early
- Call `make_name()` before `_update_raw_distance()`
- This ensures friendly names are available when debug logs fire

**Key consideration**: Keep the existing full advertisement processing later in the function for history tracking - only extract what's needed for name resolution early.

### Part 2: Update Logs to Use Friendly Names

Change debug logs to use `self._device.name` and `self.scanner_device.name` instead of MAC addresses.

### Part 3: Add Device-Specific Debug Filtering

Create a centralized `DEBUG_DEVICES` list in `const.py` that users can edit:

- Add `DEBUG_DEVICES = []` constant
- Users edit this list to add their devices: `["Phil's iPhone", "Kitchen Sensor"]`
- Use `if device.name in DEBUG_DEVICES` to gate debug logs

This allows enabling detailed logging for specific devices without:
- Editing code in multiple places
- Uncommenting complex conditional logic
- Hardcoding IRKs or MAC addresses
- Creating log spam for all devices

### Part 4: Preserve Existing _superchatty Pattern

Do NOT modify the existing `_superchatty` mechanism - it's for very verbose area determination logging. Only apply the new `DEBUG_DEVICES` filtering to newly added debug statements.

## Testing Plan

1. Test with Private BLE device (iPhone) to verify friendly name appears in logs
2. Test with regular MAC device to verify name resolution still works
3. Test with devices that don't send `local_name` in every advertisement
4. Verify logs can be filtered with `DEBUG_DEVICES = ["Phil's iPhone"]`
5. Verify no regressions in name resolution or distance calculation
6. Verify logs only appear for devices in DEBUG_DEVICES list (not spammy)

## Files to Modify

- `custom_components/bermuda/const.py`: Add `DEBUG_DEVICES` constant
- `custom_components/bermuda/bermuda_advert.py`:
  - Early name extraction before distance calc
  - Update logs to use friendly names
  - Gate new logs with `DEBUG_DEVICES` check
- `custom_components/bermuda/coordinator.py`:
  - Import and use `DEBUG_DEVICES` constant
  - Gate new area determination logs with device check
