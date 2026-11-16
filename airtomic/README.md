# Airtomic ESP32 firmware

ใช้ PlatformIO และ Arduino framework บน DOIT ESP32 DevKit V1 ทั้งสองบอร์ด

## การต่อเซนเซอร์

| บอร์ด | เซนเซอร์ | GPIO ของ ESP32 |
| --- | --- | --- |
| Sensor Node | DHT11 data | 4 |
| Sensor Node | SW-420 digital out | 27 |
| Sensor Node | HC-SR04 TRIG / ECHO | 26 / 33 |
| Gateway | LDR analog | 34 (ADC1) |
| Gateway | HC-SR501 OUT | 32 |

- จ่ายไฟ 5 V ให้ HC-SR04 และลดแรงดัน ECHO ก่อนเข้า GPIO33 เช่น ECHO → 2.2 kΩ → GPIO33 → 3.3 kΩ → GND ใช้กราวด์ร่วมกับ ESP32
- จ่ายไฟ 3.3 V ให้ DHT11 และใช้ตัวต้านทาน pull-up จาก data ไป 3.3 V ถ้าเป็นเซนเซอร์เปล่า ใช้สัญญาณ SW-420 และ PIR ที่ระดับ 3.3 V กับ GPIO ของ ESP32
- ต่อ LDR ในวงจรแบ่งแรงดัน `3V3 → LDR → GPIO34 → 10 kΩ → GND` ค่า ADC จะเพิ่มเมื่อแสงเพิ่ม จ่ายไฟ HC-SR501 ตามสเปกของโมดูลและใช้กราวด์ร่วม

## ทดสอบ LDR และ PIR บน Gateway

เสียบเฉพาะ Gateway แล้วเลือก environment `gateway_sensor_test` ใน VS Code จากนั้นกด **PlatformIO: Upload and Monitor** เพื่อดูค่าที่ 115200 โหมดนี้อ่านค่าจากเซนเซอร์ทุกวินาทีและแสดงผลทาง Serial

- ลองบังหรือส่องไฟที่ LDR แล้วดูค่า `LDR` เปลี่ยน
- รอ PIR อุ่นเครื่องประมาณ 60 วินาที (`PIR=-1`) แล้วลองขยับมือหน้าเซนเซอร์ ค่า `PIR` จะเป็น `1` เมื่อตรวจพบการเคลื่อนไหว

เมื่อจะใช้งาน ESP-NOW และ Firebase ให้เลือก environment `gateway` แล้วอัปโหลดอีกครั้ง

## ตั้งค่าและอัปโหลด

1. คัดลอก `include/gateway_secrets.example.h` เป็น `include/gateway_secrets.h` แล้วใส่ Wi-Fi **2.4 GHz**, Firebase API key, บัญชี Email/Password ของ Gateway และ URL หลักของ Realtime Database ใช้ค่า Firebase จากเฟิร์มแวร์เดิมได้ถ้าเป็นโปรเจกต์เดียวกัน เก็บไฟล์นี้ไว้ในเครื่องและอย่า commit
2. อัปโหลด environment `gateway` ไปที่บอร์ด Gateway แล้วเปิด Serial Monitor ที่ 115200 เมื่อเชื่อม Wi-Fi สำเร็จ จะเห็น STA MAC และ Wi-Fi channel
3. ใส่ STA MAC และ channel ที่ได้ใน `include/node_config.h` แล้วอัปโหลด environment `sensor_node` ไปที่บอร์ด Sensor Node หากเว้นสองค่านี้ไว้ Node จะแสดงค่าเซนเซอร์ทาง Serial ทุก 5 วินาทีหลังเริ่มอ่านครั้งแรก
4. ตรวจว่า Serial ที่ Node ขึ้น `ACK` และ Gateway ขึ้น `Firebase saved` แล้วดูข้อมูลล่าสุดที่ `nodes/SN01` และประวัติที่ `history/SN01` ใน Realtime Database

ใน VS Code เลือก environment ของบอร์ดใน PlatformIO Project Tasks แล้วใช้ **Upload** เพื่อ build และอัปโหลด กด **Monitor** เพื่อดู Serial ที่ 115200 โปรเจกต์เลือก `sensor_node` เป็นค่าเริ่มต้น และเก็บผล build ใต้โฟลเดอร์ PlatformIO Core เพื่อเลี่ยงปัญหา linker กับพาธ OneDrive ที่มีอักษรไทย

## ข้อมูลใน Firebase

Gateway เก็บข้อมูลล่าสุดที่ `nodes/<node_id>` และแต่ละตัวอย่างที่ `history/<node_id>/<id>` บัญชี Gateway ต้องเขียนได้ทั้งสอง path และอ่านประวัติของตัวเองกลับได้เพื่อยืนยันการเขียนซ้ำเมื่อการตอบกลับจาก Firebase ขาดหาย

- `temp` เป็น °C และ `hum` เป็น %RH ฟิลด์ที่อ่านไม่ได้จะหายจากตัวอย่างนั้น `vib` เป็น 0 หรือ 1 และ `dist` เป็น cm โดยใช้ `-1` เมื่อวัดระยะไม่ได้
- `light_raw` เป็นค่า LDR จาก ADC 12 บิต (0–4095) ใช้ฟิลด์นี้แทนค่าความสว่างใน dashboard ส่วน `pir` เป็น 0 หรือ 1 และจะเริ่มส่งหลังเซนเซอร์อุ่นเครื่อง 60 วินาที
- `ts` เป็นเวลาที่ Firebase รับข้อมูลในหน่วย Unix milliseconds ใช้ฟิลด์นี้เรียงประวัติ Gateway เก็บได้สูงสุด 32 ตัวอย่างใน RAM ระหว่างรอส่ง เมื่อคิวเต็ม Node จะไม่ได้ ACK ของตัวอย่างใหม่
