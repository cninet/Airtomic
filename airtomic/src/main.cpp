#include <Arduino.h>
#include <DHT.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>

#include <atomic>
#include <cstdio>
#include <cstring>

#include "messages.h"
#include "node_config.h"

namespace {

constexpr uint32_t dhtWarmupMs = 2100;
constexpr uint32_t echoTimeoutUs = 30000;
constexpr uint8_t dhtType = DHT11;
static_assert(sizeof(NodeConfig::nodeId) > 1 && sizeof(NodeConfig::nodeId) <= NODE_ID_LEN,
              "Node ID must fit in the ESP-NOW packet");

DHT dht(NodeConfig::dhtPin, dhtType);
portMUX_TYPE vibrationLock = portMUX_INITIALIZER_UNLOCKED;
volatile bool vibrationSeen = false;
uint8_t gatewayMac[6] = {};
std::atomic<uint32_t> acknowledgedSequence{0};
uint32_t sequence = 0;
uint32_t lastReadAt = 0;
uint32_t readIntervalMs = dhtWarmupMs;
bool radioReady = false;

void IRAM_ATTR markVibration() {
  portENTER_CRITICAL_ISR(&vibrationLock);
  vibrationSeen = true;
  portEXIT_CRITICAL_ISR(&vibrationLock);
}

bool takeVibration() {
  portENTER_CRITICAL(&vibrationLock);
  const bool seen = vibrationSeen;
  vibrationSeen = false;
  portEXIT_CRITICAL(&vibrationLock);
  return seen;
}

float readDistanceCm() {
  digitalWrite(NodeConfig::triggerPin, HIGH);
  delayMicroseconds(10);
  digitalWrite(NodeConfig::triggerPin, LOW);

  const unsigned long echoUs = pulseIn(NodeConfig::echoPin, HIGH, echoTimeoutUs);
  const float distanceCm = echoUs / 58.0f;
  return distanceCm >= 2.0f && distanceCm <= 400.0f ? distanceCm : -1.0f;
}

void onAck(const esp_now_recv_info_t *sender, const uint8_t *data, int length) {
  if (length != sizeof(AckMsg) ||
      std::memcmp(sender->src_addr, gatewayMac, sizeof(gatewayMac)) != 0) {
    return;
  }

  AckMsg ack;
  std::memcpy(&ack, data, sizeof(ack));
  if (ack.ver == AIRTOMIC_MSG_VER &&
      std::strncmp(ack.node_id, NodeConfig::nodeId, NODE_ID_LEN) == 0) {
    acknowledgedSequence.store(ack.seq, std::memory_order_relaxed);
  }
}

bool readGatewayMac() {
  const char *configuredMac = NodeConfig::gatewayMac;
  const uint8_t unsetMac[6] = {};
  return std::strlen(configuredMac) == 17 &&
         std::sscanf(configuredMac, "%2hhx:%2hhx:%2hhx:%2hhx:%2hhx:%2hhx",
                     &gatewayMac[0], &gatewayMac[1], &gatewayMac[2],
                     &gatewayMac[3], &gatewayMac[4], &gatewayMac[5]) == 6 &&
         (gatewayMac[0] & 1) == 0 &&
         std::memcmp(gatewayMac, unsetMac, sizeof(gatewayMac)) != 0;
}

bool startRadio() {
  if (NodeConfig::gatewayMac[0] == '\0' || NodeConfig::wifiChannel == 0) {
    Serial.println("ESP-NOW is disabled until the gateway MAC and Wi-Fi channel are set.");
    return false;
  }
  if (!readGatewayMac()) {
    Serial.println("Set the gateway STA MAC in AA:BB:CC:DD:EE:FF format.");
    return false;
  }
  if (!WiFi.mode(WIFI_STA)) {
    Serial.println("Cannot start Wi-Fi in station mode.");
    return false;
  }

  esp_err_t result = esp_wifi_set_channel(NodeConfig::wifiChannel, WIFI_SECOND_CHAN_NONE);
  if (result != ESP_OK) {
    Serial.printf("Cannot set the Wi-Fi channel (%s).\n", esp_err_to_name(result));
    return false;
  }

  result = esp_now_init();
  if (result != ESP_OK) {
    Serial.printf("Cannot start ESP-NOW (%s).\n", esp_err_to_name(result));
    return false;
  }

  esp_now_peer_info_t peer = {};
  std::memcpy(peer.peer_addr, gatewayMac, sizeof(gatewayMac));
  peer.ifidx = WIFI_IF_STA;
  peer.channel = NodeConfig::wifiChannel;
  result = esp_now_add_peer(&peer);
  if (result == ESP_OK) {
    result = esp_now_register_recv_cb(onAck);
  }
  if (result != ESP_OK) {
    Serial.printf("Cannot configure the ESP-NOW link (%s).\n", esp_err_to_name(result));
    esp_now_deinit();
    return false;
  }

  Serial.printf("ESP-NOW ready on Wi-Fi channel %u.\n", NodeConfig::wifiChannel);
  return true;
}

bool sendWithRetry(const NodeMsg &reading) {
  acknowledgedSequence.store(reading.seq - 1, std::memory_order_relaxed);
  for (int attempt = 0; attempt <= MAX_RETRIES; ++attempt) {
    if (acknowledgedSequence.load(std::memory_order_relaxed) == reading.seq) {
      return true;
    }

    const esp_err_t result = esp_now_send(
        gatewayMac, reinterpret_cast<const uint8_t *>(&reading), sizeof(reading));
    if (result != ESP_OK) {
      Serial.printf("ESP-NOW send attempt %d failed (%s).\n", attempt + 1,
                    esp_err_to_name(result));
    }

    const uint32_t attemptStartedAt = millis();
    while (millis() - attemptStartedAt < ACK_TIMEOUT_MS) {
      if (acknowledgedSequence.load(std::memory_order_relaxed) == reading.seq) {
        return true;
      }
      delay(1);
    }
  }
  return acknowledgedSequence.load(std::memory_order_relaxed) == reading.seq;
}

}  // namespace

void setup() {
  Serial.begin(115200);
  dht.begin();
  Serial.printf("DHT%u DATA=GPIO%u\n", dhtType, NodeConfig::dhtPin);
  pinMode(NodeConfig::triggerPin, OUTPUT);
  digitalWrite(NodeConfig::triggerPin, LOW);
  pinMode(NodeConfig::echoPin, INPUT);
  pinMode(NodeConfig::vibrationPin, INPUT);
  attachInterrupt(digitalPinToInterrupt(NodeConfig::vibrationPin), markVibration,
                  NodeConfig::vibrationEdge);

  lastReadAt = millis();
  radioReady = startRadio();
}

void loop() {
  const uint32_t now = millis();
  if (now - lastReadAt < readIntervalMs) {
    delay(5);
    return;
  }
  lastReadAt = now;
  readIntervalMs = NODE_PERIOD_MS;

  NodeMsg reading = {};
  reading.ver = AIRTOMIC_MSG_VER;
  std::memcpy(reading.node_id, NodeConfig::nodeId, sizeof(NodeConfig::nodeId));
  reading.seq = ++sequence;
  const int dhtIdleLevel = digitalRead(NodeConfig::dhtPin);
  reading.temp = dht.readTemperature();
  reading.hum = dht.readHumidity();
  if (isnan(reading.temp) || isnan(reading.hum)) {
    Serial.printf("DHT%u read failed on GPIO%u (DATA idle=%s)\n", dhtType,
                  NodeConfig::dhtPin, dhtIdleLevel == HIGH ? "HIGH" : "LOW");
  }
  reading.dist = readDistanceCm();
  reading.vib = takeVibration();

  const char *status = "serial only";
  if (radioReady) {
    status = sendWithRetry(reading) ? "ACK" : "no ACK";
  }
  Serial.printf("%s seq=%lu temp_c=%.1f hum_pct=%.1f vib=%u dist_cm=%.1f | %s\n",
                reading.node_id, static_cast<unsigned long>(reading.seq), reading.temp,
                reading.hum, reading.vib, reading.dist, status);
}
