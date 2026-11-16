#pragma once

struct LocalReadings {
  int lightRaw;  // ESP32 ADC reading, 0 to 4095.
  int pir;       // -1 during warm-up, 0 idle, 1 motion.
};

void beginLocalSensors();
LocalReadings readLocalSensors();
