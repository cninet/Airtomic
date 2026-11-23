/*
 * Airtomic Simulated Sensor Nodes (SN02 & SN03) over ESP-NOW
 * ==========================================================
 * Purpose:
 *   - Satisfies Section II Scalability: runs 2 simulated sensor nodes (SN02, SN03)
 *     alongside 1 physical sensor node (SN01) simultaneously.
 *   - Uses the exact binary contract from airtomic/include/messages.h
 *     (NodeMsg = 26 bytes, AckMsg = 13 bytes, AIRTOMIC_MSG_VER = 1).
 *   - Implements E4 Reliable Communication: unicast ESP-NOW, ACK wait (200 ms),
 *     up to MAX_RETRIES (3) retries with the SAME seq, and channel auto-rescan.
 *   - Supports realistic 3-state room simulation (AWAKE, SLEEPING, ABSENT) plus
 *     interactive Serial fault injection (Duplicate Packet, Packet Loss / Seq Gap,
 *     Forced Retry, Sensor NaN Fault) for QA testing.
 *
 * Board:   ESP32 Dev Module / DOIT ESP32 DevKit V1 (Arduino-ESP32 core 3.x)
 * Setup:   Copy secrets.example.h -> secrets.h and fill in GATEWAY_MAC_STR and
 *          GATEWAY_WIFI_CHANNEL (secrets.h is git-ignored).
 * Serial:  115200 baud (send 'h' for interactive test menu).
 */

#include <Arduino.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>

#include <atomic>
#include <cmath>
#include <cstdio>
#include <cstring>

#if !__has_include("secrets.h")
#error "Copy tests/sim_nodes/secrets.example.h to secrets.h and fill in GATEWAY_MAC_STR + GATEWAY_WIFI_CHANNEL"
#endif
#include "secrets.h"

#if __has_include("../../airtomic/include/messages.h")
#include "../../airtomic/include/messages.h"
#else
// Exact fallback copy of airtomic/include/messages.h (AIRTOMIC_MSG_VER = 1)
#define AIRTOMIC_MSG_VER 1
#define NODE_ID_LEN 8

typedef struct __attribute__((packed)) {
  uint8_t ver;
  char node_id[NODE_ID_LEN];
  uint32_t seq;
  float temp;
  float hum;
  uint8_t vib;
  float dist;
} NodeMsg;

typedef struct __attribute__((packed)) {
  uint8_t ver;
  char node_id[NODE_ID_LEN];
  uint32_t seq;
} AckMsg;

static_assert(sizeof(NodeMsg) == 26, "NodeMsg layout mismatch with airtomic/include/messages.h");
static_assert(sizeof(AckMsg) == 13, "AckMsg layout mismatch with airtomic/include/messages.h");

#define NODE_PERIOD_MS 5000
#define ACK_TIMEOUT_MS 200
#define MAX_RETRIES 3
#endif

namespace {

constexpr size_t kSimNodeCount = 2;
constexpr uint32_t kScenarioCycleMs = 60000;  // Auto-rotate room state every 60 s
constexpr uint8_t kRescanAfterMisses = 5;

enum class RoomScenario : uint8_t {
  AWAKE = 0,     // Person present and active (AC on, door distance varies)
  SLEEPING = 1,  // Person sleeping / sitting still (AC on, door closed steady)
  ABSENT = 2     // Empty room with AC/light forgotten ON (cold room, steady door)
};

const char *scenarioName(RoomScenario s) {
  switch (s) {
    case RoomScenario::AWAKE:
      return "AWAKE";
    case RoomScenario::SLEEPING:
      return "SLEEPING";
    case RoomScenario::ABSENT:
      return "ABSENT";
  }
  return "UNKNOWN";
}

struct SimNodeState {
  char nodeId[NODE_ID_LEN];
  uint32_t seq;
  uint32_t nextSendAt;
  RoomScenario scenario;
  bool autoCycle;
  uint32_t lastScenarioChange;

  // Last transmitted message (used for E4 duplicate packet injection)
  NodeMsg lastMsg;
  bool hasLastMsg;

  // Per-node QA statistics
  uint32_t totalReadings;
  uint32_t ackedReadings;
  uint32_t totalTxAttempts;
  uint32_t totalRetries;
  uint32_t droppedSeqs;
  uint32_t dupSent;
  uint8_t consecutiveMisses;
};

SimNodeState simNodes[kSimNodeCount] = {};
uint8_t gatewayMac[6] = {};
uint8_t activeChannel = GATEWAY_WIFI_CHANNEL;

// Active ACK handshake state (protected via atomics between Wi-Fi task callback & loop)
std::atomic<bool> ackPending{false};
std::atomic<bool> ackReceived{false};
std::atomic<uint32_t> expectedAckSeq{0};
char expectedAckNode[NODE_ID_LEN] = {};
std::atomic<uint8_t> dropAcksRemaining{0};  // E4 test: ignore N ACKs to force real retries

// Fault injection flags toggled via Serial CLI
bool injectSeqGapNext = false;
bool injectNanNext = false;
bool continuousPacketLossMode = false;
uint8_t forceRetryCountNext = 0;

float randFloat(float minVal, float maxVal) {
  const long r = random(0, 10001);
  return minVal + (maxVal - minVal) * (static_cast<float>(r) / 10000.0f);
}

bool parseMac(const char *str, uint8_t out[6]) {
  const uint8_t zeroMac[6] = {};
  if (std::strlen(str) != 17) return false;
  if (std::sscanf(str, "%2hhx:%2hhx:%2hhx:%2hhx:%2hhx:%2hhx",
                  &out[0], &out[1], &out[2], &out[3], &out[4], &out[5]) != 6) {
    return false;
  }
  if ((out[0] & 1) != 0) return false;  // Must be unicast MAC
  return std::memcmp(out, zeroMac, sizeof(zeroMac)) != 0;
}

uint8_t scanChannelForSsid(const char *ssid) {
  if (ssid == nullptr || ssid[0] == '\0') return 0;
  Serial.printf("[SIM] Scanning 2.4 GHz channel for SSID \"%s\"...\n", ssid);
  const int n = WiFi.scanNetworks(false, true);
  uint8_t foundChannel = 0;
  for (int i = 0; i < n; ++i) {
    if (WiFi.SSID(i) == ssid) {
      foundChannel = static_cast<uint8_t>(WiFi.channel(i));
      Serial.printf("[SIM] Found \"%s\" on channel %u (RSSI %d dBm)\n",
                    ssid, foundChannel, static_cast<int>(WiFi.RSSI(i)));
      break;
    }
  }
  WiFi.scanDelete();
  return foundChannel;
}

void onEspNowRecv(const esp_now_recv_info_t *info, const uint8_t *data, int len) {
  if (len != sizeof(AckMsg)) return;
  if (std::memcmp(info->src_addr, gatewayMac, sizeof(gatewayMac)) != 0) return;

  AckMsg ack = {};
  std::memcpy(&ack, data, sizeof(ack));
  if (ack.ver != AIRTOMIC_MSG_VER) return;

  if (!ackPending.load(std::memory_order_acquire)) return;
  if (ack.seq != expectedAckSeq.load(std::memory_order_relaxed)) return;
  if (std::strncmp(ack.node_id, expectedAckNode, NODE_ID_LEN) != 0) return;

  // Optional E4 fault injection: intentionally ignore first K ACKs to force genuine retries
  uint8_t dropLeft = dropAcksRemaining.load(std::memory_order_relaxed);
  while (dropLeft > 0) {
    if (dropAcksRemaining.compare_exchange_weak(dropLeft, dropLeft - 1)) {
      return;
    }
  }

  ackReceived.store(true, std::memory_order_release);
}

bool configureRadio(uint8_t channel) {
  esp_now_deinit();
  WiFi.mode(WIFI_STA);
  WiFi.disconnect(false, false);

  if (channel == 0) {
    channel = scanChannelForSsid(GATEWAY_WIFI_SSID);
    if (channel == 0) {
      Serial.println("[SIM] Channel scan failed; falling back to channel 1.");
      channel = 1;
    }
  }
  activeChannel = channel;

  esp_err_t err = esp_wifi_set_channel(activeChannel, WIFI_SECOND_CHAN_NONE);
  if (err != ESP_OK) {
    Serial.printf("[SIM] esp_wifi_set_channel(%u) failed: %s\n", activeChannel, esp_err_to_name(err));
    return false;
  }

  err = esp_now_init();
  if (err != ESP_OK) {
    Serial.printf("[SIM] esp_now_init failed: %s\n", esp_err_to_name(err));
    return false;
  }

  esp_now_peer_info_t peer = {};
  std::memcpy(peer.peer_addr, gatewayMac, sizeof(gatewayMac));
  peer.ifidx = WIFI_IF_STA;
  peer.channel = activeChannel;
  peer.encrypt = false;
  err = esp_now_add_peer(&peer);
  if (err != ESP_OK) {
    Serial.printf("[SIM] esp_now_add_peer failed: %s\n", esp_err_to_name(err));
    return false;
  }

  err = esp_now_register_recv_cb(onEspNowRecv);
  if (err != ESP_OK) {
    Serial.printf("[SIM] esp_now_register_recv_cb failed: %s\n", esp_err_to_name(err));
    return false;
  }

  Serial.printf("[SIM] ESP-NOW ready | Sim MAC=%s | Gateway MAC=%s | Ch=%u\n",
                WiFi.macAddress().c_str(), GATEWAY_MAC_STR, activeChannel);
  return true;
}

NodeMsg buildSimulatedReading(SimNodeState &node, bool injectNan) {
  NodeMsg msg = {};
  msg.ver = AIRTOMIC_MSG_VER;
  std::strncpy(msg.node_id, node.nodeId, NODE_ID_LEN - 1);
  msg.node_id[NODE_ID_LEN - 1] = '\0';
  msg.seq = ++node.seq;

  if (injectNan) {
    msg.temp = NAN;
    msg.hum = NAN;
    msg.vib = 0;
    msg.dist = -1.0f;
    return msg;
  }

  // Slight per-node offset so SN02 and SN03 produce distinct realistic room signatures
  const float roomOffset = (std::strcmp(node.nodeId, "SN03") == 0) ? 0.9f : 0.0f;

  switch (node.scenario) {
    case RoomScenario::AWAKE:
      // Active occupant: AC running, mild temp, door distance fluctuates as person moves
      msg.temp = randFloat(24.6f, 26.4f) + roomOffset;
      msg.hum = randFloat(52.0f, 61.0f);
      msg.vib = 1;
      msg.dist = (random(0, 100) < 35) ? randFloat(35.0f, 115.0f) : randFloat(150.0f, 215.0f);
      break;

    case RoomScenario::SLEEPING:
      // Sleeping / sitting still: AC running, stable cooler temp, door closed steady
      msg.temp = randFloat(23.2f, 24.8f) + roomOffset;
      msg.hum = randFloat(48.0f, 56.0f);
      msg.vib = 1;
      msg.dist = randFloat(210.0f, 224.0f);
      break;

    case RoomScenario::ABSENT:
      // Empty room with AC forgotten ON: cold steady air, vibration=1, door closed steady
      msg.temp = randFloat(21.8f, 23.4f) + roomOffset;
      msg.hum = randFloat(44.0f, 52.0f);
      msg.vib = 1;
      msg.dist = randFloat(218.0f, 226.0f);
      break;
  }

  return msg;
}

struct TxResult {
  bool acked;
  uint8_t attempts;
  uint32_t rttMs;
};

TxResult sendWithAckRetry(const NodeMsg &msg, uint8_t simulatedAckDrops) {
  std::strncpy(expectedAckNode, msg.node_id, NODE_ID_LEN);
  expectedAckSeq.store(msg.seq, std::memory_order_relaxed);
  dropAcksRemaining.store(simulatedAckDrops, std::memory_order_relaxed);
  ackReceived.store(false, std::memory_order_relaxed);
  ackPending.store(true, std::memory_order_release);

  TxResult res{false, 0, 0};
  const uint32_t firstTxMs = millis();

  for (uint8_t attempt = 0; attempt <= MAX_RETRIES; ++attempt) {
    res.attempts = attempt + 1;
    const esp_err_t sendErr = esp_now_send(
        gatewayMac, reinterpret_cast<const uint8_t *>(&msg), sizeof(msg));
    if (sendErr != ESP_OK) {
      Serial.printf("[SIM] %s seq=%lu attempt=%u send err: %s\n",
                    msg.node_id, static_cast<unsigned long>(msg.seq),
                    res.attempts, esp_err_to_name(sendErr));
    }

    const uint32_t waitStart = millis();
    while (millis() - waitStart < ACK_TIMEOUT_MS) {
      if (ackReceived.load(std::memory_order_acquire)) {
        res.acked = true;
        res.rttMs = millis() - firstTxMs;
        ackPending.store(false, std::memory_order_release);
        return res;
      }
      delay(1);
    }
  }

  ackPending.store(false, std::memory_order_release);
  res.rttMs = millis() - firstTxMs;
  return res;
}

void logCsvRow(const NodeMsg &msg, RoomScenario scenario, const TxResult &res, const char *faultMode) {
  // Machine-readable CSV line for tests/metrics/analyze_metrics.py
  Serial.printf("SIM_LOG,%lu,%s,%lu,%s,%u,%u,%lu,%s,%.2f,%.2f,%u,%.2f\n",
                static_cast<unsigned long>(millis()),
                msg.node_id,
                static_cast<unsigned long>(msg.seq),
                scenarioName(scenario),
                res.attempts,
                res.acked ? 1 : 0,
                static_cast<unsigned long>(res.rttMs),
                faultMode,
                msg.temp,
                msg.hum,
                msg.vib,
                msg.dist);
}

void triggerDuplicateBurst(SimNodeState &node) {
  if (!node.hasLastMsg) {
    Serial.printf("[SIM] No previous packet for %s yet; wait for first send.\n", node.nodeId);
    return;
  }
  Serial.printf("[SIM] E4 DUPLICATE TEST: resending %s seq=%lu twice (<500 ms window)\n",
                node.nodeId, static_cast<unsigned long>(node.lastMsg.seq));
  for (int i = 0; i < 2; ++i) {
    const TxResult res = sendWithAckRetry(node.lastMsg, 0);
    node.dupSent++;
    logCsvRow(node.lastMsg, node.scenario, res, "DUPLICATE_SEQ");
    delay(120);
  }
}

void printStats() {
  Serial.println("\n=== Airtomic Simulator Statistics ===");
  for (size_t i = 0; i < kSimNodeCount; ++i) {
    const SimNodeState &n = simNodes[i];
    const float pdr = n.totalReadings > 0 ? (100.0f * n.ackedReadings / n.totalReadings) : 0.0f;
    Serial.printf("  %s | state=%-8s (auto=%d) | seq=%lu | acked=%lu/%lu (PDR=%.2f%%) | retries=%lu | seq_gaps=%lu | dups=%lu\n",
                  n.nodeId, scenarioName(n.scenario), n.autoCycle ? 1 : 0,
                  static_cast<unsigned long>(n.seq),
                  static_cast<unsigned long>(n.ackedReadings),
                  static_cast<unsigned long>(n.totalReadings),
                  pdr,
                  static_cast<unsigned long>(n.totalRetries),
                  static_cast<unsigned long>(n.droppedSeqs),
                  static_cast<unsigned long>(n.dupSent));
  }
  Serial.printf("  Active Wi-Fi Channel: %u | Continuous Loss Mode: %s\n\n",
                activeChannel, continuousPacketLossMode ? "ON (20%)" : "OFF");
}

void printHelp() {
  Serial.println(F(
      "\n=== Airtomic Sim Nodes (SN02 + SN03) Interactive QA Menu ===\n"
      "  1 / 2 / 3 : Force SN02 to AWAKE / SLEEPING / ABSENT\n"
      "  a / s / e : Force SN03 to AWAKE / SLEEPING / ABSENT\n"
      "  c         : Resume auto-cycling scenarios on both nodes\n"
      "  d         : [E4] Send DUPLICATE seq burst for SN02 (tests Gateway dedup)\n"
      "  r         : [E4] Force 2 retries on next packet (simulates lost ACKs)\n"
      "  g         : [E4] Inject 1 lost packet / seq gap (tests PDR calculation)\n"
      "  l         : [E4] Toggle continuous 20%% simulated packet loss\n"
      "  n         : [FAIL] Inject NaN temp/hum & dist=-1 on next packet\n"
      "  p         : Print live per-node PDR & retry statistics\n"
      "  h         : Show this menu\n"
      "CSV Header: SIM_LOG,ms,node_id,seq,scenario,attempts,acked,ack_rtt_ms,fault_mode,temp,hum,vib,dist\n"));
}

void handleSerialCommand() {
  while (Serial.available() > 0) {
    const char cmd = static_cast<char>(Serial.read());
    switch (cmd) {
      case '1':
        simNodes[0].scenario = RoomScenario::AWAKE;
        simNodes[0].autoCycle = false;
        Serial.println("[SIM] SN02 -> AWAKE (locked)");
        break;
      case '2':
        simNodes[0].scenario = RoomScenario::SLEEPING;
        simNodes[0].autoCycle = false;
        Serial.println("[SIM] SN02 -> SLEEPING (locked)");
        break;
      case '3':
        simNodes[0].scenario = RoomScenario::ABSENT;
        simNodes[0].autoCycle = false;
        Serial.println("[SIM] SN02 -> ABSENT (locked)");
        break;
      case 'a':
        simNodes[1].scenario = RoomScenario::AWAKE;
        simNodes[1].autoCycle = false;
        Serial.println("[SIM] SN03 -> AWAKE (locked)");
        break;
      case 's':
        simNodes[1].scenario = RoomScenario::SLEEPING;
        simNodes[1].autoCycle = false;
        Serial.println("[SIM] SN03 -> SLEEPING (locked)");
        break;
      case 'e':
        simNodes[1].scenario = RoomScenario::ABSENT;
        simNodes[1].autoCycle = false;
        Serial.println("[SIM] SN03 -> ABSENT (locked)");
        break;
      case 'c':
        simNodes[0].autoCycle = true;
        simNodes[1].autoCycle = true;
        Serial.println("[SIM] Auto-cycle scenario resumed for SN02 & SN03");
        break;
      case 'd':
        triggerDuplicateBurst(simNodes[0]);
        break;
      case 'r':
        forceRetryCountNext = 2;
        Serial.println("[SIM] Next packet will drop 2 ACKs locally to force 3 real transmissions");
        break;
      case 'g':
        injectSeqGapNext = true;
        Serial.println("[SIM] Next reading will simulate packet loss (seq incremented, frame not sent)");
        break;
      case 'l':
        continuousPacketLossMode = !continuousPacketLossMode;
        Serial.printf("[SIM] Continuous 20%% packet loss mode: %s\n",
                      continuousPacketLossMode ? "ENABLED" : "DISABLED");
        break;
      case 'n':
        injectNanNext = true;
        Serial.println("[SIM] Next reading will inject DHT NaN + HC-SR04 -1 fault");
        break;
      case 'p':
        printStats();
        break;
      case 'h':
      case '?':
        printHelp();
        break;
      default:
        break;
    }
  }
}

void processNode(SimNodeState &node, uint32_t now) {
  if (node.autoCycle && (now - node.lastScenarioChange >= kScenarioCycleMs)) {
    node.lastScenarioChange = now;
    node.scenario = static_cast<RoomScenario>((static_cast<uint8_t>(node.scenario) + 1) % 3);
    Serial.printf("[SIM] %s auto-switched scenario -> %s\n", node.nodeId, scenarioName(node.scenario));
  }

  if (static_cast<int32_t>(now - node.nextSendAt) < 0) return;
  node.nextSendAt = now + NODE_PERIOD_MS;

  const bool sendNan = injectNanNext;
  injectNanNext = false;

  const NodeMsg msg = buildSimulatedReading(node, sendNan);
  node.totalReadings++;

  const bool dropPacket = injectSeqGapNext || (continuousPacketLossMode && random(0, 100) < 20);
  injectSeqGapNext = false;

  if (dropPacket) {
    node.droppedSeqs++;
    const TxResult dropped{false, 0, 0};
    logCsvRow(msg, node.scenario, dropped, "PACKET_LOSS_SIM");
    return;
  }

  const uint8_t ackDrops = forceRetryCountNext;
  forceRetryCountNext = 0;

  const TxResult res = sendWithAckRetry(msg, ackDrops);
  node.lastMsg = msg;
  node.hasLastMsg = true;
  node.totalTxAttempts += res.attempts;
  if (res.attempts > 1) {
    node.totalRetries += (res.attempts - 1);
  }

  if (res.acked) {
    node.ackedReadings++;
    node.consecutiveMisses = 0;
  } else {
    node.consecutiveMisses++;
    if (node.consecutiveMisses >= kRescanAfterMisses && GATEWAY_WIFI_CHANNEL == 0) {
      Serial.printf("[SIM] %s missed %u ACKs in a row -> rescanning Wi-Fi channel\n",
                    node.nodeId, node.consecutiveMisses);
      node.consecutiveMisses = 0;
      configureRadio(0);
    }
  }

  const char *faultTag = sendNan ? "SENSOR_NAN"
                                 : (ackDrops > 0 ? "FORCED_RETRY" : "NORMAL");
  logCsvRow(msg, node.scenario, res, faultTag);
}

}  // namespace

void setup() {
  Serial.begin(115200);
  delay(300);
  randomSeed(esp_random());

  if (!parseMac(GATEWAY_MAC_STR, gatewayMac)) {
    Serial.println("[SIM] ERROR: Invalid GATEWAY_MAC_STR in secrets.h (expected AA:BB:CC:DD:EE:FF)");
    while (true) delay(1000);
  }

  // Initialize SN02 (starts AWAKE) and SN03 (starts SLEEPING, staggered by 2.5 s)
  const uint32_t now = millis();
  std::strncpy(simNodes[0].nodeId, "SN02", NODE_ID_LEN);
  simNodes[0].scenario = RoomScenario::AWAKE;
  simNodes[0].autoCycle = true;
  simNodes[0].lastScenarioChange = now;
  simNodes[0].nextSendAt = now + 1000;

  std::strncpy(simNodes[1].nodeId, "SN03", NODE_ID_LEN);
  simNodes[1].scenario = RoomScenario::SLEEPING;
  simNodes[1].autoCycle = true;
  simNodes[1].lastScenarioChange = now;
  simNodes[1].nextSendAt = now + 1000 + (NODE_PERIOD_MS / 2);

  configureRadio(GATEWAY_WIFI_CHANNEL);
  printHelp();
}

void loop() {
  handleSerialCommand();
  const uint32_t now = millis();
  for (size_t i = 0; i < kSimNodeCount; ++i) {
    processNode(simNodes[i], now);
  }
  delay(5);
}
