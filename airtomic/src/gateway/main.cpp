#include <Arduino.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_random.h>
#include <freertos/queue.h>

#include <cinttypes>
#include <cstring>

#include "firebase_cloud.h"
#include "local_sensors.h"
#include "messages.h"

namespace {
constexpr size_t kRecentCount = 16;
constexpr uint32_t kDuplicateWindowMs = ACK_TIMEOUT_MS * (MAX_RETRIES + 1) + 300;
constexpr size_t kCloudQueueDepth = 32;

struct ReceivedPacket {
  NodeMsg reading;
  uint8_t mac[ESP_NOW_ETH_ALEN];
};

struct PendingUpload {
  NodeMsg reading;
  LocalReadings local;
  char historyId[26];
};

struct RecentPacket {
  ReceivedPacket packet;
  uint32_t acceptedAt;
};

QueueHandle_t radioQueue = nullptr;
QueueHandle_t cloudQueue = nullptr;
RecentPacket recent[kRecentCount] = {};
size_t recentIndex = 0;
uint32_t bootIdHigh = 0;
uint32_t bootIdLow = 0;
uint32_t uploadSequence = 0;

void onReceive(const esp_now_recv_info_t *info, const uint8_t *data, int length) {
  if (length != sizeof(NodeMsg)) return;

  ReceivedPacket packet;
  memcpy(&packet.reading, data, sizeof(packet.reading));
  memcpy(packet.mac, info->src_addr, sizeof(packet.mac));
  xQueueSend(radioQueue, &packet, 0);
}

bool validNodeId(const char *id) {
  for (size_t i = 0; i < NODE_ID_LEN; ++i) {
    const char ch = id[i];
    if (ch == '\0') return i > 0;
    if ((ch < 'A' || ch > 'Z') && (ch < 'a' || ch > 'z') &&
        (ch < '0' || ch > '9') && ch != '_' && ch != '-') {
      return false;
    }
  }
  return false;
}

bool recentlyAccepted(const ReceivedPacket &packet, uint32_t now) {
  for (const RecentPacket &entry : recent) {
    if (now - entry.acceptedAt <= kDuplicateWindowMs &&
        packet.reading.seq == entry.packet.reading.seq &&
        strcmp(packet.reading.node_id, entry.packet.reading.node_id) == 0 &&
        memcmp(packet.mac, entry.packet.mac, sizeof(packet.mac)) == 0) {
      return true;
    }
  }
  return false;
}

bool ensurePeer(const uint8_t *mac) {
  if (esp_now_is_peer_exist(mac)) return true;

  esp_now_peer_info_t peer = {};
  memcpy(peer.peer_addr, mac, sizeof(peer.peer_addr));
  peer.ifidx = WIFI_IF_STA;
  // The station follows the connected router's Wi-Fi channel.
  peer.channel = 0;
  const esp_err_t result = esp_now_add_peer(&peer);
  if (result != ESP_OK) Serial.printf("ESP-NOW add peer failed (%d)\n", result);
  return result == ESP_OK;
}

void acknowledge(const ReceivedPacket &packet) {
  AckMsg ack = {};
  ack.ver = AIRTOMIC_MSG_VER;
  memcpy(ack.node_id, packet.reading.node_id, sizeof(ack.node_id));
  ack.seq = packet.reading.seq;
  const esp_err_t result = esp_now_send(
      packet.mac, reinterpret_cast<const uint8_t *>(&ack), sizeof(ack));
  if (result != ESP_OK) Serial.printf("ESP-NOW ACK failed (%d)\n", result);
}

void handlePacket(const ReceivedPacket &packet) {
  const NodeMsg &reading = packet.reading;
  if (reading.ver != AIRTOMIC_MSG_VER || !validNodeId(reading.node_id) ||
      reading.vib > 1 || !ensurePeer(packet.mac)) {
    return;
  }
  const uint32_t now = millis();
  if (recentlyAccepted(packet, now)) {
    acknowledge(packet);
    return;
  }
  if (uxQueueSpacesAvailable(cloudQueue) == 0) {
    Serial.println("Firebase queue full; reading not accepted");
    return;
  }

  PendingUpload upload = {};
  upload.reading = reading;
  upload.local = readLocalSensors();
  snprintf(upload.historyId, sizeof(upload.historyId),
           "%08" PRIx32 "%08" PRIx32 "-%08" PRIx32,
           bootIdHigh, bootIdLow, ++uploadSequence);
  if (xQueueSend(cloudQueue, &upload, 0) != pdPASS) return;

  recent[recentIndex] = {packet, now};
  recentIndex = (recentIndex + 1) % kRecentCount;
  acknowledge(packet);
  Serial.printf("Queued %s #%lu, LDR=%d PIR=%d\n", reading.node_id,
                static_cast<unsigned long>(reading.seq), upload.local.lightRaw, upload.local.pir);
}

void uploadTask(void *) {
  for (;;) {
    if (WiFi.status() != WL_CONNECTED) {
      vTaskDelay(pdMS_TO_TICKS(250));
      continue;
    }

    PendingUpload upload;
    if (xQueuePeek(cloudQueue, &upload, pdMS_TO_TICKS(250)) != pdPASS) continue;
    if (publishToFirebase(upload.reading, upload.historyId, upload.local.lightRaw, upload.local.pir)) {
      Serial.printf("Firebase saved %s #%lu\n", upload.reading.node_id,
                    static_cast<unsigned long>(upload.reading.seq));
      xQueueReceive(cloudQueue, &upload, 0);
    } else {
      vTaskDelay(pdMS_TO_TICKS(3000));
    }
  }
}
}  // namespace

void setup() {
  Serial.begin(115200);
  beginLocalSensors();
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.setSleep(false);
  Serial.printf("Gateway STA MAC %s\n", WiFi.macAddress().c_str());

  bootIdHigh = esp_random();
  bootIdLow = esp_random();
  radioQueue = xQueueCreate(8, sizeof(ReceivedPacket));
  cloudQueue = xQueueCreate(kCloudQueueDepth, sizeof(PendingUpload));
  if (radioQueue == nullptr || cloudQueue == nullptr) {
    Serial.println("Gateway queue allocation failed");
    return;
  }
  if (esp_now_init() != ESP_OK || esp_now_register_recv_cb(onReceive) != ESP_OK) {
    Serial.println("Gateway ESP-NOW initialization failed");
  }

  if (cloudWifiSsid()[0] != '\0') {
    WiFi.begin(cloudWifiSsid(), cloudWifiPassword());
  }
  if (!cloudConfigured()) {
    Serial.println("Set Wi-Fi and Firebase in include/gateway_secrets.h");
    return;
  }
  if (xTaskCreate(uploadTask, "Firebase", 8192, nullptr, 1, nullptr) != pdPASS) {
    Serial.println("Firebase task creation failed");
  }
}

void loop() {
  static int lastChannel = 0;
  const int channel = WiFi.status() == WL_CONNECTED ? WiFi.channel() : 0;
  if (channel != lastChannel) {
    if (channel > 0) {
      Serial.printf("Gateway STA MAC %s, Wi-Fi channel %d\n", WiFi.macAddress().c_str(), channel);
    } else {
      Serial.println("Gateway Wi-Fi disconnected");
    }
    lastChannel = channel;
  }

  ReceivedPacket packet;
  if (radioQueue != nullptr && xQueueReceive(radioQueue, &packet, pdMS_TO_TICKS(20)) == pdPASS) {
    handlePacket(packet);
  } else {
    delay(1);
  }
}
