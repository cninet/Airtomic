"""Replay real Firebase `history/<node_id>` JSON or CSV logs through AirtomicRuleAI.

Use this once your ESP32 Gateway starts logging real telemetry into Firebase
(`history/SN01` exported as JSON) to verify/tune rule thresholds and export
labeled CSVs for Phase 2 ML comparison (Decision Tree / Random Forest).
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
from typing import Any, Dict, List

from rule_ai import AirtomicRuleEngine, RuleAIConfig, SensorInput, explain_rules


def _to_float(val: Any, default: float) -> float:
    if val is None or val == "":
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _to_int(val: Any, default: int) -> int:
    if val is None or val == "":
        return default
    try:
        return int(round(float(val)))
    except (TypeError, ValueError):
        return default


def load_records(path: Path) -> List[Dict[str, Any]]:
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))

    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(raw, dict):
        # Firebase RTDB /history/<node_id> export: dict keyed by push/history ID
        records = [v for v in raw.values() if isinstance(v, dict)]
    elif isinstance(raw, list):
        records = [v for v in raw if isinstance(v, dict)]
    else:
        raise ValueError(f"Unsupported JSON root type in {path}")

    records.sort(key=lambda r: (_to_int(r.get("ts", r.get("timestamp_ms", 0)), 0), _to_int(r.get("seq", 0), 0)))
    return records


def record_to_sensor_input(rec: Dict[str, Any], force_ble: bool | None) -> SensorInput:
    ts = _to_int(rec.get("ts", rec.get("timestamp_ms", 0)), 0)
    # Firebase server timestamps are Unix ms; convert seconds to ms if needed
    if 0 < ts < 10_000_000_000:
        ts *= 1000

    rssi_raw = rec.get("rssi")
    has_rssi = rssi_raw is not None and rssi_raw != ""
    ble_enabled = force_ble if force_ble is not None else has_rssi
    rssi = _to_int(rssi_raw, -127) if has_rssi else -127

    return SensorInput(
        timestamp_ms=ts,
        temp_c=_to_float(rec.get("temp", rec.get("temp_c")), float("nan")),
        hum_pct=_to_float(rec.get("hum", rec.get("hum_pct")), float("nan")),
        vib=_to_int(rec.get("vib"), -1),
        dist_cm=_to_float(rec.get("dist", rec.get("dist_cm")), -1.0),
        pir=_to_int(rec.get("pir"), -1),
        light_raw=_to_int(rec.get("light_raw"), -1),
        lux=_to_float(rec.get("lux"), -1.0),
        ble_scanner_enabled=ble_enabled,
        rssi=rssi,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay Firebase history JSON or CSV through AirtomicRuleAI.")
    parser.add_argument("input_path", type=Path, help="Path to exported Firebase JSON or CSV log")
    parser.add_argument("--demo", action="store_true", help="Use demo_fast timing preset")
    parser.add_argument("--inverted-light", action="store_true", help="Set light_inverted=True (KY-018 inverted ADC)")
    parser.add_argument("--no-ble", action="store_true", help="Ignore RSSI field even if present")
    parser.add_argument("--ble", action="store_true", help="Force BLE scanner enabled")
    parser.add_argument("--out-csv", type=Path, default=None, help="Optional output CSV with AI decisions")
    args = parser.parse_args()

    if args.no_ble and args.ble:
        raise SystemExit("Cannot pass both --ble and --no-ble")

    force_ble = True if args.ble else (False if args.no_ble else None)
    cfg = RuleAIConfig.demo_fast() if args.demo else RuleAIConfig.room_default()
    cfg.light_inverted = args.inverted_light

    engine = AirtomicRuleEngine(cfg)
    records = load_records(args.input_path)
    if not records:
        print("No records found.")
        return 0

    rows_out = []
    counts = {"AWAKE": 0, "SLEEPING": 0, "ABSENT": 0}
    last_state = None

    for rec in records:
        inp = record_to_sensor_input(rec, force_ble)
        dec = engine.update(inp)
        counts[dec.state.value] += 1

        if dec.should_turn_off_ac or dec.should_turn_off_light:
            engine.notify_auto_off_triggered(
                dec.should_turn_off_ac, dec.should_turn_off_light, inp.timestamp_ms
            )

        if dec.state != last_state or dec.should_alert or dec.should_turn_off_ac or dec.should_turn_off_light:
            print(
                f"ts={inp.timestamp_ms} | state={dec.state.value:8s} ({dec.confidence:.2f}) "
                f"| ac={'ON' if dec.ac_on else 'OFF'} light={'ON' if dec.light_on else 'OFF'} "
                f"| rules={', '.join(explain_rules(dec.active_rules))}"
            )
            last_state = dec.state

        rows_out.append({
            "ts_ms": inp.timestamp_ms,
            "temp_c": "" if math.isnan(inp.temp_c) else inp.temp_c,
            "hum_pct": "" if math.isnan(inp.hum_pct) else inp.hum_pct,
            "vib": inp.vib,
            "dist_cm": inp.dist_cm,
            "pir": inp.pir,
            "light_raw": inp.light_raw,
            "lux": inp.lux,
            "rssi": inp.rssi,
            "pred_state": dec.state.value,
            "confidence": round(dec.confidence, 4),
            "ac": "ON" if dec.ac_on else "OFF",
            "light": "ON" if dec.light_on else "OFF",
            "wasting_energy": int(dec.wasting_energy),
            "should_alert": int(dec.should_alert),
            "should_turn_off_ac": int(dec.should_turn_off_ac),
            "should_turn_off_light": int(dec.should_turn_off_light),
            "door_baseline_cm": round(dec.door_baseline_cm, 2),
            "score_awake": round(dec.score_awake, 3),
            "score_sleeping": round(dec.score_sleeping, 3),
            "score_absent": round(dec.score_absent, 3),
            "active_rules": "|".join(explain_rules(dec.active_rules)),
        })

    print(f"\nProcessed {len(records)} records -> {counts}")

    if args.out_csv:
        args.out_csv.parent.mkdir(parents=True, exist_ok=True)
        with args.out_csv.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows_out[0].keys()))
            writer.writeheader()
            writer.writerows(rows_out)
        print(f"Saved replay CSV to: {args.out_csv}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
