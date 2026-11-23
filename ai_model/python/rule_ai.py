"""Rule-Based AI Engine for Airtomic (Python mirror of cpp/AirtomicRuleAI).

This module mirrors the exact scoring, temporal state machine, appliance
debounce/hysteresis, and auto-off verification logic of the ESP32 C++ engine
so scenarios can be simulated, evaluated, and tuned on PC before flashing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntFlag, Enum
import math
from typing import List


class RoomState(str, Enum):
    AWAKE = "AWAKE"
    SLEEPING = "SLEEPING"
    ABSENT = "ABSENT"


class DoorTriggerMode(str, Enum):
    SHORTER_ONLY = "SHORTER_ONLY"
    BOTH_DIRECTIONS = "BOTH_DIRECTIONS"


class VerificationStatus(str, Enum):
    NONE = "NONE"
    PENDING = "PENDING"
    VERIFIED_OFF = "VERIFIED_OFF"
    RETRY_NEEDED = "RETRY_NEEDED"
    FAILED_MAX_RETRIES = "FAILED_MAX_RETRIES"


class RuleFlag(IntFlag):
    RULE_NONE = 0
    RULE_PIR_WARMUP = 1 << 0
    RULE_PIR_ACTIVE = 1 << 1
    RULE_PIR_RECENT_STILL = 1 << 2
    RULE_PIR_PROLONGED_STILL = 1 << 3
    RULE_DOOR_EVENT_DETECTED = 1 << 4
    RULE_DOOR_EXIT_UNCONFIRMED = 1 << 5
    RULE_MOTION_AFTER_DOOR = 1 << 6
    RULE_NO_DOOR_LONG_IDLE = 1 << 7
    RULE_DOOR_OPEN_TOO_LONG = 1 << 8
    RULE_BLE_NEAR = 1 << 9
    RULE_BLE_FAR_OR_LOST = 1 << 10
    RULE_LIGHT_DARK_INSIDE = 1 << 11
    RULE_LIGHT_ON_WHILE_IDLE = 1 << 12
    RULE_LIGHT_HUMAN_TOGGLED = 1 << 13
    RULE_TEMP_AC_COOLING = 1 << 14


RULE_DESCRIPTIONS = {
    RuleFlag.RULE_PIR_WARMUP: "PIR warming up (safe hold)",
    RuleFlag.RULE_PIR_ACTIVE: "PIR active / recent motion",
    RuleFlag.RULE_PIR_RECENT_STILL: "PIR paused recently",
    RuleFlag.RULE_PIR_PROLONGED_STILL: "PIR prolonged stillness",
    RuleFlag.RULE_DOOR_EVENT_DETECTED: "Doorway event detected",
    RuleFlag.RULE_DOOR_EXIT_UNCONFIRMED: "Door event without post-grace motion",
    RuleFlag.RULE_MOTION_AFTER_DOOR: "Motion confirmed inside after door event",
    RuleFlag.RULE_NO_DOOR_LONG_IDLE: "No door event + long idle since boot",
    RuleFlag.RULE_DOOR_OPEN_TOO_LONG: "Door ajar / obstructed too long",
    RuleFlag.RULE_BLE_NEAR: "BLE phone nearby",
    RuleFlag.RULE_BLE_FAR_OR_LOST: "BLE phone far or lost",
    RuleFlag.RULE_LIGHT_DARK_INSIDE: "Dark room while inside",
    RuleFlag.RULE_LIGHT_ON_WHILE_IDLE: "Light on while room still",
    RuleFlag.RULE_LIGHT_HUMAN_TOGGLED: "Recent human light switch toggle",
    RuleFlag.RULE_TEMP_AC_COOLING: "DHT temperature dropping with AC on",
}


def explain_rules(mask: int) -> List[str]:
    """Return human-readable descriptions for active RuleFlag bits."""
    return [desc for flag, desc in RULE_DESCRIPTIONS.items() if mask & int(flag)]


@dataclass
class SensorInput:
    timestamp_ms: int = 0
    temp_c: float = float("nan")
    hum_pct: float = float("nan")
    vib: int = -1
    dist_cm: float = -1.0
    pir: int = -1
    light_raw: int = -1
    lux: float = -1.0
    ble_scanner_enabled: bool = False
    rssi: int = -127


@dataclass
class RuleAIConfig:
    # Light detection (supports either BH1750 lux or LDR light_raw)
    light_inverted: bool = False
    light_raw_on_threshold: int = 1800
    light_raw_off_threshold: int = 1400
    lux_on_threshold: float = 60.0
    lux_off_threshold: float = 35.0

    # AC detection via debounced SW-420 vibration
    vib_window_size: int = 4
    vib_on_min_hits: int = 2
    ac_vib_off_grace_ms: int = 15000

    # HC-SR04 doorway baseline & event detection
    door_trigger_mode: DoorTriggerMode = DoorTriggerMode.SHORTER_ONLY
    door_baseline_cm: float = -1.0
    door_trigger_delta_cm: float = 20.0
    door_noise_margin_cm: float = 8.0
    door_exit_grace_ms: int = 15000
    door_ajar_timeout_ms: int = 120000

    # BLE RSSI filtering
    ble_ema_alpha: float = 0.4
    ble_present_threshold_dbm: int = -82
    ble_absent_threshold_dbm: int = -92
    ble_lost_timeout_ms: int = 45000

    # Temporal human-presence windows
    awake_hold_ms: int = 60000
    still_transition_ms: int = 180000
    no_door_long_idle_ms: int = 900000
    dark_absent_after_door_ms: int = 90000
    light_human_toggle_window_ms: int = 45000

    # Softmax confidence & ABSENT confirmation / safety gating
    softmax_temp: float = 2.0
    absent_score_margin: float = 0.8
    min_auto_off_conf: float = 0.75
    alert_delay_ms: int = 60000
    absent_confirm_ms: int = 180000
    verify_wait_ms: int = 20000
    auto_off_cooldown_ms: int = 60000
    max_auto_off_retries: int = 2

    @classmethod
    def room_default(cls) -> "RuleAIConfig":
        return cls()

    @classmethod
    def demo_fast(cls) -> "RuleAIConfig":
        return cls(
            door_exit_grace_ms=10000,
            door_ajar_timeout_ms=30000,
            ble_lost_timeout_ms=15000,
            awake_hold_ms=15000,
            still_transition_ms=25000,
            no_door_long_idle_ms=60000,
            dark_absent_after_door_ms=20000,
            light_human_toggle_window_ms=15000,
            alert_delay_ms=15000,
            absent_confirm_ms=30000,
            verify_wait_ms=15000,
            auto_off_cooldown_ms=20000,
        )


@dataclass
class RoomDecision:
    state: RoomState = RoomState.AWAKE
    confidence: float = 0.0
    ac_on: bool = False
    light_on: bool = False
    wasting_energy: bool = False
    door_open_too_long: bool = False
    should_alert: bool = False
    should_turn_off_ac: bool = False
    should_turn_off_light: bool = False
    verification: VerificationStatus = VerificationStatus.NONE

    absent_duration_ms: int = 0
    score_awake: float = 0.0
    score_sleeping: float = 0.0
    score_absent: float = 0.0
    door_baseline_cm: float = -1.0
    smoothed_rssi: float = -127.0
    active_rules: int = 0


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else (hi if v > hi else v)


def _compute_softmax_confidence(
    s_awake: float,
    s_sleep: float,
    s_absent: float,
    temp: float,
    absent_margin: float,
) -> tuple[RoomState, float]:
    inv_t = (1.0 / temp) if temp > 0.05 else 1.0
    m = max(s_awake, s_sleep, s_absent)

    e_awake = math.exp((s_awake - m) * inv_t)
    e_sleep = math.exp((s_sleep - m) * inv_t)
    e_absent = math.exp((s_absent - m) * inv_t)
    total = e_awake + e_sleep + e_absent

    p_awake = e_awake / total
    p_sleep = e_sleep / total
    p_absent = e_absent / total

    if (
        s_absent > s_awake
        and s_absent > s_sleep
        and (s_absent - max(s_awake, s_sleep)) >= absent_margin
    ):
        state = RoomState.ABSENT
        conf = p_absent
    elif s_sleep > s_awake:
        state = RoomState.SLEEPING
        conf = p_sleep
    else:
        state = RoomState.AWAKE
        conf = p_awake

    return state, _clamp(conf, 0.0, 0.99)


@dataclass
class AirtomicRuleEngine:
    config: RuleAIConfig = field(default_factory=RuleAIConfig.room_default)

    def __post_init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._initialized = False
        self._first_seen_ms = 0
        self._last_sample_ms = 0

        self._ac_on = False
        self._light_on = False
        self._vib_history: List[int] = [0] * 16
        self._vib_write_idx = 0
        self._vib_sample_count = 0
        self._has_last_vib_one = False
        self._last_vib_one_ms = 0

        self._has_prev_temp = False
        self._prev_temp_c = float("nan")

        self._has_last_light_toggle = False
        self._last_human_light_toggle_ms = 0
        self._last_light_toggle_was_on = False
        self._suppress_light_toggle_valid = False
        self._suppress_light_toggle_until_ms = 0

        self._has_ever_seen_motion = False
        self._last_motion_ms = 0
        self._pir_ready = False
        self._pir_ready_at_ms = 0

        self._door_baseline_cm = self.config.door_baseline_cm if self.config.door_baseline_cm > 0.0 else -1.0
        self._door_init_count = 0
        self._door_init_samples = [0.0, 0.0, 0.0]
        self._door_higher_count = 0
        self._door_higher_samples = [0.0, 0.0, 0.0]
        self._door_currently_deviated = False
        self._door_deviation_start_ms = 0
        self._has_door_event = False
        self._last_door_event_ms = 0
        self._motion_confirmed_after_door = True

        self._has_ble_reading = False
        self._has_seen_ble_near = False
        self._smoothed_rssi = -127.0
        self._last_ble_seen_near_ms = 0
        self._ble_enabled_since_ms = 0
        self._ble_was_enabled = False

        self._in_absent_state = False
        self._absent_started_ms = 0
        self._alert_sent_for_current_absence = False

        self._verification = VerificationStatus.NONE
        self._verify_target_ac = False
        self._verify_target_light = False
        self._last_auto_off_attempt_ms = 0
        self._has_auto_off_attempt = False
        self._auto_off_retry_count = 0

    def notify_auto_off_triggered(
        self, targeted_ac: bool, targeted_light: bool, timestamp_ms: int
    ) -> None:
        if not targeted_ac and not targeted_light:
            return
        self._has_auto_off_attempt = True
        self._last_auto_off_attempt_ms = timestamp_ms
        self._verify_target_ac = targeted_ac
        self._verify_target_light = targeted_light
        self._verification = VerificationStatus.PENDING
        if self._auto_off_retry_count < 255:
            self._auto_off_retry_count += 1
        if targeted_light:
            self._suppress_light_toggle_valid = True
            self._suppress_light_toggle_until_ms = (
                timestamp_ms + self.config.light_human_toggle_window_ms
            )

    def _update_appliances(self, inp: SensorInput) -> None:
        now_ms = inp.timestamp_ms
        prev_light = self._light_on
        light_evaluated = False

        if inp.lux >= 0.0 and math.isfinite(inp.lux):
            light_evaluated = True
            if not self._light_on and inp.lux >= self.config.lux_on_threshold:
                self._light_on = True
            elif self._light_on and inp.lux <= self.config.lux_off_threshold:
                self._light_on = False
        elif 0 <= inp.light_raw <= 4095:
            light_evaluated = True
            eff_raw = (4095 - inp.light_raw) if self.config.light_inverted else inp.light_raw
            if not self._light_on and eff_raw >= self.config.light_raw_on_threshold:
                self._light_on = True
            elif self._light_on and eff_raw <= self.config.light_raw_off_threshold:
                self._light_on = False

        if light_evaluated and self._initialized and self._light_on != prev_light:
            suppressed = (
                self._suppress_light_toggle_valid
                and now_ms <= self._suppress_light_toggle_until_ms
            )
            if not suppressed:
                self._has_last_light_toggle = True
                self._last_human_light_toggle_ms = now_ms
                self._last_light_toggle_was_on = self._light_on

        if inp.vib in (0, 1):
            window = max(1, min(self.config.vib_window_size, 16))
            self._vib_history[self._vib_write_idx] = inp.vib
            self._vib_write_idx = (self._vib_write_idx + 1) % window
            if self._vib_sample_count < window:
                self._vib_sample_count += 1

            hits = sum(self._vib_history[: self._vib_sample_count])
            if inp.vib == 1:
                self._has_last_vib_one = True
                self._last_vib_one_ms = now_ms

            req_hits = max(1, min(self.config.vib_on_min_hits, window))
            if not self._ac_on and hits >= req_hits:
                self._ac_on = True
            elif self._ac_on:
                timed_out = (
                    not self._has_last_vib_one
                    or (now_ms - self._last_vib_one_ms) >= self.config.ac_vib_off_grace_ms
                )
                if timed_out and inp.vib == 0:
                    self._ac_on = False

    def _update_door(self, inp: SensorInput) -> None:
        now_ms = inp.timestamp_ms
        if not (math.isfinite(inp.dist_cm) and 2.0 <= inp.dist_cm <= 400.0):
            return

        if self._door_baseline_cm <= 0.0:
            self._door_init_samples[self._door_init_count] = inp.dist_cm
            self._door_init_count += 1
            if self._door_init_count >= 3:
                a, b, c = self._door_init_samples
                self._door_baseline_cm = (a + b + c) - min(a, b, c) - max(a, b, c)
            return

        drop = self._door_baseline_cm - inp.dist_cm
        abs_delta = abs(drop)
        triggered = (
            drop >= self.config.door_trigger_delta_cm
            if self.config.door_trigger_mode == DoorTriggerMode.SHORTER_ONLY
            else abs_delta >= self.config.door_trigger_delta_cm
        )

        if triggered:
            self._door_higher_count = 0
            if not self._door_currently_deviated:
                self._door_currently_deviated = True
                self._door_deviation_start_ms = now_ms
                self._has_door_event = True
                self._last_door_event_ms = now_ms
                self._motion_confirmed_after_door = False
        else:
            self._door_currently_deviated = False
            if self.config.door_baseline_cm <= 0.0:
                if abs_delta <= self.config.door_noise_margin_cm:
                    self._door_higher_count = 0
                    self._door_baseline_cm = 0.05 * inp.dist_cm + 0.95 * self._door_baseline_cm
                elif (
                    self.config.door_trigger_mode == DoorTriggerMode.SHORTER_ONLY
                    and (inp.dist_cm - self._door_baseline_cm) > self.config.door_noise_margin_cm
                ):
                    self._door_higher_samples[self._door_higher_count] = inp.dist_cm
                    self._door_higher_count += 1
                    if self._door_higher_count >= 3:
                        a, b, c = self._door_higher_samples
                        self._door_baseline_cm = (a + b + c) - min(a, b, c) - max(a, b, c)
                        self._door_higher_count = 0
                else:
                    self._door_higher_count = 0

    def _update_motion(self, inp: SensorInput) -> None:
        now_ms = inp.timestamp_ms
        if inp.pir < 0:
            self._pir_ready = False
            return

        if not self._pir_ready:
            self._pir_ready = True
            self._pir_ready_at_ms = now_ms

        if inp.pir == 1:
            self._has_ever_seen_motion = True
            self._last_motion_ms = now_ms
            if (
                not self._has_door_event
                or (now_ms - self._last_door_event_ms) > self.config.door_exit_grace_ms
            ):
                self._motion_confirmed_after_door = True

    def _update_ble(self, inp: SensorInput) -> None:
        now_ms = inp.timestamp_ms
        if not inp.ble_scanner_enabled:
            self._ble_was_enabled = False
            return

        if not self._ble_was_enabled:
            self._ble_was_enabled = True
            self._ble_enabled_since_ms = now_ms

        if -126 <= inp.rssi <= -5:
            rssi_f = float(inp.rssi)
            if not self._has_ble_reading:
                self._smoothed_rssi = rssi_f
                self._has_ble_reading = True
            else:
                a = _clamp(self.config.ble_ema_alpha, 0.05, 1.0)
                self._smoothed_rssi = a * rssi_f + (1.0 - a) * self._smoothed_rssi

            if self._smoothed_rssi >= float(self.config.ble_present_threshold_dbm):
                self._has_seen_ble_near = True
                self._last_ble_seen_near_ms = now_ms

    def _update_verification_and_actions(self, inp: SensorInput, out: RoomDecision) -> None:
        now_ms = inp.timestamp_ms

        if out.state == RoomState.ABSENT:
            if not self._in_absent_state:
                self._in_absent_state = True
                self._absent_started_ms = now_ms
                self._alert_sent_for_current_absence = False
            out.absent_duration_ms = now_ms - self._absent_started_ms
        else:
            self._in_absent_state = False
            out.absent_duration_ms = 0
            self._alert_sent_for_current_absence = False
            if self._verification in (
                VerificationStatus.PENDING,
                VerificationStatus.RETRY_NEEDED,
            ):
                self._verification = VerificationStatus.NONE
            self._auto_off_retry_count = 0

        if self._verification == VerificationStatus.PENDING and self._has_auto_off_attempt:
            ac_cleared = (not self._verify_target_ac) or (not self._ac_on)
            light_cleared = (not self._verify_target_light) or (not self._light_on)
            if ac_cleared and light_cleared:
                self._verification = VerificationStatus.VERIFIED_OFF
            elif (now_ms - self._last_auto_off_attempt_ms) >= self.config.verify_wait_ms:
                if self._auto_off_retry_count < self.config.max_auto_off_retries:
                    self._verification = VerificationStatus.RETRY_NEEDED
                else:
                    self._verification = VerificationStatus.FAILED_MAX_RETRIES

        out.verification = self._verification

        safe_to_act = (
            out.state == RoomState.ABSENT
            and out.wasting_energy
            and out.confidence >= self.config.min_auto_off_conf
            and inp.pir >= 0
        )
        if not safe_to_act:
            return

        if not self._alert_sent_for_current_absence and out.absent_duration_ms >= self.config.alert_delay_ms:
            out.should_alert = True
            self._alert_sent_for_current_absence = True

        blocked = self._verification in (
            VerificationStatus.PENDING,
            VerificationStatus.FAILED_MAX_RETRIES,
        )
        cooldown_ok = (
            not self._has_auto_off_attempt
            or (now_ms - self._last_auto_off_attempt_ms) >= self.config.auto_off_cooldown_ms
        )
        if not blocked and cooldown_ok and out.absent_duration_ms >= self.config.absent_confirm_ms:
            out.should_turn_off_ac = self._ac_on
            out.should_turn_off_light = self._light_on

    def update(self, inp: SensorInput) -> RoomDecision:
        now_ms = inp.timestamp_ms
        if not self._initialized:
            self._first_seen_ms = now_ms

        self._update_appliances(inp)
        self._update_door(inp)
        self._update_motion(inp)
        self._update_ble(inp)

        self._initialized = True
        self._last_sample_ms = now_ms

        out = RoomDecision(
            ac_on=self._ac_on,
            light_on=self._light_on,
            door_baseline_cm=self._door_baseline_cm,
            smoothed_rssi=self._smoothed_rssi,
        )

        if (
            self._door_currently_deviated
            and (now_ms - self._door_deviation_start_ms) >= self.config.door_ajar_timeout_ms
        ):
            out.door_open_too_long = True
            out.active_rules |= int(RuleFlag.RULE_DOOR_OPEN_TOO_LONG)

        ble_presence = "UNKNOWN"
        if inp.ble_scanner_enabled:
            if (
                self._has_seen_ble_near
                and (now_ms - self._last_ble_seen_near_ms) <= self.config.ble_lost_timeout_ms
                and self._smoothed_rssi > float(self.config.ble_absent_threshold_dbm)
            ):
                ble_presence = "NEAR"
            elif (
                self._has_seen_ble_near
                and (now_ms - self._last_ble_seen_near_ms) > self.config.ble_lost_timeout_ms
            ) or (
                not self._has_seen_ble_near
                and (now_ms - self._ble_enabled_since_ms) >= self.config.ble_lost_timeout_ms
            ):
                ble_presence = "FAR_OR_LOST"

        time_since_motion_ms = 0
        if self._pir_ready:
            ref_ms = self._last_motion_ms if self._has_ever_seen_motion else self._pir_ready_at_ms
            time_since_motion_ms = max(0, now_ms - ref_ms)

        s_awake = 0.0
        s_sleep = 0.0
        s_absent = 0.0

        # Rule 1: PIR Motion / Stillness
        if not self._pir_ready or inp.pir < 0:
            s_awake += 0.5
            out.active_rules |= int(RuleFlag.RULE_PIR_WARMUP)
        elif inp.pir == 1 or time_since_motion_ms <= self.config.awake_hold_ms:
            s_awake += 4.0
            out.active_rules |= int(RuleFlag.RULE_PIR_ACTIVE)
        elif time_since_motion_ms <= self.config.still_transition_ms:
            s_awake += 1.5
            s_sleep += 1.2
            out.active_rules |= int(RuleFlag.RULE_PIR_RECENT_STILL)
        else:
            s_sleep += 1.5
            s_absent += 1.5
            out.active_rules |= int(RuleFlag.RULE_PIR_PROLONGED_STILL)

        # Rule 2: Doorway (HC-SR04) + Post-door motion
        if self._has_door_event and (now_ms - self._last_door_event_ms) <= self.config.door_exit_grace_ms:
            out.active_rules |= int(RuleFlag.RULE_DOOR_EVENT_DETECTED)

        likely_left_via_door = (
            self._has_door_event
            and not self._motion_confirmed_after_door
            and (now_ms - self._last_door_event_ms) >= self.config.door_exit_grace_ms
            and inp.pir == 0
        )

        if likely_left_via_door:
            s_absent += 3.2
            out.active_rules |= int(RuleFlag.RULE_DOOR_EXIT_UNCONFIRMED)
        elif self._has_door_event and self._motion_confirmed_after_door:
            out.active_rules |= int(RuleFlag.RULE_MOTION_AFTER_DOOR)
            if self._pir_ready and time_since_motion_ms > self.config.still_transition_ms:
                s_sleep += 2.6
            elif self._pir_ready and inp.pir == 0:
                s_awake += 0.8
        elif (
            not self._has_door_event
            and self._pir_ready
            and time_since_motion_ms > self.config.no_door_long_idle_ms
        ):
            out.active_rules |= int(RuleFlag.RULE_NO_DOOR_LONG_IDLE)
            if ble_presence == "FAR_OR_LOST":
                s_absent += 1.8
            else:
                s_sleep += 1.5

        # Rule 3: BLE Phone RSSI (optional)
        if ble_presence == "NEAR":
            out.active_rules |= int(RuleFlag.RULE_BLE_NEAR)
            if not self._pir_ready or time_since_motion_ms <= self.config.still_transition_ms:
                s_awake += 1.8
            else:
                s_sleep += 2.5
        elif ble_presence == "FAR_OR_LOST" and self._pir_ready and inp.pir >= 0:
            out.active_rules |= int(RuleFlag.RULE_BLE_FAR_OR_LOST)
            s_absent += 2.8

        # Rule 4: Light context + Recent manual light switch toggle
        has_light_sensor = (inp.lux >= 0.0 and math.isfinite(inp.lux)) or (0 <= inp.light_raw <= 4095)
        if has_light_sensor and self._pir_ready and time_since_motion_ms > self.config.still_transition_ms:
            if not self._light_on and not likely_left_via_door and ble_presence != "FAR_OR_LOST":
                s_sleep += 1.8
                out.active_rules |= int(RuleFlag.RULE_LIGHT_DARK_INSIDE)
            elif self._light_on:
                out.active_rules |= int(RuleFlag.RULE_LIGHT_ON_WHILE_IDLE)
                if likely_left_via_door or ble_presence == "FAR_OR_LOST":
                    s_absent += 1.2
                else:
                    s_sleep += 1.0

        if (
            self._has_last_light_toggle
            and (now_ms - self._last_human_light_toggle_ms) <= self.config.light_human_toggle_window_ms
        ):
            out.active_rules |= int(RuleFlag.RULE_LIGHT_HUMAN_TOGGLED)
            if self._last_light_toggle_was_on:
                s_awake += 2.0
            else:
                s_awake += 0.5
                s_sleep += 1.0

        # Rule 5: AC & Temp contextual assist
        if math.isfinite(inp.temp_c):
            if self._has_prev_temp and self._ac_on and (self._prev_temp_c - inp.temp_c) >= 0.5:
                out.active_rules |= int(RuleFlag.RULE_TEMP_AC_COOLING)
            self._prev_temp_c = inp.temp_c
            self._has_prev_temp = True

        if not self._ac_on and not self._light_on and self._pir_ready and time_since_motion_ms > self.config.still_transition_ms:
            s_absent += 0.4

        out.score_awake = s_awake
        out.score_sleeping = s_sleep
        out.score_absent = s_absent

        out.state, out.confidence = _compute_softmax_confidence(
            s_awake,
            s_sleep,
            s_absent,
            self.config.softmax_temp,
            self.config.absent_score_margin,
        )
        out.wasting_energy = (out.state == RoomState.ABSENT) and (out.ac_on or out.light_on)
        self._update_verification_and_actions(inp, out)
        return out
