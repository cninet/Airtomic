"""Cross-language parity test: verifies Python and C++ Rule AI engines match identically."""

from __future__ import annotations

import csv
import math
from pathlib import Path
import subprocess
import sys
import tempfile

from rule_ai import AirtomicRuleEngine, RuleAIConfig
from simulator import generate_scenarios


def main() -> int:
    ai_root = Path(__file__).resolve().parent.parent
    cpp_dir = ai_root / "cpp"
    cpp_src = cpp_dir / "AirtomicRuleAI.cpp"
    cpp_test = cpp_dir / "test_rule_ai.cpp"

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        exe_path = tmp / ("test_rule_ai.exe" if sys.platform.startswith("win") else "test_rule_ai")
        compile_cmd = [
            "g++",
            "-std=c++17",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            str(cpp_src),
            str(cpp_test),
            "-o",
            str(exe_path),
        ]
        subprocess.run(compile_cmd, check=True)

        scenarios = generate_scenarios(seed=123, add_noise=True)
        total_steps = 0

        for sc in scenarios:
            cfg = RuleAIConfig.demo_fast() if sc.use_demo_config else RuleAIConfig.room_default()
            py_engine = AirtomicRuleEngine(cfg)

            csv_in = tmp / f"{sc.name}.csv"
            py_rows = []
            with csv_in.open("w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "timestamp_ms", "temp_c", "hum_pct", "vib", "dist_cm", "pir",
                    "light_raw", "lux", "ble_enabled", "rssi", "notify_ac", "notify_light"
                ])
                for step in sc.steps:
                    s = step.sensor
                    dec = py_engine.update(s)
                    notify_ac = 1 if dec.should_turn_off_ac else 0
                    notify_light = 1 if dec.should_turn_off_light else 0
                    if notify_ac or notify_light:
                        py_engine.notify_auto_off_triggered(
                            bool(notify_ac), bool(notify_light), s.timestamp_ms
                        )
                    writer.writerow([
                        s.timestamp_ms,
                        "nan" if math.isnan(s.temp_c) else f"{s.temp_c:.6f}",
                        "nan" if math.isnan(s.hum_pct) else f"{s.hum_pct:.6f}",
                        s.vib,
                        f"{s.dist_cm:.6f}",
                        s.pir,
                        s.light_raw,
                        f"{s.lux:.6f}",
                        1 if s.ble_scanner_enabled else 0,
                        s.rssi,
                        notify_ac,
                        notify_light,
                    ])
                    py_rows.append(dec)

            args = [str(exe_path), "--csv", str(csv_in)]
            if sc.use_demo_config:
                args.append("--demo")
            proc = subprocess.run(args, check=True, capture_output=True, text=True)
            cpp_lines = list(csv.DictReader(proc.stdout.splitlines()))
            assert len(cpp_lines) == len(py_rows), f"{sc.name}: length mismatch"

            for idx, (py_dec, cpp_row) in enumerate(zip(py_rows, cpp_lines)):
                assert py_dec.state.value == cpp_row["state"], f"{sc.name}[{idx}] state"
                assert ("ON" if py_dec.ac_on else "OFF") == cpp_row["ac"], f"{sc.name}[{idx}] ac"
                assert ("ON" if py_dec.light_on else "OFF") == cpp_row["light"], f"{sc.name}[{idx}] light"
                assert int(py_dec.wasting_energy) == int(cpp_row["wasting_energy"]), f"{sc.name}[{idx}] waste"
                assert int(py_dec.should_alert) == int(cpp_row["should_alert"]), f"{sc.name}[{idx}] alert"
                assert int(py_dec.should_turn_off_ac) == int(cpp_row["should_turn_off_ac"]), f"{sc.name}[{idx}] off_ac"
                assert int(py_dec.should_turn_off_light) == int(cpp_row["should_turn_off_light"]), f"{sc.name}[{idx}] off_light"
                assert int(py_dec.door_open_too_long) == int(cpp_row["door_open_too_long"]), f"{sc.name}[{idx}] ajar"
                assert py_dec.verification.value == cpp_row["verification"], f"{sc.name}[{idx}] verif"
                assert py_dec.active_rules == int(cpp_row["active_rules"]), f"{sc.name}[{idx}] rules"
                assert abs(py_dec.confidence - float(cpp_row["confidence"])) < 1e-4, (
                    f"{sc.name}[{idx}] conf {py_dec.confidence} vs {cpp_row['confidence']}"
                )
                total_steps += 1

        print(f"Parity check PASSED: 100% match across {len(scenarios)} scenarios ({total_steps} samples)!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
