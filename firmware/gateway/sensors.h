// Gateway's own sensors (M1 needs >= 2 sensors wired to the gateway).
//
//   BH1750 light   VCC -> 3.3V, GND -> GND, SDA -> GPIO21, SCL -> GPIO22, ADDR -> unconnected
//                  Own register-level I2C driver below, no library (candidate for E8).
//   HC-SR501 PIR   VCC -> 5V (VIN), GND -> GND, OUT -> GPIO27 (OUT is 3.3V, safe for the ESP32)
//                  Read with an interrupt (E2). Needs ~60 s after power-up before it is reliable.
#pragma once
#include <Wire.h>
#include <atomic>

const int I2C_SDA = 21, I2C_SCL = 22, PIR_PIN = 27;

// ---- BH1750 (datasheet: 0x01 = power on, 0x10 = continuous high-res mode, 1 lx, ~120 ms;
//      result is a 16-bit big-endian count, lux = count / 1.2) ----
uint8_t bh1750Addr = 0;  // 0x23 (ADDR low/unconnected) or 0x5C (ADDR high); 0 = not found

bool bh1750Cmd(uint8_t addr, uint8_t cmd) {
  Wire.beginTransmission(addr);
  Wire.write(cmd);
  return Wire.endTransmission() == 0;  // 0 = the sensor ACKed
}

bool bh1750Begin() {
  for (uint8_t a : {0x23, 0x5C}) {
    if (bh1750Cmd(a, 0x01) && bh1750Cmd(a, 0x10)) {
      bh1750Addr = a;
      delay(180);  // first measurement takes up to 180 ms
      Serial.printf("BH1750 found at 0x%02X\n", a);
      return true;
    }
  }
  bh1750Addr = 0;
  Serial.println("BH1750 not found (check SDA=21, SCL=22, 3.3V, GND)");
  return false;
}

// Lux, or -1 if the sensor is missing/unplugged (it is looked for again on the next call).
float readLux() {
  if (!bh1750Addr && !bh1750Begin()) return -1;
  if (Wire.requestFrom(bh1750Addr, (size_t)2) != 2) {
    bh1750Addr = 0;
    return -1;
  }
  uint16_t raw = Wire.read() << 8;
  raw |= Wire.read();
  return raw / 1.2f;
}

// ---- HC-SR501 PIR ----
std::atomic<uint32_t> motionCount{0};

void IRAM_ATTR onPir() {  // ISR: only count; the work happens later in takeMotion() (deferred processing)
  motionCount++;
}

// 1 if motion was seen since the previous call (or the PIR output is still high), else 0.
int takeMotion() {
  uint32_t n = motionCount.exchange(0);
  return (n > 0 || digitalRead(PIR_PIN) == HIGH) ? 1 : 0;
}

void sensorsBegin() {
  Wire.begin(I2C_SDA, I2C_SCL);
  bh1750Begin();
  pinMode(PIR_PIN, INPUT_PULLDOWN);  // reads 0 when unplugged instead of floating
  attachInterrupt(digitalPinToInterrupt(PIR_PIN), onPir, RISING);
}
