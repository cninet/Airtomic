#pragma once

#include <Arduino.h>

namespace NodeConfig {

constexpr char nodeId[] = "SN01";

// Leave unset to read the sensors over Serial while setting up the gateway.
constexpr char gatewayMac[] = "6C:C8:40:93:8A:A8";
constexpr uint8_t wifiChannel = 6;

constexpr uint8_t dhtPin = 4;
constexpr uint8_t vibrationPin = 27;
constexpr uint8_t triggerPin = 26;
constexpr uint8_t echoPin = 33;

// Either transition marks activity, regardless of the module's output polarity.
constexpr int vibrationEdge = CHANGE;

}  // namespace NodeConfig
