#!/usr/bin/env python3
"""
Airtomic 24-Hour Field Test Mock Data Generator
===============================================
Generates realistic 24-hour datasets matching the live Airtomic schema so
`analyze_metrics.py` can be validated and tuned immediately before hardware
field testing finishes.

Outputs (in tests/metrics/data/ by default):
  1. firebase_export_24h.json : Exact Firebase RTDB export schema
     (`nodes/<id>` + `history/<id>/<entry>` matching airtomic-dashboard/interfaces.ts)
  2. serial_events_24h.csv    : Combined Node/Gateway serial timestamp log for
     ESP-NOW RTT, E2E latency, Servo actuator latency, retries, duplicates,
     Wi-Fi/power outage events, and Store-and-Forward (E4) buffer flushes.
  3. ai_ground_truth_24h.csv  : Ground-truth room occupancy labels (`AWAKE`,
     `SLEEPING`, `ABSENT`) aligned with `(node_id, seq)` for AI evaluation.
"""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np

STATES = ("AWAKE", "SLEEPING", "ABSENT")


@dataclass(frozen=True)
class OutageWindow:
    name: str
    kind: str  # "WIFI_OUTAGE" or "POWER_CYCLE"
    start_sec: int
    duration_sec: int
    recovery_sec: float


def circadian_true_state(hour_of_day: float, node_id: str) -> str:
    """Realistic 24-hour dormitory/bedroom occupancy schedule per node."""
    shift = {"SN01": 0.0, "SN02": 0.5, "SN03": -0.5}.get(node_id, 0.0)
    h = (hour_of_day + shift) % 24.0
    if 0.0 <= h < 7.0 or h >= 23.0:
        return "SLEEPING"
    if 7.0 <= h < 9.0 or 17.5 <= h < 23.0:
        return "AWAKE"
    return "ABSENT"


def sample_predicted_state(true_state: str, rng: np.random.Generator) -> Tuple[str, float]:
    """Simulate realistic TinyML/Edge AI occupancy classifier (~94% accuracy)."""
    # Confusion profile: minor confusion between SLEEPING and ABSENT (still occupant, PIR=0)
    probs = {
        "AWAKE": (0.962, 0.028, 0.010),
        "SLEEPING": (0.025, 0.918, 0.057),
        "ABSENT": (0.008, 0.042, 0.950),
    }[true_state]
    pred = str(rng.choice(STATES, p=probs))
    if pred == true_state:
        conf = float(np.clip(rng.normal(0.92, 0.045), 0.72, 0.99))
    else:
        conf = float(np.clip(rng.normal(0.68, 0.09), 0.50, 0.88))
    return pred, round(conf, 2)


def synthesize_sensors(
    true_state: str,
    ac_on: bool,
    light_on: bool,
    node_id: str,
    rng: np.random.Generator,
) -> Dict:
    offset = {"SN01": 0.0, "SN02": 0.7, "SN03": -0.5}.get(node_id, 0.0)
    if true_state == "AWAKE":
        temp = float(np.clip(rng.normal(25.3 + offset, 0.45), 22.0, 29.0))
        hum = float(np.clip(rng.normal(56.0, 3.0), 40.0, 75.0))
        dist = float(rng.uniform(35.0, 120.0) if rng.random() < 0.35 else rng.uniform(155.0, 220.0))
        lux = float(np.clip(rng.normal(340.0, 45.0), 180.0, 520.0)) if light_on else float(np.clip(rng.normal(14.0, 5.0), 1.0, 40.0))
        pir = 1 if rng.random() < 0.88 else 0
        rssi = int(np.clip(rng.normal(-56, 4.0), -72, -42))
    elif true_state == "SLEEPING":
        temp = float(np.clip(rng.normal(24.0 + offset, 0.35), 21.5, 27.5))
        hum = float(np.clip(rng.normal(52.0, 2.5), 40.0, 68.0))
        dist = float(np.clip(rng.normal(218.0, 2.5), 205.0, 230.0))
        lux = float(np.clip(rng.normal(6.0, 3.0), 0.0, 20.0)) if not light_on else float(np.clip(rng.normal(150.0, 20.0), 80.0, 240.0))
        pir = 1 if rng.random() < 0.08 else 0
        rssi = int(np.clip(rng.normal(-60, 3.5), -74, -46))
    else:  # ABSENT
        temp = float(np.clip(rng.normal(22.6 + offset, 0.40), 20.0, 27.0)) if ac_on else float(np.clip(rng.normal(28.2 + offset, 0.6), 25.0, 33.0))
        hum = float(np.clip(rng.normal(48.0, 2.5), 38.0, 65.0))
        dist = float(np.clip(rng.normal(220.0, 1.8), 212.0, 228.0))
        lux = float(np.clip(rng.normal(290.0, 35.0), 180.0, 420.0)) if light_on else float(np.clip(rng.normal(3.0, 2.0), 0.0, 15.0))
        pir = 0
        rssi = int(np.clip(rng.normal(-92, 4.5), -105, -80))

    return {
        "temp": round(temp, 1),
        "hum": round(hum, 1),
        "vib": 1 if ac_on else 0,
        "dist": round(dist, 1),
        "lux": round(lux, 0),
        "pir": pir,
        "rssi": rssi,
    }


def generate_dataset(out_dir: Path, hours: float, period_sec: int, seed: int) -> None:
    rng = np.random.default_rng(seed)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Start timestamp: 2026-11-22 00:00:00 +07:00 (Unix ms)
    base_ts_ms = 1795280400000
    total_sec = int(hours * 3600)
    nodes = ["SN01", "SN02", "SN03"]

    # Per-node radio profiles (SN01 physical closest; SN02/SN03 simulated slightly noisier)
    node_profiles = {
        "SN01": {"base_loss": 0.005, "retry_p": 0.032, "dup_p": 0.009, "rtt_mu": 11.5, "fb_mu": 265.0},
        "SN02": {"base_loss": 0.009, "retry_p": 0.045, "dup_p": 0.011, "rtt_mu": 14.0, "fb_mu": 285.0},
        "SN03": {"base_loss": 0.012, "retry_p": 0.055, "dup_p": 0.012, "rtt_mu": 16.5, "fb_mu": 298.0},
    }

    # Realistic field-test outage windows scaled to the requested test duration
    outages: List[OutageWindow] = [
        OutageWindow("Wi-Fi Drop #1 (Router Glitched)", "WIFI_OUTAGE", int(total_sec * 0.25), 50, 9.4),
        OutageWindow("Wi-Fi Drop #2 (AP Channel Switch)", "WIFI_OUTAGE", int(total_sec * 0.58), 110, 12.8),
        OutageWindow("Gateway Power Cycle (E4/Robustness)", "POWER_CYCLE", int(total_sec * 0.83), 24, 18.6),
    ]

    firebase_export: Dict = {"nodes": {}, "history": {nid: {} for nid in nodes}}
    serial_rows: List[List] = []
    ground_truth_rows: List[List] = []

    # Record outage start/recovery markers in serial log
    for ow in outages:
        start_ms = base_ts_ms + ow.start_sec * 1000
        end_ms = base_ts_ms + (ow.start_sec + ow.duration_sec) * 1000
        recover_ms = int(end_ms + ow.recovery_sec * 1000)
        drop_evt = "WIFI_DROP" if ow.kind == "WIFI_OUTAGE" else "POWER_LOSS"
        rec_evt = "WIFI_RECOVER" if ow.kind == "WIFI_OUTAGE" else "BOOT_RECOVER"
        serial_rows.append([start_ms, "GW01", 0, start_ms, "", "", "", 0, 0, 0, "", "", "", 0, drop_evt])
        serial_rows.append([recover_ms, "GW01", 0, end_ms, "", recover_ms, "", 0, 1, 0, round(ow.recovery_sec * 1000.0, 2), "", "", 1, rec_evt])

    # Store-and-forward queue depth (matches airtomic/src/gateway/main.cpp kCloudQueueDepth = 32)
    queue_depth = 32
    buffer_queue: List[Tuple[str, Dict, int, int, int, float]] = []
    seq_counters = {nid: 0 for nid in nodes}
    entry_counter = 0

    def active_outage(sec: int) -> OutageWindow | None:
        for ow in outages:
            if ow.start_sec <= sec < (ow.start_sec + ow.duration_sec):
                return ow
        return None

    was_offline = False
    for step_idx, sec in enumerate(range(0, total_sec, period_sec)):
        hour_of_day = (sec / 3600.0) % 24.0
        ow = active_outage(sec)

        # Flush Store-and-Forward buffer in FIFO order immediately when Wi-Fi recovers
        # (before new online readings in this step, matching Gateway's FreeRTOS FIFO cloudQueue)
        if was_offline and ow is None and buffer_queue:
            flush_base_ms = base_ts_ms + sec * 1000 - len(buffer_queue) * 180
            for q_idx, (nid, rec, orig_sent_ms, orig_gw_ms, attempts, rtt_ms) in enumerate(buffer_queue):
                entry_counter += 1
                fb_write_ms = flush_base_ms + q_idx * 180
                rec["ts"] = fb_write_ms
                entry_id = f"-MockHist{entry_counter:07d}"
                firebase_export["history"][nid][entry_id] = rec
                firebase_export["nodes"][nid] = rec
                serial_rows.append([
                    fb_write_ms, nid, rec["seq"], orig_sent_ms, orig_gw_ms, fb_write_ms, "",
                    attempts, 1, 0, round(float(fb_write_ms - orig_sent_ms), 2), round(rtt_ms, 2), "", 1, "QUEUE_FLUSH"
                ])
            buffer_queue.clear()
        was_offline = ow is not None

        for n_idx, nid in enumerate(nodes):
            prof = node_profiles[nid]
            seq_counters[nid] += 1
            seq = seq_counters[nid]
            sent_ms = base_ts_ms + sec * 1000 + n_idx * 1200

            true_state = circadian_true_state(hour_of_day, nid)
            # Simulate forgotten AC/Light during two daytime ABSENT intervals
            forgotten_episode = (
                true_state == "ABSENT" and ((9.5 <= hour_of_day < 10.2) or (14.5 <= hour_of_day < 15.0))
            )
            ac_on = (true_state in ("AWAKE", "SLEEPING")) or forgotten_episode
            light_on = (true_state == "AWAKE") or forgotten_episode

            pred_state, conf = sample_predicted_state(true_state, rng)
            sensors = synthesize_sensors(true_state, ac_on, light_on, nid, rng)

            # During a Gateway power cycle, Gateway is dead -> Node exhausts 3 retries (4 attempts) and gets no ACK
            if ow is not None and ow.kind == "POWER_CYCLE":
                serial_rows.append([
                    sent_ms, nid, seq, sent_ms, "", "", "", 4, 0, 0, "", "", "", 0, "PACKET_LOSS"
                ])
                continue

            # Normal RF packet loss (even after 3 retries)
            if rng.random() < prof["base_loss"]:
                serial_rows.append([
                    sent_ms, nid, seq, sent_ms, "", "", "", 4, 0, 0, "", "", "", 1, "PACKET_LOSS"
                ])
                continue

            # Retry model (E4: 1..4 attempts; each retry adds ~200 ms ACK timeout)
            attempts = 1
            if rng.random() < prof["retry_p"]:
                attempts = int(rng.choice([2, 3, 4], p=[0.65, 0.25, 0.10]))
            base_rtt = float(np.clip(rng.normal(prof["rtt_mu"], 3.5), 5.0, 35.0))
            espnow_rtt_ms = base_rtt + (attempts - 1) * float(rng.uniform(195.0, 215.0))
            gw_rx_ms = int(sent_ms + espnow_rtt_ms * 0.5)

            record = {
                "gw": "GW01",
                "node_id": nid,
                "seq": seq,
                **sensors,
                "state": pred_state,
                "conf": conf,
                "ac": "ON" if ac_on else "OFF",
                "light": "ON" if light_on else "OFF",
                "ts": sent_ms,  # updated upon Firebase write
            }

            # Optional duplicate packet event (E4: node resent same seq, Gateway ACKed & deduplicated)
            if rng.random() < prof["dup_p"]:
                dup_ts = gw_rx_ms + int(rng.uniform(120, 420))
                serial_rows.append([
                    dup_ts, nid, seq, sent_ms, dup_ts, "", "", 1, 1, 1, "", round(base_rtt, 2), "", 1 if ow is None else 0, "DUPLICATE_DROPPED"
                ])

            # During Wi-Fi outage, Gateway still receives ESP-NOW and buffers up to 32 packets in RAM (E4 Store-and-Forward)
            if ow is not None and ow.kind == "WIFI_OUTAGE":
                if len(buffer_queue) < queue_depth:
                    buffer_queue.append((nid, record, sent_ms, gw_rx_ms, attempts, espnow_rtt_ms))
                    ground_truth_rows.append([sent_ms, nid, seq, true_state])
                else:
                    # Queue full -> Gateway refuses ACK (matches airtomic/src/gateway/main.cpp line 110)
                    serial_rows.append([
                        sent_ms, nid, seq, sent_ms, "", "", "", 4, 0, 0, "", "", "", 0, "QUEUE_FULL_DROP"
                    ])
                continue

            # Online upload to Firebase RTDB (HTTPS TLS)
            fb_upload_ms = float(np.clip(rng.lognormal(np.log(prof["fb_mu"]), 0.28), 110.0, 1450.0))
            e2e_ms = espnow_rtt_ms + fb_upload_ms
            fb_ack_ms = int(sent_ms + e2e_ms)
            record["ts"] = fb_ack_ms

            entry_counter += 1
            entry_id = f"-MockHist{entry_counter:07d}"
            firebase_export["history"][nid][entry_id] = record
            firebase_export["nodes"][nid] = record
            ground_truth_rows.append([fb_ack_ms, nid, seq, true_state])

            # Simulate Servo actuator trigger latency when forgotten appliance detected in ABSENT state
            actuator_ts = ""
            actuator_lat = ""
            if forgotten_episode and pred_state == "ABSENT" and (step_idx % 30 == 0):
                servo_lat_ms = float(np.clip(rng.normal(640.0, 95.0), 380.0, 1150.0))
                actuator_ts = int(sent_ms + servo_lat_ms)
                actuator_lat = round(servo_lat_ms, 2)

            serial_rows.append([
                fb_ack_ms, nid, seq, sent_ms, gw_rx_ms, fb_ack_ms, actuator_ts,
                attempts, 1, 0, round(e2e_ms, 2), round(espnow_rtt_ms, 2), actuator_lat, 1, "TELEMETRY"
            ])

    # Sort serial log chronologically
    serial_rows.sort(key=lambda r: r[0])

    fb_path = out_dir / "firebase_export_24h.json"
    serial_path = out_dir / "serial_events_24h.csv"
    gt_path = out_dir / "ai_ground_truth_24h.csv"

    fb_path.write_text(json.dumps(firebase_export, indent=2), encoding="utf-8")

    with serial_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "event_ts_ms", "node_id", "seq", "sent_ts_ms", "gw_rx_ts_ms", "fb_ack_ts_ms",
            "actuator_ts_ms", "attempts", "acked", "is_duplicate", "e2e_latency_ms",
            "espnow_rtt_ms", "actuator_latency_ms", "gw_online", "event_type",
        ])
        writer.writerows(serial_rows)

    with gt_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["ts_ms", "node_id", "seq", "true_state"])
        writer.writerows(ground_truth_rows)

    total_hist = sum(len(v) for v in firebase_export["history"].values())
    print(f"Generated 24h mock dataset (hours={hours}, period={period_sec}s, seed={seed}):")
    print(f"  - Firebase JSON : {fb_path} ({total_hist:,} history records across {len(nodes)} nodes)")
    print(f"  - Serial CSV    : {serial_path} ({len(serial_rows):,} events)")
    print(f"  - Ground Truth  : {gt_path} ({len(ground_truth_rows):,} labeled samples)")


def parse_args() -> argparse.Namespace:
    default_dir = Path(__file__).resolve().parent / "data"
    parser = argparse.ArgumentParser(description="Generate realistic 24-hour mock data for Airtomic QA metrics.")
    parser.add_argument("--out-dir", type=Path, default=default_dir, help="Output directory for generated mock files")
    parser.add_argument("--hours", type=float, default=24.0, help="Duration in hours (default: 24.0)")
    parser.add_argument("--period-sec", type=int, default=10, help="Telemetry period in seconds (default: 10)")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducible outputs")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    generate_dataset(args.out_dir, args.hours, args.period_sec, args.seed)
