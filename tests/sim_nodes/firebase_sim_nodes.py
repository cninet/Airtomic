#!/usr/bin/env python3
"""
Airtomic Firebase RTDB Direct Simulator for SN02 & SN03
=======================================================
Directly publishes simulated SN02 and SN03 telemetry to Firebase Realtime Database
using the exact schema defined in `airtomic-dashboard/interfaces.ts` and security
rules in `firebase/database.rules.json` (`nodes/<node_id>`, `history/<node_id>`,
and polling `cmd/<node_id>`).

Use this to test the Next.js Dashboard multi-node selector, alerts, and remote
AC/Light shutoff buttons without needing extra physical ESP32 boards.

Security (E5 / M8):
  All credentials are loaded strictly from environment variables or `tests/sim_nodes/.env`.
  Never hardcode passwords or API keys in source code.

Examples:
  # Safe dry-run (no Firebase credentials required):
  python3 firebase_sim_nodes.py --dry-run --count 6

  # Live run against Firebase RTDB (after copying .env.example -> .env):
  python3 firebase_sim_nodes.py --nodes SN02 SN03 --interval 5 --log-csv ../metrics/data/live_sim_log.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

try:
    import requests
except ImportError:
    requests = None  # Handled gracefully in --dry-run mode

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

SCENARIOS = ("AWAKE", "SLEEPING", "ABSENT")


@dataclass
class VirtualNode:
    node_id: str
    scenario: str
    auto_cycle: bool = True
    seq: int = 0
    ac: str = "ON"
    light: str = "ON"
    last_payload: Optional[Dict] = None
    last_switch_ts: float = field(default_factory=time.time)

    def maybe_rotate_scenario(self, cycle_sec: float) -> None:
        if not self.auto_cycle:
            return
        now = time.time()
        if now - self.last_switch_ts >= cycle_sec:
            idx = (SCENARIOS.index(self.scenario) + 1) % len(SCENARIOS)
            self.scenario = SCENARIOS[idx]
            self.last_switch_ts = now
            # Reset appliances ON when entering AWAKE so forgotten-ON can be tested in ABSENT
            if self.scenario == "AWAKE":
                self.ac = "ON"
                self.light = "ON"

    def build_telemetry(self, gw_id: str, force_nan: bool = False) -> Dict:
        self.seq += 1
        offset = 0.8 if self.node_id == "SN03" else 0.0

        if self.scenario == "AWAKE":
            temp = round(random.uniform(24.5, 26.5) + offset, 1)
            hum = round(random.uniform(52.0, 62.0), 1)
            dist = round(random.uniform(40.0, 140.0), 1)
            lux = round(random.uniform(260.0, 480.0), 0) if self.light == "ON" else round(random.uniform(5.0, 25.0), 0)
            pir = 1 if random.random() < 0.85 else 0
            rssi = random.randint(-62, -48)
            conf = round(random.uniform(0.90, 0.98), 2)
        elif self.scenario == "SLEEPING":
            temp = round(random.uniform(23.2, 24.8) + offset, 1)
            hum = round(random.uniform(48.0, 56.0), 1)
            dist = round(random.uniform(210.0, 224.0), 1)
            lux = round(random.uniform(2.0, 18.0), 0) if self.light == "OFF" else round(random.uniform(120.0, 220.0), 0)
            pir = 0 if random.random() < 0.92 else 1
            rssi = random.randint(-66, -54)
            conf = round(random.uniform(0.84, 0.95), 2)
        else:  # ABSENT
            temp = round(random.uniform(21.8, 23.5) + offset, 1)
            hum = round(random.uniform(44.0, 52.0), 1)
            dist = round(random.uniform(218.0, 226.0), 1)
            lux = round(random.uniform(240.0, 410.0), 0) if self.light == "ON" else round(random.uniform(1.0, 8.0), 0)
            pir = 0
            rssi = random.randint(-98, -86)
            conf = round(random.uniform(0.89, 0.98), 2)

        payload = {
            "gw": gw_id,
            "node_id": self.node_id,
            "seq": self.seq,
            "temp": temp,
            "hum": hum,
            "vib": 1 if self.ac == "ON" else 0,
            "dist": dist if not force_nan else -1.0,
            "lux": lux,
            "pir": pir,
            "rssi": rssi,
            "state": self.scenario,
            "conf": conf,
            "ac": self.ac,
            "light": self.light,
            "ts": {".sv": "timestamp"},
        }
        if force_nan:
            # Mirrors airtomic/src/gateway/firebase_cloud.cpp which omits NaN temp/hum
            payload.pop("temp", None)
            payload.pop("hum", None)
        self.last_payload = payload
        return payload


class FirebaseGatewayClient:
    def __init__(self, db_url: str, api_key: str, email: str, password: str) -> None:
        if requests is None:
            raise RuntimeError("Missing dependency 'requests'. Install requirements.txt first.")
        self.db_url = db_url.rstrip("/")
        self.api_key = api_key
        self.email = email
        self.password = password
        self.id_token: str = ""
        self.token_at: float = 0.0
        self.session = requests.Session()

    def ensure_token(self) -> None:
        if self.id_token and (time.time() - self.token_at) < 50 * 60:
            return
        url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={self.api_key}"
        resp = self.session.post(
            url,
            json={"email": self.email, "password": self.password, "returnSecureToken": True},
            timeout=8,
        )
        resp.raise_for_status()
        self.id_token = resp.json()["idToken"]
        self.token_at = time.time()

    def _url(self, path: str) -> str:
        return f"{self.db_url}/{path.lstrip('/')}.json?auth={self.id_token}"

    def publish(self, node_id: str, payload: Dict) -> tuple[int, int, float]:
        self.ensure_token()
        t0 = time.perf_counter()
        r_latest = self.session.put(self._url(f"nodes/{node_id}"), json=payload, timeout=8)
        r_hist = self.session.post(self._url(f"history/{node_id}"), json=payload, timeout=8)
        latency_ms = (time.perf_counter() - t0) * 1000.0
        if r_latest.status_code == 401 or r_hist.status_code == 401:
            self.id_token = ""
        return r_latest.status_code, r_hist.status_code, latency_ms

    def poll_and_apply_commands(self, node: VirtualNode) -> List[str]:
        self.ensure_token()
        resp = self.session.get(self._url(f"cmd/{node.node_id}"), timeout=8)
        if resp.status_code != 200 or resp.text.strip() == "null":
            return []
        cmds = resp.json() or {}
        executed: List[str] = []
        if cmds.get("ac") == "OFF":
            node.ac = "OFF"
            self.session.delete(self._url(f"cmd/{node.node_id}/ac"), timeout=8)
            executed.append("AC->OFF")
        if cmds.get("light") == "OFF":
            node.light = "OFF"
            self.session.delete(self._url(f"cmd/{node.node_id}/light"), timeout=8)
            executed.append("LIGHT->OFF")
        return executed


def load_config(env_path: Path) -> Dict[str, str]:
    if load_dotenv is not None and env_path.exists():
        load_dotenv(env_path)
    return {
        "FB_DB_URL": os.environ.get("FB_DB_URL", "").strip(),
        "FB_API_KEY": os.environ.get("FB_API_KEY", "").strip(),
        "FB_EMAIL": os.environ.get("FB_EMAIL", "").strip(),
        "FB_PASSWORD": os.environ.get("FB_PASSWORD", "").strip(),
        "GW_ID": os.environ.get("GW_ID", "GW01").strip() or "GW01",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Simulate Airtomic SN02 & SN03 telemetry to Firebase RTDB.")
    parser.add_argument("--nodes", nargs="+", default=["SN02", "SN03"], help="Simulated node IDs (default: SN02 SN03)")
    parser.add_argument("--interval", type=float, default=5.0, help="Seconds between full rounds (default: 5.0)")
    parser.add_argument("--cycle-sec", type=float, default=60.0, help="Seconds before rotating scenario in auto mode")
    parser.add_argument("--scenario", choices=["auto", *SCENARIOS], default="auto", help="Room scenario mode")
    parser.add_argument("--count", type=int, default=0, help="Number of rounds (0 = run forever)")
    parser.add_argument("--inject-loss", type=float, default=0.0, help="Probability [0..1] of skipping a seq (PDR test)")
    parser.add_argument("--inject-dup", type=float, default=0.0, help="Probability [0..1] of sending duplicate seq")
    parser.add_argument("--env-file", type=Path, default=Path(__file__).with_name(".env"), help="Path to .env file")
    parser.add_argument("--log-csv", type=Path, default=None, help="Optional CSV output path for latency/PDR analysis")
    parser.add_argument("--dry-run", action="store_true", help="Print JSON payloads without connecting to Firebase")
    parser.add_argument("--seed", type=int, default=None, help="Optional random seed")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.seed is not None:
        random.seed(args.seed)

    cfg = load_config(args.env_file)
    client: Optional[FirebaseGatewayClient] = None
    if not args.dry_run:
        missing = [k for k in ("FB_DB_URL", "FB_API_KEY", "FB_EMAIL", "FB_PASSWORD") if not cfg[k]]
        if missing:
            print(
                f"ERROR: Missing required env vars: {', '.join(missing)}.\n"
                f"Copy {Path(__file__).with_name('.env.example')} to {args.env_file} and fill it in, "
                f"or run with --dry-run.",
                file=sys.stderr,
            )
            return 2
        client = FirebaseGatewayClient(cfg["FB_DB_URL"], cfg["FB_API_KEY"], cfg["FB_EMAIL"], cfg["FB_PASSWORD"])

    nodes = [
        VirtualNode(
            node_id=nid,
            scenario=SCENARIOS[i % len(SCENARIOS)] if args.scenario == "auto" else args.scenario,
            auto_cycle=(args.scenario == "auto"),
        )
        for i, nid in enumerate(args.nodes)
    ]

    csv_file = None
    csv_writer = None
    if args.log_csv is not None:
        args.log_csv.parent.mkdir(parents=True, exist_ok=True)
        csv_file = args.log_csv.open("w", newline="", encoding="utf-8")
        csv_writer = csv.writer(csv_file)
        csv_writer.writerow(
            [
                "event_ts_ms", "node_id", "seq", "sent_ts_ms", "gw_rx_ts_ms", "fb_ack_ts_ms",
                "actuator_ts_ms", "attempts", "acked", "is_duplicate", "e2e_latency_ms",
                "espnow_rtt_ms", "actuator_latency_ms", "gw_online", "event_type",
            ]
        )

    round_num = 0
    try:
        while args.count == 0 or round_num < args.count:
            round_num += 1
            for node in nodes:
                node.maybe_rotate_scenario(args.cycle_sec)
                payload = node.build_telemetry(cfg["GW_ID"])
                sent_ms = int(time.time() * 1000)

                if args.inject_loss > 0 and random.random() < args.inject_loss:
                    print(f"[LOSS-SIM] {node.node_id} seq={payload['seq']} skipped (simulated packet loss)")
                    if csv_writer:
                        csv_writer.writerow([sent_ms, node.node_id, payload["seq"], sent_ms, "", "", "", 4, 0, 0, "", "", "", 1, "PACKET_LOSS"])
                    continue

                if args.dry_run:
                    print(f"[DRY-RUN] PUT /nodes/{node.node_id} -> {json.dumps(payload, ensure_ascii=False)}")
                    e2e_ms = round(random.uniform(180.0, 320.0), 2)
                    if csv_writer:
                        csv_writer.writerow([sent_ms, node.node_id, payload["seq"], sent_ms, sent_ms + 12, int(sent_ms + e2e_ms), "", 1, 1, 0, e2e_ms, 12.0, "", 1, "TELEMETRY"])
                else:
                    assert client is not None
                    s_latest, s_hist, e2e_ms = client.publish(node.node_id, payload)
                    cmds = client.poll_and_apply_commands(node)
                    cmd_note = f" | cmd={','.join(cmds)}" if cmds else ""
                    print(
                        f"[LIVE] {node.node_id} seq={payload['seq']} state={payload['state']} "
                        f"nodes={s_latest} hist={s_hist} rtt={e2e_ms:.1f}ms{cmd_note}"
                    )
                    if csv_writer:
                        acked = 1 if (s_latest == 200 and s_hist == 200) else 0
                        csv_writer.writerow([sent_ms, node.node_id, payload["seq"], sent_ms, sent_ms + 10, int(sent_ms + e2e_ms), "", 1, acked, 0, round(e2e_ms, 2), 10.0, "", 1, "TELEMETRY"])

                    # Optional duplicate packet test
                    if args.inject_dup > 0 and random.random() < args.inject_dup:
                        s_dup, _, dup_ms = client.publish(node.node_id, payload)
                        print(f"[DUP-SIM] {node.node_id} seq={payload['seq']} re-sent status={s_dup} ({dup_ms:.1f}ms)")
                        if csv_writer:
                            csv_writer.writerow([int(time.time() * 1000), node.node_id, payload["seq"], sent_ms, "", int(time.time() * 1000), "", 1, 1, 1, round(dup_ms, 2), "", "", 1, "DUPLICATE"])

            if args.count == 0 or round_num < args.count:
                time.sleep(0.05 if args.dry_run else args.interval)
    except KeyboardInterrupt:
        print("\nStopped by user.")
    finally:
        if csv_file is not None:
            csv_file.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
