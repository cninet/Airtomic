#!/usr/bin/env python3
"""
Airtomic Quantitative Metrics Analyzer & Plot Generator
=======================================================
Computes Section IV Quantitative Metrics (with repeated-trial variation:
Mean, Median, Std, Min, Max, P95), generates 300 DPI publication-ready PNG
figures, and exports copy-ready Markdown & CSV summary tables.

Supported Metrics:
  1. End-to-End Latency (ms): ESP-NOW RTT, Sensor->Gateway->Firebase, and
     Sensor->Actuator (Servo), overall & per node_id (SN01, SN02, SN03).
  2. Packet Delivery Rate (PDR %) & E4 Reliability: per-node PDR from seq gaps,
     hourly window variation (Mean/Std/Min/Max), Retry Rate, and Duplicate
     Deduplication Rate.
  3. System Uptime & Auto-Recovery (24h Field Test): Uptime %, Downtime,
     Outage Recovery Time stats, and Store-and-Forward (E4) buffer flushes.
  4. AI Model Performance (Ready for ai_model/): Accuracy, Precision, Recall,
     F1-Score, False-Absent Rate, and 3x3 Confusion Matrix (AWAKE/SLEEPING/ABSENT).

Usage:
  python3 analyze_metrics.py \
    --firebase data/firebase_export_24h.json \
    --serial data/serial_events_24h.csv \
    --ground-truth data/ai_ground_truth_24h.csv \
    --output output/
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.metrics import accuracy_score, confusion_matrix, precision_recall_fscore_support

CLASSES = ["AWAKE", "SLEEPING", "ABSENT"]

# Clean publication-ready styling
plt.rcParams.update({
    "figure.dpi": 300,
    "savefig.dpi": 300,
    "font.size": 10,
    "axes.titlesize": 12,
    "axes.titleweight": "bold",
    "axes.labelsize": 10,
    "axes.grid": True,
    "grid.alpha": 0.3,
    "grid.linestyle": "--",
    "axes.spines.top": False,
    "axes.spines.right": False,
})

NODE_COLORS = {
    "SN01": "#1b7837",  # Airtomic emerald green (physical node)
    "SN02": "#2166ac",  # Blue (simulated node 1)
    "SN03": "#d95f02",  # Orange (simulated node 2)
}


def describe_series(values: pd.Series) -> Dict[str, float]:
    clean = pd.to_numeric(values, errors="coerce").dropna()
    if clean.empty:
        return {"count": 0, "mean": np.nan, "median": np.nan, "std": np.nan, "min": np.nan, "max": np.nan, "p95": np.nan}
    return {
        "count": int(clean.count()),
        "mean": float(clean.mean()),
        "median": float(clean.median()),
        "std": float(clean.std(ddof=1)) if len(clean) > 1 else 0.0,
        "min": float(clean.min()),
        "max": float(clean.max()),
        "p95": float(np.percentile(clean, 95)),
    }


def load_firebase_history(path: Path) -> pd.DataFrame:
    raw = json.loads(path.read_text(encoding="utf-8"))
    # Support both full DB export {"history": {"SN01": {...}}} and direct history export {"SN01": {...}}
    hist_root = raw.get("history", raw) if isinstance(raw, dict) else {}
    rows: List[Dict] = []
    for node_key, entries in hist_root.items():
        if not isinstance(entries, dict):
            continue
        for entry_id, rec in entries.items():
            if not isinstance(rec, dict):
                continue
            item = dict(rec)
            item.setdefault("node_id", node_key)
            item["_entry_id"] = entry_id
            # Unify lux / light_raw seamlessly
            if "lux" not in item and "light_raw" in item:
                item["lux"] = item["light_raw"]
            rows.append(item)
    if not rows:
        raise ValueError(f"No history records found in {path}")
    df = pd.DataFrame(rows)
    df["seq"] = pd.to_numeric(df["seq"], errors="coerce")
    df["ts"] = pd.to_numeric(df["ts"], errors="coerce")
    df = df.dropna(subset=["seq", "ts"]).copy()
    df["seq"] = df["seq"].astype(int)
    df["ts"] = df["ts"].astype(np.int64)
    df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True).dt.tz_convert("Asia/Bangkok")
    df = df.sort_values(["node_id", "ts", "seq"]).reset_index(drop=True)
    return df


def load_serial_log(path: Optional[Path]) -> Optional[pd.DataFrame]:
    if path is None or not path.exists():
        return None
    # Support either standard CSV header or raw Arduino SIM_LOG lines from sim_nodes.ino
    first_line = path.read_text(encoding="utf-8", errors="ignore").splitlines()[0]
    if first_line.startswith("SIM_LOG,") or "SIM_LOG," in first_line:
        parsed = []
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if not line.startswith("SIM_LOG,"):
                continue
            parts = line.strip().split(",")
            if len(parts) < 13:
                continue
            parsed.append({
                "event_ts_ms": int(parts[1]),
                "node_id": parts[2],
                "seq": int(parts[3]),
                "attempts": int(parts[5]),
                "acked": int(parts[6]),
                "espnow_rtt_ms": float(parts[7]),
                "is_duplicate": 1 if parts[8] == "DUPLICATE_SEQ" else 0,
                "e2e_latency_ms": np.nan,
                "actuator_latency_ms": np.nan,
                "gw_online": 1,
                "event_type": "DUPLICATE_DROPPED" if parts[8] == "DUPLICATE_SEQ" else ("PACKET_LOSS" if int(parts[6]) == 0 else "TELEMETRY"),
            })
        return pd.DataFrame(parsed) if parsed else None
    df = pd.read_csv(path)
    return df


def compute_pdr_metrics(fb_df: pd.DataFrame, serial_df: Optional[pd.DataFrame]) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Compute per-node and overall PDR (%) from seq continuity + hourly rolling PDR variation."""
    summary_rows = []
    hourly_rows = []

    for node_id, grp in fb_df.groupby("node_id"):
        grp = grp.sort_values("ts")
        unique_seqs = grp["seq"].drop_duplicates()
        received_unique = int(len(unique_seqs))
        duplicates_in_db = int(len(grp) - received_unique)

        # Handle genuine ESP32 counter restarts (e.g., seq drops back near 1 after a reboot),
        # while ignoring minor out-of-order arrivals from Store-and-Forward buffer flushes.
        seq_vals = grp["seq"].to_numpy()
        segments: List[List[int]] = [[int(seq_vals[0])]]
        seg_max = int(seq_vals[0])
        for raw_s in seq_vals[1:]:
            s = int(raw_s)
            if (seg_max - s) > 50 and s <= 25:
                segments.append([s])
                seg_max = s
            else:
                segments[-1].append(s)
                if s > seg_max:
                    seg_max = s
        expected = sum((max(seg) - min(seg) + 1) for seg in segments)
        expected = max(expected, received_unique)

        missing = max(0, expected - received_unique)
        pdr_pct = (100.0 * received_unique / expected) if expected > 0 else np.nan

        retry_rate = np.nan
        dup_dropped = 0
        dedup_acc = np.nan
        if serial_df is not None and not serial_df.empty:
            s_node = serial_df[serial_df["node_id"] == node_id]
            tx_events = s_node[s_node["event_type"].isin(["TELEMETRY", "QUEUE_FLUSH", "PACKET_LOSS", "QUEUE_FULL_DROP"])]
            if not tx_events.empty and "attempts" in tx_events.columns:
                retried = (pd.to_numeric(tx_events["attempts"], errors="coerce") > 1).sum()
                retry_rate = 100.0 * float(retried) / len(tx_events)
            dup_dropped = int((s_node["event_type"] == "DUPLICATE_DROPPED").sum())
            total_dups = dup_dropped + duplicates_in_db
            if total_dups > 0:
                dedup_acc = 100.0 * dup_dropped / total_dups

        # Hourly window PDR for repeated-trial variation
        grp = grp.copy()
        grp["hour_bin"] = ((grp["ts"] - fb_df["ts"].min()) // 3600_000).astype(int)
        for h_bin, h_grp in grp.groupby("hour_bin"):
            h_seqs = h_grp["seq"].drop_duplicates()
            if len(h_seqs) < 2:
                continue
            h_exp = int(h_seqs.max() - h_seqs.min() + 1)
            if h_exp <= 0 or h_exp > len(h_seqs) * 5:
                continue
            hourly_rows.append({
                "node_id": node_id,
                "hour_bin": int(h_bin),
                "pdr_pct": min(100.0, 100.0 * len(h_seqs) / h_exp),
            })

        summary_rows.append({
            "node_id": node_id,
            "expected_packets": int(expected),
            "received_unique": received_unique,
            "missing_packets": int(missing),
            "pdr_pct": pdr_pct,
            "retry_rate_pct": retry_rate,
            "dup_dropped_by_gw": dup_dropped,
            "dup_leaked_to_db": duplicates_in_db,
            "dedup_accuracy_pct": dedup_acc,
        })

    pdr_df = pd.DataFrame(summary_rows)
    hourly_df = pd.DataFrame(hourly_rows)

    # Append OVERALL row
    tot_exp = int(pdr_df["expected_packets"].sum())
    tot_rx = int(pdr_df["received_unique"].sum())
    tot_miss = int(pdr_df["missing_packets"].sum())
    tot_dup_drop = int(pdr_df["dup_dropped_by_gw"].sum())
    tot_dup_leak = int(pdr_df["dup_leaked_to_db"].sum())
    overall = {
        "node_id": "OVERALL",
        "expected_packets": tot_exp,
        "received_unique": tot_rx,
        "missing_packets": tot_miss,
        "pdr_pct": 100.0 * tot_rx / tot_exp if tot_exp > 0 else np.nan,
        "retry_rate_pct": float(pdr_df["retry_rate_pct"].mean(skipna=True)),
        "dup_dropped_by_gw": tot_dup_drop,
        "dup_leaked_to_db": tot_dup_leak,
        "dedup_accuracy_pct": (100.0 * tot_dup_drop / (tot_dup_drop + tot_dup_leak)) if (tot_dup_drop + tot_dup_leak) > 0 else np.nan,
    }
    pdr_df = pd.concat([pdr_df, pd.DataFrame([overall])], ignore_index=True)
    return pdr_df, hourly_df


def compute_uptime_and_recovery(
    fb_df: pd.DataFrame, serial_df: Optional[pd.DataFrame], period_sec: float
) -> Tuple[Dict, pd.DataFrame]:
    t_start_ms = int(fb_df["ts"].min())
    t_end_ms = int(fb_df["ts"].max())
    total_sec = max(1.0, (t_end_ms - t_start_ms) / 1000.0)

    outages: List[Dict] = []
    buffered_flushed = 0

    if serial_df is not None and not serial_df.empty and "event_type" in serial_df.columns:
        buffered_flushed = int((serial_df["event_type"] == "QUEUE_FLUSH").sum())
        drops = serial_df[serial_df["event_type"].isin(["WIFI_DROP", "POWER_LOSS"])].sort_values("event_ts_ms")
        recovers = serial_df[serial_df["event_type"].isin(["WIFI_RECOVER", "BOOT_RECOVER"])].sort_values("event_ts_ms")

        rec_iter = list(recovers.to_dict("records"))
        used_rec = set()
        for _, drow in drops.iterrows():
            d_ts = int(drow["event_ts_ms"])
            matched = None
            for idx, rrow in enumerate(rec_iter):
                if idx in used_rec:
                    continue
                if int(rrow["event_ts_ms"]) > d_ts:
                    matched = (idx, rrow)
                    break
            if matched is not None:
                idx, rrow = matched
                used_rec.add(idx)
                r_ts = int(rrow["event_ts_ms"])
                downtime_s = (r_ts - d_ts) / 1000.0
                rec_lat_ms = pd.to_numeric(rrow.get("e2e_latency_ms", np.nan), errors="coerce")
                rec_time_s = float(rec_lat_ms) / 1000.0 if pd.notna(rec_lat_ms) else downtime_s
                outages.append({
                    "event_kind": str(drow["event_type"]),
                    "start_ts_ms": d_ts,
                    "end_ts_ms": r_ts,
                    "downtime_sec": downtime_s,
                    "recovery_sec": rec_time_s,
                })

    # Fallback: infer outage gaps directly from Firebase timestamps if no serial outage markers
    if not outages:
        all_ts = np.sort(fb_df["ts"].unique())
        gap_threshold_ms = max(35_000, int(period_sec * 3500))
        diffs = np.diff(all_ts)
        for idx in np.where(diffs > gap_threshold_ms)[0]:
            d_ts = int(all_ts[idx])
            r_ts = int(all_ts[idx + 1])
            dt_s = (r_ts - d_ts) / 1000.0
            outages.append({
                "event_kind": "INFERRED_GAP",
                "start_ts_ms": d_ts,
                "end_ts_ms": r_ts,
                "downtime_sec": dt_s,
                "recovery_sec": dt_s,
            })

    outage_df = pd.DataFrame(outages)
    total_downtime_sec = float(outage_df["downtime_sec"].sum()) if not outage_df.empty else 0.0
    uptime_pct = max(0.0, 100.0 * (1.0 - total_downtime_sec / total_sec))
    rec_stats = describe_series(outage_df["recovery_sec"]) if not outage_df.empty else describe_series(pd.Series(dtype=float))

    summary = {
        "duration_hours": total_sec / 3600.0,
        "uptime_pct": uptime_pct,
        "total_downtime_sec": total_downtime_sec,
        "outage_count": len(outage_df),
        "buffered_flushed_packets": buffered_flushed,
        "recovery_stats": rec_stats,
    }
    return summary, outage_df


def compute_ai_metrics(fb_df: pd.DataFrame, gt_path: Optional[Path]) -> Optional[Dict]:
    if gt_path is None or not gt_path.exists() or "state" not in fb_df.columns:
        return None
    gt_df = pd.read_csv(gt_path)
    if not {"node_id", "seq", "true_state"}.issubset(gt_df.columns):
        return None

    merged = pd.merge(
        fb_df[["node_id", "seq", "state"]].drop_duplicates(subset=["node_id", "seq"]),
        gt_df[["node_id", "seq", "true_state"]].drop_duplicates(subset=["node_id", "seq"]),
        on=["node_id", "seq"],
        how="inner",
    )
    merged = merged[merged["state"].isin(CLASSES) & merged["true_state"].isin(CLASSES)]
    if merged.empty:
        return None

    y_true = merged["true_state"].to_numpy()
    y_pred = merged["state"].to_numpy()

    acc = float(accuracy_score(y_true, y_pred))
    prec, rec, f1, sup = precision_recall_fscore_support(y_true, y_pred, labels=CLASSES, zero_division=0)
    macro_p, macro_r, macro_f1, _ = precision_recall_fscore_support(y_true, y_pred, average="macro", zero_division=0)
    cm = confusion_matrix(y_true, y_pred, labels=CLASSES)

    # Critical Airtomic safety KPI: False-Absent Rate when person is actually SLEEPING
    sleeping_mask = y_true == "SLEEPING"
    false_absent_while_sleeping = (
        float(np.mean(y_pred[sleeping_mask] == "ABSENT")) if np.any(sleeping_mask) else np.nan
    )

    per_class = pd.DataFrame({
        "class": CLASSES,
        "precision": prec,
        "recall": rec,
        "f1_score": f1,
        "support": sup,
    })

    return {
        "n_samples": len(merged),
        "accuracy": acc,
        "macro_precision": float(macro_p),
        "macro_recall": float(macro_r),
        "macro_f1": float(macro_f1),
        "false_absent_while_sleeping_pct": false_absent_while_sleeping * 100.0,
        "per_class": per_class,
        "confusion_matrix": cm,
    }


def plot_latency(serial_df: Optional[pd.DataFrame], out_dir: Path) -> None:
    if serial_df is None or serial_df.empty:
        return
    online = serial_df[serial_df["event_type"] == "TELEMETRY"].copy()
    online["e2e_latency_ms"] = pd.to_numeric(online["e2e_latency_ms"], errors="coerce")
    online = online.dropna(subset=["e2e_latency_ms"])
    if online.empty:
        return

    fig, axes = plt.subplots(1, 2, figsize=(12, 4.6))
    nodes = sorted(online["node_id"].unique())

    # Left: Overall E2E Histogram with Mean, Median, P95
    vals = online["e2e_latency_ms"]
    axes[0].hist(vals, bins=45, color="#2b8cbe", edgecolor="white", alpha=0.85)
    mean_v, med_v, p95_v = vals.mean(), vals.median(), np.percentile(vals, 95)
    axes[0].axvline(mean_v, color="#e31a1c", linestyle="-", linewidth=1.8, label=f"Mean: {mean_v:.1f} ms")
    axes[0].axvline(med_v, color="#1b7837", linestyle="--", linewidth=1.6, label=f"Median: {med_v:.1f} ms")
    axes[0].axvline(p95_v, color="#ff7f00", linestyle=":", linewidth=1.8, label=f"P95: {p95_v:.1f} ms")
    axes[0].set_title("End-to-End Latency Distribution (Sensor -> Firebase)")
    axes[0].set_xlabel("Latency (ms)")
    axes[0].set_ylabel("Packet Count")
    axes[0].legend(frameon=True, fontsize=8.5)

    # Right: Boxplot per node
    data_per_node = [online.loc[online["node_id"] == n, "e2e_latency_ms"].to_numpy() for n in nodes]
    bp = axes[1].boxplot(data_per_node, tick_labels=nodes, patch_artist=True, showfliers=False,
                         medianprops={"color": "#111111", "linewidth": 1.6})
    for patch, nid in zip(bp["boxes"], nodes):
        patch.set_facecolor(NODE_COLORS.get(nid, "#66c2a5"))
        patch.set_alpha(0.75)
    axes[1].axhline(1000.0, color="#cb181d", linestyle="--", linewidth=1.2, label="Target (< 1000 ms)")
    axes[1].set_title("End-to-End Latency Comparison Across Nodes")
    axes[1].set_xlabel("Sensor Node ID (SN01=Physical, SN02/03=Simulated)")
    axes[1].set_ylabel("Latency (ms)")
    axes[1].legend(frameon=True, fontsize=8.5)

    fig.tight_layout()
    fig.savefig(out_dir / "01_latency_distribution.png", bbox_inches="tight")
    plt.close(fig)


def plot_pdr(pdr_df: pd.DataFrame, hourly_df: pd.DataFrame, out_dir: Path) -> None:
    node_rows = pdr_df[pdr_df["node_id"] != "OVERALL"].copy()
    if node_rows.empty:
        return

    fig, axes = plt.subplots(1, 2, figsize=(12, 4.6))
    nodes = node_rows["node_id"].tolist()
    colors = [NODE_COLORS.get(n, "#4daf4a") for n in nodes]

    # Left: PDR per node with hourly std error bars
    means = node_rows["pdr_pct"].to_numpy()
    stds = []
    for n in nodes:
        h_vals = hourly_df.loc[hourly_df["node_id"] == n, "pdr_pct"] if not hourly_df.empty else pd.Series(dtype=float)
        stds.append(float(h_vals.std(ddof=1)) if len(h_vals) > 1 else 0.0)

    bars = axes[0].bar(nodes, means, yerr=stds, capsize=5, color=colors, alpha=0.88, edgecolor="#222222", linewidth=0.7)
    axes[0].axhline(95.0, color="#cb181d", linestyle="--", linewidth=1.2, label="Target (>= 95%)")
    axes[0].set_ylim(max(0, min(means) - 5), 101.8)
    axes[0].set_title("Packet Delivery Rate (PDR) per Node (+/- 1 Hourly SD)")
    axes[0].set_xlabel("Sensor Node ID")
    axes[0].set_ylabel("Packet Delivery Rate (%)")
    for bar, val, sd in zip(bars, means, stds):
        axes[0].text(bar.get_x() + bar.get_width() / 2, val + sd + 0.15, f"{val:.2f}%", ha="center", va="bottom", fontweight="bold", fontsize=9)
    axes[0].legend(loc="lower right", frameon=True, fontsize=8.5)

    # Right: E4 Retry Rate & Duplicate Packets Deduplicated
    x = np.arange(len(nodes))
    w = 0.36
    retries = node_rows["retry_rate_pct"].fillna(0).to_numpy()
    dups = node_rows["dup_dropped_by_gw"].fillna(0).to_numpy()

    ax2 = axes[1].twinx()
    b1 = axes[1].bar(x - w / 2, retries, width=w, color="#fdae6b", edgecolor="#333333", linewidth=0.7, label="Retry Rate (%)")
    b2 = ax2.bar(x + w / 2, dups, width=w, color="#756bb1", edgecolor="#333333", linewidth=0.7, label="Duplicates Deduplicated (count)")
    axes[1].set_xticks(x)
    axes[1].set_xticklabels(nodes)
    axes[1].set_title("E4 Reliability: Retries & Gateway Deduplication")
    axes[1].set_xlabel("Sensor Node ID")
    axes[1].set_ylabel("Retry Rate (%)", color="#a63603")
    ax2.set_ylabel("Duplicate Packets Dropped (count)", color="#54278f")
    ax2.grid(False)
    lines = [b1, b2]
    labels = [l.get_label() for l in lines]
    axes[1].legend(lines, labels, loc="upper left", frameon=True, fontsize=8.5)

    fig.tight_layout()
    fig.savefig(out_dir / "02_pdr_and_e4_reliability.png", bbox_inches="tight")
    plt.close(fig)


def plot_uptime_timeline(fb_df: pd.DataFrame, outage_df: pd.DataFrame, out_dir: Path) -> None:
    sn1 = fb_df[fb_df["node_id"] == "SN01"].copy()
    if sn1.empty:
        sn1 = fb_df[fb_df["node_id"] == fb_df["node_id"].iloc[0]].copy()
    sn1 = sn1.set_index("dt").sort_index()
    # Downsample to 5-minute rolling average for crisp 24h readability
    res = sn1[["temp", "lux"]].apply(pd.to_numeric, errors="coerce").resample("5min").mean()

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12.5, 6.2), sharex=True)

    # Panel 1: Temperature + Outage shaded spans
    ax1.plot(res.index, res["temp"], color="#d95f02", linewidth=1.6, label="Temperature (SN01, deg C)")
    ax1.set_ylabel("Temperature (deg C)")
    ax1.set_title("24-Hour Continuous Field Test Timeline & Auto-Recovery Events")

    # Panel 2: Ambient Light + Packet arrival count per 5-min bin
    pkt_counts = fb_df.set_index("dt").resample("5min")["seq"].count()
    ax2.plot(res.index, res["lux"], color="#2166ac", linewidth=1.5, label="Light Intensity (Lux / ADC)")
    ax2.set_ylabel("Light Intensity (Lux)", color="#2166ac")
    ax2_r = ax2.twinx()
    ax2_r.fill_between(pkt_counts.index, pkt_counts.values, color="#74c476", alpha=0.28, label="All-Node Packets / 5 min")
    ax2_r.set_ylabel("Packets / 5 min (SN01+02+03)", color="#1b7837")
    ax2_r.grid(False)

    # Highlight outage windows & recovery annotations
    y_top = ax1.get_ylim()[1]
    ax1.set_ylim(ax1.get_ylim()[0], y_top + 1.2)
    if not outage_df.empty:
        for idx, row in outage_df.iterrows():
            s_dt = pd.to_datetime(row["start_ts_ms"], unit="ms", utc=True).tz_convert("Asia/Bangkok")
            e_dt = pd.to_datetime(row["end_ts_ms"], unit="ms", utc=True).tz_convert("Asia/Bangkok")
            color = "#e31a1c" if "POWER" in str(row["event_kind"]) else "#ff7f00"
            for ax in (ax1, ax2):
                ax.axvspan(s_dt, e_dt, color=color, alpha=0.22, label=f"{row['event_kind']} (rec {row['recovery_sec']:.1f}s)" if ax is ax1 else None)
            ax1.annotate(
                f"{row['event_kind']} ({row['recovery_sec']:.1f}s)",
                xy=(e_dt, y_top + 0.15),
                xytext=(4, 2),
                textcoords="offset points",
                fontsize=7.8,
                color=color,
                fontweight="bold",
            )

    ax1.legend(loc="lower left", frameon=True, fontsize=8, ncol=2)
    h2, l2 = ax2.get_legend_handles_labels()
    h2r, l2r = ax2_r.get_legend_handles_labels()
    ax2.legend(h2 + h2r, l2 + l2r, loc="lower left", frameon=True, fontsize=8, ncol=2)
    ax2.set_xlabel("Local Time (Asia/Bangkok)")
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=sn1.index.tz))
    fig.tight_layout()
    fig.savefig(out_dir / "03_uptime_24h_timeline.png", bbox_inches="tight")
    plt.close(fig)


def plot_recovery_times(outage_df: pd.DataFrame, out_dir: Path) -> None:
    if outage_df.empty:
        return
    fig, ax = plt.subplots(figsize=(8.2, 4.2))
    labels = [f"Event #{i+1}\n({k})" for i, k in enumerate(outage_df["event_kind"])]
    vals = outage_df["recovery_sec"].to_numpy()
    colors = ["#cb181d" if "POWER" in str(k) else "#fe9929" for k in outage_df["event_kind"]]

    bars = ax.bar(labels, vals, color=colors, edgecolor="#222222", linewidth=0.7, width=0.52)
    ax.axhline(30.0, color="#1b7837", linestyle="--", linewidth=1.3, label="Target Auto-Recovery (< 30 s)")
    for bar, v in zip(bars, vals):
        ax.text(bar.get_x() + bar.get_width() / 2, v + 0.4, f"{v:.1f} s", ha="center", va="bottom", fontweight="bold")
    ax.set_ylim(0, max(34.0, float(vals.max()) * 1.25))
    ax.set_title("System Auto-Recovery Time After Fault Injections (24h Field Test)")
    ax.set_ylabel("Recovery Time (seconds)")
    ax.legend(frameon=True, fontsize=8.5)

    fig.tight_layout()
    fig.savefig(out_dir / "04_recovery_times.png", bbox_inches="tight")
    plt.close(fig)


def plot_ai_confusion(ai_res: Optional[Dict], out_dir: Path) -> None:
    if ai_res is None:
        return
    cm = ai_res["confusion_matrix"]
    row_sums = cm.sum(axis=1, keepdims=True)
    cm_pct = np.divide(cm, row_sums, out=np.zeros_like(cm, dtype=float), where=row_sums != 0) * 100.0

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11.5, 4.6), gridspec_kw={"width_ratios": [1.25, 1.0]})

    im = ax1.imshow(cm_pct, cmap="Greens", vmin=0, vmax=100)
    ax1.set_xticks(np.arange(len(CLASSES)))
    ax1.set_yticks(np.arange(len(CLASSES)))
    ax1.set_xticklabels(CLASSES)
    ax1.set_yticklabels(CLASSES)
    ax1.set_xlabel("Predicted Room State")
    ax1.set_ylabel("Ground Truth Room State")
    ax1.set_title(f"AI Occupancy Confusion Matrix (Acc = {ai_res['accuracy']*100:.2f}%)")
    ax1.grid(False)

    for i in range(len(CLASSES)):
        for j in range(len(CLASSES)):
            txt_color = "white" if cm_pct[i, j] > 55 else "#111111"
            ax1.text(j, i, f"{cm[i, j]:,}\n({cm_pct[i, j]:.1f}%)", ha="center", va="center", color=txt_color, fontsize=9, fontweight="bold")
    fig.colorbar(im, ax=ax1, fraction=0.046, pad=0.04, label="Row Recall (%)")

    # Right: Per-class Precision / Recall / F1
    pc = ai_res["per_class"]
    x = np.arange(len(CLASSES))
    w = 0.25
    ax2.bar(x - w, pc["precision"] * 100, width=w, label="Precision", color="#2166ac")
    ax2.bar(x, pc["recall"] * 100, width=w, label="Recall", color="#1b7837")
    ax2.bar(x + w, pc["f1_score"] * 100, width=w, label="F1-Score", color="#d95f02")
    ax2.set_xticks(x)
    ax2.set_xticklabels(CLASSES)
    ax2.set_ylim(0, 108)
    ax2.set_ylabel("Score (%)")
    ax2.set_title(f"Per-Class AI Performance (Macro F1 = {ai_res['macro_f1']*100:.2f}%)")
    ax2.legend(loc="lower right", frameon=True, fontsize=8.5)

    fig.tight_layout()
    fig.savefig(out_dir / "05_ai_confusion_matrix.png", bbox_inches="tight")
    plt.close(fig)


def fmt(val: float, digits: int = 2) -> str:
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return "N/A"
    return f"{val:.{digits}f}"


def export_tables(
    latency_rows: List[Dict],
    pdr_df: pd.DataFrame,
    hourly_pdr_stats: Dict[str, float],
    uptime_summary: Dict,
    outage_df: pd.DataFrame,
    ai_res: Optional[Dict],
    out_dir: Path,
) -> None:
    # 1. Flat CSV summary
    csv_rows: List[Dict] = []
    for r in latency_rows:
        csv_rows.append({"category": "Latency", **r})
    for _, r in pdr_df.iterrows():
        csv_rows.append({
            "category": "PDR_E4",
            "metric": f"PDR_{r['node_id']}",
            "unit": "%",
            "count": int(r["expected_packets"]),
            "mean": round(float(r["pdr_pct"]), 4) if pd.notna(r["pdr_pct"]) else np.nan,
            "median": np.nan,
            "std": np.nan,
            "min": np.nan,
            "max": np.nan,
            "p95": np.nan,
        })
    csv_rows.append({
        "category": "PDR_E4",
        "metric": "Hourly_PDR_Variation_AllNodes",
        "unit": "%",
        **hourly_pdr_stats,
    })
    csv_rows.append({
        "category": "Uptime",
        "metric": "System_Uptime_24h",
        "unit": "%",
        "count": uptime_summary["outage_count"],
        "mean": uptime_summary["uptime_pct"],
        "median": np.nan,
        "std": np.nan,
        "min": np.nan,
        "max": np.nan,
        "p95": np.nan,
    })
    csv_rows.append({
        "category": "Uptime",
        "metric": "Auto_Recovery_Time",
        "unit": "s",
        **uptime_summary["recovery_stats"],
    })
    if ai_res is not None:
        csv_rows.append({
            "category": "AI_Model",
            "metric": "Overall_Accuracy",
            "unit": "%",
            "count": ai_res["n_samples"],
            "mean": ai_res["accuracy"] * 100.0,
            "median": np.nan, "std": np.nan, "min": np.nan, "max": np.nan, "p95": np.nan,
        })
        csv_rows.append({
            "category": "AI_Model",
            "metric": "Macro_F1_Score",
            "unit": "%",
            "count": ai_res["n_samples"],
            "mean": ai_res["macro_f1"] * 100.0,
            "median": np.nan, "std": np.nan, "min": np.nan, "max": np.nan, "p95": np.nan,
        })
        csv_rows.append({
            "category": "AI_Model",
            "metric": "False_Absent_While_Sleeping",
            "unit": "%",
            "count": ai_res["n_samples"],
            "mean": ai_res["false_absent_while_sleeping_pct"],
            "median": np.nan, "std": np.nan, "min": np.nan, "max": np.nan, "p95": np.nan,
        })

    pd.DataFrame(csv_rows).to_csv(out_dir / "summary_metrics.csv", index=False)

    # 2. Publication-ready Markdown report snippet
    md: List[str] = [
        "# Airtomic Quantitative Evaluation Summary (Section IV)",
        "",
        "> Auto-generated by `tests/metrics/analyze_metrics.py`. Copy these tables and PNG figures (`tests/metrics/output/`) directly into the 10-page project report.",
        "",
        "## 1. End-to-End & Subsystem Latency (ms)",
        "",
        "| Metric | Unit | Samples (N) | Mean | Median | Std (SD) | Min | Max | P95 |",
        "| --- | :---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in latency_rows:
        md.append(
            f"| {r['metric']} | {r['unit']} | {r['count']:,} | {fmt(r['mean'])} | {fmt(r['median'])} | "
            f"{fmt(r['std'])} | {fmt(r['min'])} | {fmt(r['max'])} | {fmt(r['p95'])} |"
        )

    md += [
        "",
        "## 2. Packet Delivery Rate (PDR) & E4 Reliability per Node",
        "",
        "| Node ID | Expected | Received (Unique) | Missing | PDR (%) | Retry Rate (%) | Duplicates Dropped (GW) | Duplicates Leaked (DB) | Dedup Accuracy (%) |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for _, r in pdr_df.iterrows():
        md.append(
            f"| **{r['node_id']}** | {int(r['expected_packets']):,} | {int(r['received_unique']):,} | "
            f"{int(r['missing_packets']):,} | {fmt(r['pdr_pct'])}% | {fmt(r['retry_rate_pct'])}% | "
            f"{int(r['dup_dropped_by_gw']):,} | {int(r['dup_leaked_to_db']):,} | {fmt(r['dedup_accuracy_pct'])}% |"
        )

    md += [
        "",
        f"**Hourly PDR Variation (across {hourly_pdr_stats['count']} node-hour windows):** "
        f"Mean = {fmt(hourly_pdr_stats['mean'])}%, Median = {fmt(hourly_pdr_stats['median'])}%, "
        f"SD = {fmt(hourly_pdr_stats['std'])}%, Min = {fmt(hourly_pdr_stats['min'])}%, "
        f"Max = {fmt(hourly_pdr_stats['max'])}%, P95 = {fmt(hourly_pdr_stats['p95'])}%",
        "",
        "## 3. System Uptime & Auto-Recovery (24-Hour Field Test)",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Observation Window | {uptime_summary['duration_hours']:.2f} hours |",
        f"| Effective System Uptime | **{uptime_summary['uptime_pct']:.3f}%** |",
        f"| Total Recorded Downtime | {uptime_summary['total_downtime_sec']:.1f} s |",
        f"| Outage / Fault Events | {uptime_summary['outage_count']} |",
        f"| Store-and-Forward Buffered Packets Flushed (E4) | {uptime_summary['buffered_flushed_packets']:,} |",
        f"| Auto-Recovery Time — Mean ± SD | {fmt(uptime_summary['recovery_stats']['mean'])} ± {fmt(uptime_summary['recovery_stats']['std'])} s |",
        f"| Auto-Recovery Time — Min / Median / P95 / Max | {fmt(uptime_summary['recovery_stats']['min'])} / {fmt(uptime_summary['recovery_stats']['median'])} / {fmt(uptime_summary['recovery_stats']['p95'])} / {fmt(uptime_summary['recovery_stats']['max'])} s |",
        "",
    ]

    if not outage_df.empty:
        md += [
            "### Recorded Outage & Recovery Log",
            "",
            "| # | Fault Type | Downtime (s) | Auto-Recovery Time (s) |",
            "| :---: | --- | ---: | ---: |",
        ]
        for idx, row in outage_df.iterrows():
            md.append(f"| {idx + 1} | {row['event_kind']} | {row['downtime_sec']:.1f} | {row['recovery_sec']:.1f} |")
        md.append("")

    md.append("## 4. AI Occupancy Classification Performance")
    md.append("")
    if ai_res is None:
        md.append("> *Pending `ai_model/` integration or Ground Truth CSV (`--ground-truth`). Re-run once real predictions and ground-truth labels are collected.*")
    else:
        md += [
            f"- **Evaluated Samples (N):** {ai_res['n_samples']:,}",
            f"- **Overall Accuracy:** **{ai_res['accuracy'] * 100:.2f}%**",
            f"- **Macro Precision / Recall / F1-Score:** {ai_res['macro_precision'] * 100:.2f}% / {ai_res['macro_recall'] * 100:.2f}% / **{ai_res['macro_f1'] * 100:.2f}%**",
            f"- **False-Absent Rate while SLEEPING (Safety KPI):** {ai_res['false_absent_while_sleeping_pct']:.2f}%",
            "",
            "| Room Class | Precision (%) | Recall (%) | F1-Score (%) | Support (N) |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
        for _, r in ai_res["per_class"].iterrows():
            md.append(
                f"| `{r['class']}` | {r['precision']*100:.2f}% | {r['recall']*100:.2f}% | "
                f"{r['f1_score']*100:.2f}% | {int(r['support']):,} |"
            )

    (out_dir / "summary_metrics.md").write_text("\n".join(md) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    base = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Analyze Airtomic field-test telemetry & generate report figures.")
    parser.add_argument("--firebase", type=Path, default=base / "data/firebase_export_24h.json", help="Path to Firebase RTDB JSON export")
    parser.add_argument("--serial", type=Path, default=base / "data/serial_events_24h.csv", help="Optional path to Serial event CSV log")
    parser.add_argument("--ground-truth", type=Path, default=base / "data/ai_ground_truth_24h.csv", help="Optional path to AI ground-truth CSV")
    parser.add_argument("--output", type=Path, default=base / "output", help="Output directory for PNG charts & summary tables")
    parser.add_argument("--period-sec", type=float, default=10.0, help="Expected telemetry period in seconds")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    fb_df = load_firebase_history(args.firebase)
    serial_df = load_serial_log(args.serial)

    # Metric 1: Latency statistics
    latency_rows: List[Dict] = []
    if serial_df is not None and not serial_df.empty:
        telem = serial_df[serial_df["event_type"] == "TELEMETRY"]
        if "espnow_rtt_ms" in telem.columns:
            latency_rows.append({"metric": "ESP-NOW RTT (Sensor -> Gateway, All)", "unit": "ms", **describe_series(telem["espnow_rtt_ms"])})
        if "e2e_latency_ms" in telem.columns:
            latency_rows.append({"metric": "E2E Latency (Sensor -> Firebase, All)", "unit": "ms", **describe_series(telem["e2e_latency_ms"])})
            for nid in sorted(telem["node_id"].dropna().unique()):
                sub = telem[telem["node_id"] == nid]
                latency_rows.append({"metric": f"E2E Latency ({nid} -> Firebase)", "unit": "ms", **describe_series(sub["e2e_latency_ms"])})
        if "actuator_latency_ms" in telem.columns:
            act_stats = describe_series(telem["actuator_latency_ms"])
            if act_stats["count"] > 0:
                latency_rows.append({"metric": "Actuator Response (Sensor -> Servo)", "unit": "ms", **act_stats})
    else:
        # Fallback: compute inter-arrival interval statistics directly from Firebase `ts`
        for nid, grp in fb_df.groupby("node_id"):
            deltas = grp["ts"].diff().dropna()
            deltas = deltas[(deltas > 0) & (deltas < args.period_sec * 3000)]
            latency_rows.append({"metric": f"Firebase Inter-Arrival ({nid})", "unit": "ms", **describe_series(deltas)})

    # Metric 2: PDR & E4 Reliability
    pdr_df, hourly_df = compute_pdr_metrics(fb_df, serial_df)
    hourly_pdr_stats = describe_series(hourly_df["pdr_pct"]) if not hourly_df.empty else describe_series(pd.Series(dtype=float))

    # Metric 3: Uptime & Auto-Recovery
    uptime_summary, outage_df = compute_uptime_and_recovery(fb_df, serial_df, args.period_sec)

    # Metric 4: AI Model Performance
    ai_res = compute_ai_metrics(fb_df, args.ground_truth)

    # Generate high-resolution 300 DPI plots
    plot_latency(serial_df, args.output)
    plot_pdr(pdr_df, hourly_df, args.output)
    plot_uptime_timeline(fb_df, outage_df, args.output)
    plot_recovery_times(outage_df, args.output)
    plot_ai_confusion(ai_res, args.output)

    # Export Markdown & CSV summary tables
    export_tables(latency_rows, pdr_df, hourly_pdr_stats, uptime_summary, outage_df, ai_res, args.output)

    print(f"Analysis complete! Outputs written to: {args.output}")
    for fpath in sorted(args.output.iterdir()):
        print(f"  - {fpath.name} ({fpath.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
