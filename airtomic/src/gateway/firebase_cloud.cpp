#include "firebase_cloud.h"

#include <Arduino.h>
#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <cmath>
#include <cstring>
#include <time.h>

#include "firebase_certs.h"

#if __has_include("gateway_secrets.h")
#include "gateway_secrets.h"
#else
#include "gateway_secrets.example.h"
#endif

namespace {
constexpr char GatewayId[] = "GW01";
constexpr uint32_t TokenRefreshMs = 50UL * 60 * 1000;
constexpr time_t EarliestNetworkTime = 1704067200;  // 2024-01-01 UTC

String idToken;
uint32_t tokenAtMs = 0;

bool networkTimeReady() {
  if (time(nullptr) >= EarliestNetworkTime) return true;

  static bool started = false;
  if (!started) {
    configTime(0, 0, "pool.ntp.org", "time.google.com");
    Serial.println("Firebase is waiting for network time");
    started = true;
  }
  return false;
}

int httpsRequest(const char *method, const String &url, const String &body,
                 String *response, bool createOnly = false) {
  WiFiClientSecure tls;
  tls.setCACert(GOOGLE_ROOT_CAS);
  tls.setHandshakeTimeout(5);

  HTTPClient http;
  http.setConnectTimeout(3500);
  http.setTimeout(5000);
  http.setReuse(false);
  if (!http.begin(tls, url)) return HTTPC_ERROR_CONNECTION_REFUSED;

  if (!body.isEmpty()) http.addHeader("Content-Type", "application/json");
  if (createOnly) http.addHeader("If-Match", "null_etag");
  const int status = http.sendRequest(method, body);
  if (response && status > 0) *response = http.getString();
  http.end();
  return status;
}

bool signIn() {
  JsonDocument credentials;
  credentials["email"] = FB_EMAIL;
  credentials["password"] = FB_PASSWORD;
  credentials["returnSecureToken"] = true;
  String body;
  serializeJson(credentials, body);

  const String url = String("https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key=") + FB_API_KEY;
  String response;
  const int status = httpsRequest("POST", url, body, &response);
  if (status != HTTP_CODE_OK) {
    Serial.printf("Firebase sign-in failed (%d)\n", status);
    return false;
  }

  JsonDocument auth;
  if (deserializeJson(auth, response) || !auth["idToken"].is<const char *>() ||
      auth["idToken"].as<const char *>()[0] == '\0') {
    Serial.println("Firebase sign-in did not return an ID token");
    return false;
  }

  idToken = auth["idToken"].as<const char *>();
  tokenAtMs = millis();
  return !idToken.isEmpty();
}

String databaseUrl(const String &path) {
  String url = FB_DB_URL;
  if (url.endsWith("/")) url.remove(url.length() - 1);
  return url + path + ".json?auth=" + idToken;
}

void refreshAfterUnauthorized(int status, const String &response) {
  // Firebase also returns 401 when an otherwise valid account lacks permission.
  if (status == HTTP_CODE_UNAUTHORIZED && response.indexOf("Permission denied") < 0) {
    idToken.clear();
  }
}

bool writeHistory(const String &url, const String &body, const NodeMsg &reading,
                  JsonDocument &saved) {
  // Create only once. A lost HTTP response must not replace an existing sample.
  String response;
  const int status = httpsRequest("PUT", url, body, &response, true);
  if (status == HTTP_CODE_OK) {
    deserializeJson(saved, response);
    return true;
  }

  if (status == HTTP_CODE_PRECONDITION_FAILED || status == HTTP_CODE_FORBIDDEN ||
      status == HTTP_CODE_UNAUTHORIZED) {
    String existing;
    const int readStatus = httpsRequest("GET", url, "", &existing);
    if (readStatus == HTTP_CODE_OK && !deserializeJson(saved, existing) &&
        saved["node_id"] == reading.node_id && saved["gw"] == GatewayId &&
        saved["seq"].is<uint32_t>() && saved["seq"].as<uint32_t>() == reading.seq) {
      return true;
    }
    Serial.printf("Firebase history write failed (%d), read-back status (%d)\n", status, readStatus);
    refreshAfterUnauthorized(readStatus, existing);
  } else {
    Serial.printf("Firebase history write failed (%d)\n", status);
  }

  refreshAfterUnauthorized(status, response);
  return false;
}
}  // namespace

const char *cloudWifiSsid() { return WIFI_SSID; }
const char *cloudWifiPassword() { return WIFI_PASS; }

bool cloudConfigured() {
  return WIFI_SSID[0] && FB_API_KEY[0] && FB_EMAIL[0] && FB_PASSWORD[0] &&
         strncmp(FB_DB_URL, "https://", 8) == 0 && FB_DB_URL[8];
}

bool publishToFirebase(const NodeMsg &reading, const char *historyId, int lightRaw, int pir) {
  if (!cloudConfigured() || WiFi.status() != WL_CONNECTED || !networkTimeReady()) return false;
  if ((idToken.isEmpty() || millis() - tokenAtMs >= TokenRefreshMs) && !signIn()) return false;

  JsonDocument telemetry;
  telemetry["gw"] = GatewayId;
  telemetry["node_id"] = reading.node_id;
  telemetry["seq"] = static_cast<uint32_t>(reading.seq);
  if (std::isfinite(reading.temp)) telemetry["temp"] = static_cast<float>(reading.temp);
  if (std::isfinite(reading.hum)) telemetry["hum"] = static_cast<float>(reading.hum);
  telemetry["vib"] = reading.vib;
  telemetry["dist"] = std::isfinite(reading.dist) ? reading.dist : -1.0f;
  // The LDR reports its ADC reading. Dashboard fields without a sensor are omitted.
  if (lightRaw >= 0) telemetry["light_raw"] = lightRaw;
  if (pir >= 0) telemetry["pir"] = pir;
  telemetry["ts"][".sv"] = "timestamp";

  String body;
  serializeJson(telemetry, body);
  const String historyPath = String("/history/") + reading.node_id + "/" + historyId;
  JsonDocument saved;
  if (!writeHistory(databaseUrl(historyPath), body, reading, saved)) return false;

  if (saved["ts"].is<uint64_t>()) {
    telemetry["ts"] = saved["ts"].as<uint64_t>();
    body.clear();
    serializeJson(telemetry, body);
  }

  const String latestUrl = databaseUrl(String("/nodes/") + reading.node_id);
  String response;
  const int status = httpsRequest("PUT", latestUrl, body, &response);
  if (status == HTTP_CODE_OK) return true;

  Serial.printf("Firebase latest reading write failed (%d)\n", status);
  refreshAfterUnauthorized(status, response);
  return false;
}
