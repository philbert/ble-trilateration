# Implementation Plan: Per-Scanner Settings (ESPresense-style)

## Goal
Make Bermuda more like ESPresense by moving per-scanner configuration from the config flow UI to Number entities attached to each scanner device. This will provide:
1. **RSSI Offset** - Fine-tune signal strength readings per scanner (moved from config flow to Number entity)
2. **Absorption/Attenuation Factor** - Adjusts for room characteristics (walls, furniture, etc.)
3. **Maximum Distance Cutoff** - Filters out devices beyond a certain distance from each scanner

## Implementation Status

**STATUS**: Not yet implemented. This document provides a complete implementation plan incorporating all lessons learned from the initial prototype implementation.

### What Will Be Implemented

Three Number entities per Bluetooth scanner device:

1. **RSSI Offset** - Fine-tune signal strength readings per scanner
   - Migrates existing `CONF_RSSI_OFFSETS` values from config flow
   - Bidirectional sync between config flow and entity during transition
   - Eventually replaces config flow UI (deprecation path defined)

2. **Attenuation** - Environmental absorption factor for distance calculation
   - Already used in existing distance calculation code
   - Just needs per-scanner entity wiring

3. **Max Radius** - Maximum tracking distance per scanner
   - Currently uses global value in area determination
   - Needs per-scanner entity wiring into area logic

## Architecture Change: Config Flow → Device Entities

### Current Approach (To Be Deprecated)
- RSSI offsets configured through Bermuda's config flow UI
- Stored in `CONF_RSSI_OFFSETS` dictionary in integration options
- Buried in calibration menus, not easily discoverable
- Cannot be automated or adjusted via scripts

### New Approach (Number Entities on Scanner Devices)
- Each scanner device gets Number entities for configuration
- Entities attached to the scanner's Home Assistant device (ESPHome, Shelly, etc.)
- Values stored in entity state, with backup in config entry data
- Can be adjusted via UI, automations, scripts, or services
- More discoverable - appears directly on the scanner device
- Follows Home Assistant best practices for device-specific settings

## Entity-Based Architecture Details

### Entity Structure

Each Bluetooth proxy/scanner will have three Number entities created by Bermuda:

1. **RSSI Offset** (`number.{scanner_name}_rssi_offset`)
   - Purpose: Fine-tune RSSI readings for this specific scanner
   - Range: -127 to +127 dBm
   - Default: 0
   - Use case: Compensate for antenna differences, enclosures, or mounting positions

2. **Attenuation** (`number.{scanner_name}_attenuation`)
   - Purpose: Environmental absorption factor for distance calculation
   - Range: 1.0 to 10.0
   - Default: Uses global `CONF_ATTENUATION` value (3.0)
   - Use case: Adjust for room materials (lower for open space, higher for concrete walls)

3. **Max Radius** (`number.{scanner_name}_max_radius`)
   - Purpose: Maximum tracking distance for this scanner
   - Range: 1.0 to 100.0 meters
   - Default: Uses global `CONF_MAX_RADIUS` value (20.0)
   - Use case: Limit tracking to appropriate room/area size

### Device Association

Entities are associated with the scanner's **source device**, not Bermuda's internal device:

```python
# Example device_info for RSSI Offset entity
{
    "identifiers": {("bluetooth", "AA:BB:CC:DD:EE:FF")},  # Scanner's Bluetooth device
    # OR for ESPHome/network devices:
    "identifiers": {("esphome", "living_room_proxy")},
}
```

This ensures the entities appear on the **ESPHome proxy**, **Shelly device**, or **USB Bluetooth adapter** device page, not on Bermuda's own device entries.

### Data Flow

```
┌─────────────────────────────────────────────────────────────┐
│ User adjusts Number entity value                            │
│ (UI, automation, script, or service call)                   │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│ BermudaScannerNumber.async_set_native_value()              │
│ - Validates input                                            │
│ - Updates entity state                                       │
│ - Persists to config entry data (backup)                    │
│ - Calls coordinator.update_scanner_config()                 │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│ Coordinator.update_scanner_config()                         │
│ - Updates scanner configuration                              │
│ - Calls scanner.reload_advert_configs()                     │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│ BermudaDevice.reload_advert_configs()                       │
│ - Reloads config for all adverts from this scanner          │
│ - Recalculates distances with new settings                  │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│ BermudaAdvert instances refresh                             │
│ - Pick up new rssi_offset / attenuation / max_radius        │
│ - Recalculate distances                                      │
│ - Update area determinations if needed                       │
└─────────────────────────────────────────────────────────────┘
```

### Value Resolution Priority

When `BermudaAdvert` needs a configuration value, it checks in this order:

1. **Entity state** - Check if Number entity exists and has a value
2. **Config entry data** - Fall back to persisted value (for migration/backup)
3. **Global default** - Use integration-wide default value

```python
def get_scanner_rssi_offset(self, scanner_address: str) -> int:
    """Get RSSI offset for a scanner, checking entity first."""
    # 1. Try entity state
    entity_id = f"number.{scanner_slug}_rssi_offset"
    if (state := self.hass.states.get(entity_id)) is not None:
        return int(float(state.state))

    # 2. Try config entry data (migration path)
    if scanner_address in self.options.get(CONF_RSSI_OFFSETS, {}):
        return self.options[CONF_RSSI_OFFSETS][scanner_address]

    # 3. Use default
    return 0
```

### Migration Strategy

**Automatic Migration from Config Flow to Entities:**

Existing installations with RSSI offsets configured in the config flow (`CONF_RSSI_OFFSETS`) will be automatically migrated to Number entities:

1. **On first load after upgrade**:
   - Check for existing `CONF_RSSI_OFFSETS` values in config entry options
   - For each scanner with an RSSI offset, create the Number entity with that value
   - Entity state immediately persists the migrated value via `RestoreNumber`

2. **Bidirectional sync during transition**:
   - If user changes RSSI offset in config flow UI, entity state updates automatically
   - If user changes entity value, it takes precedence over config flow
   - Legacy `CONF_RSSI_OFFSETS` remains in config as backup during transition

3. **Value resolution priority** (see coordinator helper methods):
   ```
   Entity state → CONF_RSSI_OFFSETS (legacy) → Default (0)
   ```

4. **Future deprecation path**:
   - The per-scanner RSSI offset section in the config flow UI will be **deprecated**
   - Eventually it will be **removed** entirely in favor of the Number entities
   - Timeline: TBD (at least 2 major releases with deprecation warnings)
   - Users will be directed to adjust RSSI offsets via the Bluetooth proxy device page

**Migration guarantees:**
- ✅ No data loss - existing RSSI offset values are preserved
- ✅ No manual migration required - happens automatically on upgrade
- ✅ Backwards compatible - config flow continues to work during transition
- ✅ User-friendly - entities appear on familiar scanner device pages

## Current State Analysis

### Existing Configuration Structure

**Global Settings** (apply to all scanners):
- `CONF_MAX_RADIUS` (default: 20m) - Global maximum tracking distance
- `CONF_ATTENUATION` (default: 3) - Environmental signal attenuation factor
- `CONF_REF_POWER` (default: -55.0 dBm) - Expected RSSI at 1 meter

**Per-Scanner Settings** (already partially implemented):
- `CONF_RSSI_OFFSETS` - Dictionary with scanner address as key, RSSI offset as value
  - Example: `{"aa:bb:cc:dd:ee:ff": 5, "11:22:33:44:55:66": -3}`
  - Applied in `bermuda_advert.py:283` during distance calculation

### Key Code Locations

1. **Distance Calculation** - `bermuda_advert.py:283`
   ```python
   distance = rssi_to_metres(self.rssi + self.conf_rssi_offset, ref_power, self.conf_attenuation)
   ```

2. **Configuration Access** - `bermuda_advert.py:97-100`
   ```python
   self.conf_rssi_offset = self.options.get(CONF_RSSI_OFFSETS, {}).get(self.scanner_address, 0)
   self.conf_ref_power = self.options.get(CONF_REF_POWER)
   self.conf_attenuation = self.options.get(CONF_ATTENUATION)
   self.conf_max_velocity = self.options.get(CONF_MAX_VELOCITY)
   ```

3. **Area Determination** - `coordinator.py:1310-1356`
   ```python
   _max_radius = self.options.get(CONF_MAX_RADIUS, DEFAULT_MAX_RADIUS)
   ...
   if challenger.rssi_distance > _max_radius:
       continue  # Scanner reading is too far away
   ```

4. **Config Flow UI** - `config_flow.py:463-539`
   - `async_step_calibration2_scanners()` handles per-scanner RSSI offset configuration

## Proposed Changes

### 1. New Configuration Constants (`const.py`)

Add new per-scanner configuration keys:

```python
# Per-scanner configuration dictionaries
CONF_SCANNER_ATTENUATION = "scanner_attenuation"
DOCS[CONF_SCANNER_ATTENUATION] = "Per-scanner attenuation factor for environmental effects"

CONF_SCANNER_MAX_RADIUS = "scanner_max_radius"
DOCS[CONF_SCANNER_MAX_RADIUS] = "Per-scanner maximum tracking distance in meters"

# Default values when not configured
DEFAULT_SCANNER_ATTENUATION = None  # None means use global default
DEFAULT_SCANNER_MAX_RADIUS = None   # None means use global default
```

### 2. Data Structure

Each per-scanner setting will be stored as a dictionary with scanner MAC address as the key:

```python
# Example options structure after implementation:
{
    # Global defaults (fallback values)
    "max_area_radius": 20,
    "attenuation": 3.0,
    "ref_power": -55.0,

    # Per-scanner overrides
    "scanner_attenuation": {
        "aa:bb:cc:dd:ee:ff": 2.5,  # Office scanner - less walls
        "11:22:33:44:55:66": 4.0,  # Garage scanner - concrete walls
    },
    "scanner_max_radius": {
        "aa:bb:cc:dd:ee:ff": 10.0,  # Office - smaller room
        "11:22:33:44:55:66": 25.0,  # Garage - larger space
    },

    # Existing per-scanner settings
    "rssi_offsets": {
        "aa:bb:cc:dd:ee:ff": 2,
        "11:22:33:44:55:66": -3,
    }
}
```

### 3. Update `BermudaAdvert` Class (`bermuda_advert.py`)

**Modify initialization** (around line 97-101):

```python
# Get scanner-specific settings with fallback to global defaults
scanner_attenuations = self.options.get(CONF_SCANNER_ATTENUATION, {})
self.conf_attenuation = scanner_attenuations.get(
    self.scanner_address,
    self.options.get(CONF_ATTENUATION, DEFAULT_ATTENUATION)
)

scanner_max_radii = self.options.get(CONF_SCANNER_MAX_RADIUS, {})
self.conf_max_radius = scanner_max_radii.get(
    self.scanner_address,
    self.options.get(CONF_MAX_RADIUS, DEFAULT_MAX_RADIUS)
)

# Keep existing RSSI offset
self.conf_rssi_offset = self.options.get(CONF_RSSI_OFFSETS, {}).get(self.scanner_address, 0)
self.conf_ref_power = self.options.get(CONF_REF_POWER, DEFAULT_REF_POWER)
self.conf_max_velocity = self.options.get(CONF_MAX_VELOCITY, DEFAULT_MAX_VELOCITY)
self.conf_smoothing_samples = self.options.get(CONF_SMOOTHING_SAMPLES, DEFAULT_SMOOTHING_SAMPLES)
```

**No changes needed to `_update_raw_distance()`** - it already uses `self.conf_attenuation` from instance.

### 4. Update Area Determination (`coordinator.py`)

**Modify `_refresh_area_by_min_distance()`** (around line 1310-1356):

```python
def _refresh_area_by_min_distance(self, device: BermudaDevice):
    """Very basic Area setting by finding closest proxy to a given device."""
    incumbent: BermudaAdvert | None = device.area_advert

    # Remove global max_radius lookup
    # _max_radius = self.options.get(CONF_MAX_RADIUS, DEFAULT_MAX_RADIUS)

    nowstamp = monotonic_time_coarse()
    tests = self.AreaTests()
    tests.device = device.name
    _superchatty = False

    for challenger in device.adverts.values():
        # ... existing checks ...

        # NEW: Use per-scanner max_radius instead of global
        # Each BermudaAdvert now has its own conf_max_radius
        if (
            challenger.rssi_distance is None
            or challenger.rssi_distance > challenger.conf_max_radius  # CHANGED
            or challenger.area_id is None
        ):
            continue

        # ... rest of the logic unchanged ...
```

### 5. Enhanced Config Flow UI (`config_flow.py`)

Create a new step `async_step_calibration3_advanced_scanners()`:

```python
async def async_step_calibration3_advanced_scanners(self, user_input=None):
    """
    Per-scanner advanced configuration: attenuation and max distance.

    Similar to calibration2_scanners but for attenuation and max_radius
    instead of RSSI offsets. More user-friendly than RSSI adjustments.
    """
    if user_input is not None:
        if user_input.get(CONF_SAVE_AND_CLOSE):
            # Build per-scanner dicts
            scanner_attenuations = {}
            scanner_max_radii = {}

            for scanner_address in self.coordinator.scanner_list:
                scanner_name = self.coordinator.devices[scanner_address].name
                scanner_data = user_input[CONF_SCANNER_INFO].get(scanner_name, {})

                # Store attenuation if provided (not None)
                if (atten := scanner_data.get("attenuation")) is not None:
                    scanner_attenuations[scanner_address] = max(min(float(atten), 10.0), 1.0)

                # Store max_radius if provided (not None)
                if (max_rad := scanner_data.get("max_radius")) is not None:
                    scanner_max_radii[scanner_address] = max(min(float(max_rad), 100.0), 1.0)

            self.options.update({
                CONF_SCANNER_ATTENUATION: scanner_attenuations,
                CONF_SCANNER_MAX_RADIUS: scanner_max_radii,
            })
            return await self._update_options()

        # Store for refresh
        self._last_scanner_info = user_input[CONF_SCANNER_INFO]
        self._last_device = user_input.get(CONF_DEVICES)

    # Build default values from saved config
    saved_attenuations = self.options.get(CONF_SCANNER_ATTENUATION, {})
    saved_max_radii = self.options.get(CONF_SCANNER_MAX_RADIUS, {})
    global_attenuation = self.options.get(CONF_ATTENUATION, DEFAULT_ATTENUATION)
    global_max_radius = self.options.get(CONF_MAX_RADIUS, DEFAULT_MAX_RADIUS)

    scanner_config_dict = {}
    for scanner_address in self.coordinator.scanner_list:
        scanner_name = self.coordinator.devices[scanner_address].name
        scanner_config_dict[scanner_name] = {
            "attenuation": saved_attenuations.get(scanner_address, global_attenuation),
            "max_radius": saved_max_radii.get(scanner_address, global_max_radius),
        }

    data_schema = {
        vol.Optional(CONF_DEVICES): DeviceSelector(DeviceSelectorConfig(integration=DOMAIN)),
        vol.Required(
            CONF_SCANNER_INFO,
            default=scanner_config_dict if not self._last_scanner_info else self._last_scanner_info,
        ): ObjectSelector(),
        vol.Optional(CONF_SAVE_AND_CLOSE, default=False): vol.Coerce(bool),
    }

    # Build description with distance estimates if device selected
    description_suffix = "Configure per-scanner settings. Lower attenuation for open spaces, higher for rooms with thick walls."

    if self._last_device and isinstance(self._last_scanner_info, dict):
        device = self._get_bermuda_device_from_registry(self._last_device)
        if device is not None:
            results_str = "\n\n**Current Estimated Distances:**\n\n"
            results_str += "| Scanner | Distance | Attenuation | Max Radius |\n"
            results_str += "|---------|----------|-------------|------------|\n"

            for scanner_address in self.coordinator.scanner_list:
                scanner_name = self.coordinator.devices[scanner_address].name
                scanner_data = self._last_scanner_info.get(scanner_name, {})
                atten = scanner_data.get("attenuation", global_attenuation)
                max_rad = scanner_data.get("max_radius", global_max_radius)

                if (advert := device.get_scanner(scanner_address)) is not None:
                    # Recalculate with new settings
                    if advert.rssi is not None:
                        distance = rssi_to_metres(
                            advert.rssi + advert.conf_rssi_offset,
                            self.options.get(CONF_REF_POWER, DEFAULT_REF_POWER),
                            atten,
                        )
                        status = "✓" if distance <= max_rad else "✗ (too far)"
                        results_str += f"| {scanner_name} | {distance:.2f}m {status} | {atten} | {max_rad}m |\n"

            description_suffix = results_str

    return self.async_show_form(
        step_id="calibration3_advanced_scanners",
        data_schema=vol.Schema(data_schema),
        description_placeholders={"suffix": description_suffix},
    )
```

**Update the main calibration menu** (`async_step_init()`) to include the new option:

```python
# In the calibration menu options, add:
"calibration3_advanced_scanners": "Advanced Per-Scanner Settings (Attenuation, Max Distance)",
```

### 6. Backwards Compatibility

The implementation maintains full backwards compatibility:

1. **Existing configs** will continue to work - global settings are still used as defaults
2. **Migration not required** - per-scanner settings are optional and default to global values
3. **RSSI offsets preserved** - existing calibration2_scanners step remains unchanged
4. **Gradual adoption** - users can configure per-scanner settings for some scanners while others use defaults

### 7. User Experience Improvements

**Benefits over current RSSI offset approach:**

| Current (RSSI Offset) | Proposed (Per-Scanner Settings) |
|----------------------|--------------------------------|
| Obscure RSSI value adjustment (-127 to +127) | Clear attenuation factor (1.0 to 10.0) |
| Requires understanding of dBm | Intuitive: lower = open space, higher = thick walls |
| Global max radius for all scanners | Each scanner has appropriate range |
| No visual feedback on effect | Shows calculated distances in real-time |
| Single calibration step | Separate basic (RSSI) and advanced (attenuation) steps |

**Example user scenarios:**

1. **Office Scanner** (open plan, drywall):
   - Attenuation: 2.5 (lower than default 3.0)
   - Max Radius: 10m (smaller room)

2. **Garage Scanner** (concrete walls, metal doors):
   - Attenuation: 4.5 (higher than default)
   - Max Radius: 25m (larger space, but signals don't penetrate walls)

3. **Bedroom Scanner** (upstairs, wood/plaster):
   - Attenuation: 3.0 (use global default)
   - Max Radius: 15m

## Implementation Checklist

### Phase 1: Number Entities for Scanner Configuration

**File**: `custom_components/bermuda/number.py`

#### 1.1 Setup and Entity Creation

**Requirements:**
- Listen to `SIGNAL_SCANNERS_CHANGED` in addition to existing `SIGNAL_DEVICE_NEW`
- When signal received, create three Number entities for each scanner device
- Track which scanners already have entities created to avoid duplicates

**Entity Types:**
1. `BermudaScannerRSSIOffset` - RSSI calibration
2. `BermudaScannerAttenuation` - Environmental absorption
3. `BermudaScannerMaxRadius` - Maximum detection distance

#### 1.2 BermudaScannerRSSIOffset Requirements

**Architecture:**
- Inherit from: `BermudaEntity` (device association) + `RestoreNumber` (persistence)
- Entity category: `CONFIG`
- Device class: `SIGNAL_STRENGTH`
- Range: -127 to +127 dBm, step 1
- Unique ID: `{scanner_unique_id}_scanner_rssi_offset`

**Critical Behaviors:**

1. **Legacy Migration** (in `async_added_to_hass`):
   - Check if entity has restored data
   - If no restored data exists, check `coordinator.options["rssi_offsets"][scanner_address]`
   - If legacy value exists, save it as entity state and write immediately
   - This ensures existing config flow values are preserved

2. **Bidirectional Sync** (in `_handle_coordinator_update`):
   - Check if `coordinator.options["rssi_offsets"]` has changed
   - If entity value differs from config flow value, update entity state
   - This allows config flow changes to propagate to entities during transition

3. **Value Resolution** (in `native_value` property):
   - Priority: Restored entity state → Legacy `CONF_RSSI_OFFSETS` → Default (0)
   - Must check BOTH sources for proper migration

4. **Change Propagation** (in `async_set_native_value`):
   - Update entity state
   - Call `coordinator.reload_all_advert_configs()` to trigger recalculation
   - Distance calculations must update immediately

**Testing Criteria:**
- ✅ Existing RSSI offsets from config flow appear in entity
- ✅ Changing value in config flow updates entity
- ✅ Changing entity updates distance calculations immediately
- ✅ Values persist across HA restart

#### 1.3 BermudaScannerAttenuation Requirements

**Architecture:**
- Inherit from: `BermudaEntity` + `RestoreNumber`
- Entity category: `CONFIG`
- Range: 1.0 to 10.0, step 0.1
- Unique ID: `{scanner_unique_id}_scanner_attenuation`

**Critical Behaviors:**

1. **No Migration Needed**: No legacy config exists for attenuation
2. **Default Value**: Falls back to global `coordinator.options["attenuation"]` (3.0)
3. **Change Propagation**: Call `coordinator.reload_all_advert_configs()` on value change

**Testing Criteria:**
- ✅ Defaults to global attenuation value (3.0)
- ✅ Changing entity updates distance calculations
- ✅ Different scanners can have different attenuation values

#### 1.4 BermudaScannerMaxRadius Requirements

**Architecture:**
- Inherit from: `BermudaEntity` + `RestoreNumber`
- Entity category: `CONFIG`
- Range: 0 to 100 meters, step 1
- Unit: meters
- Unique ID: `{scanner_unique_id}_scanner_max_radius`

**Critical Behaviors:**

1. **No Migration Needed**: No legacy per-scanner config exists
2. **Default Value**: Falls back to global `coordinator.options["max_area_radius"]` (20.0)
3. **Change Propagation**: Call `coordinator.reload_all_advert_configs()` on value change

**Testing Criteria:**
- ✅ Defaults to global max radius value (20.0)
- ✅ Changing entity affects area determination
- ✅ Devices beyond scanner's max radius not assigned to that area

### Phase 2: Coordinator Helper Methods

**File**: `custom_components/bermuda/coordinator.py`

**Location**: Add after `scanner_list_del()` method (around line 324)

#### 2.1 Value Resolution Methods

Create three helper methods that implement the value resolution priority:

**`get_scanner_rssi_offset(scanner_address: str) -> float`**
- **Priority**: Entity state → `CONF_RSSI_OFFSETS` → Default (0.0)
- **Purpose**: Allows BermudaAdvert to get RSSI offset without knowing about entities
- **Key**: Must check entity registry for entity ID, then hass.states for value
- **Fallback**: Legacy config ensures smooth migration

**`get_scanner_attenuation(scanner_address: str) -> float`**
- **Priority**: Entity state → Global `CONF_ATTENUATION` → Default (3.0)
- **Purpose**: Get per-scanner attenuation for distance calculations
- **No legacy fallback**: Attenuation was never per-scanner before

**`get_scanner_max_radius(scanner_address: str) -> float`**
- **Priority**: Entity state → Global `CONF_MAX_RADIUS` → Default (20.0)
- **Purpose**: Get per-scanner max radius for area determination
- **No legacy fallback**: Max radius was never per-scanner before

**Common Pattern:**
1. Look up scanner device from `self.devices`
2. Build entity unique_id (e.g., `{scanner.unique_id}_scanner_rssi_offset`)
3. Use `self.er.async_get_entity_id(Platform.NUMBER, DOMAIN, unique_id)`
4. Get state from `self.hass.states.get(entity_id)`
5. Handle "unknown"/"unavailable" states gracefully
6. Fall back through priority chain

#### 2.2 Configuration Reload Method

**`reload_all_advert_configs() -> None`**
- **Purpose**: Trigger all BermudaAdvert instances to reload their config values
- **When called**: When any scanner Number entity value changes
- **Implementation**: Loop through all devices, all adverts, call `advert.reload_config_values()`
- **Effect**: Distance calculations and area determinations use new values immediately

### Phase 3: BermudaAdvert Configuration Integration

**File**: `custom_components/bermuda/bermuda_advert.py`

#### 3.1 Update `__init__()` Configuration Loading

**Location**: Around line 96-102 where config values are currently loaded

**Requirements:**
- Replace direct `self.options.get()` calls with coordinator helper methods
- Add new `self.conf_max_radius` attribute
- Keep non-per-scanner configs (ref_power, max_velocity, smoothing_samples) as-is

**Changes:**
- `conf_rssi_offset`: Use `self._device._coordinator.get_scanner_rssi_offset(self.scanner_address)`
- `conf_attenuation`: Use `self._device._coordinator.get_scanner_attenuation(self.scanner_address)`
- `conf_max_radius`: NEW - Use `self._device._coordinator.get_scanner_max_radius(self.scanner_address)`

**Why**:
- Decouples BermudaAdvert from knowing about entities
- Ensures proper value resolution priority
- Already has access to coordinator via `self._device._coordinator`

#### 3.2 Add Dynamic Reload Method

**Method**: `reload_config_values()`
**Location**: Add after `apply_new_scanner()` method (around line 118)

**Requirements:**
- Reload the three per-scanner config values from coordinator
- Called by `coordinator.reload_all_advert_configs()` when entity values change
- Should NOT reload ref_power, max_velocity, or smoothing_samples (these remain global)

**Effect**: Allows distance/area calculations to pick up new entity values without recreating BermudaAdvert objects

**Note**: Distance calculation at line 289 already uses `self.conf_attenuation`, no changes needed there

### Phase 4: Area Determination Max Radius Integration

**File**: `custom_components/bermuda/coordinator.py`

**Method**: `_refresh_area_by_min_distance()`
**Location**: Around line 1387-1432

#### 4.1 Problem

Currently uses **global** `_max_radius` value for all scanners:
- Line 1392: `_max_radius = self.options.get(CONF_MAX_RADIUS, DEFAULT_MAX_RADIUS)`
- Line 1432: Checks `challenger.rssi_distance > _max_radius`

This prevents different scanners from having different detection ranges.

#### 4.2 Solution

**Remove**: Global `_max_radius` variable lookup (line 1392)

**Change**: Line 1432 from global to per-scanner check
- **Old**: `if challenger.rssi_distance is None or challenger.rssi_distance > _max_radius or challenger.area_id is None:`
- **New**: `if challenger.rssi_distance is None or challenger.rssi_distance > challenger.conf_max_radius or challenger.area_id is None:`

**Why**:
- Each `challenger` is a `BermudaAdvert` instance
- After Phase 3, it will have `conf_max_radius` attribute loaded from entity
- This allows per-scanner max radius to affect area determination

**Effect**: Scanners with smaller max_radius won't "claim" distant devices

### Phase 5: Testing and Validation

After implementation, verify:

1. **Entity Creation**
   - [ ] Three Number entities appear on each Bluetooth scanner device
   - [ ] Entities are in CONFIG category (appear in device configuration section)
   - [ ] Entity names are clear: "RSSI Offset", "Attenuation", "Max Radius"

2. **RSSI Offset Migration**
   - [ ] Existing `CONF_RSSI_OFFSETS` values automatically populate entity state
   - [ ] Changes in config flow UI update entity state
   - [ ] Changes in entity UI update distance calculations immediately

3. **Attenuation**
   - [ ] Entity values default to global `CONF_ATTENUATION` (3.0)
   - [ ] Changing entity value updates distance calculations
   - [ ] Different attenuation per scanner works correctly

4. **Max Radius**
   - [ ] Entity values default to global `CONF_MAX_RADIUS` (20.0)
   - [ ] Changing entity value affects area determination
   - [ ] Devices beyond scanner's max_radius are not assigned to that area

5. **Value Persistence**
   - [ ] Entity values persist across HA restarts
   - [ ] RestoreNumber mechanism working correctly

### Phase 6: Future Deprecation Path (Not Implemented Yet)

After the entities are stable and working well:

- [ ] Add deprecation warning banner in `calibration2_scanners` config flow step
  - Message: "RSSI offset configuration is moving to Number entities on each scanner device. Please use the entities on your Bluetooth proxy devices instead."
  - Link to scanner device pages
- [ ] Add "View Scanner Entities" button in config flow
- [ ] Keep existing config flow functional for backwards compatibility (2+ major releases)
- [ ] Plan removal timeline:
  - Version N: Add deprecation warnings
  - Version N+1: Stronger warnings with countdown
  - Version N+2: Remove `calibration2_scanners` config flow step entirely

### Phase 7: Documentation
- [ ] Update README.md with new per-scanner configuration options
- [ ] Add examples of typical attenuation values for different environments
- [ ] Update wiki documentation
- [ ] Create migration guide for users currently using RSSI offsets

## Testing Scenarios

### Test 1: New Installation
1. Install integration with defaults
2. Configure basic device tracking
3. Navigate to advanced scanner settings
4. Set different attenuation/max_radius for each scanner
5. Verify devices tracked correctly with per-scanner settings

### Test 2: Existing Installation Upgrade
1. Start with existing config using RSSI offsets
2. Upgrade to new version
3. Verify existing tracking continues to work
4. Add per-scanner settings for one scanner
5. Verify mixed global/per-scanner configuration works

### Test 3: Edge Cases
1. Set attenuation to 1.0 (minimum) - should give longer distance estimates
2. Set attenuation to 10.0 (maximum) - should give shorter distance estimates
3. Set max_radius to 1.0m - should only detect very close devices
4. Set max_radius to 100m - should detect all devices in range
5. Remove per-scanner setting - should fall back to global default

## Migration Path for Existing RSSI Offset Users

### Current Implementation Status (✅ Complete)

The following migration features are **already implemented**:

1. **Automatic value migration** (`number.py:199-221`)
   - On entity creation, checks for existing `CONF_RSSI_OFFSETS` values
   - Automatically populates Number entity with legacy config value
   - Persists to entity state via `RestoreNumber` mechanism
   - No user action required

2. **Config flow synchronization** (`number.py:223-240`)
   - `_handle_coordinator_update()` monitors config changes
   - When RSSI offset changes in config flow UI, entity updates automatically
   - Ensures config flow changes are reflected in entity state immediately

3. **Value resolution with fallback** (`coordinator.py:326-351`)
   - `get_scanner_rssi_offset()` checks entity state first
   - Falls back to `CONF_RSSI_OFFSETS` if no entity value exists
   - Finally defaults to 0 if neither source has a value
   - Ensures smooth transition with no data loss

4. **Dynamic configuration reload** (`bermuda_advert.py:121-124`, `coordinator.py:397-406`)
   - When entity values change, all affected `BermudaAdvert` instances reload configs
   - Distance calculations update immediately with new RSSI offset values
   - No integration reload required

### User Experience During Migration

**Immediate post-upgrade:**
- Existing RSSI offset values appear in new Number entities on scanner devices
- Both config flow UI and entities show the same values
- Changes in either location sync to the other

**Recommended workflow:**
1. Upgrade to version with Number entities
2. Navigate to scanner device pages (ESPHome, Shelly, USB adapter)
3. Verify RSSI Offset entities show correct migrated values
4. Going forward, adjust values via Number entities instead of config flow
5. Config flow RSSI offset UI will be deprecated in future releases

### Future Attenuation/Max Radius Migration

Users currently using RSSI offsets can:

1. **Keep using RSSI offsets** - they continue to work as before
2. **Switch to attenuation** - provides more intuitive control (when implemented)
3. **Use both** - RSSI offset applied first, then attenuation used in distance calculation

**Conversion guidance** (approximate):
- RSSI offset of +10 dBm ≈ Attenuation factor increase of 0.3-0.5
- RSSI offset of -10 dBm ≈ Attenuation factor decrease of 0.3-0.5

Users should recalibrate using the new per-scanner settings rather than trying to convert.

## Future Enhancements

1. **Auto-calibration**: Walk a device through the house and automatically determine scanner attenuation
2. **Templates**: Preset attenuation values for common environments (office, home, warehouse)
3. **Per-scanner ref_power**: Allow different reference power per scanner (different antenna gains)
4. **Visualization**: Show scanner ranges as circles on a floor plan
5. **Scanner profiles**: Save/load complete scanner configuration sets

## ESPresense Feature Comparison

After implementation, Bermuda will have feature parity with ESPresense for:
- ✅ Per-scanner absorption/attenuation factor
- ✅ Per-scanner maximum distance cutoff
- ✅ Flexible room-specific tuning
- ✅ Visual feedback during configuration

Additional advantages over ESPresense:
- Integrated with Home Assistant device registry
- No separate MQTT broker required
- Leverages existing ESPHome bluetooth proxy infrastructure
- Automatic iBeacon and Private BLE device support

---

## Implementation Summary

This plan provides a complete, step-by-step guide to implement per-scanner configuration via Number entities. Key features:

### ✅ **What Works Out of the Box**
1. **Three Number entities per scanner**: RSSI Offset, Attenuation, Max Radius
2. **Automatic migration**: Existing RSSI offset config flow values → entity state
3. **Bidirectional sync**: Config flow changes update entities during transition period
4. **Immediate effect**: Entity changes trigger `reload_all_advert_configs()` for instant updates
5. **Proper fallbacks**: Entity state → Legacy config → Global defaults
6. **RestoreNumber persistence**: Values survive HA restarts

### 🔧 **Critical Requirements (Must Not Be Missed)**

**RSSI Offset Entity:**
1. Implement `_handle_coordinator_update()` for bidirectional config flow sync
2. Check BOTH `restored_data` AND `options["rssi_offsets"]` in `native_value` property
3. Migrate legacy values in `async_added_to_hass()` and write state immediately
4. Call `coordinator.reload_all_advert_configs()` when value changes

**All Entities:**
1. Inherit from `BermudaEntity` + `RestoreNumber`
2. Entity category must be `CONFIG`
3. Call `coordinator.reload_all_advert_configs()` in `async_set_native_value()`

**Coordinator:**
1. Helper methods must check entity registry → entity state → fallback
2. Handle "unknown"/"unavailable" entity states gracefully
3. `reload_all_advert_configs()` must iterate all devices and adverts

**BermudaAdvert:**
1. Load all per-scanner configs via coordinator helpers (not direct options access)
2. Add `conf_max_radius` attribute (new)
3. `reload_config_values()` must reload all three per-scanner values

**Area Logic:**
1. Remove global `_max_radius` lookup
2. Use `challenger.conf_max_radius` (per-scanner value)

### 📁 **Files To Modify**
1. `number.py` - Entity classes + setup (~300+ lines)
2. `coordinator.py` - Helper methods + area logic (~100 lines)
3. `bermuda_advert.py` - Config loading + reload (~15 lines)

### 🎯 **Testing Checklist**
- Entities appear on scanner devices (not Bermuda devices)
- Legacy RSSI offsets migrate automatically
- Config flow changes sync to entities
- Entity changes affect distance/area immediately
- Values persist across restarts
- Works with ESPHome, Shelly, USB Bluetooth adapters

### 📝 **Future Work**
- Add deprecation warnings to config flow UI
- Eventually remove config flow RSSI offset step
- Document typical attenuation values per environment

