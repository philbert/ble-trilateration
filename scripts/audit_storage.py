#!/usr/bin/env python3
# ruff: noqa: T201, D103, D213, EXE001, ARG005
"""Offline audit of Bermuda trilateration storage files.

Drives the real calibration-manager classification code against copies of the
Home Assistant storage files, and reports:

- the canonical layout hash and which stored hashes are alias churn vs real change
- effective active ordinary samples, transition samples, and bootstrap records
  by canonical layout identity
- corrupted / repairable / stale sample classifications
- scanner anchor Z vs configured floor surface Z bands

Usage:
    python scripts/audit_storage.py [storage_dir]

storage_dir must contain Home Assistant storage wrapper JSON files named
calibration_samples, scanner_anchors, floor_config, trilat_bootstrap.
Defaults to the repository root.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from custom_components.ble_trilateration.calibration import BermudaCalibrationManager  # noqa: E402


def load_storage(path: Path) -> dict:
    with path.open() as handle:
        return json.load(handle)["data"]


class _FakeStore:
    def __init__(self, data: dict) -> None:
        self._data = data

    @property
    def samples(self):
        return json.loads(json.dumps(self._data.get("samples", [])))

    @property
    def transition_samples(self):
        return json.loads(json.dumps(self._data.get("transition_samples", [])))

    @property
    def sample_count(self):
        return len(self._data.get("samples", []))

    @property
    def acknowledged_layout_hashes(self):
        return list(self._data.get("acknowledged_layout_hashes", []))

    async def async_ensure_loaded(self):
        return None


def build_manager(storage_dir: Path) -> tuple[BermudaCalibrationManager, dict, dict, dict]:
    cal_data = load_storage(storage_dir / "calibration_samples")
    anchors_data = load_storage(storage_dir / "scanner_anchors")
    floors_data = load_storage(storage_dir / "floor_config")
    bootstrap_data = load_storage(storage_dir / "trilat_bootstrap")

    scanners = anchors_data.get("scanners", {})
    devices: dict[str, SimpleNamespace] = {}
    for storage_key, payload in scanners.items():
        coords = payload.get("coordinates", {})
        devices[storage_key] = SimpleNamespace(
            address=storage_key,
            address_ble_mac=None,
            address_wifi_mac=None,
            unique_id=storage_key,
            name=payload.get("name", storage_key),
            anchor_x_m=coords.get("anchor_x_m"),
            anchor_y_m=coords.get("anchor_y_m"),
            anchor_z_m=coords.get("anchor_z_m"),
            is_scanner=True,
        )

    bootstrap_records = {
        address: SimpleNamespace(**record)
        for address, record in bootstrap_data.get("devices", {}).items()
    }

    coordinator = SimpleNamespace(
        scanner_anchor_store=SimpleNamespace(scanners=scanners),
        scanner_list=set(scanners),
        devices=devices,
        trilat_bootstrap_store=SimpleNamespace(records=bootstrap_records),
        ar=SimpleNamespace(async_get_area=lambda area_id: None),
        fr=SimpleNamespace(async_get_floor=lambda floor_id: None),
    )

    manager = BermudaCalibrationManager(None, coordinator, _FakeStore(cal_data))
    manager.refresh_layout_identity()
    return manager, scanners, floors_data, bootstrap_data


def audit_floor_z(scanners: dict, floors_data: dict) -> list[dict]:
    floors = {
        floor_id: cfg.get("floor_z_m")
        for floor_id, cfg in (floors_data.get("floors") or {}).items()
        if cfg.get("floor_z_m") is not None
    }
    ordered = sorted(floors.items(), key=lambda row: row[1])
    rows = []
    for storage_key, payload in sorted(scanners.items()):
        anchor_z = (payload.get("coordinates") or {}).get("anchor_z_m")
        nearest_floor = None
        band = None
        if anchor_z is not None and ordered:
            for index, (floor_id, floor_z) in enumerate(ordered):
                next_z = ordered[index + 1][1] if index + 1 < len(ordered) else floor_z + 3.5
                if floor_z <= anchor_z < next_z + 0.5:
                    nearest_floor = floor_id
                    band = (floor_z, next_z + 0.5)
            if nearest_floor is None:
                nearest_floor = ordered[0][0] if anchor_z < ordered[0][1] else ordered[-1][0]
        rows.append(
            {
                "scanner": payload.get("name", storage_key),
                "storage_key": storage_key,
                "anchor_z_m": anchor_z,
                "nearest_floor_by_z": nearest_floor,
                "z_band": band,
            }
        )
    return rows


def main() -> int:
    storage_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO_ROOT
    manager, scanners, floors_data, bootstrap_data = build_manager(storage_dir)

    current_hash = manager.current_anchor_layout_hash
    equivalent = manager.equivalent_layout_hashes()
    summary = manager.classify_stored_samples()

    print("=== Layout identity ===")
    print(f"canonical layout hash : {current_hash}")
    print(f"equivalent hashes     : {sorted(equivalent)}")
    print(f"geometry complete     : {summary['current_geometry_complete']}")
    if summary["incomplete_anchor_scanners"]:
        print(f"incomplete anchors    : {summary['incomplete_anchor_scanners']}")

    print("\n=== Ordinary samples ===")
    for classification, count in sorted(summary["samples"].items()):
        print(f"{classification:32s}: {count}")

    print("\n=== Transition samples ===")
    for classification, count in sorted(summary["transition_samples"].items()):
        print(f"{classification:32s}: {count}")

    print("\n=== Bootstrap records ===")
    for bucket, count in sorted(summary.get("bootstrap_records", {}).items()):
        print(f"{bucket:32s}: {count}")
    for address, record in (bootstrap_data.get("devices") or {}).items():
        layout_hash = record.get("layout_hash", "")
        status = "current" if layout_hash in equivalent else "stale_layout"
        print(
            f"  {address[:12]:<12s} {status:12s} quality={record.get('geometry_quality_01')} "
            f"floor_conf={record.get('floor_confidence')} saved={record.get('saved_at')}"
        )

    print("\n=== Scanner anchor Z vs floor config ===")
    for row in audit_floor_z(scanners, floors_data):
        print(
            f"  {row['scanner']:<42s} z={row['anchor_z_m']!s:<6s} "
            f"nearest_floor_by_z={row['nearest_floor_by_z']}"
        )
    print(
        "\nNote: HA floor assignments are validated at runtime via diagnostics; this "
        "offline view only checks anchor Z against floor_config bands."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
