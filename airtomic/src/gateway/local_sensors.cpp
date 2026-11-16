#include "local_sensors.h"

#include <Arduino.h>
#include <esp_timer.h>

namespace {

constexpr uint8_t kLdrPin = 34;  // ADC1 works while Wi-Fi is active.
constexpr uint8_t kPirPin = 32;
constexpr int64_t kPirWarmupUs = 60LL * 1000 * 1000;

int64_t pirReadyAtUs;
portMUX_TYPE pirMux = portMUX_INITIALIZER_UNLOCKED;
bool pirMotionLatched = false;

void IRAM_ATTR onPirRising() {
  if (esp_timer_get_time() >= pirReadyAtUs) {
    portENTER_CRITICAL_ISR(&pirMux);
    pirMotionLatched = true;
    portEXIT_CRITICAL_ISR(&pirMux);
  }
}

}  // namespace

void beginLocalSensors() {
  pinMode(kLdrPin, INPUT);
  analogReadResolution(12);
  analogSetPinAttenuation(kLdrPin, ADC_11db);

  pinMode(kPirPin, INPUT);
  pirReadyAtUs = esp_timer_get_time() + kPirWarmupUs;
  attachInterrupt(kPirPin, onPirRising, RISING);
}

LocalReadings readLocalSensors() {
  LocalReadings readings{analogRead(kLdrPin), -1};
  if (esp_timer_get_time() < pirReadyAtUs) {
    return readings;
  }

  portENTER_CRITICAL(&pirMux);
  const bool detected = pirMotionLatched;
  pirMotionLatched = false;
  portEXIT_CRITICAL(&pirMux);

  readings.pir = (detected || digitalRead(kPirPin) == HIGH) ? 1 : 0;
  return readings;
}
