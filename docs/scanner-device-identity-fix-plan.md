# Scanner Device Identity Fix Plan

## Problem Summary

Bermuda scanner entities are not being attached consistently to one stable Home Assistant device.

Observed failure modes:
- some scanner entities appear under a dedicated Bermuda BLE proxy device
- some scanner entities get attached to the host integration device instead (`Shelly`, `ESPHome`, etc.)
- some scanners end up with duplicate entities after identity resolution changes
- behavior differs by device type and by discovery timing

This is not a timestamp-sync-sensor-specific bug. The timestamp sync sensor only exposed the deeper issue more visibly.

The real problem is that Bermuda currently mixes several different identities for the same scanner:
- `self.address`
- `self.address_ble_mac`
- `self.address_wifi_mac`
- `self.unique_id`
- device registry matches via `connections`

Those identities are also resolved at different times, so entity creation order affects which HA device the entity lands on.

## Root Cause

### 1. Scanner identity is mutable

`BermudaDevice.unique_id` for scanners is reassigned in `async_as_scanner_resolve_device_entries()` based on whichever HA device-registry data is available at the time.

That means the same scanner can start life with one identity and later switch to another after Bluetooth / Shelly / ESPHome device metadata is resolved.

### 2. Scanner entities do not share one canonical base key

Different scanner entities use different identity inputs:
- anchor numbers use `self._device.unique_id`
- some per-scanner range entities use `address_wifi_mac or address`
- timestamp sync was recently changed several times between `unique_id`, Wi-Fi MAC, and BLE MAC

So Bermuda has no single immutable scanner entity key.

### 3. Scanner `device_info` merges with host devices

`BermudaEntity.device_info` for scanners currently exposes `connections` containing:
- network MAC
- Bluetooth MAC

That causes HA device-registry matching against existing Shelly / ESPHome / Bluetooth devices.

Depending on which connections are known at entity-creation time, Bermuda entities may be merged into:
- the Bluetooth device
- the host integration device
- a Bermuda-created proxy device

This is why behavior looks random between device types and reload order.

### 4. Bermuda creates scanner entities before identity is fully stabilized

Scanner entity creation is triggered from scanner-discovery callbacks, while scanner metadata resolution is still evolving.

Even if entity creation is later retried, the first device-registry association may already be wrong.

## Goal State

For every Bermuda scanner/proxy:
- Bermuda owns exactly one canonical scanner device in HA
- all Bermuda scanner entities always attach to that device
- host devices remain separate and are linked via `via_device`, not merged by shared `connections`
- scanner entity unique IDs never change once created
- reload order and device type do not affect the result

Expected user-facing result:
- a scanner like `Pool heater P1` always has one Bermuda proxy device page
- all Bermuda scanner entities for that proxy always appear there
- host entities from Shelly / ESPHome remain on their own host device page
- no `_2` duplicates, no wrong-device attachments

## Implementation Plan

### Phase 1: Define a canonical Bermuda scanner identity

Add a dedicated immutable scanner key on `BermudaDevice`, for example:
- `scanner_entity_key`

Rules:
- only used for scanner/proxy entities
- assigned once
- never overwritten after first assignment
- independent from mutable `unique_id`

Recommended source:
- prefer the scanner BLE identity (`address_ble_mac` or original scanner address)
- do not use Wi-Fi MAC as the canonical Bermuda scanner key

Reasoning:
- the thing Bermuda models is the BLE proxy/scanner, not the host MCU/integration device
- BLE identity is closer to what the Bluetooth integration already treats as the scanner identity
- Wi-Fi MAC should remain metadata for linking, not Bermuda ownership

### Phase 2: Introduce a dedicated scanner entity base class

Create a `BermudaScannerEntity` base class for entities that belong to the scanner itself:
- anchor X
- anchor Y
- anchor Z
- timestamp sync
- any future scanner diagnostics/config entities

This base class should provide:
- a stable scanner `unique_id` prefix from `scanner_entity_key`
- a scanner-specific `device_info`

Do not let scanner entities inherit scanner `device_info` from generic `BermudaEntity`.

### Phase 3: Make Bermuda own the proxy device explicitly

Change scanner `device_info` so Bermuda creates and owns its own scanner device entry using:
- `identifiers = {(DOMAIN, f"scanner:{scanner_entity_key}")}`

Do not include host `connections` on Bermuda scanner device_info.

This is the most important rule in the whole fix.

If `connections` include the Shelly / ESPHome / Bluetooth MACs, HA will keep merging Bermuda entities into external devices unpredictably.

Instead:
- keep scanner metadata on the Bermuda-owned proxy device
- use copied fields like name/model/manufacturer/sw_version when available
- optionally use `via_device` to link to the host integration device

### Phase 4: Represent host linkage explicitly

If a host device is known, link the Bermuda proxy device to it via `via_device`, not by shared connections.

Needed runtime fields on `BermudaDevice`:
- `scanner_host_via_identifier`
- or equivalent resolved tuple for the host device

Candidate host target:
- the non-Bluetooth source integration device (`Shelly`, `ESPHome`, `ESPresense`, etc.) when present

If no host device is known:
- the Bermuda proxy device still stands on its own

Important:
- `via_device` is only linkage metadata
- it must not change the Bermuda scanner device’s identity

### Phase 5: Standardize all scanner entity unique IDs

Convert every scanner-owned Bermuda entity to use the same base:
- `scanner:{scanner_entity_key}:anchor_x`
- `scanner:{scanner_entity_key}:anchor_y`
- `scanner:{scanner_entity_key}:anchor_z`
- `scanner:{scanner_entity_key}:timestamp_sync`

Per-device-per-scanner entities can continue using:
- tracked device key + scanner key

But the scanner part must use the same canonical `scanner_entity_key`.

Do not use:
- mutable `self._device.unique_id`
- `address_wifi_mac or address`
- ad hoc BLE/Wi-Fi fallbacks per entity class

### Phase 6: Separate scanner model identity from compatibility aliases

Keep these concepts separate:
- canonical Bermuda scanner key
- BLE MAC metadata
- Wi-Fi MAC metadata
- host integration identifiers

If needed, store legacy aliases for cleanup/migration:
- `scanner_legacy_unique_ids`

But never reuse those aliases as live entity IDs after migration.

### Phase 7: Add registry migration and cleanup

Implement explicit cleanup/migration for old scanner entities.

This must include:
- anchor X/Y/Z entities created with old mutable IDs
- timestamp sync entities created with Wi-Fi-keyed or mutable IDs
- duplicate stale registry entries left behind by prior experiments

Migration behavior:
- compute canonical scanner key for each live scanner
- compute all known legacy candidate unique IDs for that scanner
- if a legacy entity exists and canonical one does not, migrate or remove/recreate
- if both exist, remove the stale legacy one

This cleanup should run during setup before new scanner entities are added.

### Phase 8: Make scanner entity creation wait for scanner identity readiness

Add a scanner-identity readiness check before scanner entities are created.

The ready state should mean:
- canonical scanner key is assigned
- scanner `device_info` can be generated deterministically

It should not require every optional metadata field to exist.

The key point is:
- no scanner entity should be created before the canonical scanner identity is known

### Phase 9: Add explicit diagnostics for scanner identity

Add temporary debug logging around scanner identity creation:
- canonical scanner key
- BLE MAC
- Wi-Fi MAC
- chosen `via_device`
- generated scanner `device_info`
- legacy unique IDs removed

This should be easy to disable later, but it is needed during rollout.

## Test Plan

### Unit tests for scanner identity model

Add tests proving that scanner canonical identity does not change when:
- Wi-Fi MAC becomes known later
- Bluetooth device registry entry appears later
- host integration device registry entry appears later

### Entity attachment tests

Add tests that scanner-owned entities:
- attach to the Bermuda proxy device
- never attach directly to the host Shelly / ESPHome device
- all share the same `device_info.identifiers`

Test at least these combinations:
- Shelly 1PM / Plus / Pro style host device with distinct Wi-Fi and BLE MACs
- ESPHome proxy with BLE and Wi-Fi identities
- ESPresense Lite
- scanner with only Bluetooth metadata available

### Ordering tests

Simulate different resolution orders:
- scanner entity creation before host device metadata
- host device metadata before scanner entity creation
- Bluetooth metadata added late

Expected result in every case:
- same canonical Bermuda proxy device
- same entity unique IDs
- no duplicates

### Migration tests

Add tests for old registry entries:
- old mutable-ID anchor entities
- old Wi-Fi-keyed timestamp sync
- old BLE-keyed timestamp sync
- wrong-device-attached stale entries

Expected result:
- one surviving canonical entity
- attached to the canonical Bermuda proxy device

## Rollout Strategy

### Step 1

Implement the canonical scanner identity model and the new `BermudaScannerEntity` base class.

### Step 2

Move anchor X/Y/Z and timestamp sync onto that base class.

### Step 3

Add migration/cleanup for stale legacy scanner entities.

### Step 4

Verify on a real installation with mixed:
- Shelly
- ESPHome
- ESPresense

Check both:
- fresh reload
- existing registry with old entities

## Non-Goals

This fix should not:
- merge Bermuda scanner entities into host integration devices
- keep trying to “guess right” using different MACs per entity class
- special-case timestamp sync separately from the rest of the scanner model

This is a scanner identity architecture fix, not a one-sensor fix.

## Recommendation

Before more scanner-owned entities are added, Bermuda should centralize scanner identity and scanner `device_info`.

If this is not done, every new scanner entity will keep reintroducing the same class of bug:
- wrong device attachment
- duplicates
- inconsistent behavior between Shelly / ESPHome / ESPresense
