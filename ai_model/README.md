# Airtomic Rule-Based AI (`ai_model`)

โมดูล AI สำหรับโปรเจกต์ **Airtomic** เพื่อจำแนกสถานะห้อง (`AWAKE` / `SLEEPING` / `ABSENT`), คำนวณค่าความมั่นใจ (`conf`), ตรวจจับสถานะเครื่องใช้ไฟฟ้า (`ac` / `light`), และควบคุมจังหวะแจ้งเตือน + สั่ง Servo ปิดอัตโนมัติพร้อมตรวจผลซ้ำ

---

## ทำไมตอนนี้ (ยังไม่มีข้อมูลจริง) ถึงเริ่มทำ Rule-Based AI ได้เลย?

1. **ไม่ต้องใช้ Dataset ฝึกโมเดลล่วงหน้า** — Rule-Based AI ใช้ความรู้เชิงฟิสิกส์ของเซนเซอร์ในห้องพัก (PIR, Ultrasonic หน้าประตู, LDR/BH1750, SW-420, DHT11/22 และ BLE RSSI แบบ optional) เขียนเป็นกฎเชิงเวลา (Temporal Rules + Evidence Scoring) ได้ทันที
2. **รันบน ESP32 Gateway ได้จริงตั้งแต่วันแรก** — ไฟล์ C++ (`cpp/AirtomicRuleAI.h`, `cpp/AirtomicRuleAI.cpp`) ไม่ใช้ dynamic heap allocation ใช้งาน RAM น้อยมาก และทำงานระดับไมโครวินาทีใน FreeRTOS Task ได้เลย
3. **แก้จุดอ่อนของ PIR แบบเดิมได้ครบ** — ไม่ได้ดูแค่ค่าปัจจุบัน 1 จุด แต่จำเหตุการณ์ **ประตู (HC-SR04) เทียบกับ PIR หลังผ่านช่วงหน่วง** ทำให้แยกออกว่า:
   - มีคนขยับตัว → `AWAKE`
   - ไม่มีคนขยับนาน แต่ **ไม่ได้เดินออกประตู** (หรือมือถือยังอยู่ / ไฟดับตอนนอน) → `SLEEPING` (`"มีคน - นอนหลับ/นั่งนิ่ง"`) **ไม่ตัดไฟ/แอร์**
   - มีเหตุการณ์ **ประตู (เข้า-ออก)** แล้ว PIR นิ่งต่อเนื่องหลังพ้นช่วง `doorExitGraceMs` (และ/หรือ BLE หาย) → `ABSENT` พร้อมหน่วงยืนยันก่อนสั่ง Servo ปิด
4. **ต่อยอดเป็น Machine Learning (Phase 2) ได้ง่าย** — เมื่อบอร์ดเริ่มส่งข้อมูลเข้า Firebase `history/<node_id>` แล้ว สามารถ export ข้อมูลจริงจาก Firebase มาปรับ threshold หรือฝึกโมเดล ML (เช่น Decision Tree / Random Forest) เพื่อเปรียบเทียบต่อได้

---

## โครงสร้างไฟล์

```text
ai_model/
├── README.md                    # เอกสารอธิบายสถาปัตยกรรม, กฎ, และการเชื่อมต่อ Gateway
└── cpp/
    ├── AirtomicRuleAI.h         # Header พร้อมใช้บน ESP32 Gateway (C++11/17)
    ├── AirtomicRuleAI.cpp       # Implementation (Zero-heap, Temporal Rule Scoring)
    └── test_rule_ai.cpp         # Unit tests บน PC (g++)
```

---

## สรุปการทำงานของ Rule-Based AI

### 1. สถานะของเครื่องใช้ไฟฟ้า (Appliance Detection + Debounce/Hysteresis)
- **ไฟส่องสว่าง (`light`: `ON` / `OFF`)**:
  - รองรับทั้ง `lux` จาก BH1750 (`>= 60` เปิด, `<= 35` ปิด) และ `lightRaw` จาก LDR ADC 12 บิต (`>= 1800` เปิด, `<= 1400` ปิด; มี `lightInverted` เผื่อกรณีต่อ LDR แบบกลับขั้ว)
  - ใช้ Hysteresis ป้องกันค่ากระพริบ และจำเวลาที่คนกดเปิด/ปิดไฟล่าสุด (ยกเว้นช่วงที่ Servo ของระบบเพิ่งกดปิดเอง)
- **แอร์ (`ac`: `ON` / `OFF`)**:
  - ใช้ Sliding Window กับค่า `vib` จาก SW-420/KY-002 (ต้องพบการสั่น `>= 2` จาก 4 รอบล่าสุดจึงนับว่าเปิด กันสัญญาณกระแทก/เคาะครั้งเดียวหลอก และต้องนิ่งต่อเนื่อง `acVibOffGraceMs = 15s` จึงนับว่าปิด)

### 2. การตรวจจับประตู (HC-SR04 Self-Calibrating Doorway Detector)
- รองรับการติด **ข้างกำแพงขวางหน้าประตู (`DoorTriggerMode::SHORTER_ONLY`, ค่าเริ่มต้น)** ซึ่งระยะปกติคือเต็มความกว้างประตู (`doorBaselineCm`, เช่น ~80–120 cm) และจะ **สั้นลง** เมื่อเปิดประตูหรือมีคนเดินผ่าน
- ช่วงเริ่มต้นจะเก็บค่า `distCm` ที่อ่านได้ 3 ค่าแรกแล้วหาค่ามัธยฐาน (Median-of-3) เป็นระยะประตูปกติ (`doorBaselineCm`) และปรับตามช้า ๆ เมื่อค่านิ่ง
  - **กันสัญญาณสะท้อนขึ้นหลอก**: ในโหมด `SHORTER_ONLY` ค่า `distCm` ที่พุ่งสูงกว่า baseline ชั่วคราวจะไม่ทำให้เกิด `doorEvent` หลอก
  - **แก้ตัวอัตโนมัติถ้ามีคนยืนขวางตอนเปิดเครื่อง**: หาก baseline แรกคลาดเคลื่อนต่ำเกินไปและระยะจริงกลับมาเต็มประตูนานเกิน 3 รอบ ระบบจะปรับ `doorBaselineCm` ขึ้นให้อัตโนมัติ
  - *(หากติดเซนเซอร์หันเข้าบานประตูที่เปิดแล้วระยะไกลขึ้น สามารถเปลี่ยนเป็น `DoorTriggerMode::BOTH_DIRECTIONS` ได้)*
- เมื่อ `doorBaselineCm - distCm >= 20 cm` จะถือว่าเกิด `doorEvent` และรีเซ็ตสถานะ `motionConfirmedAfterDoor = false`
- หากหลังจาก `doorExitGraceMs` (15 วินาที, กันจังหวะ hold-time ของ HC-SR501 ตอนเดินออก) มี `pir == 1` เกิดขึ้นในห้อง → ยืนยันว่ายังอยู่ข้างใน (`motionConfirmedAfterDoor = true`)
- หากระยะสั้นลงต่อเนื่องนานเกิน `doorAjarTimeoutMs` → ตั้งธง `doorOpenTooLong = true`

> **เคล็ดลับฝั่ง Sensor Node (`airtomic/src/main.cpp`)**: ปัจจุบัน Sensor Node อ่านค่า `HC-SR04` เพียง 1 ครั้งทุก 5 วินาที (`NODE_PERIOD_MS = 5000`) หากคนเดินผ่านประตูเร็ว (~1 วินาที) และปิดประตูทันทีอาจหลุดช่วงที่ส่งพอดี แนะนำให้ฝั่ง Sensor Node อ่าน `HC-SR04` ถี่ขึ้น (เช่น ทุก 150–200 ms) แล้วส่ง **ค่าน้อยที่สุด (`minDist`) ในรอบ 5 วินาทีนั้น** ขึ้นมาใน packet (หลักการเดียวกับ `vib` ที่จำค่าไว้ทั้งหน้าต่าง 5 วินาที) จะจับการเดินเข้า-ออกได้แม่นขึ้นมากครับ

### 3. การให้คะแนนแบบถ่วงน้ำหนัก (Weighted Evidence Scoring → Softmax Confidence)
ระบบคำนวณคะแนนสะสมให้ 3 สถานะ (`scoreAwake`, `scoreSleeping`, `scoreAbsent`) จากหลักฐานที่พร้อมใช้งานจริง (ถ้าเซนเซอร์ตัวไหนพัง/หลุด เช่น `temp=NaN`, `dist=-1`, `pir=-1`, หรือยังไม่เปิด BLE จะไม่ทำให้ระบบพังและลดน้ำหนักส่วนนั้นอัตโนมัติ):

| สัญญาณ / กฎ | ผลต่อคะแนน | เหตุผล |
| --- | --- | --- |
| `pir == -1` (Warmup 60 วินาทีแรก) | `AWAKE +0.5`, ห้าม `ABSENT`/Auto-off | รอ HC-SR501 อุ่นเครื่องให้เสถียรก่อนตัดสิน |
| `pir == 1` หรือเพิ่งขยับไม่เกิน `awakeHoldMs` | `AWAKE +4.0` | พบการเคลื่อนไหวชัดเจน |
| นิ่งไม่เกิน `stillTransitionMs` (3 นาที) | `AWAKE +1.5`, `SLEEPING +1.2` | ช่วงเปลี่ยนผ่านจากการขยับเป็นนั่งนิ่ง |
| นิ่งเกิน `stillTransitionMs` | `SLEEPING +1.5`, `ABSENT +1.5` | นิ่งนาน ต้องใช้ประตู/BLE/ไฟช่วยตัดสิน |
| เกิด `doorEvent` แล้วไม่มี PIR หลังพ้น `doorExitGraceMs` | `ABSENT +3.2` | พบรูปแบบเดินออกประตูแล้วห้องเงียบ |
| มี PIR ในห้องหลัง `doorEvent` แล้วต่อมานิ่งนาน | `SLEEPING +2.6` | ยังอยู่ข้างในแล้วนั่งนิ่งหรือหลับ |
| BLE RSSI มือถืออยู่ใกล้ (`> -82 dBm`) ขณะนิ่งนาน | `SLEEPING +2.5` (หรือ `AWAKE +1.8` ถ้านิ่งไม่นาน) | มือถือยังอยู่ในห้อง |
| BLE สแกนเปิดอยู่ แต่มือถือหลุดระยะ/หายเกิน `bleLostTimeoutMs` | `ABSENT +2.8` | มือถือออกจากห้องแล้ว (ทำงานเฉพาะหลัง PIR พ้น warmup) |
| ห้องมืดและยังอยู่ในห้อง (ไม่พบเดินออก & BLE ไม่หาย) | `SLEEPING +1.8` | ปิดไฟนอนหลับ |
| ไฟยังเปิดขณะนิ่งนาน | หากเข้าข่ายออกประตู/BLE หาย → `ABSENT +1.2` มิฉะนั้น `SLEEPING +1.0` | แยกระหว่าง "ลืมปิดไฟ" กับ "นั่งอ่านหนังสือ/ทำงานนิ่ง ๆ" |

จากนั้นแปลงคะแนนเป็น `(state, confidence)` ด้วย Softmax (`T = 2.0`, clamp สูงสุด `0.99`) โดยตั้ง tie-breaker และ margin (`absentScoreMargin = 0.8`) ให้เอียงไปฝั่งมีคนอยู่เพื่อความปลอดภัยไม่ตัดไฟผิดจังหวะ

### 4. การแจ้งเตือน + สั่ง Servo + ตรวจผลซ้ำ (Auto-Off Verification)
- แจ้งเตือน (`shouldAlert`) เมื่ออยู่สถานะ `ABSENT` ต่อเนื่อง >= `alertDelayMs` (`60s`) และยังเปิดแอร์หรือไฟทิ้งไว้ (`wastingEnergy`)
- สั่ง Servo (`shouldTurnOffAc` / `shouldTurnOffLight`) เมื่ออยู่ `ABSENT` ต่อเนื่อง >= `absentConfirmMs` (`180s` ในโหมดจริง, `30s` ใน `demoFast()`) และ `confidence >= 0.75`
- เมื่อ Gateway สั่ง Servo กดปิด ให้เรียก `engine.notifyAutoOffTriggered(offAc, offLight, nowMs)` เพื่อเริ่มสถานะ `PENDING` และตรวจซ้ำด้วย `vib` / `lightRaw` (หรือ `lux`):
  - ถ้าดับจริง → `VERIFIED_OFF`
  - ถ้ายังติดอยู่หลัง `verifyWaitMs` → `RETRY_NEEDED` (ลองใหม่ได้ไม่เกิน `maxAutoOffRetries = 2` ครั้ง เพื่อไม่ให้ Servo กดปุ่ม Toggle วนเปิด-ปิดซ้ำไปมา)

---

## วิธีรันทดสอบบน PC

```powershell
g++ -std=c++17 -O2 -Wall -Wextra -Werror ai_model\cpp\AirtomicRuleAI.cpp ai_model\cpp\test_rule_ai.cpp -o $env:TEMP\test_rule_ai.exe
& $env:TEMP\test_rule_ai.exe
```

---

## แนวทางเชื่อมต่อเข้า ESP32 Gateway (`airtomic/`)

1. คัดลอก `ai_model/cpp/AirtomicRuleAI.h` และ `ai_model/cpp/AirtomicRuleAI.cpp` ไปไว้ใน `airtomic/src/gateway/` (หรือเพิ่มใน `lib/AirtomicRuleAI/` ของ PlatformIO)
2. ใน `airtomic/src/gateway/main.cpp` สร้าง instance ต่อ Node (เช่น `AirtomicRuleEngine aiEngine;` หรือ `Demo` ตอนพรีเซนต์ด้วย `RuleAIConfig::demoFast()`):
```cpp
#include "AirtomicRuleAI.h"
#include <esp_timer.h>

// ใช้ RuleAIConfig::roomDefault() สำหรับใช้งานจริง หรือ RuleAIConfig::demoFast() ตอนเดโม 30 วินาที
airtomic_ai::AirtomicRuleEngine aiEngine(airtomic_ai::RuleAIConfig::roomDefault());

// ใน handlePacket(const ReceivedPacket &packet) หลังอ่าน local sensors แล้ว:
airtomic_ai::SensorInput aiInput;
aiInput.timestampMs = static_cast<uint64_t>(esp_timer_get_time() / 1000ULL);
aiInput.tempC = reading.temp;
aiInput.humPct = reading.hum;
aiInput.vib = static_cast<int8_t>(reading.vib);
aiInput.distCm = reading.dist;
aiInput.pir = static_cast<int8_t>(upload.local.pir);
aiInput.lightRaw = static_cast<int16_t>(upload.local.lightRaw);
aiInput.bleScannerEnabled = false; // เปลี่ยนเป็น true และใส่ค่า rssi เมื่อเพิ่ม BLE Scanner แล้ว

airtomic_ai::RoomDecision decision = aiEngine.update(aiInput);
```
3. ใน `airtomic/src/gateway/firebase_cloud.cpp` ส่งฟิลด์ `state`, `conf`, `ac`, `light` (ตรงกับ `airtomic-dashboard/interfaces.ts`):
```cpp
telemetry["state"] = airtomic_ai::roomStateToString(decision.state); // "AWAKE" | "SLEEPING" | "ABSENT"
telemetry["conf"]  = roundf(decision.confidence * 100.0f) / 100.0f;
telemetry["ac"]    = decision.acOn ? "ON" : "OFF";
telemetry["light"] = decision.lightOn ? "ON" : "OFF";
```
> **หมายเหตุเรื่องฟิลด์ Dashboard**: ปัจจุบัน `airtomic/src/gateway/firebase_cloud.cpp` ส่งค่าความสว่างในชื่อ `light_raw` ขณะที่ `airtomic-dashboard/app/page.tsx` อ่าน `data.lux` ดังนั้นเมื่อเชื่อมต่อ Gateway กับ Dashboard ควรปรับให้ใช้ชื่อฟิลด์ตรงกัน (หรือ map `light_raw` ในฝั่ง Dashboard/Gateway) ด้วยครับ
