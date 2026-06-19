// ESP32 voice mic: read I2S digital mic, stream 16 kHz / 16-bit / mono PCM to the
// Fly voice backend over a (secure) WebSocket. Dumb pump — all the smarts (wake
// word, transcription) live on the server.

#include <Arduino.h>
#include <WiFi.h>
#include <WebSocketsClient.h>
#include <driver/i2s.h>
#include "config.h"

static const i2s_port_t I2S_PORT = I2S_NUM_0;
static const int SAMPLE_RATE = 16000;
static const int FRAME_SAMPLES = 1280;          // 80 ms — matches the backend

static const int LED_PIN = 2;                   // onboard blue LED (single color)

WebSocketsClient ws;
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
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("Connecting to WiFi");
  while (WiFi.status() != WL_CONNECTED) { delay(300); Serial.print("."); }
  Serial.printf("\nConnected to WiFi  %s\n", WiFi.localIP().toString().c_str());

  setupI2S();

  if (WS_TLS) ws.beginSSL(WS_HOST, WS_PORT, WS_PATH);
  else        ws.begin(WS_HOST, WS_PORT, WS_PATH);
  ws.onEvent(onWsEvent);
  ws.setReconnectInterval(2000);
}

void loop() {
  ws.loop();

  size_t bytesRead = 0;
  i2s_read(I2S_PORT, i2sBuf, sizeof(i2sBuf), &bytesRead, portMAX_DELAY);
  int samples = bytesRead / sizeof(int32_t);

  // INMP441 outputs 24-bit data left-justified in the 32-bit slot (RIGHT channel
  // at 32-bit). Shift down by 9 to a well-levelled 16-bit sample, then clamp.
  for (int i = 0; i < samples; i++) {
    int32_t v = i2sBuf[i] >> 9;
    if (v > 32767) v = 32767;
    else if (v < -32768) v = -32768;
    pcmBuf[i] = (int16_t)v;
  }

  if (wsConnected && samples > 0) {
    ws.sendBIN((uint8_t *)pcmBuf, samples * sizeof(int16_t));
  }
}
