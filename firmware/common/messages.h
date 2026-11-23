// Airtomic: sensor node <-> gateway message format over ESP-NOW.
// THE CONTRACT: copy this file unchanged into both sketches (firmware/node/ and firmware/gateway/).
// If you change it, bump AIRTOMIC_MSG_VER and update both boards; the gateway drops other versions.
#pragma once
#include <stdint.h>

#define AIRTOMIC_MSG_VER 1
#define NODE_ID_LEN 8

// Node -> gateway, one per reading.
typedef struct __attribute__((packed)) {
  uint8_t ver;                   // = AIRTOMIC_MSG_VER
  char node_id[NODE_ID_LEN];     // "SN01" (NUL-terminated); simulated nodes use "SN02", "SN03"
  uint32_t seq;                  // +1 for each NEW reading; a retry resends the SAME seq
  float temp;                    // °C from DHT22, NAN if the read failed
  float hum;                     // %RH from DHT22, NAN if the read failed
  uint8_t vib;                   // SW-420: 1 if vibration was seen since the previous reading, else 0
  float dist;                    // cm from HC-SR04, -1 if no echo
} NodeMsg;

// Gateway -> node, sent back for every NodeMsg received (including duplicates).
typedef struct __attribute__((packed)) {
  uint8_t ver;                   // = AIRTOMIC_MSG_VER
  char node_id[NODE_ID_LEN];     // copied from the NodeMsg
  uint32_t seq;                  // the seq being acknowledged
} AckMsg;

static_assert(sizeof(NodeMsg) == 26, "NodeMsg layout changed: bump AIRTOMIC_MSG_VER and update both boards");
static_assert(sizeof(AckMsg) == 13, "AckMsg layout changed: bump AIRTOMIC_MSG_VER and update both boards");

// Protocol rules (E4 reliable communication):
//  - Node sends a NodeMsg every NODE_PERIOD_MS, unicast to the gateway's MAC (printed by the gateway at boot).
//  - After sending, wait up to ACK_TIMEOUT_MS for an AckMsg with the same node_id and seq.
//    No ACK -> resend the same message, up to MAX_RETRIES times, then give up on that reading.
//  - The gateway ACKs everything and drops duplicates by remembering the last seq per node_id.
//  - Channel: ESP-NOW must use the Wi-Fi channel of the gateway's router/hotspot. At boot the node scans
//    for that SSID, takes its channel, and rescans if several readings in a row get no ACK.
#define NODE_PERIOD_MS 5000
#define ACK_TIMEOUT_MS 200
#define MAX_RETRIES 3
