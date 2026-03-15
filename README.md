# BLE Trilateration

**BLE Trilateration** is a [Home Assistant](https://home-assistant.io/) custom integration that tracks the physical location of Bluetooth Low Energy (BLE) devices inside your home using a network of BLE scanner anchors and a topology-aware trilateration engine.

> **Forked from [Bermuda BLE Trilateration](https://github.com/agittins/bermuda) by [@agittins](https://github.com/agittins).** Full credit to the original author for the foundational integration design. This fork continues the project as an independent effort with significant changes to the estimation pipeline, floor inference, and configuration model.

---

## What it does

BLE Trilateration receives RSSI signal-strength readings from a set of fixed BLE scanner anchors (ESPHome bluetooth proxies, Shelly Gen2+ devices, or USB bluetooth adapters) and computes a 3D Cartesian position estimate for each tracked device. From that position it infers the device's room (Home Assistant Area) and floor.

The core algorithm is a **topology-gated trilateration pipeline**:

1. **3D geometry solve** — a weighted least-squares trilateration using all visible anchors, producing an `(x, y, z)` estimate along with a geometry-quality score.
2. **Calibration fingerprinting** — stored RSSI signatures from known room positions are matched against live readings to produce a room probability vector.
3. **Floor reachability gate** — before a floor change is accepted, the system asks whether the device could physically have reached that floor given its recent position history and the locations of configured transition zones (stairs, lifts). Physically impossible floor changes are blocked before they can corrupt room inference.
4. **Floor evidence fusion** — fingerprint evidence, RSSI floor evidence, and geometry-derived Z hints are combined among only the reachable floors.
5. **Room inference** — final room assignment is made within the confirmed floor.
6. **Hysteresis** — stability smoothing is applied at legitimate room boundaries, not as a first line of defense against impossible teleportation.

The key design principle is that **topology and physical reachability are first-class constraints**, not post-hoc vetoes. A device that was stable in a room on one floor cannot appear on another floor two seconds later unless it passed through a configured transition zone.

### What you get

- `sensor` entities for Area (room) and Distance for each tracked device
- `device_tracker` entities linkable to Home Assistant Persons for Home/Away tracking
- Supports iBeacon devices including Android phones with randomised MAC addresses running the HA Companion App
- Supports IRK (resolvable keys) via the [Private BLE Device](https://www.home-assistant.io/integrations/private_ble_device/) core integration
- Multi-floor tracking with topology-gated floor inference

---

## What you need

- **Home Assistant** — a recent release
- **BLE scanner anchors** — one or more of:
  - ESPHome devices with the `bluetooth_proxy` component enabled
  - Shelly Plus (Gen2 or later) devices with Bluetooth proxying enabled
  - A USB Bluetooth adapter on the HA host (limited — no packet timestamps, suitable only for Home/Away and coarse area detection)
- **BLE devices to track** — phones, smart watches, beacon tiles, thermometers, etc.
- **At least three anchors** for meaningful 2D trilateration; more anchors and vertical spread improve 3D and multi-floor accuracy.

---

## Installation

Install via HACS by adding this repository as a custom repository, then search for **BLE Trilateration**.

Alternatively, copy the `custom_components/bermuda/` directory into your HA `custom_components/` folder and restart Home Assistant.

After installation, add the integration in **Settings → Devices & Services → Add Integration** and search for **BLE Trilateration**.

---

## Setup Guide

Setup is done in stages. Each stage builds on the previous one. You can stop after any stage and the integration will still provide value with whatever is configured.

### Stage 0: Choose a Coordinate Origin

BLE Trilateration works in a **Cartesian coordinate system** that you define. Before placing anchors you need to pick an origin point — a fixed reference location in your home that will be `(0, 0, 0)`.

Good choices:
- A corner of the house at ground/street level
- The centre of a room on the main floor

The coordinate system convention is:
- `x` — horizontal, positive toward one side of the house (e.g. East)
- `y` — horizontal, positive toward the other side (e.g. North)
- `z` — vertical, positive upward; the origin floor surface should be at `z = 0`

All anchor positions, calibration samples, and transition zones must use the same coordinate system.

---

### Stage 1: Add BLE Scanner Anchors

Each scanner anchor needs a **3D position** measured from your chosen origin.

In **Settings → Devices & Services → BLE Trilateration → Configure → Scanner Anchors**, for each anchor:
- Enter its `x`, `y`, and `z` coordinates in metres
- Give it a human-readable name

> **Tips:**
> - Measure anchor positions as accurately as you can. Errors here directly limit position accuracy.
> - Anchors at different heights (e.g. a device on the ceiling vs. one at desk height) provide Z separation that helps floor inference.
> - Aim for anchors that cover every room you want to track, with no room having fewer than two visible anchors.

---

### Stage 2: Add Tracked Devices

In **Configure → Select Devices**, choose the BLE devices you want to track. The list shows all currently visible devices. Selecting a device creates sensor and device_tracker entities for it.

Devices can be:
- Regular BLE peripherals (by MAC address)
- iBeacon devices (by UUID)
- Private BLE devices set up via the [Private BLE Device](https://www.home-assistant.io/integrations/private_ble_device/) integration (for iOS and Android devices with rotating MACs)

---

### Stage 3: Set Floor Z Heights

For multi-floor homes, tell the integration where each floor's surface is in your coordinate system.

In **Configure → Floor Heights**, for each Home Assistant floor:
- Set `floor_z_m` — the Z coordinate of the floor surface in metres
- Optionally set a `floor_z_min_m` / `floor_z_max_m` range for floors with natural Z variation (outdoor areas, sloped entries, garages)

> **Example:** If your origin is at ground-floor level, `ground_floor` might have `floor_z_m = 0`, a first floor above might have `floor_z_m = 2.5`, and a basement `floor_z_m = -2.8`.

The integration uses these values to derive a **phone-height band** (`floor_z_m` to `floor_z_m + 1.2 m`) as a strong prior for the Z estimate during trilateration. Floors with non-overlapping bands can often be discriminated from Z alone.

---

### Stage 4: Collect Calibration Samples

Calibration samples are RSSI fingerprints recorded at known positions. They allow the classifier to distinguish rooms whose RSSI patterns differ even when trilateration geometry is ambiguous.

In **Configure → Calibration Samples → Record New Sample**:
1. Stand (or place your tracked device) at a representative position in a room
2. Choose the room/area and confirm the `(x, y, z)` position
3. Let the integration record for 60 seconds

> **Tips:**
> - Collect at least one sample per room you want to track
> - For large rooms, collect multiple samples at different positions
> - Collect samples with your home in its normal state (furniture in place, doors as usually set)
> - Samples are tied to the current anchor layout hash — adding or repositioning anchors invalidates existing samples and requires recapture

---

### Stage 5: Record Transition Zones (Multi-Floor Homes)

Transition zones tell the integration where floor changes are physically possible — typically stairs, lifts, or ramps.

Without transition zones configured for a floor pair, the integration falls back to evidence-only floor inference with no topology gate for that pair.

In **Configure → Transition Samples → Record New Zone**:
1. Stand at the entry point of the transition (e.g. the bottom of the stairs)
2. Record a capture — this records your position and current RSSI fingerprint
3. Stand at the exit point of the transition (e.g. the top of the stairs) and record a second capture
4. Assign the zone a name and specify the **floor pairs** it connects (e.g. `ground_floor → first_floor`)

> **Notes:**
> - Each end of the transition (bottom and top of stairs) should be a separate capture. Do **not** average them — the integration stores each as an independent Gaussian kernel.
> - A zone authorises specific directional floor pairs. A zone connecting `ground_floor → first_floor` does not automatically authorise `ground_floor → basement`.
> - The integration blocks floor changes that would require passing through a transition zone that the device has not been near recently.

---

## How the Floor Inference Works

The integration separates two questions before assigning a floor:

1. **Which floors are physically reachable right now?** — answered by the topology and reachability gate using transition zones and recent device motion
2. **Among the reachable floors, which one has the best evidence?** — answered by fingerprint matching, RSSI, and Z geometry

The reachability gate works as follows:
- When a new floor candidate (challenger) appears, the system freezes the device's last confident pre-challenge position
- It estimates how far the device could have moved since then, based on elapsed time and observed motion
- It checks whether the nearest transition zone covering that floor pair is within that reach
- If not, the challenger is blocked — it cannot win the evidence competition regardless of RSSI

A **background traversal tracker** continuously records when the device enters and exits each transition zone. If the device genuinely traversed a zone (entered and then exited) in the recent past, the gate is lifted and the floor change is allowed to compete on evidence normally.

This means a legitimate floor change — where the user actually walked up or down the stairs — will succeed. An impossible floor change — where the classifier was confused by multipath signals through a floor slab — will be blocked before it can cascade into wrong room inference.

---

## Developer / Diagnostic Tools

The `bermuda.dump_devices` service returns the full internal state of the integration as JSON/YAML. This is useful for:
- Inspecting raw RSSI readings per scanner
- Checking position estimates and geometry quality scores
- Debugging room classification and floor inference
- Building custom template sensors from integration data

Call it from **Developer Tools → Services** in Home Assistant. Pass an `addresses` parameter to filter output to a single device.

> Note: the internal data structure is not a stable public API. Fields may change between releases.

---

## Credits

This project is a fork of [Bermuda BLE Trilateration](https://github.com/agittins/bermuda) by [@agittins](https://github.com/agittins). The original integration introduced the concept of BLE-proxy-based room presence in Home Assistant and provided the foundational architecture this fork builds on.

The integration template was originally generated from [@oncleben31](https://github.com/oncleben31)'s [Home Assistant Custom Component Cookiecutter](https://github.com/oncleben31/cookiecutter-homeassistant-custom-component) and [@Ludeeus](https://github.com/ludeeus)'s [integration_blueprint](https://github.com/custom-components/integration_blueprint).

---

## Contributing

Contributions are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md) before submitting a pull request.
