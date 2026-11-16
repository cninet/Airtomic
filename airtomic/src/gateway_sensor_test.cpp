#include <Arduino.h>

#include "gateway/local_sensors.h"

void setup() {
  Serial.begin(115200);
  beginLocalSensors();
  Serial.println("Gateway sensor test (PIR=-1 during warm-up)");
}

void loop() {
  const LocalReadings readings = readLocalSensors();
  Serial.printf("LDR=%d PIR=%d\n", readings.lightRaw, readings.pir);
  delay(1000);
}
