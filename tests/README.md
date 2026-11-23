# Airtomic QA, Simulation & Quantitative Evaluation Suite (`tests/`)

โฟลเดอร์นี้รวบรวมเครื่องมือทดสอบทั้งหมดของโปรเจกต์ **Airtomic** (Embedded System CEDT 2026) โดยไม่แก้ไขโค้ดในโฟลเดอร์ firmware/dashboard หลักของสมาชิกในทีม ครอบคลุม 3 ภารกิจสำคัญตามเกณฑ์คะแนน:

1. **Section II — Scalability:** จำลอง Sensor Node เพิ่มเติม 2 โหนด (`SN02`, `SN03`) ทำงานพร้อมกับ Physical Node (`SN01`)
2. **Section IV — Test Plan (3/3):** เอกสารแผนการทดสอบครบทั้ง Normal Use, Edge Cases, Failure Conditions และ Rubric Checklist
3. **Section IV — Quantitative Metrics (4/4) & Analysis (3/3):** สคริปต์วิเคราะห์สถิติ (Mean, Median, Std, Min, Max, P95) และสร้างกราฟ PNG 300 DPI + ตาราง Markdown/CSV สำหรับใส่รายงาน 10 หน้า

---

## โครงสร้างไฟล์ภายใน `tests/`

```text
tests/
├── .gitignore                        # ป้องกันไม่ให้ commit secrets.h, .env, .venv
├── README.md                         # คู่มือการใช้งาน (ไฟล์นี้)
├── TEST_PLAN.md                      # แผนการทดสอบฉบับสมบูรณ์ + Rubric Checklist + Analysis Template
├── sim_nodes/                        # ส่วนที่ 1: โค้ดจำลอง Sensor Node (SN02, SN03)
│   ├── secrets.example.h             # ตัวอย่างตั้งค่า Gateway MAC / Channel (คัดลอกเป็น secrets.h)
│   ├── sim_nodes.ino                 # เฟิร์มแวร์ ESP32 จำลอง SN02+SN03 ผ่าน ESP-NOW + ACK/Retry + Fault Injection
│   ├── .env.example                  # ตัวอย่างตัวแปร Firebase (คัดลอกเป็น .env)
│   └── firebase_sim_nodes.py         # สคริปต์ Python ยิงข้อมูล SN02/SN03 เข้า Firebase โดยตรงเพื่อเทสต์ Dashboard
└── metrics/                          # ส่วนที่ 2: วิเคราะห์ Quantitative Metrics & สร้างกราฟรายงาน
    ├── requirements.txt              # Python dependencies
    ├── generate_mock_data.py         # สร้างข้อมูลจำลอง 24 ชม. (Firebase JSON + Serial CSV + AI Ground Truth)
    ├── analyze_metrics.py            # คำนวณ 4 ตัวชี้วัดหลัก + พล็อตกราฟ 300 DPI + ตาราง .md/.csv
    ├── data/                         # เก็บไฟล์ข้อมูลขาเข้า (Mock หรือ Real Export)
    └── output/                       # ผลลัพธ์: กราฟ .png (300 DPI), summary_metrics.md, summary_metrics.csv
```

---

## กฎความปลอดภัย (M8 / E5 / Section 4.5)

> **ห้ามฮาร์ดโค้ดและห้าม Commit Secret ใด ๆ ลง Git เด็ดขาด** (Wi-Fi Password, Firebase API Key, Gateway Password, LINE/Discord Token)

- ก่อนใช้งานฝั่ง ESP32 ให้คัดลอก `tests/sim_nodes/secrets.example.h` → `tests/sim_nodes/secrets.h` (ถูก ignore ไว้ใน `tests/.gitignore`)
- ก่อนใช้งานฝั่ง Python Firebase Simulator ให้คัดลอก `tests/sim_nodes/.env.example` → `tests/sim_nodes/.env` (ถูก ignore ไว้ใน `tests/.gitignore`)
- แนะนำให้ทีมเพิ่มบรรทัด `gateway_secrets.h`, `.env`, `.venv/` ใน root `.gitignore` ของโปรเจกต์ด้วย
- ทดสอบความปลอดภัย Firebase Rules ได้ตลอดเวลาด้วยสคริปต์ของโปรเจกต์:
  ```bash
  FB_PASSWORD='your-gateway-password' bash firebase/test_rules.sh
  ```

---

## วิธีใช้งานส่วนที่ 1: รันโค้ดจำลอง Sensor Nodes (`SN02`, `SN03`)

### แบบ A: จำลองผ่านฮาร์ดแวร์ ESP32 ด้วย ESP-NOW (แนะนำสำหรับ Demo Scalability + E4)

โค้ดใน `tests/sim_nodes/sim_nodes.ino` ใช้ `NodeMsg` (26 bytes) และ `AckMsg` (13 bytes) ตรงตาม `airtomic/include/messages.h` ทุกไบต์ โดยบอร์ด ESP32 เพียง 1 ตัวสามารถจำลองทั้ง `SN02` และ `SN03` สลับเวลากันทุก 2.5 วินาที (คาบโหนดละ 5 วินาที)

1. คัดลอกไฟล์ตั้งค่า:
   ```bash
   cp tests/sim_nodes/secrets.example.h tests/sim_nodes/secrets.h
   ```
2. เปิด `tests/sim_nodes/secrets.h` แล้วกรอก `GATEWAY_MAC_STR` และ `GATEWAY_WIFI_CHANNEL` ที่อ่านได้จาก Serial Monitor ของบอร์ด Gateway (หรือตั้ง `GATEWAY_WIFI_CHANNEL 0` พร้อมระบุ `GATEWAY_WIFI_SSID` เพื่อให้สแกนช่องอัตโนมัติ)
3. เปิด `tests/sim_nodes/sim_nodes.ino` ด้วย Arduino IDE (เลือกบอร์ด `ESP32 Dev Module`, esp32 core 3.x) แล้ว Upload ลงบอร์ด ESP32 ตัวจำลอง
4. เปิด Serial Monitor ที่ **115200 baud** แล้วพิมพ์คำสั่งทดสอบแบบ Interactive ได้ทันที:
   - `1` / `2` / `3` — บังคับสถานะ `SN02` เป็น `AWAKE` / `SLEEPING` / `ABSENT`
   - `a` / `s` / `e` — บังคับสถานะ `SN03` เป็น `AWAKE` / `SLEEPING` / `ABSENT`
   - `c` — กลับเข้าสู่โหมดสลับสถานะอัตโนมัติ (Auto-cycle ทุก 60 วินาที)
   - `d` — **[E4 Duplicate Packet Test]** ส่ง `seq` เดิมของ `SN02` ซ้ำ 2 ครั้งภายใน `< 500 ms` เพื่อพิสูจน์ว่า Gateway ส่ง ACK แต่ไม่บันทึกซ้ำลง Firebase
   - `r` — **[E4 Forced Retry Test]** จงใจเมิน ACK 2 ครั้งแรกเพื่อบังคับให้ส่งซ้ำจริงบนอากาศ 3 ครั้ง
   - `g` — **[E4 Packet Loss Test]** ข้ามการส่ง 1 แพ็กเก็ต (เกิดช่องว่าง `seq`) เพื่อเทสต์การคำนวณ PDR
   - `l` — เปิด/ปิดโหมดจำลอง Packet Loss ต่อเนื่อง 20%
   - `n` — **[Failure Test]** ฉีดค่าเซนเซอร์เสีย (`temp=NAN, hum=NAN, dist=-1`) 1 รอบ
   - `p` — แสดงสถิติสด (PDR%, จำนวน Retry, Packet Loss, Duplicate) แยกตาม `SN02` และ `SN03`

> **หมายเหตุ:** บรรทัดที่ขึ้นต้นด้วย `SIM_LOG,...` สามารถบันทึกเป็นไฟล์ `.csv` เพื่อป้อนเข้า `tests/metrics/analyze_metrics.py --serial` ได้โดยตรง

### แบบ B: จำลองยิงข้อมูลเข้า Firebase RTDB โดยตรงด้วย Python (สำหรับเทสต์ Dashboard)

ใช้เมื่อต้องการทดสอบหน้าเว็บ `airtomic-dashboard` (ตัวเลือกสลับโหนด `SN01`/`SN02`/`SN03`, แบนเนอร์แจ้งเตือน, และปุ่มกดสั่งปิด AC/Light) โดยไม่ต้องใช้บอร์ด ESP32 เพิ่ม:

```bash
# 1. ทดสอบแบบ Dry-run ก่อน (ไม่ต้องใช้รหัสผ่าน Firebase)
python3 tests/sim_nodes/firebase_sim_nodes.py --dry-run --count 4

# 2. ใช้งานจริงกับ Firebase RTDB
cp tests/sim_nodes/.env.example tests/sim_nodes/.env
# แก้ไข tests/sim_nodes/.env ด้วยข้อมูล Firebase ของกลุ่ม จากนั้นรัน:
.venv/bin/python tests/sim_nodes/firebase_sim_nodes.py \
  --nodes SN02 SN03 \
  --interval 5 \
  --log-csv tests/metrics/data/live_sim_log.csv
```

---

## วิธีใช้งานส่วนที่ 2: วิเคราะห์ Quantitative Metrics และสร้างกราฟรายงาน

### 1. ติดตั้งไลบรารี Python (ใช้ Virtual Environment เสมอ)

```bash
python3 -m venv .venv
.venv/bin/pip install -r tests/metrics/requirements.txt
```

### 2. ทดสอบระบบทั้งหมดทันทีด้วยข้อมูลจำลอง 24 ชั่วโมง (Mock Data)

```bash
# ขั้นที่ 1: สร้างชุดข้อมูลจำลอง 24 ชม. (SN01, SN02, SN03 + เน็ตหลุด 2 ครั้ง + ไฟดับ 1 ครั้ง)
.venv/bin/python tests/metrics/generate_mock_data.py

# ขั้นที่ 2: วิเคราะห์สถิติและสร้างกราฟ PNG 300 DPI + ตารางสรุปผล
.venv/bin/python tests/metrics/analyze_metrics.py \
  --firebase tests/metrics/data/firebase_export_24h.json \
  --serial tests/metrics/data/serial_events_24h.csv \
  --ground-truth tests/metrics/data/ai_ground_truth_24h.csv \
  --output tests/metrics/output
```

ผลลัพธ์จะถูกสร้างใน `tests/metrics/output/`:
- `01_latency_distribution.png` — Histogram & Boxplot เปรียบเทียบ End-to-End Latency ของ `SN01`, `SN02`, `SN03`
- `02_pdr_and_e4_reliability.png` — เปรียบเทียบ PDR (%) รายโหนดพร้อม Error Bar (±1 Hourly SD) และสถิติ E4 Retry / Deduplication
- `03_uptime_24h_timeline.png` — กราฟ Time-series 24 ชม. แสดงช่วง Wi-Fi Drop / Power Cycle และเวลา Auto-Recovery
- `04_recovery_times.png` — เปรียบเทียบเวลาที่ใช้กู้คืนระบบอัตโนมัติในแต่ละเหตุการณ์เทียบกับเป้าหมาย (`< 30 s`)
- `05_ai_confusion_matrix.png` — Confusion Matrix 3×3 (`AWAKE`, `SLEEPING`, `ABSENT`) และกราฟ Precision/Recall/F1 รายคลาส
- `summary_metrics.md` — ตาราง Markdown สรุปผล (Mean, Median, Std, Min, Max, P95) พร้อมก๊อปปี้ลงรายงาน
- `summary_metrics.csv` — ตารางตัวเลขแบบ CSV สำหรับวิเคราะห์ต่อใน Excel/Google Sheets

### 3. วิธี Export ข้อมูลจริงจาก Firebase RTDB หลังการทดสอบ 24 ชั่วโมง

เลือกทำได้ 2 วิธี:

- **วิธีที่ 1 (Firebase Console — ง่ายที่สุด):**
  1. เข้า Firebase Console → **Realtime Database** → แท็บ **Data**
  2. คลิกที่ node `history` (หรือ root) → กดเมนูจุดสามจุด (`⋮`) มุมขวาบน → เลือก **Export JSON**
  3. บันทึกไฟล์ไว้ที่ `tests/metrics/data/firebase_export_real.json`
- **วิธีที่ 2 (REST API ด้วย `curl` — ไม่เก็บ Token ในสคริปต์):**
  ```bash
  # ใช้ ID token ของบัญชี gateway หรือ operator ที่มีสิทธิ์อ่าน history/
  curl -s "https://airtomic-29e37-default-rtdb.asia-southeast1.firebasedatabase.app/history.json?auth=${FB_ID_TOKEN}" \
    -o tests/metrics/data/firebase_export_real.json
  ```
- **รันวิเคราะห์จากข้อมูลจริง:**
  ```bash
  # กรณีมีเฉพาะ Firebase JSON (ยังไม่มี Serial Log หรือ AI Ground Truth):
  .venv/bin/python tests/metrics/analyze_metrics.py \
    --firebase tests/metrics/data/firebase_export_real.json \
    --serial tests/metrics/data/nonexistent.csv \
    --ground-truth tests/metrics/data/nonexistent_gt.csv \
    --output tests/metrics/output_real

  # กรณีมีครบทั้ง Firebase JSON + Serial Log + Ground Truth CSV:
  .venv/bin/python tests/metrics/analyze_metrics.py \
    --firebase tests/metrics/data/firebase_export_real.json \
    --serial tests/metrics/data/serial_real_24h.csv \
    --ground-truth tests/metrics/data/ground_truth_real_24h.csv \
    --output tests/metrics/output_real
  ```

---

## รูปแบบไฟล์ Ground Truth CSV สำหรับประเมิน AI Model (Metric 4)

เมื่อเพื่อนส่ง `ai_model/` มาและเริ่มทดสอบเก็บผลจริง ให้บันทึกเหตุการณ์จริง (เช่น จดเวลาเริ่ม-จบการตื่น/นอน/ออกจากห้อง แล้ว map เป็นช่วงเวลา) ในรูปแบบ CSV ดังนี้:

```csv
ts_ms,node_id,seq,true_state
1795280400000,SN01,1,SLEEPING
1795280410000,SN01,2,SLEEPING
1795305600000,SN01,2521,AWAKE
1795312800000,SN01,3241,ABSENT
```

โดยค่า `true_state` ต้องตรงกับ union type ใน `airtomic-dashboard/interfaces.ts` คือ `AWAKE`, `SLEEPING`, หรือ `ABSENT` เท่านั้น
