#include "AirtomicRuleAI.h"

#include <algorithm>

namespace airtomic_ai {

namespace {

enum class BlePresence : uint8_t {
  UNKNOWN = 0,
  NEAR = 1,
  FAR_OR_LOST = 2,
};

float clampf(float v, float lo, float hi) {
  return (v < lo) ? lo : ((v > hi) ? hi : v);
}

void computeSoftmaxConfidence(float sAwake, float sSleep, float sAbsent, float temp,
                              float absentMargin, RoomState &outState, float &outConf) {
  const float invT = (temp > 0.05f) ? (1.0f / temp) : 1.0f;
  const float m = std::max(sAwake, std::max(sSleep, sAbsent));

  const float eAwake = std::exp((sAwake - m) * invT);
  const float eSleep = std::exp((sSleep - m) * invT);
  const float eAbsent = std::exp((sAbsent - m) * invT);
  const float sum = eAwake + eSleep + eAbsent;

  const float pAwake = eAwake / sum;
  const float pSleep = eSleep / sum;
  const float pAbsent = eAbsent / sum;

  // Tie-breaker favors human presence (AWAKE > SLEEPING > ABSENT) for safety.
  if (sAbsent > sAwake && sAbsent > sSleep &&
      (sAbsent - std::max(sAwake, sSleep)) >= absentMargin) {
    outState = RoomState::ABSENT;
    outConf = pAbsent;
  } else if (sSleep > sAwake) {
    outState = RoomState::SLEEPING;
    outConf = pSleep;
  } else {
    outState = RoomState::AWAKE;
    outConf = pAwake;
  }

  outConf = clampf(outConf, 0.0f, 0.99f);
}

} // namespace

const char *roomStateToString(RoomState state) {
  switch (state) {
    case RoomState::AWAKE:
      return "AWAKE";
    case RoomState::SLEEPING:
      return "SLEEPING";
    case RoomState::ABSENT:
      return "ABSENT";
  }
  return "AWAKE";
}

const char *verificationStatusToString(VerificationStatus status) {
  switch (status) {
    case VerificationStatus::NONE:
      return "NONE";
    case VerificationStatus::PENDING:
      return "PENDING";
    case VerificationStatus::VERIFIED_OFF:
      return "VERIFIED_OFF";
    case VerificationStatus::RETRY_NEEDED:
      return "RETRY_NEEDED";
    case VerificationStatus::FAILED_MAX_RETRIES:
      return "FAILED_MAX_RETRIES";
  }
  return "NONE";
}

AirtomicRuleEngine::AirtomicRuleEngine(const RuleAIConfig &config) : cfg_(config) {
  reset();
}

void AirtomicRuleEngine::reset() {
  initialized_ = false;
  firstSeenMs_ = 0;
  lastSampleMs_ = 0;

  acOn_ = false;
  lightOn_ = false;
  std::fill(vibHistory_, vibHistory_ + kMaxVibHistory, 0);
  vibWriteIdx_ = 0;
  vibSampleCount_ = 0;
  hasLastVibOne_ = false;
  lastVibOneMs_ = 0;

  hasPrevTemp_ = false;
  prevTempC_ = NAN;

  hasLastLightToggle_ = false;
  lastHumanLightToggleMs_ = 0;
  lastLightToggleWasOn_ = false;
  suppressLightToggleUntilMsValid_ = false;
  suppressLightToggleUntilMs_ = 0;

  hasEverSeenMotion_ = false;
  lastMotionMs_ = 0;
  pirReady_ = false;
  pirReadyAtMs_ = 0;

  doorBaselineCm_ = (cfg_.doorBaselineCm > 0.0f) ? cfg_.doorBaselineCm : -1.0f;
  doorInitCount_ = 0;
  doorInitSamples_[0] = 0.0f;
  doorInitSamples_[1] = 0.0f;
  doorInitSamples_[2] = 0.0f;
  doorHigherCount_ = 0;
  doorHigherSamples_[0] = 0.0f;
  doorHigherSamples_[1] = 0.0f;
  doorHigherSamples_[2] = 0.0f;
  doorCurrentlyDeviated_ = false;
  doorDeviationStartMs_ = 0;
  hasDoorEvent_ = false;
  lastDoorEventMs_ = 0;
  motionConfirmedAfterDoor_ = true;

  hasBleReading_ = false;
  hasSeenBleNear_ = false;
  smoothedRssi_ = -127.0f;
  lastBleSeenNearMs_ = 0;
  bleEnabledSinceMs_ = 0;
  bleWasEnabled_ = false;

  inAbsentState_ = false;
  absentStartedMs_ = 0;
  alertSentForCurrentAbsence_ = false;

  verification_ = VerificationStatus::NONE;
  verifyTargetAc_ = false;
  verifyTargetLight_ = false;
  lastAutoOffAttemptMs_ = 0;
  hasAutoOffAttempt_ = false;
  autoOffRetryCount_ = 0;
}

void AirtomicRuleEngine::setConfig(const RuleAIConfig &config) {
  cfg_ = config;
  if (cfg_.doorBaselineCm > 0.0f) {
    doorBaselineCm_ = cfg_.doorBaselineCm;
  }
}

void AirtomicRuleEngine::notifyAutoOffTriggered(bool targetedAc, bool targetedLight,
                                                uint64_t timestampMs) {
  if (!targetedAc && !targetedLight) {
    return;
  }
  hasAutoOffAttempt_ = true;
  lastAutoOffAttemptMs_ = timestampMs;
  verifyTargetAc_ = targetedAc;
  verifyTargetLight_ = targetedLight;
  verification_ = VerificationStatus::PENDING;
  if (autoOffRetryCount_ < 255) {
    ++autoOffRetryCount_;
  }
  if (targetedLight) {
    suppressLightToggleUntilMsValid_ = true;
    suppressLightToggleUntilMs_ = timestampMs + cfg_.lightHumanToggleWindowMs;
  }
}

void AirtomicRuleEngine::updateAppliances(const SensorInput &input) {
  const uint64_t nowMs = input.timestampMs;

  // 1. Light ON/OFF with hysteresis (BH1750 lux preferred if valid, else LDR lightRaw)
  const bool prevLightOn = lightOn_;
  bool lightEvaluated = false;
  if (input.lux >= 0.0f && std::isfinite(input.lux)) {
    lightEvaluated = true;
    if (!lightOn_ && input.lux >= cfg_.luxOnThreshold) {
      lightOn_ = true;
    } else if (lightOn_ && input.lux <= cfg_.luxOffThreshold) {
      lightOn_ = false;
    }
  } else if (input.lightRaw >= 0 && input.lightRaw <= 4095) {
    lightEvaluated = true;
    const int effectiveRaw = cfg_.lightInverted ? (4095 - input.lightRaw) : input.lightRaw;
    if (!lightOn_ && effectiveRaw >= cfg_.lightRawOnThreshold) {
      lightOn_ = true;
    } else if (lightOn_ && effectiveRaw <= cfg_.lightRawOffThreshold) {
      lightOn_ = false;
    }
  }

  if (lightEvaluated && initialized_ && lightOn_ != prevLightOn) {
    const bool suppressed =
        suppressLightToggleUntilMsValid_ && (nowMs <= suppressLightToggleUntilMs_);
    if (!suppressed) {
      hasLastLightToggle_ = true;
      lastHumanLightToggleMs_ = nowMs;
      lastLightToggleWasOn_ = lightOn_;
    }
  }

  // 2. AC ON/OFF via debounced SW-420/KY-002 vibration window
  if (input.vib == 0 || input.vib == 1) {
    const uint8_t window =
        std::max<uint8_t>(1, std::min<uint8_t>(cfg_.vibWindowSize, kMaxVibHistory));
    vibHistory_[vibWriteIdx_] = static_cast<uint8_t>(input.vib);
    vibWriteIdx_ = static_cast<uint8_t>((vibWriteIdx_ + 1) % window);
    if (vibSampleCount_ < window) {
      ++vibSampleCount_;
    }

    uint8_t hits = 0;
    for (uint8_t i = 0; i < vibSampleCount_; ++i) {
      hits = static_cast<uint8_t>(hits + vibHistory_[i]);
    }

    if (input.vib == 1) {
      hasLastVibOne_ = true;
      lastVibOneMs_ = nowMs;
    }

    const uint8_t requiredHits = std::max<uint8_t>(1, std::min<uint8_t>(cfg_.vibOnMinHits, window));
    if (!acOn_ && hits >= requiredHits) {
      acOn_ = true;
    } else if (acOn_) {
      const bool timedOut =
          !hasLastVibOne_ || ((nowMs - lastVibOneMs_) >= cfg_.acVibOffGraceMs);
      if (timedOut && input.vib == 0) {
        acOn_ = false;
      }
    }
  }
}

void AirtomicRuleEngine::updateDoor(const SensorInput &input) {
  const uint64_t nowMs = input.timestampMs;
  const bool distValid =
      std::isfinite(input.distCm) && input.distCm >= 2.0f && input.distCm <= 400.0f;

  if (!distValid) {
    return;
  }

  if (doorBaselineCm_ <= 0.0f) {
    doorInitSamples_[doorInitCount_++] = input.distCm;
    if (doorInitCount_ >= 3) {
      const float a = doorInitSamples_[0];
      const float b = doorInitSamples_[1];
      const float c = doorInitSamples_[2];
      const float lo = std::min(a, std::min(b, c));
      const float hi = std::max(a, std::max(b, c));
      doorBaselineCm_ = (a + b + c) - lo - hi;
    }
    return;
  }

  const float drop = doorBaselineCm_ - input.distCm;
  const float absDelta = std::fabs(drop);
  const bool triggered = (cfg_.doorTriggerMode == DoorTriggerMode::SHORTER_ONLY)
                             ? (drop >= cfg_.doorTriggerDeltaCm)
                             : (absDelta >= cfg_.doorTriggerDeltaCm);

  if (triggered) {
    doorHigherCount_ = 0;
    if (!doorCurrentlyDeviated_) {
      doorCurrentlyDeviated_ = true;
      doorDeviationStartMs_ = nowMs;
      hasDoorEvent_ = true;
      lastDoorEventMs_ = nowMs;
      motionConfirmedAfterDoor_ = false;
    }
  } else {
    doorCurrentlyDeviated_ = false;
    if (cfg_.doorBaselineCm <= 0.0f) {
      if (absDelta <= cfg_.doorNoiseMarginCm) {
        doorHigherCount_ = 0;
        doorBaselineCm_ = 0.05f * input.distCm + 0.95f * doorBaselineCm_;
      } else if (cfg_.doorTriggerMode == DoorTriggerMode::SHORTER_ONLY &&
                 (input.distCm - doorBaselineCm_) > cfg_.doorNoiseMarginCm) {
        // Recover if initial baseline was calibrated while doorway was temporarily blocked.
        doorHigherSamples_[doorHigherCount_++] = input.distCm;
        if (doorHigherCount_ >= 3) {
          const float a = doorHigherSamples_[0];
          const float b = doorHigherSamples_[1];
          const float c = doorHigherSamples_[2];
          const float lo = std::min(a, std::min(b, c));
          const float hi = std::max(a, std::max(b, c));
          doorBaselineCm_ = (a + b + c) - lo - hi;
          doorHigherCount_ = 0;
        }
      } else {
        doorHigherCount_ = 0;
      }
    }
  }
}

void AirtomicRuleEngine::updateMotion(const SensorInput &input) {
  const uint64_t nowMs = input.timestampMs;
  if (input.pir < 0) {
    pirReady_ = false;
    return;
  }

  if (!pirReady_) {
    pirReady_ = true;
    pirReadyAtMs_ = nowMs;
  }

  if (input.pir == 1) {
    hasEverSeenMotion_ = true;
    lastMotionMs_ = nowMs;
    if (!hasDoorEvent_ || (nowMs - lastDoorEventMs_) > cfg_.doorExitGraceMs) {
      motionConfirmedAfterDoor_ = true;
    }
  }
}

void AirtomicRuleEngine::updateBle(const SensorInput &input) {
  const uint64_t nowMs = input.timestampMs;
  if (!input.bleScannerEnabled) {
    bleWasEnabled_ = false;
    return;
  }

  if (!bleWasEnabled_) {
    bleWasEnabled_ = true;
    bleEnabledSinceMs_ = nowMs;
  }

  if (input.rssi > -127 && input.rssi <= -5) {
    const float rssiF = static_cast<float>(input.rssi);
    if (!hasBleReading_) {
      smoothedRssi_ = rssiF;
      hasBleReading_ = true;
    } else {
      const float a = clampf(cfg_.bleEmaAlpha, 0.05f, 1.0f);
      smoothedRssi_ = a * rssiF + (1.0f - a) * smoothedRssi_;
    }

    if (smoothedRssi_ >= static_cast<float>(cfg_.blePresentThresholdDbm)) {
      hasSeenBleNear_ = true;
      lastBleSeenNearMs_ = nowMs;
    }
  }
}

void AirtomicRuleEngine::updateVerificationAndActions(const SensorInput &input, RoomDecision &out) {
  const uint64_t nowMs = input.timestampMs;

  if (out.state == RoomState::ABSENT) {
    if (!inAbsentState_) {
      inAbsentState_ = true;
      absentStartedMs_ = nowMs;
      alertSentForCurrentAbsence_ = false;
    }
    out.absentDurationMs = static_cast<uint32_t>(nowMs - absentStartedMs_);
  } else {
    inAbsentState_ = false;
    out.absentDurationMs = 0;
    alertSentForCurrentAbsence_ = false;
    if (verification_ == VerificationStatus::PENDING ||
        verification_ == VerificationStatus::RETRY_NEEDED) {
      verification_ = VerificationStatus::NONE;
    }
    autoOffRetryCount_ = 0;
  }

  if (verification_ == VerificationStatus::PENDING && hasAutoOffAttempt_) {
    const bool acCleared = !verifyTargetAc_ || !acOn_;
    const bool lightCleared = !verifyTargetLight_ || !lightOn_;
    if (acCleared && lightCleared) {
      verification_ = VerificationStatus::VERIFIED_OFF;
    } else if ((nowMs - lastAutoOffAttemptMs_) >= cfg_.verifyWaitMs) {
      if (autoOffRetryCount_ < cfg_.maxAutoOffRetries) {
        verification_ = VerificationStatus::RETRY_NEEDED;
      } else {
        verification_ = VerificationStatus::FAILED_MAX_RETRIES;
      }
    }
  }

  out.verification = verification_;

  const bool safeToAct =
      (out.state == RoomState::ABSENT) && out.wastingEnergy &&
      (out.confidence >= cfg_.minAutoOffConf) && (input.pir >= 0);

  if (!safeToAct) {
    return;
  }

  if (!alertSentForCurrentAbsence_ && out.absentDurationMs >= cfg_.alertDelayMs) {
    out.shouldAlert = true;
    alertSentForCurrentAbsence_ = true;
  }

  const bool blockedByStatus = (verification_ == VerificationStatus::PENDING) ||
                               (verification_ == VerificationStatus::FAILED_MAX_RETRIES);
  const bool cooldownOk =
      !hasAutoOffAttempt_ || ((nowMs - lastAutoOffAttemptMs_) >= cfg_.autoOffCooldownMs);

  if (!blockedByStatus && cooldownOk && out.absentDurationMs >= cfg_.absentConfirmMs) {
    out.shouldTurnOffAc = acOn_;
    out.shouldTurnOffLight = lightOn_;
  }
}

RoomDecision AirtomicRuleEngine::update(const SensorInput &input) {
  const uint64_t nowMs = input.timestampMs;
  if (!initialized_) {
    firstSeenMs_ = nowMs;
  }

  updateAppliances(input);
  updateDoor(input);
  updateMotion(input);
  updateBle(input);

  initialized_ = true;
  lastSampleMs_ = nowMs;

  RoomDecision out;
  out.acOn = acOn_;
  out.lightOn = lightOn_;
  out.doorBaselineCm = doorBaselineCm_;
  out.smoothedRssi = smoothedRssi_;

  if (doorCurrentlyDeviated_ && (nowMs - doorDeviationStartMs_) >= cfg_.doorAjarTimeoutMs) {
    out.doorOpenTooLong = true;
    out.activeRules |= RULE_DOOR_OPEN_TOO_LONG;
  }

  // Compute BLE presence sub-state
  BlePresence blePresence = BlePresence::UNKNOWN;
  if (input.bleScannerEnabled) {
    if (hasSeenBleNear_ && (nowMs - lastBleSeenNearMs_) <= cfg_.bleLostTimeoutMs &&
        smoothedRssi_ > static_cast<float>(cfg_.bleAbsentThresholdDbm)) {
      blePresence = BlePresence::NEAR;
    } else if ((hasSeenBleNear_ && (nowMs - lastBleSeenNearMs_) > cfg_.bleLostTimeoutMs) ||
               (!hasSeenBleNear_ && (nowMs - bleEnabledSinceMs_) >= cfg_.bleLostTimeoutMs)) {
      blePresence = BlePresence::FAR_OR_LOST;
    }
  }

  // Compute time since motion
  uint64_t timeSinceMotionMs = 0;
  if (pirReady_) {
    const uint64_t refMs = hasEverSeenMotion_ ? lastMotionMs_ : pirReadyAtMs_;
    timeSinceMotionMs = (nowMs >= refMs) ? (nowMs - refMs) : 0;
  }

  float sAwake = 0.0f;
  float sSleep = 0.0f;
  float sAbsent = 0.0f;

  // Rule 1: PIR Motion / Stillness
  if (!pirReady_ || input.pir < 0) {
    sAwake += 0.5f;
    out.activeRules |= RULE_PIR_WARMUP;
  } else if (input.pir == 1 || timeSinceMotionMs <= cfg_.awakeHoldMs) {
    sAwake += 4.0f;
    out.activeRules |= RULE_PIR_ACTIVE;
  } else if (timeSinceMotionMs <= cfg_.stillTransitionMs) {
    sAwake += 1.5f;
    sSleep += 1.2f;
    out.activeRules |= RULE_PIR_RECENT_STILL;
  } else {
    sSleep += 1.5f;
    sAbsent += 1.5f;
    out.activeRules |= RULE_PIR_PROLONGED_STILL;
  }

  // Rule 2: Doorway (HC-SR04) + Post-door motion
  if (hasDoorEvent_ && (nowMs - lastDoorEventMs_) <= cfg_.doorExitGraceMs) {
    out.activeRules |= RULE_DOOR_EVENT_DETECTED;
  }

  const bool likelyLeftViaDoor =
      hasDoorEvent_ && !motionConfirmedAfterDoor_ &&
      ((nowMs - lastDoorEventMs_) >= cfg_.doorExitGraceMs) && (input.pir == 0);

  if (likelyLeftViaDoor) {
    sAbsent += 3.2f;
    out.activeRules |= RULE_DOOR_EXIT_UNCONFIRMED;
  } else if (hasDoorEvent_ && motionConfirmedAfterDoor_) {
    out.activeRules |= RULE_MOTION_AFTER_DOOR;
    if (pirReady_ && timeSinceMotionMs > cfg_.stillTransitionMs) {
      sSleep += 2.6f;
    } else if (pirReady_ && input.pir == 0) {
      sAwake += 0.8f;
    }
  } else if (!hasDoorEvent_ && pirReady_ && timeSinceMotionMs > cfg_.noDoorLongIdleMs) {
    out.activeRules |= RULE_NO_DOOR_LONG_IDLE;
    if (blePresence == BlePresence::FAR_OR_LOST) {
      sAbsent += 1.8f;
    } else {
      sSleep += 1.5f;
    }
  }

  // Rule 3: BLE Phone RSSI (optional)
  if (blePresence == BlePresence::NEAR) {
    out.activeRules |= RULE_BLE_NEAR;
    if (!pirReady_ || timeSinceMotionMs <= cfg_.stillTransitionMs) {
      sAwake += 1.8f;
    } else {
      sSleep += 2.5f;
    }
  } else if (blePresence == BlePresence::FAR_OR_LOST && pirReady_ && input.pir >= 0) {
    out.activeRules |= RULE_BLE_FAR_OR_LOST;
    sAbsent += 2.8f;
  }

  // Rule 4: Light context + Recent manual light toggle
  const bool hasLightSensor =
      (input.lux >= 0.0f && std::isfinite(input.lux)) ||
      (input.lightRaw >= 0 && input.lightRaw <= 4095);

  if (hasLightSensor && pirReady_ && timeSinceMotionMs > cfg_.stillTransitionMs) {
    if (!lightOn_ && !likelyLeftViaDoor && blePresence != BlePresence::FAR_OR_LOST) {
      sSleep += 1.8f;
      out.activeRules |= RULE_LIGHT_DARK_INSIDE;
    } else if (lightOn_) {
      out.activeRules |= RULE_LIGHT_ON_WHILE_IDLE;
      if (likelyLeftViaDoor || blePresence == BlePresence::FAR_OR_LOST) {
        sAbsent += 1.2f;
      } else {
        sSleep += 1.0f; // Sitting still studying/reading with light on
      }
    }
  }

  if (hasLastLightToggle_ &&
      (nowMs - lastHumanLightToggleMs_) <= cfg_.lightHumanToggleWindowMs) {
    out.activeRules |= RULE_LIGHT_HUMAN_TOGGLED;
    if (lastLightToggleWasOn_) {
      sAwake += 2.0f;
    } else {
      sAwake += 0.5f;
      sSleep += 1.0f;
    }
  }

  // Rule 5: AC & Temp contextual assist (optional slight tie-break)
  if (std::isfinite(input.tempC)) {
    if (hasPrevTemp_ && acOn_ && (prevTempC_ - input.tempC) >= 0.5f) {
      out.activeRules |= RULE_TEMP_AC_COOLING;
    }
    prevTempC_ = input.tempC;
    hasPrevTemp_ = true;
  }

  if (!acOn_ && !lightOn_ && pirReady_ && timeSinceMotionMs > cfg_.stillTransitionMs) {
    sAbsent += 0.4f;
  }

  out.scoreAwake = sAwake;
  out.scoreSleeping = sSleep;
  out.scoreAbsent = sAbsent;

  computeSoftmaxConfidence(sAwake, sSleep, sAbsent, cfg_.softmaxTemp,
                           cfg_.absentScoreMargin, out.state, out.confidence);

  out.wastingEnergy = (out.state == RoomState::ABSENT) && (out.acOn || out.lightOn);

  updateVerificationAndActions(input, out);
  return out;
}

} // namespace airtomic_ai
