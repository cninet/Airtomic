# แผนการทดสอบระบบ Airtomic (Test Plan & Verification Matrix)

> **โปรเจกต์:** Airtomic — ระบบตรวจจับและป้องกันการลืมปิดไฟ-ปิดแอร์อัจฉริยะด้วย ESP32 + Edge AI  
> **รายวิชา:** Embedded System (CEDT) 2026  
> **ผู้รับผิดชอบ:** Project Manager & Test Lead  
> **เกณฑ์อ้างอิง:** Section II (Scalability), Section III (M1–M8 & Embedded Engineering Depth E1, E2, E4, E5, E6), Section IV (Testing & Evaluation: Test Plan 3/3, Quantitative Metrics 4/4, Analysis & Comparison 3/3)

---

## 1. ขอบเขตระบบที่ทดสอบ (System Under Test — SUT)

ระบบ Airtomic ประกอบด้วย 4 ชั้นหลักที่ทำงานร่วมกันแบบ End-to-End:

1. **Sensor Node Layer (`SN01` Physical + `SN02`, `SN03` Simulated):**
   - บอร์ด ESP32 ส่งแพ็กเก็ตไบนารี `NodeMsg` (26 bytes, `AIRTOMIC_MSG_VER = 1` ตาม `firmware/common/messages.h`) ผ่าน **ESP-NOW Unicast** ทุก `NODE_PERIOD_MS = 5000 ms`
   - เซนเซอร์บน Node: `DHT22/DHT11` (อุณหภูมิ/ความชื้นหน้าแอร์), `SW-420` (แรงสั่นแอร์ผ่าน Interrupt), `HC-SR04` (ระยะหน้าประตู)
   - การรับประกันการส่ง (E4): รอ `AckMsg` (13 bytes) ภายใน `ACK_TIMEOUT_MS = 200 ms` และส่งซ้ำด้วย `seq` เดิมสูงสุด `MAX_RETRIES = 3` ครั้ง
2. **Gateway & Edge Processing Layer (`GW01`):**
   - บอร์ด ESP32 รับแพ็กเก็ตจาก `SN01`/`SN02`/`SN03`, ส่ง `AckMsg`, ตัดแพ็กเก็ตซ้ำ (`Deduplication`), และเก็บลงคิว FreeRTOS (`cloudQueue` ขนาด 32 รายการสำหรับ Store-and-Forward)
   - เซนเซอร์บน Gateway: `BH1750` (I2C Lux) หรือ `LDR` (ADC1 GPIO34), `HC-SR501 PIR` (Interrupt + Deferred Processing + Warm-up 60 วินาที), และ `BLE RSSI` (ตรวจจับการอยู่ใกล้ของมือถือผู้ใช้งาน)
   - Edge AI Classification: จำแนกสถานะห้องเป็น 3 สถานะ (`AWAKE`, `SLEEPING`, `ABSENT`) พร้อมค่าความมั่นใจ (`conf`)
   - Actuator: `Servo Motor` กดสวิตช์ปิดไฟ/รีโมตแอร์ พร้อมระบบ Safety Timeout (M7)
3. **Cloud & Security Layer (Firebase Realtime Database over HTTPS TLS):**
   - เชื่อมต่อด้วย `WiFiClientSecure` + ตรวจสอบ Root CA (`GOOGLE_ROOT_CAS`) และยืนยันตัวตน Firebase Auth (`gateway@airtomic.app`)
   - โครงสร้างข้อมูลตาม `firebase/database.rules.json`:
     - `nodes/<node_id>` (ค่าล่าสุด)
     - `history/<node_id>/<entry>` (Append-only log สำหรับวิเคราะห์ Quantitative Metrics)
     - `cmd/<node_id>/{ac|light}` (อนุญาตเฉพาะคำสั่ง `"OFF"`)
4. **Application & Alerting Layer:**
   - Next.js Web Dashboard (`airtomic-dashboard/`) แสดงผล Real-time ผ่าน `onValue('nodes')` รองรับสลับดูหลายโหนด (`SN01`, `SN02`, `SN03`) และกดสั่งปิดไฟ/แอร์ระยะไกล
   - ระบบแจ้งเตือนผ่าน LINE / Discord Webhook เมื่อสถานะเป็น `ABSENT` แต่ `ac == "ON"` หรือ `light == "ON"`

> **หมายเหตุจาก การ Audit โค้ดปัจจุบัน (ก่อน Integrate สมบูรณ์):**  
> ขณะนี้มี Firmware 2 ชุดคือ `firmware/gateway/gateway.ino` (Arduino: BH1750 + `/cmd` polling แต่ยังไม่มี ESP-NOW) และ `airtomic/src/gateway/main.cpp` (PlatformIO: ESP-NOW + ACK + Store-and-forward แต่ส่ง `light_raw` แทน `lux` และยังไม่มี AI/BLE/Actuator/`/cmd`) ทีมต้อง Merge ฟีเจอร์ทั้งหมดและปรับชื่อฟิลด์ให้ตรง `airtomic-dashboard/interfaces.ts` ก่อนรันรอบ Final Acceptance Test

---

## 2. สภาพแวดล้อมและอุปกรณ์การทดสอบ

| รายการ | รายละเอียด |
| --- | --- |
| **Hardware (Physical)** | ESP32 #1 (`SN01`: DHT22/DHT11, SW-420, HC-SR04), ESP32 #2 (`GW01`: BH1750/LDR, HC-SR501 PIR, Servo Motor), ESP32 #3 (รัน `tests/sim_nodes/sim_nodes.ino` จำลอง `SN02`+`SN03`) |
| **Network** | เราเตอร์/Hotspot Wi-Fi 2.4 GHz (ระบุ Channel ชัดเจนเพื่อให้ ESP-NOW ตรงช่องสัญญาณ) |
| **Cloud & Web** | Firebase RTDB (`airtomic-29e37-default-rtdb.asia-southeast1.firebasedatabase.app`), Next.js Dashboard (`localhost:3000` หรือ Production URL) |
| **QA Scripts** | `tests/sim_nodes/sim_nodes.ino`, `tests/sim_nodes/firebase_sim_nodes.py`, `firebase/test_rules.sh`, `tests/metrics/analyze_metrics.py` |

---

## 3. ตารางกรณีทดสอบทั้งหมด (Test Cases Matrix)

> **วิธีบันทึกผล:** ให้ผู้ทดสอบกรอกคอลัมน์ **"ผลจริง"** ด้วยค่าตัวเลขจริง/หลักฐาน (เช่น ภาพหน้าจอ, Serial Log, หรือลิงก์ไฟล์ผลวิเคราะห์) และระบุ `Pass` หรือ `Fail` ในคอลัมน์ **"สถานะ"**

### หมวดที่ 1: การทำงานปกติ (Normal Use — `TC-N01` ถึง `TC-N10`)

| Test ID | หมวดหมู่ | เกณฑ์ที่เกี่ยวข้อง | เงื่อนไขตั้งต้น | ขั้นตอนการทดสอบ | ผลที่คาดหวัง | ผลจริง (เว้นไว้กรอก) | สถานะ (Pass/Fail) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **TC-N01** | Normal Use | M1 (Sensors — Node) | `SN01` ต่อ DHT22/DHT11, SW-420, HC-SR04 ครบและเปิด Serial Monitor 115200 | 1. เปิดบอร์ด `SN01` รอ Warm-up 3 วินาที<br>2. ส่องลมเย็น/ลมปกติใส่ DHT, เคาะ SW-420 เบา ๆ, ยื่นวัตถุระยะ 50 cm หน้า HC-SR04 | Serial แสดงค่า `temp` (15–40°C), `hum` (20–90%), `vib=1` เมื่อมีการสั่น (และรีเซ็ตเป็น 0 รอบถัดไป), `dist` อยู่ในช่วง `50 ± 3 cm` | | |
| **TC-N02** | Normal Use | M1, E2 (Sensors — Gateway + PIR ISR) | `GW01` ต่อ BH1750 (หรือ LDR) และ HC-SR501 PIR ครบ และผ่าน Warm-up PIR 60 วินาทีแล้ว | 1. เปิดไฟห้องและปิดไฟห้องเพื่อเทียบค่าความสว่าง<br>2. โบกมือผ่านหน้า PIR 1 ครั้ง แล้วหยุดนิ่ง 15 วินาที | ค่า `lux` (หรือ `light_raw`) เปลี่ยนสอดคล้องกับแสงจริงอย่างชัดเจน, PIR interrupt ทำงานและรายงาน `pir=1` ในรอบอ่านถัดไป (Deferred Processing) แล้วกลับเป็น `0` เมื่อนิ่ง | | |
| **TC-N03** | Normal Use | M1, M2 (BLE RSSI Proximity) | เปิด Bluetooth บนมือถือเจ้าของห้องที่ลงทะเบียน MAC/UUID ไว้กับ `GW01` | 1. วางมือถือในห้อง (ห่าง ~1–3 ม.) สังเกตค่า `rssi`<br>2. เดินพกมือถือออกนอกห้อง (>10 ม. หรือปิด BT) สังเกตค่า `rssi` | เมื่ออยู่ในห้อง `rssi` ประมาณ `-45` ถึง `-75 dBm`; เมื่อออกนอกห้อง `rssi < -85 dBm` หรือไม่พบอุปกรณ์ | | |
| **TC-N04** | Normal Use | M4, E4 (ESP-NOW + ACK Handshake) | `GW01` เชื่อม Wi-Fi สำเร็จและพิมพ์ MAC + Channel; `SN01` ตั้งค่า MAC/Channel ตรงกัน | 1. เปิด `SN01` และ `GW01` พร้อมกันเป็นเวลา 5 นาที (60 แพ็กเก็ต)<br>2. ตรวจสอบ Serial ของทั้งสองบอร์ด | `SN01` ได้รับ `AckMsg` ตรง `node_id` และ `seq` (`ACK`) มากกว่า 95% ของแพ็กเก็ต, `GW01` พิมพ์ `Queued SN01 #<seq>` และ `Firebase saved SN01 #<seq>` | | |
| **TC-N05** | Normal Use | M2 (AI State Classification — 3 States) | `GW01` รันโมเดล AI (`ai_model/`) ครบทุกฟีเจอร์ | ทดสอบ 3 สถานการณ์จริงอย่างละ 5 นาที:<br>1. เดิน/ขยับตัวในห้อง เปิดไฟ+แอร์ (`AWAKE`)<br>2. นอนนิ่งบนเตียง ปิดไฟ เปิดแอร์ มีมือถือใกล้ตัว (`SLEEPING`)<br>3. ออกนอกห้อง ปิดประตู ทิ้งไฟ/แอร์เปิด (`ABSENT`) | ระบบทำนายสถานะ `state` ตรงกับความเป็นจริงทั้ง 3 สถานะด้วยความแม่นยำรวม ≥ 85% และส่งค่า `conf` (0.0–1.0) ขึ้น Firebase ถูกต้อง | | |
| **TC-N06** | Normal Use | M3 (Servo Actuator — Auto & Manual OFF) | ติดตั้ง Servo ที่สวิตช์ไฟ/รีโมตแอร์ และเปิดไฟ+แอร์อยู่ | 1. กดปุ่ม **"สั่งปิด"** AC และ Light บน Dashboard<br>2. สังเกตการทำงานของ Servo และการยืนยันผลจากเซนเซอร์ (`vib`/`lux`) | Dashboard เขียน `cmd/SN01/ac="OFF"`, `GW01` รับคำสั่งภายใน ≤ 2 วินาที, Servo หมุนกดสวิตช์ปิดได้จริงแล้วคืนตำแหน่งเดิม, `GW01` ลบ `cmd` และอัปเดต `ac="OFF"` / `light="OFF"` บน Firebase | | |
| **TC-N07** | Normal Use | M5, E5 (Firebase RTDB + Dashboard Live) | เข้าสู่ระบบ Dashboard ด้วยบัญชี Operator ที่มีสิทธิ์ใน `operators/<uid>=true` | 1. เปิด Dashboard คู่กับ Serial Monitor ของ `GW01`<br>2. ตรวจสอบความสอดคล้องของทุกการ์ดและเวลาอัปเดต (`ts`)<br>3. ตรวจข้อมูลใน `history/SN01` | หน้าเว็บอัปเดต Real-time โดยไม่ต้องรีเฟรช, ค่า `temp, hum, vib, dist, lux, pir, rssi, state, conf, ac, light` ตรงกับ `GW01`, ข้อมูลประวัติถูก Append ครบทุก `seq` | | |
| **TC-N08** | Normal Use | M6 (Alerting — LINE / Discord) | ตั้งค่า Webhook URL/Token ผ่าน Environment/Secrets (ไม่ฮาร์ดโค้ดใน Git) | จำลองสถานะ `ABSENT` ขณะที่ `ac="ON"` หรือ `light="ON"` ต่อเนื่องเกินเวลาที่กำหนด (เช่น 5 นาที หรือโหมดทดสอบ 30 วินาที) | ระบบส่งข้อความแจ้งเตือนเข้า LINE/Discord สำเร็จภายใน 10 วินาที พร้อมระบุ `node_id`, สถานะอุปกรณ์ที่เปิดค้าง, และค่า `conf` และไม่ส่งสแปมซ้ำถี่เกิน Cooldown | | |
| **TC-N09** | Normal Use | Section II (Scalability: `SN01` + `SN02` + `SN03`) | `SN01` (Physical) และ ESP32 ตัวที่ 3 รัน `tests/sim_nodes/sim_nodes.ino` (`SN02`, `SN03`) ส่งหา `GW01` พร้อมกัน | 1. รันทั้ง 3 โหนดพร้อมกันเป็นเวลา 15 นาที<br>2. เปิด Dashboard และคลิก Dropdown เลือกสลับระหว่าง `SN01`, `SN02`, `SN03`<br>3. ตรวจ Firebase `nodes/` และ `history/` | `GW01` แยกเก็บและอัปโหลด `nodes/SN01`, `nodes/SN02`, `nodes/SN03` และ `history/*` ไม่ปนกัน; Dashboard แสดงตัวเลือกครบ 3 โหนด และแสดงค่าแต่ละโหนดถูกต้อง (PDR ≥ 95% ทุกโหนด) | | |
| **TC-N10** | Normal Use | E1 (FreeRTOS Concurrency ≥ 3 Tasks) | `GW01` รัน FreeRTOS Tasks แยกหน้าที่ (เช่น ESP-NOW/Sensor Task, AI/Actuator Task, Cloud Upload Task) | 1. รันระบบขณะมีโหลดจาก 3 โหนดพร้อมกันและสั่งปิดอุปกรณ์จากเว็บ<br>2. พิมพ์ FreeRTOS Task High-Water Mark & Free Heap ผ่าน Serial ทุก 60 วินาที | ไม่มี Task ใดบล็อกการรับ ESP-NOW หรือ PIR ISR แม้ช่วง HTTPS upload ช้า, ไม่เกิด Watchdog Reset, Stack High-Water Mark เหลือปลอดภัย (> 512 bytes) | | |

---

### หมวดที่ 2: กรณีขอบเขต (Edge Cases — `TC-E01` ถึง `TC-E07`)

| Test ID | หมวดหมู่ | เกณฑ์ที่เกี่ยวข้อง | เงื่อนไขตั้งต้น | ขั้นตอนการทดสอบ | ผลที่คาดหวัง | ผลจริง (เว้นไว้กรอก) | สถานะ (Pass/Fail) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **TC-E01** | Edge Case | M2, M7 (Sleeping / Sitting Still — No False Shutoff) | ผู้ทดสอบอยู่ในห้อง เปิดแอร์ (`vib=1`), ปิดไฟ (`lux < 20`), มือถืออยู่ในห้อง (`rssi ≈ -60 dBm`) | นอนหลับหรือนั่งอ่านหนังสืออยู่นิ่ง ๆ หน้า PIR เป็นเวลา 30 นาทีเต็ม (ไม่ให้ PIR Trigger) | ระบบจำแนกเป็น `SLEEPING` (ไม่ใช่ `ABSENT`), **ห้าม** สั่ง Servo ปิดแอร์โดยเด็ดขาด (False-Absent Rate ขณะนอน < 5% และไม่ผ่านเงื่อนไข Auto-Shutoff) | | |
| **TC-E02** | Edge Case | M1, M2 (Door Left Open Continuously) | ประตูห้องเปิดค้างอยู่ด้านหน้าเซนเซอร์ `HC-SR04` | เปิดประตูค้างไว้ 15 นาที (ให้ระยะ `dist` คงที่ที่ค่าต่ำ เช่น 20–40 cm หรือค่าสูงสุด `-1` เมื่อไม่มีสิ่งกีดขวาง) ทั้งตอนมีคนและไม่มีคน | ระบบไม่ค้าง ไม่เข้าใจผิดว่ามีคนเดินเข้า-ออกตลอดเวลา และยังคงอาศัยฟีเจอร์อื่น (PIR, BLE RSSI, Temp, Lux) ตัดสินใจได้ถูกต้อง | | |
| **TC-E03** | Edge Case | M1, M2 (Rapid Ambient Light Fluctuation) | ห้องอยู่ในสถานะมีคนหรือไม่มีคน โดยปิดไฟห้อง | ส่องไฟฉาย/เปิด-ปิดม่าน/เกิดแสงวาบใส่ `BH1750`/`LDR` สลับเร็วทุก 2 วินาที เป็นเวลา 1 นาที | ค่า `lux`/`light_raw` ตอบสนองปกติโดยไม่ทำให้ I2C hang หรือโมเดล AI แกว่งจนสั่งปิดไฟ/แอร์ผิดพลาดจากสัญญาณรบกวนชั่วคราว | | |
| **TC-E04** | Edge Case | E4 (Duplicate Packet Deduplication) | `GW01` และ `tests/sim_nodes/sim_nodes.ino` เชื่อมต่อกันปกติ | ส่งคำสั่ง `d` ทาง Serial ของ `sim_nodes.ino` เพื่อยิง `SN02` ด้วย `seq` เดิมซ้ำ 2 ครั้งภายใน `< 500 ms` (อยู่ในหน้าต่าง `kDuplicateWindowMs = 1100 ms`) | `GW01` ส่ง `AckMsg` กลับครบทุกครั้งเพื่อหยุด Node ไม่ให้ retry แต่ **ไม่** เขียนเรคคอร์ด `seq` ซ้ำลงใน `history/SN02` บน Firebase (Dedup Accuracy = 100%) | | |
| **TC-E05** | Edge Case | M7 (Safety: Servo Timeout & Anti-Stall) | เชื่อมต่อ Servo กับ `GW01` | สั่งปิด AC/Light แล้วลองขัดขวางแขน Servo เบา ๆ (หรือตรวจสอบเวลาจับสัญญาณ PWM) | Servo ออกแรงกดตามเวลาที่กำหนด (เช่น ≤ 1.5 วินาที) แล้วดีดกลับตำแหน่งพักและตัดสัญญาณ PWM (`detach()` หรือ 0 duty) ทันที เพื่อป้องกันมอเตอร์ไหม้และสวิตช์พัง | | |
| **TC-E06** | Edge Case | M7, E5 (Manual Emergency Override & Invalid Command Rejection) | ระบบอยู่ในช่วงนับถอยหลังเตรียมตัดไฟ หรือ Dashboard เชื่อมต่อ Firebase | 1. ผู้ใช้กดปุ่มยกเลิก/Emergency Stop หรือสั่งงานผ่านปุ่มกดบนบอร์ด<br>2. ทดสอบส่งค่าคำสั่งผิดกฎ `cmd/SN01/ac = "ON"` หรือ `cmd/SN01/fan = "OFF"` ผ่าน API | 1. การตัดไฟอัตโนมัติถูกยกเลิกทันทีและบันทึกสถานะชัดเจน<br>2. Firebase Rules ปฏิเสธด้วย HTTP `401` (อนุญาตเฉพาะ `ac`/`light` = `"OFF"` เท่านั้น) | | |
| **TC-E07** | Edge Case | M1, E2 (PIR 60-Second Warm-up Guard) | รีบูต `GW01` ใหม่ทั้งหมด | โบกมือหน้า HC-SR501 ทันทีในช่วงวินาทีที่ 5–55 หลังบูต และโบกมืออีกครั้งหลังวินาทีที่ 65 | ช่วง 0–60 วินาที ระบบละเว้นสัญญาณลวงของ PIR (ส่งค่า `-1` หรือไม่แนบ `pir` ใน JSON); หลัง 60 วินาที PIR รายงาน `pir=1` ได้แม่นยำ | | |

---

### หมวดที่ 3: สภาวะความล้มเหลว (Failure Conditions — `TC-F01` ถึง `TC-F08`)

| Test ID | หมวดหมู่ | เกณฑ์ที่เกี่ยวข้อง | เงื่อนไขตั้งต้น | ขั้นตอนการทดสอบ | ผลที่คาดหวัง | ผลจริง (เว้นไว้กรอก) | สถานะ (Pass/Fail) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **TC-F01** | Failure | Robustness, E4 (Wi-Fi Outage + Store-and-Forward) | `SN01`+`SN02`+`SN03` ส่งข้อมูลเข้า `GW01` และขึ้น Firebase ปกติ | 1. ปิด Wi-Fi Hotspot / ตัดอินเทอร์เน็ตเป็นเวลา 60 วินาที (จับเวลา)<br>2. เปิด Wi-Fi กลับคืนโดย **ห้าม** กด Reset บอร์ด<br>3. ตรวจ Serial และ Firebase `history/` | ช่วงเน็ตหลุด `GW01` ยังรับ ESP-NOW, ส่ง ACK และเก็บข้อมูลใน `cloudQueue` (RAM สูงสุด 32 รายการ); เมื่อเน็ตกลับมาระบบต่อ Wi-Fi เองภายใน ≤ 30 วินาที และ Flush ข้อมูลที่ค้างขึ้น Firebase ครบไม่หาย | | |
| **TC-F02** | Failure | Robustness, E4 (Cloud Queue Overflow > 32 Readings) | `SN01`+`SN02`+`SN03` รันพร้อมกัน (รวม ~36 แพ็กเก็ต/นาที) | ตัด Wi-Fi นาน 3 นาที (เกินความจุ `cloudQueue = 32`) แล้วต่อ Wi-Fi กลับคืน | เมื่อคิวเต็ม `GW01` พิมพ์ `Firebase queue full; reading not accepted` และงด ACK อย่างสงบ **ไม่ Crash / ไม่ OOM**; เมื่อเน็ตกลับมาจะ Flush 32 รายการและกลับมาทำงานปกติทันที | | |
| **TC-F03** | Failure | Robustness (Sensor Unplugged / NaN Mid-Run) | ระบบรันปกติทุกเซนเซอร์ | 1. ถอดสาย Data ของ DHT22/DHT11 ขณะบอร์ดทำงาน<br>2. ถอดสาย SDA/SCL ของ BH1750 (หรือ ECHO ของ HC-SR04)<br>3. เสียบสายกลับคืนขณะบอร์ดยังเปิดอยู่ (หรือใช้คำสั่ง `n` บน `sim_nodes.ino`) | Node ส่ง `temp=NAN, hum=NAN, dist=-1`, Gateway ส่ง `lux=-1` และละเว้นฟิลด์ `NaN` ไม่ให้ JSON พัง; ระบบไม่ค้าง/ไม่รีบูต และกลับมาอ่านค่าปกติเมื่อต่อสายคืน (BH1750 re-init อัตโนมัติใน `readLux()`) | | |
| **TC-F04** | Failure | Robustness (Power Loss & Cold Boot Recovery) | ระบบทำงานปกติและมีข้อมูลล่าสุดบน Firebase | 1. ดึงสายไฟเลี้ยงออกจาก `GW01` 15 วินาทีแล้วเสียบใหม่<br>2. ทำซ้ำกับ `SN01` | ทั้งสองบอร์ดบูตเอง, เชื่อม Wi-Fi, Sync NTP, Sign-in Firebase, และกลับมาส่ง-รับ ESP-NOW ได้อัตโนมัติภายใน ≤ 30 วินาทีโดยไม่ต้องกดปุ่มใด ๆ | | |
| **TC-F05** | Failure | E4 (Packet Loss & Retry Exhaustion) | `SN01` หรือ `sim_nodes.ino` รันปกติ | 1. ปิดไฟ `GW01` หรือส่งคำสั่ง `r` (Force Retry) และ `g` (Packet Loss) บน `sim_nodes.ino`<br>2. ตรวจ Serial Log | กรณี Force Retry (ดรอป 2 ACK): Node ส่งซ้ำด้วย `seq` เดิมจนได้ `ACK` ในครั้งที่ 3; กรณีปิด Gateway: Node retry ครบ `MAX_RETRIES=3` (รวม 4 ครั้ง ~800 ms) แล้วพิมพ์ `no ACK` โดยไม่ค้าง loop | | |
| **TC-F06** | Failure | E5, M8 (Security Rules & Secret Hygiene Audit) | ติดตั้ง `firebase/database.rules.json` บน Firebase จริง | 1. รัน `FB_PASSWORD='...' bash firebase/test_rules.sh`<br>2. ตรวจ `git log -p` และ `git grep` หา Wi-Fi password / Firebase password / private token | `test_rules.sh` ผ่านครบ 15/15 กรณี (`0 failure(s)` — anonymous ถูก deny `401`, history เขียนทับไม่ได้ `401`, invalid cmd `401`); ไม่พบ Secret รั่วไหลใน Git history | | |
| **TC-F07** | Failure | E6 (OTA Firmware Update & Safety) | `GW01` แบ่ง Partition แบบ `Minimal SPIFFS (1.9MB APP with OTA)` และเชื่อม Wi-Fi | 1. อัปโหลดเฟิร์มแวร์เวอร์ชันใหม่ (เปลี่ยนเลข `FW_VERSION`) ผ่าน OTA โดยไม่เสียบสาย USB<br>2. ตรวจว่าหลังรีบูต `GW01` กลับมาเชื่อม Wi-Fi + Firebase ได้ทันที | อัปเดตสำเร็จ 100% ผ่านเครือข่าย, บอร์ดรีบูตอัตโนมัติและรายงาน `FW_VERSION` ใหม่บน Serial/Firebase โดยไม่สูญเสียการตั้งค่า | | |
| **TC-F08** | Failure | Field Testing (24-Hour Continuous Endurance Run) | `SN01` + `SN02` + `SN03` + `GW01` รันต่อเนื่องในห้องจริง | เปิดระบบทิ้งไว้ต่อเนื่องอย่างน้อย **24 ชั่วโมงเต็ม** (รวมจำลอง Wi-Fi drop อย่างน้อย 1–2 ครั้ง) แล้ว Export ข้อมูล Firebase + Serial Log มารัน `tests/metrics/analyze_metrics.py` | Uptime ≥ 99%, PDR ≥ 95% ทุกโหนด, ไม่เกิด Memory Leak (Free Heap คงที่), Auto-recovery ทุกครั้งหลังเน็ตหลุด และได้กราฟ+ตารางสถิติครบ 4 ตัวชี้วัด | | |

---

## 4. Rubric Verification Checklist (ตรวจสอบความครบถ้วนตามเกณฑ์รายวิชา)

### 4.1 Mandatory Requirements (M1–M8)

| รหัส | ข้อกำหนดหลัก | หลักฐาน/วิธีการพิสูจน์ใน Airtomic | สถานะโค้ดปัจจุบัน | ผลตรวจ (Pass/Fail) |
| :---: | --- | --- | --- | :---: |
| **M1** | Sensors ≥ 2 ชนิดต่อบอร์ด / ครอบคลุมสภาพแวดล้อม | Node: DHT22/DHT11, SW-420, HC-SR04 (3 ชนิด); Gateway: BH1750/LDR, HC-SR501 PIR + BLE RSSI | มีโค้ดเกือบครบ (ต้องตกลง BH1750 vs LDR และ DHT11 vs DHT22, เพิ่ม BLE RSSI) | [ ] |
| **M2** | Edge Processing / Local Intelligence | โมเดล AI จำแนก `AWAKE` / `SLEEPING` / `ABSENT` บน Gateway (`ai_model/`) | รอ `ai_model/` มา Integrate ลง Gateway | [ ] |
| **M3** | Physical Actuator | Servo Motor กดสวิตช์ปิดไฟและรีโมตแอร์ (Non-invasive) | รอเพิ่มโค้ดคุม Servo ลง Gateway | [ ] |
| **M4** | Wireless Communication | ESP-NOW (Node ↔ Gateway) + Wi-Fi HTTPS (Gateway ↔ Firebase) | เสร็จแล้วใน `airtomic/` (ต้อง Merge กับ `firmware/gateway/`) | [ ] |
| **M5** | Cloud Storage & Dashboard | Firebase RTDB (`nodes/`, `history/`, `cmd/`) + Next.js Dashboard (`airtomic-dashboard/`) | เสร็จแล้ว (ต้องแก้ฟิลด์ `lux` vs `light_raw` และป้าย `SN01`) | [ ] |
| **M6** | Alerting / Notification | แจ้งเตือนผ่าน LINE / Discord เมื่อ `ABSENT` แต่ไฟ/แอร์ยังเปิดอยู่ | รอเพิ่ม Webhook Notification | [ ] |
| **M7** | Safety Mechanisms | Servo Timeout/Anti-stall, Manual Emergency Stop, Firebase Rules อนุญาตเฉพาะ `cmd=OFF`, ไม่ตัดไฟตอน `SLEEPING` | Rules เสร็จแล้ว; รอ Servo Safety & Emergency Stop | [ ] |
| **M8** | Security & Privacy Hygiene | Privacy-first (ไม่ใช้กล้อง), TLS CA pinning (`GOOGLE_ROOT_CAS`), Auth + RBAC (`operators/<uid>`), ไม่มี Secrets ใน Git | เสร็จแล้ว (แนะนำเพิ่ม `gateway_secrets.h` ใน root `.gitignore`) | [ ] |

### 4.2 Scalability & Embedded Engineering Depth (เลือก 5 ข้อ: E1, E2, E4, E5, E6)

| รหัส | หัวข้อ Engineering Depth | เกณฑ์การพิสูจน์ในการ Demo & รายงาน | สถานะโค้ดปัจจุบัน | ผลตรวจ (Pass/Fail) |
| :---: | --- | --- | --- | :---: |
| **Scale** | **Multi-node Scalability** | 1 Physical (`SN01`) + 2 Simulated (`SN02`, `SN03`) ทำงานพร้อมกัน, แยกโหนดบน Firebase & Dashboard ได้ | `tests/sim_nodes/` พร้อมแล้ว; Dashboard มี `<select>` แล้ว | [ ] |
| **E1** | **FreeRTOS Multi-tasking (≥ 3 Tasks)** | แยกอย่างน้อย 3 FreeRTOS Tasks บน Gateway (เช่น Radio/Sensor Task, AI/Actuator Task, Cloud Upload Task) พร้อม Queue/Mutex | ปัจจุบัน `airtomic/src/gateway/main.cpp` มี `uploadTask` + `loopTask` (2 tasks) → ต้องแยกเพิ่มอีกอย่างน้อย 1 task (เช่น AI/Command Task) | [ ] |
| **E2** | **Hardware Interrupt + Deferred Processing** | `HC-SR501 PIR` (บน Gateway) และ `SW-420` (บน Node) ใช้ `IRAM_ATTR` ISR เพียงตั้ง flag/counter แล้วประมวลผลภายนอก ISR | **เสร็จแล้ว** ทั้งใน `sensors.h`, `local_sensors.cpp` และ `src/main.cpp` | [ ] |
| **E4** | **Reliable Comms (ACK/Retry + Store-and-Forward)** | Unicast ESP-NOW + `AckMsg` ภายใน 200 ms + Retry สูงสุด 3 ครั้ง + Deduplication + `cloudQueue` (32 slots) ส่งย้อนหลังเมื่อ Wi-Fi กลับมา | **เสร็จแล้ว** ใน `airtomic/src/main.cpp` และ `airtomic/src/gateway/main.cpp` | [ ] |
| **E5** | **Encrypted Cloud & Secret Management** | `WiFiClientSecure` + `GOOGLE_ROOT_CAS` (ไม่ใช้ `setInsecure()`), Firebase Auth + Strict RTDB Rules (`test_rules.sh`), Secrets ในไฟล์ git-ignored | **เสร็จแล้ว** (รัน `firebase/test_rules.sh` ยืนยันซ้ำก่อนส่ง) | [ ] |
| **E6** | **OTA Firmware Update** | รองรับการอัปเดตเฟิร์มแวร์ผ่าน Wi-Fi โดยไม่ต่อสาย USB (ใช้ Partition `Minimal SPIFFS with OTA`) | กำหนด Partition ใน comment แล้ว → รอเพิ่ม `ArduinoOTA` / HTTP OTA ใน Gateway | [ ] |
| *(Bonus)* | *(E8 Candidate: Custom I2C Driver)* | `firmware/gateway/sensors.h` เขียน Register-level I2C driver คุยกับ BH1750 เองโดยไม่ใช้ไลบรารี | มีโค้ดใน `firmware/gateway/sensors.h` แล้ว | [ ] |

---

## 5. Analysis & Comparison Template (สำหรับ Section IV: 3/3 คะแนน)

> นำค่าสถิติจริงจากไฟล์ `tests/metrics/output/summary_metrics.md` หลังจบการทดสอบ 24 ชม. มาเติมลงในตารางด้านล่างเพื่อนำไปใส่ในเล่มรายงาน 10 หน้า

### ตาราง A: เปรียบเทียบค่าที่วัดได้จริงกับเป้าหมายของโปรเจกต์ (Measured vs. Target)

| ตัวชี้วัด (Quantitative Metric) | วิธีการวัด (Measurement Method) | เป้าหมายโปรเจกต์ (Target) | ค่าที่วัดได้จริง (Mean ± SD / 95th) | ช่วงค่า (Min – Max) | ผลการประเมิน & การวิเคราะห์ (Pass/Fail + Root Cause) |
| --- | --- | :---: | :---: | :---: | --- |
| **1. ESP-NOW RTT** (Sensor → Gateway) | จับเวลา `millis()` ตั้งแต่ `esp_now_send()` จนได้รับ `AckMsg` บน Node | `< 50 ms` | *\<เติมจาก `summary_metrics.md`\>* | *\<Min – Max\>* | *\<วิเคราะห์ผล เช่น ผลของ retry ต่อค่า Max\>* |
| **2. End-to-End Latency** (Sensor → Firebase) | ผลต่างเวลา `fb_ack_ts_ms - sent_ts_ms` จาก Serial Log + Firebase `ts` | `< 1,000 ms` | *\<เติมจาก `summary_metrics.md`\>* | *\<Min – Max\>* | *\<วิเคราะห์ TLS handshake + Wi-Fi jitter\>* |
| **3. Actuator Response Latency** (Sensor → Servo) | เวลาตั้งแต่ตรวจพบเงื่อนไขจน Servo ขยับกดสวิตช์เสร็จ | `< 2,000 ms` | *\<เติมจาก `summary_metrics.md`\>* | *\<Min – Max\>* | *\<วิเคราะห์ระยะเวลา poll `/cmd` หรือ local trigger\>* |
| **4. Packet Delivery Rate (PDR)** (`SN01`/`SN02`/`SN03`) | คำนวณจากลำดับ `seq` ที่ขาดหายใน `history/<node_id>` ตลอด 24 ชม. | `≥ 95.0%` | *\<SN01 / SN02 / SN03 % + Hourly SD\>* | *\<Min – Max\>* | *\<อธิบายบทบาทของ E4 Retry 3 ครั้ง + Dedup\>* |
| **5. System Uptime (24-Hour Field Test)** | สัดส่วนเวลาที่ระบบส่งข้อมูลได้ปกติเทียบเวลาทดสอบ 24 ชม. | `≥ 99.0%` | *\<เติมจาก `summary_metrics.md`\>* | Downtime: *\<X s\>* | *\<ระบุจำนวนครั้งที่เน็ตหลุดและ Store-and-Forward ช่วยกู้กี่แพ็กเก็ต\>* |
| **6. Auto-Recovery Time** (Wi-Fi / Power Cut) | จับเวลาตั้งแต่เครือข่าย/ไฟกลับมาจนอัปโหลดแพ็กเก็ตแรกสำเร็จ | `< 30 s` | *\<Mean ± SD s\>* | *\<Min – Max s\>* | *\<เทียบ Wi-Fi reconnect กับ Cold Boot + NTP sync\>* |
| **7. AI Occupancy Classification** (Accuracy / Macro F1) | เทียบ `state` ที่ระบบทำนายกับ Ground Truth CSV (`AWAKE`/`SLEEPING`/`ABSENT`) | `Acc ≥ 85%` / `False-Absent < 5%` | *\<Acc % / F1 % / False-Absent %\>* | Per-class F1: *\<...\>* | *\<วิเคราะห์ Confusion ระหว่าง `SLEEPING` กับ `ABSENT`\>* |

### ตาราง B: เปรียบเทียบ Airtomic กับระบบตัดไฟ/สมาร์ตโฮมทั่วไปในท้องตลาด

| เกณฑ์เปรียบเทียบ | สวิตช์ PIR ทั่วไป (PIR-Only Switch) | ปลั๊กอัจฉริยะตั้งเวลา (Smart Plug + Timer) | ระบบคีย์การ์ดโรงแรม (Hotel Keycard) | ระบบกล้อง AI ตรวจจับคน (Camera-based AI) | **Airtomic (ระบบของเรา)** |
| --- | --- | --- | --- | --- | --- |
| **ต้นทุนต่อห้อง (บาท)** | ~200 – 500 ฿ | ~300 – 800 ฿ | ~1,500 – 3,000 ฿ (รวมเดินสาย) | ~1,500 – 4,000 ฿ | **~650 – 950 ฿** (ESP32 ×2 + เซนเซอร์ + Servo) |
| **ความแม่นยำเมื่อคนนอนหลับ/นั่งนิ่ง** | ต่ำ (ตัดไฟผิดเมื่อไม่ขยับตัว) | ไม่มี (ตัดตามเวลาตายตัว) | สูง (ขึ้นกับผู้ใช้เสียบการ์ด) | สูง (แต่แย่ลงเมื่อปิดไฟมืด) | **สูง** (Sensor Fusion: PIR + BLE RSSI + Temp/Vib/Dist + AI) |
| **ความเป็นส่วนตัว (Privacy)** | สูง (ไม่มีกล้อง) | สูง (ไม่มีกล้อง) | สูง (ไม่มีกล้อง) | ต่ำ (บันทึกภาพในห้องนอน) | **สูง (Privacy-First, ไม่ใช้กล้อง)** |
| **การติดตั้งในหอพัก/ห้องเช่า (Non-Invasive)** | ต้องเดินสายไฟใหม่ | เสียบปลั๊กได้ (แต่คุมแอร์/สวิตช์ผนังไม่ได้) | ต้องเจาะ/เดินสายไฟใหม่ | ติดตั้งง่าย (แต่ควบคุมแอร์เก่าไม่ได้) | **ไม่ต้องดัดแปลงสายไฟ** (Servo กดสวิตช์/รีโมตเดิม) |
| **ความทนทานเมื่อเน็ตหลุด (Offline Resilience)** | ทำงานออฟไลน์ได้ | มักพึ่ง Cloud schedule | ทำงานออฟไลน์ได้ | มักพึ่ง Cloud AI | **Edge AI + Store-and-Forward (32 slots)** ทำงานและเก็บ log ออฟไลน์ได้ |
| **การดูข้อมูลย้อนหลัง & สั่งปิดระยะไกล** | ไม่มี | มี (ผ่านแอปเฉพาะค่าย) | ไม่มี | มี | **มี Real-time Web Dashboard + LINE/Discord Alert + Multi-node** |

---

## 6. แนวทางการอภิปรายข้อจำกัดและข้อเสนอแนะ (Discussion Prompts สำหรับรายงาน)

1. **Trade-off ของ Store-and-Forward Queue (32 รายการใน RAM):** เมื่อรัน 3 โหนดพร้อมกันที่คาบ 5 วินาที คิวขนาด 32 ช่องจะรองรับเน็ตหลุดได้ ~53 วินาทีก่อนเต็ม (ที่คาบ 10 วินาทีได้ ~106 วินาที) หากต้องการรองรับเน็ตหลุดนานกว่านั้นควรขยาย Buffer ลง Flash/LittleFS
2. **ข้อจำกัดของ BLE RSSI & Multi-occupant:** หากผู้ใช้งานลืมมือถือไว้ในห้องหรือมีผู้อยู่อาศัยมากกว่า 1 คน สัญญาณ BLE RSSI เพียงอย่างเดียวอาจคลาดเคลื่อน จึงต้องอาศัย Multi-sensor Fusion (PIR + Door Ultrasonic + Temp/Lux) ร่วมกัน
3. **การปรับปรุงรุ่นถัดไป:** เพิ่มความถี่การตรวจสอบคำสั่งด้วย Firebase REST Server-Sent Events (SSE) แทน HTTP Polling ทุก 2 วินาที เพื่อลด Actuator Latency ลงต่ำกว่า 500 ms และเพิ่มการเข้ารหัสระดับลิงก์ ESP-NOW (PMK/LMK) เพื่อยกระดับความปลอดภัย E5
