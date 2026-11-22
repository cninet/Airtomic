/*
 * Airtomic gateway (ESP32 #2)
 * Step 2: connect to Firebase Realtime Database over HTTPS and publish telemetry.
 *         lux + pir are REAL (gateway sensors, see sensors.h); the sensor-node fields
 *         (temp, hum, vib, dist), rssi and state are still FAKE until steps 3+.
 *
 * Arduino IDE:  Board "ESP32 Dev Module" (esp32 core 3.x)
 *               Partition Scheme "Minimal SPIFFS (1.9MB APP with OTA)"
 * Setup:        copy secrets.example.h -> secrets.h and fill it in (secrets.h is git-ignored)
 *
 * Cloud contract (Firebase RTDB, rules in /firebase/database.rules.json):
 *   nodes/<node_id>           latest TelemetryData (same fields as airtomic-dashboard/interfaces.ts, + ts)
 *   history/<node_id>/<push>  every published record, append-only (raw data for M5 + metrics)
 *   cmd/<node_id>/{ac|light}  "OFF"  <- written by the dashboard; gateway executes then deletes it
 */
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include "certs.h"
#include "sensors.h"

#if !__has_include("secrets.h")
#error "Copy secrets.example.h to secrets.h and fill in your Wi-Fi + Firebase credentials"
#endif
#include "secrets.h"

#define GW_ID "GW01"

const char *FB_DB_URL = "https://airtomic-29e37-default-rtdb.asia-southeast1.firebasedatabase.app";

const uint32_t PUBLISH_MS = 10000;                  // changes are also published immediately
const uint32_t CMD_POLL_MS = 2000;                  // ponytail: polling; switch to a REST event stream if 2 s feels slow
const uint32_t TOKEN_REFRESH_MS = 50UL * 60 * 1000;  // Firebase ID tokens expire after 1 h

WiFiClientSecure tls;
HTTPClient http;
String idToken;
uint32_t tokenAt = 0;

// Fake room state until real sensors arrive (step 3/4).
bool acOn = true, lightOn = true;
bool publishNow = false;

// One HTTPS request; returns the HTTP status (negative = connection error).
int request(const char *method, const String &url, const String &body, String *out = nullptr) {
  http.begin(tls, url);
  if (body.length()) http.addHeader("Content-Type", "application/json");
  int code = http.sendRequest(method, body);
  if (out) *out = http.getString();
  http.end();
  if (code == 401) idToken = "";  // expired/invalid token -> sign in again
  return code;
}

String db(const char *path) {
  return String(FB_DB_URL) + path + ".json?auth=" + idToken;
}

// Sign in as the gateway's Firebase user (email/password) to get a 1-hour ID token.
bool signIn() {
  String body = String("{\"email\":\"") + FB_EMAIL + "\",\"password\":\"" + FB_PASSWORD + "\",\"returnSecureToken\":true}";
  String resp;
  int code = request("POST", String("https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key=") + FB_API_KEY, body, &resp);
  if (code != 200) {
    Serial.printf("Firebase sign-in failed (%d): %s\n", code, resp.c_str());
    return false;
  }
  int i = resp.indexOf("\"idToken\"");
  i = resp.indexOf('"', resp.indexOf(':', i)) + 1;
  idToken = resp.substring(i, resp.indexOf('"', i));
  tokenAt = millis();
  Serial.println("Firebase signed in");
  return true;
}

void publishTelemetry() {
  static uint32_t seq = 0;
  const char *node = "SN01";
  char rec[400];
  snprintf(rec, sizeof rec,
           "{\"gw\":\"%s\",\"node_id\":\"%s\",\"seq\":%lu,"
           "\"temp\":%.1f,\"hum\":%.1f,\"vib\":%d,\"dist\":%.1f,"
           "\"lux\":%.0f,\"pir\":%d,\"rssi\":%d,"
           "\"state\":\"%s\",\"conf\":%.2f,\"ac\":\"%s\",\"light\":\"%s\","
           "\"ts\":{\".sv\":\"timestamp\"}}",
           GW_ID, node, (unsigned long)++seq,
           25.0 + random(-10, 10) / 10.0, 58.0 + random(-20, 20) / 10.0, acOn ? 1 : 0, 80.0 + random(0, 50) / 10.0,
           readLux(), takeMotion(), -60 - (int)random(0, 15),
           "ABSENT", 0.93, acOn ? "ON" : "OFF", lightOn ? "ON" : "OFF");
  int latest = request("PUT", db("/nodes/SN01"), rec);
  int hist = request("POST", db("/history/SN01"), rec);
  Serial.printf("publish nodes=%d history=%d %s\n", latest, hist, rec);
}

// Dashboard writes cmd/SN01/ac = "OFF"; we act on it, then delete it so it runs once.
void pollCommands() {
  String resp;
  if (request("GET", db("/cmd/SN01"), "", &resp) != 200 || resp == "null") return;
  Serial.printf("cmd %s\n", resp.c_str());
  if (resp.indexOf("\"ac\":\"OFF\"") >= 0) {
    acOn = false;
    request("DELETE", db("/cmd/SN01/ac"), "");
  }
  if (resp.indexOf("\"light\":\"OFF\"") >= 0) {
    lightOn = false;
    request("DELETE", db("/cmd/SN01/light"), "");
  }
  publishNow = true;  // report the new state right away
}

void setup() {
  Serial.begin(115200);
  sensorsBegin();
  WiFi.mode(WIFI_STA);
  WiFi.onEvent(
    [](WiFiEvent_t, WiFiEventInfo_t info) {
      uint8_t r = info.wifi_sta_disconnected.reason;
      Serial.printf("\nWi-Fi disconnected: reason %d (%s)\n", r, WiFi.disconnectReasonName((wifi_err_reason_t)r));
    },
    ARDUINO_EVENT_WIFI_STA_DISCONNECTED);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.print("Wi-Fi");
  uint32_t t0 = millis();
  bool diagnosed = false;
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print('.');
    if (!diagnosed && millis() - t0 > 30000) {  // stuck 30 s: say why and show what the ESP32 can see (once)
      diagnosed = true;
      wl_status_t st = WiFi.status();
      Serial.printf("\nNot connected to \"%s\" (status %d: %s). 2.4 GHz networks visible:\n", WIFI_SSID, st,
                    st == WL_NO_SSID_AVAIL ? "network not found" : st == WL_CONNECT_FAILED ? "wrong password?" : "still trying");
      WiFi.disconnect(true);  // can't scan while a connection attempt is running: stop it (radio off)
      delay(200);
      WiFi.mode(WIFI_STA);
      int n = WiFi.scanNetworks();
      Serial.printf("  (scan found %d)\n", n);
      for (int i = 0; i < n; i++) Serial.printf("  \"%s\"  ch %d  %d dBm\n", WiFi.SSID(i).c_str(), (int)WiFi.channel(i), (int)WiFi.RSSI(i));
      WiFi.scanDelete();
      WiFi.begin(WIFI_SSID, WIFI_PASS);
      t0 = millis();
    }
  }
  Serial.printf(" connected, IP %s, channel %d\n", WiFi.localIP().toString().c_str(), (int)WiFi.channel());
  Serial.printf("Gateway MAC (give this to the sensor node): %s\n", WiFi.macAddress().c_str());

  tls.setCACert(GOOGLE_ROOT_CAS);  // real certificate check (E5), no setInsecure()
}

void loop() {
  static uint32_t lastPub = 0, lastPoll = 0;
  if (WiFi.status() != WL_CONNECTED) return;  // Wi-Fi reconnects on its own

  if (idToken.isEmpty() || millis() - tokenAt > TOKEN_REFRESH_MS) {
    if (!signIn()) {
      delay(5000);
      return;
    }
  }
  if (publishNow || lastPub == 0 || millis() - lastPub >= PUBLISH_MS) {
    publishNow = false;
    lastPub = millis();
    publishTelemetry();
  }
  if (millis() - lastPoll >= CMD_POLL_MS) {
    lastPoll = millis();
    pollCommands();
  }
}
