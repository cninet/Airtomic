#pragma once

#include <cmath>
#include <cstdint>

namespace airtomic_ai {

enum class RoomState : uint8_t {
  AWAKE = 0,
  SLEEPING = 1,
  ABSENT = 2,
};

enum class DoorTriggerMode : uint8_t {
  // Mounted on wall beside doorway: full doorway = baseline; door open or person walking makes dist shorter.
  SHORTER_ONLY = 0,
  // Mounted facing door leaf where opening can make dist either shorter or longer.
  BOTH_DIRECTIONS = 1,
};

enum class VerificationStatus : uint8_t {
  NONE = 0,
  PENDING = 1,
  VERIFIED_OFF = 2,
  RETRY_NEEDED = 3,
  FAILED_MAX_RETRIES = 4,
};

enum RuleFlag : uint32_t {
  RULE_NONE = 0,
  RULE_PIR_WARMUP = 1u << 0,
  RULE_PIR_ACTIVE = 1u << 1,
  RULE_PIR_RECENT_STILL = 1u << 2,
  RULE_PIR_PROLONGED_STILL = 1u << 3,
  RULE_DOOR_EVENT_DETECTED = 1u << 4,
  RULE_DOOR_EXIT_UNCONFIRMED = 1u << 5,
  RULE_MOTION_AFTER_DOOR = 1u << 6,
  RULE_NO_DOOR_LONG_IDLE = 1u << 7,
  RULE_DOOR_OPEN_TOO_LONG = 1u << 8,
  RULE_BLE_NEAR = 1u << 9,
  RULE_BLE_FAR_OR_LOST = 1u << 10,
  RULE_LIGHT_DARK_INSIDE = 1u << 11,
  RULE_LIGHT_ON_WHILE_IDLE = 1u << 12,
  RULE_LIGHT_HUMAN_TOGGLED = 1u << 13,
  RULE_TEMP_AC_COOLING = 1u << 14,
};

struct SensorInput {
  uint64_t timestampMs = 0;       // Monotonic ms (e.g. esp_timer_get_time() / 1000ULL)
  float tempC = NAN;              // DHT11/DHT22 °C, NAN if invalid
  float humPct = NAN;             // DHT11/DHT22 %RH, NAN if invalid
  int8_t vib = -1;                // SW-420/KY-002: 1=vibration, 0=none, -1=invalid
  float distCm = -1.0f;           // HC-SR04 distance in cm, <0 if no echo/invalid
  int8_t pir = -1;                // HC-SR501: -1=warmup/invalid, 0=idle, 1=motion
  int16_t lightRaw = -1;          // LDR ADC 0..4095, -1 if unavailable
  float lux = -1.0f;              // BH1750 lux >= 0, <0 if unavailable
  bool bleScannerEnabled = false; // True when Gateway BLE scanner is active
  int16_t rssi = -127;            // BLE RSSI dBm (-100..-20 valid; <= -127 not detected)
};

struct RuleAIConfig {
  // Light detection (supports either BH1750 lux or LDR lightRaw)
  bool lightInverted = false;        // False if higher ADC = brighter (airtomic/README.md)
  int16_t lightRawOnThreshold = 1800;
  int16_t lightRawOffThreshold = 1400;
  float luxOnThreshold = 60.0f;
  float luxOffThreshold = 35.0f;

  // AC detection via debounced SW-420 vibration (+ optional DHT cooling cue)
  uint8_t vibWindowSize = 4;         // Sliding window size (4 * 5s = 20s)
  uint8_t vibOnMinHits = 2;          // Minimum 1s inside window to turn AC -> ON
  uint32_t acVibOffGraceMs = 15000;  // Continuous vib=0 time before AC -> OFF

  // HC-SR04 doorway baseline & event detection
  DoorTriggerMode doorTriggerMode = DoorTriggerMode::SHORTER_ONLY;
  float doorBaselineCm = -1.0f;      // <=0 enables auto-calibration from steady readings
  float doorTriggerDeltaCm = 20.0f;  // Delta from baseline indicating doorway crossing/open
  float doorNoiseMarginCm = 8.0f;    // Stable margin for slow baseline EMA adaptation
  uint32_t doorExitGraceMs = 15000;  // Post-door grace for trailing PIR hold time
  uint32_t doorAjarTimeoutMs = 120000;

  // BLE RSSI filtering (used only when bleScannerEnabled == true)
  float bleEmaAlpha = 0.4f;
  int16_t blePresentThresholdDbm = -82;
  int16_t bleAbsentThresholdDbm = -92;
  uint32_t bleLostTimeoutMs = 45000;

  // Temporal human-presence windows
  uint32_t awakeHoldMs = 60000;          // PIR=1 keeps AWAKE strongly supported
  uint32_t stillTransitionMs = 180000;   // 3 min: transition zone before prolonged stillness
  uint32_t noDoorLongIdleMs = 900000;    // 15 min idle with no door event seen since boot
  uint32_t darkAbsentAfterDoorMs = 90000;
  uint32_t lightHumanToggleWindowMs = 45000;

  // Softmax confidence & ABSENT confirmation / safety gating
  float softmaxTemp = 2.0f;
  float absentScoreMargin = 0.8f;    // Require clear lead over AWAKE/SLEEPING to declare ABSENT
  float minAutoOffConf = 0.75f;
  uint32_t alertDelayMs = 60000;     // Alert LINE/Discord after 60s of ABSENT + wasting energy
  uint32_t absentConfirmMs = 180000; // Auto-off Servo after 3 min of continuous ABSENT
  uint32_t verifyWaitMs = 20000;     // Wait up to 20s after Servo press to verify off
  uint32_t autoOffCooldownMs = 60000;
  uint8_t maxAutoOffRetries = 2;

  static RuleAIConfig roomDefault() { return RuleAIConfig{}; }

  // Fast timings for live demo / testing (without waiting 3+ minutes)
  static RuleAIConfig demoFast() {
    RuleAIConfig c;
    c.doorExitGraceMs = 10000;
    c.doorAjarTimeoutMs = 30000;
    c.bleLostTimeoutMs = 15000;
    c.awakeHoldMs = 15000;
    c.stillTransitionMs = 25000;
    c.noDoorLongIdleMs = 60000;
    c.darkAbsentAfterDoorMs = 20000;
    c.lightHumanToggleWindowMs = 15000;
    c.alertDelayMs = 15000;
    c.absentConfirmMs = 30000;
    c.verifyWaitMs = 15000;
    c.autoOffCooldownMs = 20000;
    return c;
  }
};

struct RoomDecision {
  RoomState state = RoomState::AWAKE;
  float confidence = 0.0f; // 0.0 .. 0.99
  bool acOn = false;
  bool lightOn = false;
  bool wastingEnergy = false;
  bool doorOpenTooLong = false;
  bool shouldAlert = false;
  bool shouldTurnOffAc = false;
  bool shouldTurnOffLight = false;
  VerificationStatus verification = VerificationStatus::NONE;

  uint32_t absentDurationMs = 0;
  float scoreAwake = 0.0f;
  float scoreSleeping = 0.0f;
  float scoreAbsent = 0.0f;
  float doorBaselineCm = -1.0f;
  float smoothedRssi = -127.0f;
  uint32_t activeRules = RULE_NONE;
};

const char *roomStateToString(RoomState state);
const char *verificationStatusToString(VerificationStatus status);

class AirtomicRuleEngine {
 public:
  static constexpr uint8_t kMaxVibHistory = 16;

  explicit AirtomicRuleEngine(const RuleAIConfig &config = RuleAIConfig::roomDefault());

  void reset();
  void setConfig(const RuleAIConfig &config);
  const RuleAIConfig &config() const { return cfg_; }

  // Call for each new reading (every ~5s from SensorNode + Gateway local sensors).
  RoomDecision update(const SensorInput &input);

  // Inform the AI when the Gateway itself toggled a servo, so the resulting
  // light/AC change is not mistaken for a human toggling the switch, and start verification.
  void notifyAutoOffTriggered(bool targetedAc, bool targetedLight, uint64_t timestampMs);

 private:
  void updateAppliances(const SensorInput &input);
  void updateDoor(const SensorInput &input);
  void updateBle(const SensorInput &input);
  void updateMotion(const SensorInput &input);
  void updateVerificationAndActions(const SensorInput &input, RoomDecision &out);

  RuleAIConfig cfg_;
  bool initialized_ = false;
  uint64_t firstSeenMs_ = 0;
  uint64_t lastSampleMs_ = 0;

  // Appliance states
  bool acOn_ = false;
  bool lightOn_ = false;
  uint8_t vibHistory_[kMaxVibHistory] = {};
  uint8_t vibWriteIdx_ = 0;
  uint8_t vibSampleCount_ = 0;
  bool hasLastVibOne_ = false;
  uint64_t lastVibOneMs_ = 0;

  bool hasPrevTemp_ = false;
  float prevTempC_ = NAN;

  // Human light interaction tracking
  bool hasLastLightToggle_ = false;
  uint64_t lastHumanLightToggleMs_ = 0;
  bool lastLightToggleWasOn_ = false;
  bool suppressLightToggleUntilMsValid_ = false;
  uint64_t suppressLightToggleUntilMs_ = 0;

  // PIR motion tracking
  bool hasEverSeenMotion_ = false;
  uint64_t lastMotionMs_ = 0;
  bool pirReady_ = false;
  uint64_t pirReadyAtMs_ = 0;

  // Doorway (HC-SR04) tracking
  float doorBaselineCm_ = -1.0f;
  uint8_t doorInitCount_ = 0;
  float doorInitSamples_[3] = {};
  uint8_t doorHigherCount_ = 0;
  float doorHigherSamples_[3] = {};
  bool doorCurrentlyDeviated_ = false;
  uint64_t doorDeviationStartMs_ = 0;
  bool hasDoorEvent_ = false;
  uint64_t lastDoorEventMs_ = 0;
  bool motionConfirmedAfterDoor_ = true; // Safe default until an unconfirmed door event occurs

  // BLE tracking
  bool hasBleReading_ = false;
  bool hasSeenBleNear_ = false;
  float smoothedRssi_ = -127.0f;
  uint64_t lastBleSeenNearMs_ = 0;
  uint64_t bleEnabledSinceMs_ = 0;
  bool bleWasEnabled_ = false;

  // Temporal ABSENT & actuation verification
  bool inAbsentState_ = false;
  uint64_t absentStartedMs_ = 0;
  bool alertSentForCurrentAbsence_ = false;

  VerificationStatus verification_ = VerificationStatus::NONE;
  bool verifyTargetAc_ = false;
  bool verifyTargetLight_ = false;
  uint64_t lastAutoOffAttemptMs_ = 0;
  bool hasAutoOffAttempt_ = false;
  uint8_t autoOffRetryCount_ = 0;
};

} // namespace airtomic_ai
