// Copy this file to secrets.h (git-ignored) and fill in your Gateway settings.
// NEVER commit real Wi-Fi credentials or private details to Git (E5 / M8).
#pragma once

// Gateway STA MAC address printed on the Gateway Serial Monitor at boot.
// Format: "AA:BB:CC:DD:EE:FF"
#define GATEWAY_MAC_STR "AA:BB:CC:DD:EE:FF"

// Wi-Fi channel used by the Gateway's router/hotspot (1-13).
// Set to 0 to automatically scan for GATEWAY_WIFI_SSID at boot.
#define GATEWAY_WIFI_CHANNEL 6

// Optional: Router/Hotspot SSID used ONLY for automatic channel scanning when
// GATEWAY_WIFI_CHANNEL == 0 (no Wi-Fi password is needed for ESP-NOW).
#define GATEWAY_WIFI_SSID "your-2.4ghz-wifi-ssid"
