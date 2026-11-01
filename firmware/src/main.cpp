// ESP32 voice mic: read I2S digital mic, stream 16 kHz / 16-bit / mono PCM to the
// Fly voice backend over a (secure) WebSocket. Dumb pump — all the smarts (wake
// word, transcription) live on the server.

#include <Arduino.h>
#include <WiFi.h>
#include <WiFiMulti.h>
#include <WebSocketsClient.h>
#include <driver/i2s.h>
#include "config.h"
#ifndef DEVICE_TOKEN
#error "config.h has no DEVICE_TOKEN; copy it from config.h.example"
#endif

static const i2s_port_t I2S_PORT = I2S_NUM_0;
static const int SAMPLE_RATE = 16000;
static const int FRAME_SAMPLES = 1280;          // 80 ms — matches the backend

static const int LED_PIN = 2;                   // onboard blue LED (single color)

WebSocketsClient ws;
WiFiMulti wifiMulti;
bool wsConnected = false;

int32_t i2sBuf[FRAME_SAMPLES];                  // raw 32-bit slots from the INMP441
int16_t pcmBuf[FRAME_SAMPLES];                  // converted 16-bit PCM

TaskHandle_t blinkTask = NULL;

void blinkLoop(void *_) {
  while (true) {
    digitalWrite(LED_PIN, HIGH); vTaskDelay(150 / portTICK_PERIOD_MS);
    digitalWrite(LED_PIN, LOW);  vTaskDelay(150 / portTICK_PERIOD_MS);
  }
}

void stopBlink() {
  if (blinkTask) { vTaskDelete(blinkTask); blinkTask = NULL; }
}

// Backend feedback commands: LED_ON (listening), LED_OFF (idle), LED_BLINK (working)
void handleCommand(const String &cmd) {
  if (cmd == "LED_ON")        { stopBlink(); digitalWrite(LED_PIN, HIGH); }
  else if (cmd == "LED_OFF")  { stopBlink(); digitalWrite(LED_PIN, LOW); }
  else if (cmd == "LED_BLINK" && !blinkTask)
    xTaskCreate(blinkLoop, "blink", 1024, NULL, 1, &blinkTask);
}

void onWsEvent(WStype_t type, uint8_t *payload, size_t len) {
  switch (type) {
    case WStype_CONNECTED:    wsConnected = true;  Serial.println("WS connected"); break;
    case WStype_DISCONNECTED: wsConnected = false; stopBlink(); digitalWrite(LED_PIN, LOW); Serial.println("WS disconnected"); break;
    case WStype_TEXT:         if (payload && len) handleCommand(String((char *)payload).substring(0, len)); break;
    default: break;
  }
}

void setupI2S() {
  i2s_config_t cfg = {
      .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX),
      .sample_rate = SAMPLE_RATE,
      .bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT,   // INMP441 = 24-bit in 32-bit slot
      .channel_format = I2S_MIC_CHANNEL,
      .communication_format = I2S_COMM_FORMAT_STAND_I2S,
      .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
      .dma_buf_count = 4,
      .dma_buf_len = 256,
      .use_apll = false,
      .tx_desc_auto_clear = false,
      .fixed_mclk = 0};
  i2s_pin_config_t pins = {
      .bck_io_num = I2S_SCK_PIN,
      .ws_io_num = I2S_WS_PIN,
      .data_out_num = I2S_PIN_NO_CHANGE,
      .data_in_num = I2S_SD_PIN};
  i2s_driver_install(I2S_PORT, &cfg, 0, NULL);
  i2s_set_pin(I2S_PORT, &pins);
  i2s_zero_dma_buffer(I2S_PORT);
}

void setup() {
  Serial.begin(115200);
  delay(200);

  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);          // disable modem power-save — the #1 cause of periodic streaming drops
  // Both the router and the extender are registered; WiFiMulti scans and joins whichever is
  // STRONGEST from where the puck actually sits, so moving it doesn't need a reflash. The mic
  // streams ~256 kbps, so a solid client link matters more than the extender's backhaul headroom.
  wifiMulti.addAP(WIFI_SSID, WIFI_PASSWORD);
  wifiMulti.addAP(WIFI_SSID_ALT, WIFI_PASSWORD_ALT);
  Serial.print("Connecting to WiFi");
  while (wifiMulti.run() != WL_CONNECTED) { delay(300); Serial.print("."); }
  Serial.printf("\nConnected to \"%s\"  %s  (RSSI %d dBm)\n",
                WiFi.SSID().c_str(), WiFi.localIP().toString().c_str(), WiFi.RSSI());

  setupI2S();

  // The server rejects the socket without this once VOICE_DEVICE_TOKEN is set on Fly. The library
  // keeps the pointer, so it must be a literal.
  ws.setExtraHeaders("X-Device-Token: " DEVICE_TOKEN);
  if (WS_TLS) ws.beginSSL(WS_HOST, WS_PORT, WS_PATH);
  else        ws.begin(WS_HOST, WS_PORT, WS_PATH);
  ws.onEvent(onWsEvent);
  ws.setReconnectInterval(2000);
  // Client-side keepalive: ping every 10s, expect a pong within 3s, give up after 2 misses (~16s).
  // A weak-WiFi blip leaves the TCP socket half-open; without this the ESP keeps pumping audio into
  // a dead socket and only notices after the multi-minute OS TCP timeout. The heartbeat catches it
  // in seconds so the fast (2s) auto-reconnect can kick in. The server auto-pongs these pings.
  ws.enableHeartbeat(10000, 3000, 2);
}

void loop() {
  ws.loop();

  // WiFi watchdog + signal telemetry. i2s_read below blocks ~80ms, so this runs roughly that often.
  static uint32_t lastWifiTry = 0, lastRssi = 0;
  uint32_t now = millis();
  if (WiFi.status() != WL_CONNECTED) {
    if (now - lastWifiTry > 5000) {        // throttle reconnect attempts while the AP is unreachable
      lastWifiTry = now;
      Serial.println("WiFi dropped — rescanning");
      wifiMulti.run();           // re-scan, not WiFi.reconnect(): the strongest AP may have changed
    }
  } else if (now - lastRssi > 30000) {     // report signal strength so it's visible in `fly logs`
    lastRssi = now;
    Serial.printf("WiFi \"%s\" RSSI %d dBm  ws=%s\n", WiFi.SSID().c_str(), WiFi.RSSI(), wsConnected ? "up" : "down");
    // Shaped to slot into the server's "device %s %s dBm" log line, which appends the units.
    if (wsConnected) ws.sendTXT("RSSI on " + WiFi.SSID() + ": " + String(WiFi.RSSI()));
  }

  size_t bytesRead = 0;
  i2s_read(I2S_PORT, i2sBuf, sizeof(i2sBuf), &bytesRead, portMAX_DELAY);
  int samples = bytesRead / sizeof(int32_t);

  // INMP441 outputs 24-bit data left-justified in the 32-bit slot (RIGHT channel
  // at 32-bit). Shift down by 12: >>10 left ~64x too much gain, so loud/close speech
  // railed the int16 range (peak clamped at 32767) and destroyed the speaker-ID match.
  // The Fly server re-gains (GAIN) and peak-normalizes before ECAPA/Whisper, so a
  // quieter CLEAN signal is strictly better than a loud clipped one. Then clamp.
  for (int i = 0; i < samples; i++) {
    int32_t v = i2sBuf[i] >> 12;
    if (v > 32767) v = 32767;
    else if (v < -32768) v = -32768;
    pcmBuf[i] = (int16_t)v;
  }

  if (wsConnected && samples > 0) {
    ws.sendBIN((uint8_t *)pcmBuf, samples * sizeof(int16_t));
  }
}
