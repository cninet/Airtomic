#include "AirtomicRuleAI.h"

#include <cassert>
#include <cmath>
#include <iostream>

using namespace airtomic_ai;

namespace {

SensorInput makeSample(uint64_t tMs, int8_t pir, float distCm, int8_t vib, int16_t lightRaw,
                       bool bleEnabled = false, int16_t rssi = -127, float tempC = 25.0f,
                       float humPct = 55.0f, float lux = -1.0f) {
  SensorInput s;
  s.timestampMs = tMs;
  s.tempC = tempC;
  s.humPct = humPct;
  s.vib = vib;
  s.distCm = distCm;
  s.pir = pir;
  s.lightRaw = lightRaw;
  s.lux = lux;
  s.bleScannerEnabled = bleEnabled;
  s.rssi = rssi;
  return s;
}

void testPirWarmupSafety() {
  AirtomicRuleEngine engine(RuleAIConfig::demoFast());
  for (uint64_t t = 0; t <= 60000; t += 5000) {
    RoomDecision d = engine.update(makeSample(t, -1, 85.0f, 1, 2400, true, -127));
    assert(d.state != RoomState::ABSENT);
    assert(!d.shouldTurnOffAc);
    assert(!d.shouldTurnOffLight);
    assert((d.activeRules & RULE_PIR_WARMUP) != 0);
  }
}

void testLeaveRoomForgotAcAndLightWithBle() {
  AirtomicRuleEngine engine(RuleAIConfig::roomDefault());
  // 0..120s: person awake inside with AC=ON, Light=ON, phone BLE near (-62 dBm)
  for (uint64_t t = 0; t <= 120000; t += 5000) {
    RoomDecision d = engine.update(makeSample(t, 1, 85.0f, 1, 2600, true, -62));
    assert(d.state == RoomState::AWAKE);
    assert(d.lightOn);
  }
  // At 125s: person walks through doorway (dist drops to 32 cm), trailing PIR=1
  RoomDecision dDoor = engine.update(makeSample(125000, 1, 32.0f, 1, 2600, true, -74));
  assert(dDoor.acOn);
  assert(dDoor.lightOn);

  // 130s..480s: door closes back to 85 cm, PIR=0, phone gone (-127), AC & Light left ON
  bool sawAbsent = false;
  bool sawAlert = false;
  bool sawAutoOff = false;
  bool verifiedOff = false;

  for (uint64_t t = 130000; t <= 480000; t += 5000) {
    int8_t vib = verifiedOff ? 0 : (sawAutoOff ? 0 : 1);
    int16_t light = verifiedOff ? 200 : (sawAutoOff ? 200 : 2600);
    // Keep AC/Light ON until engine triggers auto-off, then simulate servo turning them OFF
    if (sawAutoOff) {
      vib = 0;
      light = 200;
    }
    RoomDecision d = engine.update(makeSample(t, 0, 85.0f, vib, light, true, -127));
    if (d.state == RoomState::ABSENT && d.confidence >= 0.75f) {
      sawAbsent = true;
    }
    if (d.shouldAlert) {
      sawAlert = true;
    }
    if (!sawAutoOff && (d.shouldTurnOffAc || d.shouldTurnOffLight)) {
      sawAutoOff = true;
      assert(d.shouldTurnOffAc);
      assert(d.shouldTurnOffLight);
      engine.notifyAutoOffTriggered(d.shouldTurnOffAc, d.shouldTurnOffLight, t);
    }
    if (d.verification == VerificationStatus::VERIFIED_OFF) {
      verifiedOff = true;
    }
  }

  assert(sawAbsent);
  assert(sawAlert);
  assert(sawAutoOff);
  assert(verifiedOff);
}

void testLeaveRoomWithoutBle() {
  AirtomicRuleEngine engine(RuleAIConfig::roomDefault());
  for (uint64_t t = 0; t <= 60000; t += 5000) {
    engine.update(makeSample(t, 1, 90.0f, 1, 2500, false));
  }
  // Exit via door at 65s
  engine.update(makeSample(65000, 1, 30.0f, 1, 2500, false));
  // One trailing PIR pulse at 70s inside grace period
  engine.update(makeSample(70000, 1, 90.0f, 1, 2500, false));

  bool triggeredAutoOff = false;
  for (uint64_t t = 75000; t <= 500000; t += 5000) {
    RoomDecision d = engine.update(makeSample(t, 0, 90.0f, 1, 2500, false));
    if (d.shouldTurnOffAc && d.shouldTurnOffLight) {
      triggeredAutoOff = true;
      assert(d.state == RoomState::ABSENT);
      assert(d.confidence >= 0.75f);
      break;
    }
  }
  assert(triggeredAutoOff);
}

void testSleepingWithAcNeverTurnsOff() {
  AirtomicRuleEngine engine(RuleAIConfig::roomDefault());
  // Settle baseline (3 samples) then enter through door and stay awake 1 min
  engine.update(makeSample(0, 1, 90.0f, 1, 2400, false));
  engine.update(makeSample(5000, 1, 90.0f, 1, 2400, false));
  engine.update(makeSample(10000, 1, 90.0f, 1, 2400, false));
  engine.update(makeSample(15000, 1, 35.0f, 1, 2400, false)); // entered door
  for (uint64_t t = 20000; t <= 60000; t += 5000) {
    engine.update(makeSample(t, 1, 90.0f, 1, 2400, false));
  }
  // Turns off light to sleep, stays inside for 30 minutes with PIR=0, AC=ON
  for (uint64_t t = 65000; t <= 1865000; t += 5000) {
    RoomDecision d = engine.update(makeSample(t, 0, 90.0f, 1, 150, false));
    assert(d.state != RoomState::ABSENT);
    assert(!d.shouldAlert);
    assert(!d.shouldTurnOffAc);
    assert(!d.shouldTurnOffLight);
    if (t >= 300000) {
      assert(d.state == RoomState::SLEEPING);
      assert(d.confidence >= 0.75f);
    }
  }
}

void testSittingStillWithLightAndBle() {
  AirtomicRuleEngine engine(RuleAIConfig::roomDefault());
  for (uint64_t t = 0; t <= 60000; t += 5000) {
    engine.update(makeSample(t, 1, 88.0f, 1, 2600, true, -60));
  }
  // Sits quietly studying for 12 minutes, light ON, BLE near, no door exit
  for (uint64_t t = 65000; t <= 780000; t += 5000) {
    RoomDecision d = engine.update(makeSample(t, 0, 88.0f, 1, 2600, true, -63));
    assert(d.state != RoomState::ABSENT);
    assert(!d.shouldTurnOffAc);
    assert(!d.shouldTurnOffLight);
    if (t >= 300000) {
      assert(d.state == RoomState::SLEEPING);
    }
  }
}

void testBriefDoorCheckReturnsToAwake() {
  AirtomicRuleEngine engine(RuleAIConfig::demoFast());
  for (uint64_t t = 0; t <= 20000; t += 5000) {
    engine.update(makeSample(t, 1, 85.0f, 1, 2500, false));
  }
  // Door opens at 25s
  engine.update(makeSample(25000, 1, 28.0f, 1, 2500, false));
  // Person stays inside, moves again at 40s (after 10s doorExitGraceMs)
  engine.update(makeSample(30000, 0, 85.0f, 1, 2500, false));
  engine.update(makeSample(35000, 0, 85.0f, 1, 2500, false));
  RoomDecision dBack = engine.update(makeSample(40000, 1, 85.0f, 1, 2500, false));
  assert(dBack.state == RoomState::AWAKE);
  assert((dBack.activeRules & RULE_MOTION_AFTER_DOOR) != 0);
}

void testVibrationDebounceAndDoorAjar() {
  AirtomicRuleEngine engine(RuleAIConfig::demoFast());
  // Initialize baseline at 85 cm
  engine.update(makeSample(0, 1, 85.0f, 0, 200));
  engine.update(makeSample(5000, 1, 85.0f, 0, 200));
  engine.update(makeSample(10000, 1, 85.0f, 0, 200));

  // Single stray vibration spike -> AC should stay OFF
  RoomDecision dSpike = engine.update(makeSample(15000, 1, 85.0f, 1, 200));
  assert(!dSpike.acOn);

  // Single stray upward ultrasonic reflection (220cm > 85cm) must NOT trigger door event in SHORTER_ONLY
  RoomDecision dEcho = engine.update(makeSample(20000, 1, 220.0f, 0, 200));
  assert((dEcho.activeRules & RULE_DOOR_EVENT_DETECTED) == 0);

  // Sustained door open blocking doorway beam (35cm < 85cm) for >30s -> doorOpenTooLong
  bool ajarFlagged = false;
  for (uint64_t t = 25000; t <= 65000; t += 5000) {
    RoomDecision d = engine.update(makeSample(t, 1, 35.0f, 0, 200));
    if (d.doorOpenTooLong) {
      ajarFlagged = true;
    }
  }
  assert(ajarFlagged);
}

void testBlockedBootDoorBaselineRecovery() {
  AirtomicRuleEngine engine(RuleAIConfig::demoFast());
  // Person stands in doorway (35cm) during first 10s of boot, then steps away (90cm full door)
  engine.update(makeSample(0, 1, 35.0f, 0, 2200));
  engine.update(makeSample(5000, 1, 35.0f, 0, 2200));
  engine.update(makeSample(10000, 1, 90.0f, 0, 2200)); // median initially 35cm
  engine.update(makeSample(15000, 1, 90.0f, 0, 2200));
  engine.update(makeSample(20000, 1, 90.0f, 0, 2200));
  RoomDecision dRecovered = engine.update(makeSample(25000, 1, 90.0f, 0, 2200));
  assert(std::fabs(dRecovered.doorBaselineCm - 90.0f) < 1.0f);
  // Now walking past doorway (40cm) must trigger door event properly
  RoomDecision dWalk = engine.update(makeSample(30000, 1, 40.0f, 0, 2200));
  assert((dWalk.activeRules & RULE_DOOR_EVENT_DETECTED) != 0);
}

void testSensorFaultsGraceful() {
  AirtomicRuleEngine engine(RuleAIConfig::roomDefault());
  for (uint64_t t = 0; t <= 60000; t += 5000) {
    RoomDecision d = engine.update(makeSample(t, 1, -1.0f, -1, -1, false, -127, NAN, NAN, -1.0f));
    assert(std::isfinite(d.confidence));
    assert(d.confidence >= 0.0f && d.confidence <= 0.99f);
    assert(d.state == RoomState::AWAKE);
  }
}

void testRetryCapWhenServoFails() {
  AirtomicRuleEngine engine(RuleAIConfig::demoFast());
  for (uint64_t t = 0; t <= 20000; t += 5000) {
    engine.update(makeSample(t, 1, 85.0f, 1, 2600, true, -60));
  }
  engine.update(makeSample(25000, 1, 30.0f, 1, 2600, true, -75));

  int triggerCount = 0;
  bool reachedMaxRetries = false;
  for (uint64_t t = 30000; t <= 240000; t += 5000) {
    // Appliances stubbornly stay ON (e.g. servo misaligned)
    RoomDecision d = engine.update(makeSample(t, 0, 85.0f, 1, 2600, true, -127));
    if (d.shouldTurnOffAc || d.shouldTurnOffLight) {
      ++triggerCount;
      engine.notifyAutoOffTriggered(d.shouldTurnOffAc, d.shouldTurnOffLight, t);
    }
    if (d.verification == VerificationStatus::FAILED_MAX_RETRIES) {
      reachedMaxRetries = true;
    }
  }
  assert(triggerCount == 2); // maxAutoOffRetries == 2
  assert(reachedMaxRetries);
}

} // namespace

int main() {
  testPirWarmupSafety();
  testLeaveRoomForgotAcAndLightWithBle();
  testLeaveRoomWithoutBle();
  testSleepingWithAcNeverTurnsOff();
  testSittingStillWithLightAndBle();
  testBriefDoorCheckReturnsToAwake();
  testVibrationDebounceAndDoorAjar();
  testBlockedBootDoorBaselineRecovery();
  testSensorFaultsGraceful();
  testRetryCapWhenServoFails();

  std::cout << "All 10 C++ AirtomicRuleAI tests passed!\n";
  return 0;
}
