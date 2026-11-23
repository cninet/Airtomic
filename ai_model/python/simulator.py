"""Synthetic Dorm-Room Telemetry Simulator for Airtomic Rule-Based AI.

Generates realistic 5-second interval sensor streams matching the ESP32
Sensor Node + Gateway schema (`messages.h` + `local_sensors.cpp` + optional BLE)
with ground-truth room state & appliance labels for evaluation before real data
is collected.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import random
from typing import List

from rule_ai import RoomState, SensorInput


@dataclass
class SimulatedStep:
    sensor: SensorInput
    expected_state: RoomState
    expected_ac_on: bool
    expected_light_on: bool
    in_transition: bool = False  # True during expected physical settling window


@dataclass
class SimulatedScenario:
    name: str
    description: str
    use_demo_config: bool
    simulate_servo_auto_off: bool
    steps: List[SimulatedStep]


def _clamp_int(val: float, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(round(val))))


def generate_scenarios(seed: int = 42, add_noise: bool = True) -> List[SimulatedScenario]:
    rng = random.Random(seed)
    step_ms = 5000

    def n_float(val: float, sigma: float) -> float:
        return val + (rng.gauss(0.0, sigma) if add_noise else 0.0)

    def n_int(val: float, sigma: float, lo: int, hi: int) -> int:
        return _clamp_int(val + (rng.gauss(0.0, sigma) if add_noise else 0.0), lo, hi)

    scenarios: List[SimulatedScenario] = []

    # 1. Morning rush: forgot AC & Light ON, walks out with phone (BLE enabled)
    steps1: List[SimulatedStep] = []
    for i in range(108):  # 9 minutes (540s)
        t = i * step_ms
        if t < 120_000:
            # Awake moving inside
            pir = 1 if (i % 2 == 0 or rng.random() < 0.7) else 0
            dist = n_float(85.0, 1.5)
            rssi = n_int(-62, 2.5, -90, -35)
            light_raw = n_int(2600, 40, 0, 4095)
            vib = 1
            exp_state = RoomState.AWAKE
            trans = False
        elif t == 120_000:
            # Walks through doorway
            pir = 1
            dist = n_float(32.0, 2.0)
            rssi = n_int(-74, 2.0, -95, -40)
            light_raw = n_int(2600, 40, 0, 4095)
            vib = 1
            exp_state = RoomState.AWAKE
            trans = True
        elif t <= 130_000:
            # Trailing PIR pulse right as door closes
            pir = 1 if t == 125_000 else 0
            dist = n_float(85.0, 1.5)
            rssi = n_int(-86, 2.0, -105, -60)
            light_raw = n_int(2600, 40, 0, 4095)
            vib = 1
            exp_state = RoomState.AWAKE
            trans = True
        else:
            # Outside room, forgot AC & light; physical truth is ABSENT once past awake hold
            pir = 0
            dist = n_float(85.0, 1.5)
            rssi = -127 if t > 145_000 else n_int(-96, 2.0, -115, -80)
            light_raw = n_int(2600, 40, 0, 4095)
            vib = 1
            exp_state = RoomState.ABSENT
            trans = t < 200_000  # Allow awakeHoldMs + BLE fade window as transition

        steps1.append(
            SimulatedStep(
                sensor=SensorInput(
                    timestamp_ms=t,
                    temp_c=round(n_float(24.5, 0.2), 1),
                    hum_pct=round(n_float(56.0, 0.5), 1),
                    vib=vib,
                    dist_cm=round(dist, 1),
                    pir=pir,
                    light_raw=light_raw,
                    ble_scanner_enabled=True,
                    rssi=rssi,
                ),
                expected_state=exp_state,
                expected_ac_on=True,
                expected_light_on=True,
                in_transition=trans,
            )
        )
    scenarios.append(
        SimulatedScenario(
            name="morning_rush_forgot_all_ble",
            description="Student walks out leaving AC + Light ON (with BLE RSSI + Servo verification)",
            use_demo_config=False,
            simulate_servo_auto_off=True,
            steps=steps1,
        )
    )

    # 2. Forgot AC only (turned off light), NO BLE scanner enabled
    steps2: List[SimulatedStep] = []
    for i in range(120):  # 10 minutes
        t = i * step_ms
        if t < 60_000:
            pir = 1
            dist = n_float(90.0, 1.5)
            light_raw = n_int(2500, 35, 0, 4095)
            exp_state = RoomState.AWAKE
            exp_light = True
            trans = False
        elif t == 60_000:
            pir = 1
            dist = n_float(28.0, 2.0)  # Walks out door
            light_raw = n_int(180, 20, 0, 4095)
            exp_state = RoomState.AWAKE
            exp_light = False
            trans = True
        else:
            pir = 0
            dist = n_float(90.0, 1.5)
            light_raw = n_int(180, 20, 0, 4095)
            exp_state = RoomState.ABSENT
            exp_light = False
            trans = t < 130_000

        steps2.append(
            SimulatedStep(
                sensor=SensorInput(
                    timestamp_ms=t,
                    temp_c=round(n_float(23.8, 0.2), 1),
                    hum_pct=round(n_float(54.0, 0.5), 1),
                    vib=1,
                    dist_cm=round(dist, 1),
                    pir=pir,
                    light_raw=light_raw,
                    ble_scanner_enabled=False,
                ),
                expected_state=exp_state,
                expected_ac_on=True,
                expected_light_on=exp_light,
                in_transition=trans,
            )
        )
    scenarios.append(
        SimulatedScenario(
            name="forgot_ac_only_no_ble",
            description="Student turns off light, forgets AC, walks out (no BLE)",
            use_demo_config=False,
            simulate_servo_auto_off=True,
            steps=steps2,
        )
    )

    # 3. Sleeping with AC ON, light OFF, NO BLE (critical zero false-off scenario)
    steps3: List[SimulatedStep] = []
    for i in range(360):  # 30 minutes
        t = i * step_ms
        if t == 15_000:
            # Walks into room after initial 15s baseline settle
            pir = 1
            dist = n_float(30.0, 2.0)
            light_raw = n_int(2300, 35, 0, 4095)
            exp_state = RoomState.AWAKE
            exp_light = True
            trans = False
        elif t < 90_000:
            pir = 1 if i % 2 == 0 else 0
            dist = n_float(92.0, 1.5)
            light_raw = n_int(2300, 35, 0, 4095)
            exp_state = RoomState.AWAKE
            exp_light = True
            trans = False
        else:
            # Turns off light and sleeps silently for 28+ minutes
            pir = 0
            dist = n_float(92.0, 1.5)
            light_raw = n_int(140, 20, 0, 4095)
            exp_state = RoomState.SLEEPING
            exp_light = False
            trans = t < 280_000  # Settling from AWAKE -> SLEEPING over stillTransitionMs

        steps3.append(
            SimulatedStep(
                sensor=SensorInput(
                    timestamp_ms=t,
                    temp_c=round(n_float(24.0, 0.2), 1),
                    hum_pct=round(n_float(55.0, 0.5), 1),
                    vib=1,
                    dist_cm=round(dist, 1),
                    pir=pir,
                    light_raw=light_raw,
                    ble_scanner_enabled=False,
                ),
                expected_state=exp_state,
                expected_ac_on=True,
                expected_light_on=exp_light,
                in_transition=trans,
            )
        )
    scenarios.append(
        SimulatedScenario(
            name="sleeping_with_ac_no_ble",
            description="Student sleeps 30 min with AC ON and light OFF (must NOT trigger ABSENT)",
            use_demo_config=False,
            simulate_servo_auto_off=False,
            steps=steps3,
        )
    )

    # 4. Sitting still studying/coding with light ON + BLE near
    steps4: List[SimulatedStep] = []
    for i in range(180):  # 15 minutes
        t = i * step_ms
        if t < 60_000:
            pir = 1
            exp_state = RoomState.AWAKE
            trans = False
        else:
            pir = 0
            exp_state = RoomState.SLEEPING  # Dashboard label: "มีคน - นอนหลับ/นั่งนิ่ง"
            trans = t < 250_000

        steps4.append(
            SimulatedStep(
                sensor=SensorInput(
                    timestamp_ms=t,
                    temp_c=round(n_float(25.2, 0.2), 1),
                    hum_pct=round(n_float(58.0, 0.5), 1),
                    vib=1,
                    dist_cm=round(n_float(88.0, 1.5), 1),
                    pir=pir,
                    light_raw=n_int(2650, 35, 0, 4095),
                    ble_scanner_enabled=True,
                    rssi=n_int(-60, 2.5, -80, -40),
                ),
                expected_state=exp_state,
                expected_ac_on=True,
                expected_light_on=True,
                in_transition=trans,
            )
        )
    scenarios.append(
        SimulatedScenario(
            name="sitting_still_studying_ble",
            description="Student sits still at desk 15 min with light ON & phone nearby",
            use_demo_config=False,
            simulate_servo_auto_off=False,
            steps=steps4,
        )
    )

    # 5. Brief door check (delivery), stays inside
    steps5: List[SimulatedStep] = []
    for i in range(48):  # 4 minutes
        t = i * step_ms
        dist = n_float(28.0, 2.0) if t == 60_000 else n_float(85.0, 1.5)
        pir = 1 if (t < 60_000 or t >= 85_000) else 0
        steps5.append(
            SimulatedStep(
                sensor=SensorInput(
                    timestamp_ms=t,
                    temp_c=25.0,
                    hum_pct=57.0,
                    vib=1,
                    dist_cm=round(dist, 1),
                    pir=pir,
                    light_raw=2500,
                    ble_scanner_enabled=False,
                ),
                expected_state=RoomState.AWAKE,
                expected_ac_on=True,
                expected_light_on=True,
                in_transition=(60_000 <= t <= 90_000),
            )
        )
    scenarios.append(
        SimulatedScenario(
            name="brief_door_check",
            description="Door opens briefly then person moves inside again -> stays AWAKE",
            use_demo_config=False,
            simulate_servo_auto_off=False,
            steps=steps5,
        )
    )

    # 6. Leaves then returns before auto-off confirmation
    steps6: List[SimulatedStep] = []
    for i in range(72):  # 6 minutes
        t = i * step_ms
        if t < 40_000:
            pir, dist, rssi, exp, trans = 1, 85.0, -63, RoomState.AWAKE, False
        elif t == 40_000:
            pir, dist, rssi, exp, trans = 1, 30.0, -76, RoomState.AWAKE, True
        elif t < 180_000:
            pir, dist = 0, 85.0
            rssi = -127 if t > 65_000 else -94
            exp = RoomState.ABSENT
            trans = t < 110_000
        elif t == 180_000:
            # Returns through doorway!
            pir, dist, rssi, exp, trans = 1, 32.0, -70, RoomState.AWAKE, True
        else:
            pir, dist, rssi, exp, trans = 1, 85.0, -61, RoomState.AWAKE, False

        steps6.append(
            SimulatedStep(
                sensor=SensorInput(
                    timestamp_ms=t,
                    temp_c=24.8,
                    hum_pct=56.0,
                    vib=1,
                    dist_cm=round(n_float(dist, 1.0), 1),
                    pir=pir,
                    light_raw=2550,
                    ble_scanner_enabled=True,
                    rssi=rssi,
                ),
                expected_state=exp,
                expected_ac_on=True,
                expected_light_on=True,
                in_transition=trans,
            )
        )
    scenarios.append(
        SimulatedScenario(
            name="return_before_auto_off",
            description="Student returns within 2 min before auto-off fires -> cancels ABSENT",
            use_demo_config=False,
            simulate_servo_auto_off=False,
            steps=steps6,
        )
    )

    # 7. Demo Mode fast showcase (BH1750 lux sensor variant)
    steps7: List[SimulatedStep] = []
    for i in range(28):  # 140s
        t = i * step_ms
        if t < 20_000:
            pir, dist, rssi, exp, trans = 1, 85.0, -58, RoomState.AWAKE, False
        elif t == 20_000:
            pir, dist, rssi, exp, trans = 1, 30.0, -74, RoomState.AWAKE, True
        else:
            pir, dist = 0, 85.0
            rssi = -127 if t > 30_000 else -95
            exp = RoomState.ABSENT
            trans = t < 45_000

        steps7.append(
            SimulatedStep(
                sensor=SensorInput(
                    timestamp_ms=t,
                    temp_c=25.0,
                    hum_pct=55.0,
                    vib=1,
                    dist_cm=round(n_float(dist, 1.0), 1),
                    pir=pir,
                    light_raw=-1,
                    lux=320.0,
                    ble_scanner_enabled=True,
                    rssi=rssi,
                ),
                expected_state=exp,
                expected_ac_on=True,
                expected_light_on=True,
                in_transition=trans,
            )
        )
    scenarios.append(
        SimulatedScenario(
            name="demo_fast_bh1750",
            description="Demo preset (30s auto-off) using BH1750 lux instead of LDR",
            use_demo_config=True,
            simulate_servo_auto_off=True,
            steps=steps7,
        )
    )

    # 8. PIR warmup + sensor dropouts + stray vibration spike when AC is OFF
    steps8: List[SimulatedStep] = []
    for i in range(36):  # 3 minutes
        t = i * step_ms
        pir = -1 if t < 60_000 else 1
        if i in (5, 14):
            dist = -1.0
        elif i == 22:
            dist = 230.0  # Stray upward ultrasonic bounce; SHORTER_ONLY must ignore
        else:
            dist = n_float(85.0, 1.0)
        temp = float("nan") if i in (7, 19) else 28.5
        hum = float("nan") if i in (7, 19) else 65.0
        vib = 1 if i == 10 else 0  # Single stray knock on wall at t=50s
        steps8.append(
            SimulatedStep(
                sensor=SensorInput(
                    timestamp_ms=t,
                    temp_c=temp,
                    hum_pct=hum,
                    vib=vib,
                    dist_cm=round(dist, 1) if dist > 0 else -1.0,
                    pir=pir,
                    light_raw=300,
                    ble_scanner_enabled=True,
                    rssi=-127 if t < 60_000 else -64,
                ),
                expected_state=RoomState.AWAKE,
                expected_ac_on=False,
                expected_light_on=False,
                in_transition=False,
            )
        )
    scenarios.append(
        SimulatedScenario(
            name="warmup_faults_and_vib_spike",
            description="60s PIR warmup, DHT/HC-SR04 dropouts, single false vibration spike",
            use_demo_config=False,
            simulate_servo_auto_off=False,
            steps=steps8,
        )
    )

    # 9. Booted while already inside & goes to sleep without touching door, no BLE
    steps9: List[SimulatedStep] = []
    for i in range(240):  # 20 minutes
        t = i * step_ms
        if t < 60_000:
            pir = 1
            light_raw = n_int(2100, 30, 0, 4095)
            exp_state = RoomState.AWAKE
            exp_light = True
            trans = False
        else:
            pir = 0
            light_raw = n_int(120, 20, 0, 4095)
            exp_state = RoomState.SLEEPING
            exp_light = False
            trans = t < 245_000
        steps9.append(
            SimulatedStep(
                sensor=SensorInput(
                    timestamp_ms=t,
                    temp_c=round(n_float(24.2, 0.2), 1),
                    hum_pct=round(n_float(55.0, 0.5), 1),
                    vib=1,
                    dist_cm=round(n_float(90.0, 1.5), 1),
                    pir=pir,
                    light_raw=light_raw,
                    ble_scanner_enabled=False,
                ),
                expected_state=exp_state,
                expected_ac_on=True,
                expected_light_on=exp_light,
                in_transition=trans,
            )
        )
    scenarios.append(
        SimulatedScenario(
            name="sleep_without_prior_door_event",
            description="System boots while resident is inside, then sleeps (no door event, no BLE)",
            use_demo_config=False,
            simulate_servo_auto_off=False,
            steps=steps9,
        )
    )

    return scenarios
