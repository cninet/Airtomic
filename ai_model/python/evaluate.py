"""Evaluate the Airtomic Rule-Based AI across synthetic room scenarios."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Dict, List

from rule_ai import (
    AirtomicRuleEngine,
    RoomState,
    RuleAIConfig,
    VerificationStatus,
    explain_rules,
)
from simulator import SimulatedScenario, generate_scenarios


def run_scenario(scenario: SimulatedScenario) -> Dict[str, object]:
    cfg = RuleAIConfig.demo_fast() if scenario.use_demo_config else RuleAIConfig.room_default()
    engine = AirtomicRuleEngine(cfg)

    servo_turned_off_ac = False
    servo_turned_off_light = False
    alerts = 0
    auto_off_triggers = 0
    verified_off = False
    false_auto_off = 0

    steady_correct = 0
    steady_total = 0
    all_correct = 0
    all_total = 0
    confusion: Counter[tuple[str, str]] = Counter()
    trace = []

    for step in scenario.steps:
        inp = step.sensor
        # If simulated servo turned appliances off, reflect physical shutdown in sensors
        if scenario.simulate_servo_auto_off:
            eff_vib = 0 if servo_turned_off_ac else inp.vib
            eff_light_raw = 180 if (servo_turned_off_light and inp.light_raw >= 0) else inp.light_raw
            eff_lux = 12.0 if (servo_turned_off_light and inp.lux >= 0) else inp.lux
            inp = replace(inp, vib=eff_vib, light_raw=eff_light_raw, lux=eff_lux)

        dec = engine.update(inp)

        if dec.should_alert:
            alerts += 1
        if dec.should_turn_off_ac or dec.should_turn_off_light:
            auto_off_triggers += 1
            if step.expected_state != RoomState.ABSENT:
                false_auto_off += 1
            engine.notify_auto_off_triggered(
                dec.should_turn_off_ac, dec.should_turn_off_light, inp.timestamp_ms
            )
            if scenario.simulate_servo_auto_off:
                if dec.should_turn_off_ac:
                    servo_turned_off_ac = True
                if dec.should_turn_off_light:
                    servo_turned_off_light = True

        if dec.verification == VerificationStatus.VERIFIED_OFF:
            verified_off = True

        all_total += 1
        if dec.state == step.expected_state:
            all_correct += 1
        if not step.in_transition:
            steady_total += 1
            if dec.state == step.expected_state:
                steady_correct += 1
            confusion[(step.expected_state.value, dec.state.value)] += 1

        trace.append((inp.timestamp_ms / 1000.0, step.expected_state, dec))

    return {
        "name": scenario.name,
        "description": scenario.description,
        "steady_correct": steady_correct,
        "steady_total": steady_total,
        "all_correct": all_correct,
        "all_total": all_total,
        "alerts": alerts,
        "auto_off_triggers": auto_off_triggers,
        "false_auto_off": false_auto_off,
        "verified_off": verified_off,
        "confusion": confusion,
        "trace": trace,
    }


def save_plots(results: List[Dict[str, object]], out_dir: Path) -> None:
    try:
        import matplotlib.pyplot as plt
    except (ImportError, ModuleNotFoundError) as exc:
        raise RuntimeError(
            "matplotlib + pillow are required for --plot (e.g. pip install matplotlib pillow)"
        ) from exc

    out_dir.mkdir(parents=True, exist_ok=True)
    state_to_y = {RoomState.ABSENT: 0, RoomState.SLEEPING: 1, RoomState.AWAKE: 2}

    fig, axes = plt.subplots(len(results), 1, figsize=(11, 2.6 * len(results)), sharex=False)
    if len(results) == 1:
        axes = [axes]

    for ax, res in zip(axes, results):
        trace = res["trace"]
        xs = [t for t, _, _ in trace]
        y_exp = [state_to_y[exp] for _, exp, _ in trace]
        y_pred = [state_to_y[dec.state] for _, _, dec in trace]
        confs = [dec.confidence for _, _, dec in trace]

        ax.step(xs, y_exp, where="post", label="Expected", linestyle="--", linewidth=1.6, alpha=0.75)
        ax.step(xs, y_pred, where="post", label="Predicted", linewidth=1.8)
        ax.fill_between(xs, 0, [c * 2.0 for c in confs], alpha=0.12, label="Confidence (scaled)")
        ax.set_yticks([0, 1, 2])
        ax.set_yticklabels(["ABSENT", "SLEEPING", "AWAKE"])
        ax.set_ylim(-0.2, 2.3)
        acc = 100.0 * res["steady_correct"] / max(1, res["steady_total"])
        ax.set_title(f"{res['name']} (steady acc={acc:.1f}%, false_off={res['false_auto_off']})")
        ax.set_xlabel("Time (s)")
        ax.grid(alpha=0.25)
        ax.legend(loc="upper right", fontsize=8, ncol=3)

    fig.tight_layout()
    plot_path = out_dir / "simulation_timelines.png"
    fig.savefig(plot_path, dpi=150)
    plt.close(fig)
    print(f"\nSaved timeline plot to: {plot_path}")


def print_timeline(res: Dict[str, object]) -> None:
    print(f"\n=== Timeline: {res['name']} ===")
    print(f"Description: {res['description']}")
    last_key = None
    for t_s, exp, dec in res["trace"]:
        key = (
            exp.value,
            dec.state.value,
            dec.ac_on,
            dec.light_on,
            dec.should_alert,
            dec.should_turn_off_ac,
            dec.should_turn_off_light,
            dec.verification.value,
        )
        if key != last_key:
            rules_txt = ", ".join(explain_rules(dec.active_rules))
            flags = []
            if dec.should_alert:
                flags.append("ALERT")
            if dec.should_turn_off_ac:
                flags.append("OFF_AC")
            if dec.should_turn_off_light:
                flags.append("OFF_LIGHT")
            flag_str = f" [{' '.join(flags)}]" if flags else ""
            print(
                f"  t={t_s:6.0f}s | exp={exp.value:8s} pred={dec.state.value:8s} "
                f"conf={dec.confidence:.2f} ac={'ON ' if dec.ac_on else 'OFF'} "
                f"light={'ON ' if dec.light_on else 'OFF'} verif={dec.verification.value:12s}"
                f"{flag_str} | {rules_txt}"
            )
            last_key = key


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate Airtomic Rule-Based AI on synthetic scenarios.")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for sensor noise")
    parser.add_argument("--no-noise", action="store_true", help="Disable synthetic Gaussian noise")
    parser.add_argument("--monte-carlo", type=int, default=1, help="Run N noisy seeds and aggregate")
    parser.add_argument("--timeline", type=str, default="", help="Print transition timeline for scenario name (or 'all')")
    parser.add_argument("--plot", action="store_true", help="Save timeline plots into ai_model/outputs/")
    args = parser.parse_args()

    total_steady_ok = 0
    total_steady = 0
    total_all_ok = 0
    total_all = 0
    total_false_off = 0
    agg_confusion: Counter[tuple[str, str]] = Counter()
    first_run_results: List[Dict[str, object]] = []

    for run_idx in range(args.monte_carlo):
        scenarios = generate_scenarios(seed=args.seed + run_idx, add_noise=not args.no_noise)
        run_results = [run_scenario(sc) for sc in scenarios]
        if run_idx == 0:
            first_run_results = run_results
        for r in run_results:
            total_steady_ok += r["steady_correct"]
            total_steady += r["steady_total"]
            total_all_ok += r["all_correct"]
            total_all += r["all_total"]
            total_false_off += r["false_auto_off"]
            agg_confusion.update(r["confusion"])

    print(f"=== Airtomic Rule-Based AI Evaluation (runs={args.monte_carlo}, noise={not args.no_noise}) ===")
    for r in first_run_results:
        s_acc = 100.0 * r["steady_correct"] / max(1, r["steady_total"])
        a_acc = 100.0 * r["all_correct"] / max(1, r["all_total"])
        print(
            f"- {r['name']:30s} | steady={r['steady_correct']:3d}/{r['steady_total']:3d} ({s_acc:5.1f}%) "
            f"| all={a_acc:5.1f}% | alerts={r['alerts']} | auto_off={r['auto_off_triggers']} "
            f"| verified={str(r['verified_off']):5s} | false_off={r['false_auto_off']}"
        )

    overall_steady = 100.0 * total_steady_ok / max(1, total_steady)
    overall_all = 100.0 * total_all_ok / max(1, total_all)
    print("\n--- Aggregate Summary ---")
    print(f"Steady-state Accuracy : {total_steady_ok}/{total_steady} ({overall_steady:.2f}%)")
    print(f"All-samples Accuracy  : {total_all_ok}/{total_all} ({overall_all:.2f}%)")
    print(f"False Auto-Off Events : {total_false_off} (target: 0)")

    labels = [s.value for s in RoomState]
    print("\n--- Steady-State Confusion Matrix (rows=Expected, cols=Predicted) ---")
    print(f"{'':10s} " + " ".join(f"{lbl:>9s}" for lbl in labels))
    for exp in labels:
        row = " ".join(f"{agg_confusion[(exp, pred)]:9d}" for pred in labels)
        print(f"{exp:10s} {row}")

    if args.timeline:
        for r in first_run_results:
            if args.timeline == "all" or r["name"] == args.timeline:
                print_timeline(r)

    if args.plot:
        out_dir = Path(__file__).resolve().parent.parent / "outputs"
        save_plots(first_run_results, out_dir)

    return 0 if total_false_off == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
