#include <M5Unified.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <mbedtls/base64.h>
#include <Preferences.h>
#include <SD.h>
#include <vector>
#include <string.h>

Preferences preferences;

const char* DEFAULT_SSID     = "YOUR_WIFI_SSID";
const char* DEFAULT_PASSWORD = "YOUR_WIFI_PASSWORD";
const char* DEFAULT_SERVER   = "http://192.168.1.157:5000";

String wifiSsid     = DEFAULT_SSID;
String wifiPassword = DEFAULT_PASSWORD;
String serverBase   = DEFAULT_SERVER;

#define COLOR_BG      0x0841
#define COLOR_TEAL    0x03A6
#define COLOR_PURPLE  0x580A
#define COLOR_GREEN   0x07E0
#define COLOR_WHITE   0xFFFF
#define COLOR_RED     0xF800
#define COLOR_DARK    0x2104

enum AppState { STATE_CHAT, STATE_SETTINGS, STATE_KEYBOARD, STATE_PICTURE };
AppState currentState = STATE_CHAT;

String lastReply = "Ready. Type or press A for Image.";
bool isConnected = false;
bool isBusy      = false;
bool isRecording = false;

String modelMode  = "realistic";
String activeLora = "None";

// Long reply paging
int replyPage = 0;
const int CHARS_PER_PAGE = 160;

String* activeEditingField = nullptr;
String keyboardBuffer = "";
String keyboardTitle  = "";
enum KeyboardMode { MODE_LOWER, MODE_UPPER, MODE_SYMBOLS };
KeyboardMode kbMode = MODE_LOWER;
bool shiftHeld = false;

#define RECORD_SECONDS 4
#define SAMPLE_RATE    16000
int16_t* micBuffer = nullptr;
size_t   micSamples = 0;

uint8_t* currentImageBuffer = nullptr;
int touchStartX = 0;
unsigned long pictureEnterMs = 0;

unsigned long btnBPressStart = 0;
bool btnBLongDone = false;

void drawChatUI();
void drawSettingsUI();
void drawKeyboardUI();
void drawPictureUI();
void connectWiFi();
void sendChat(const String& msg);
void fetchAndPlayTTS(const String& text);
void requestImage(const String& prompt);
void cycleLora();
void clearLora();
void startRecording();

// ---------- settings ----------
void loadSettings() {
    if (SD.begin(4) && SD.exists("/wifi_config.json")) {
        File f = SD.open("/wifi_config.json", FILE_READ);
        if (f) {
            JsonDocument doc;
            if (!deserializeJson(doc, f)) {
                if (doc["ssid"].is<const char*>())   wifiSsid     = doc["ssid"].as<String>();
                if (doc["pass"].is<const char*>())   wifiPassword = doc["pass"].as<String>();
                if (doc["server"].is<const char*>()) serverBase   = doc["server"].as<String>();
            }
            f.close();
        }
    }
    preferences.begin("mantiskai", true);
    wifiSsid     = preferences.getString("ssid",   wifiSsid);
    wifiPassword = preferences.getString("pass",   wifiPassword);
    serverBase   = preferences.getString("server", serverBase);
    preferences.end();
    if (serverBase.endsWith("/")) serverBase.remove(serverBase.length() - 1);
}

void saveSettings() {
    preferences.begin("mantiskai", false);
    preferences.putString("ssid", wifiSsid);
    preferences.putString("pass", wifiPassword);
    preferences.putString("server", serverBase);
    preferences.end();
    if (SD.begin(4)) {
        File f = SD.open("/wifi_config.json", FILE_WRITE);
        if (f) {
            JsonDocument doc;
            doc["ssid"] = wifiSsid; doc["pass"] = wifiPassword; doc["server"] = serverBase;
            serializeJson(doc, f);
            f.close();
        }
    }
}

void connectWiFi() {
    WiFi.disconnect(true);
    delay(80);
    WiFi.mode(WIFI_STA);
    WiFi.begin(wifiSsid.c_str(), wifiPassword.c_str());
    int n = 0;
    while (WiFi.status() != WL_CONNECTED && n++ < 20) delay(500);
    isConnected = (WiFi.status() == WL_CONNECTED);
}

// ---------- UI ----------
void drawHeader(const char* title, uint16_t color = COLOR_TEAL) {
    M5.Display.fillRect(0, 0, 320, 32, color);
    M5.Display.setTextColor(COLOR_WHITE, color);
    M5.Display.setTextSize(2);
    M5.Display.setCursor(8, 6);
    M5.Display.print(title);
    M5.Display.fillCircle(300, 16, 7, isConnected ? COLOR_GREEN : COLOR_RED);
}

void drawChatUI() {
    M5.Display.fillScreen(COLOR_BG);
    drawHeader("MantisKAI Chat");

    M5.Display.fillRoundRect(255, 4, 30, 24, 4, COLOR_PURPLE);
    M5.Display.setTextSize(1);
    M5.Display.setCursor(261, 10);
    M5.Display.print("CFG");

    M5.Display.drawRoundRect(8, 40, 304, 110, 6, COLOR_PURPLE);
    M5.Display.setTextColor(COLOR_WHITE, COLOR_BG);
    M5.Display.setTextSize(1);
    M5.Display.setCursor(14, 46);
    M5.Display.print(isBusy ? "Thinking..." : "AI Reply:");

    // Page through long replies
    int totalPages = (lastReply.length() + CHARS_PER_PAGE - 1) / CHARS_PER_PAGE;
    if (totalPages < 1) totalPages = 1;
    if (replyPage >= totalPages) replyPage = totalPages - 1;
    if (replyPage < 0) replyPage = 0;
    int start = replyPage * CHARS_PER_PAGE;
    String page = lastReply.substring(start, start + CHARS_PER_PAGE);

    M5.Display.setTextColor(COLOR_GREEN, COLOR_BG);
    int y = 62;
    while (page.length() && y < 140) {
        String line = page.substring(0, 38);
        int sp = line.lastIndexOf(' ');
        if (sp > 20 && page.length() > 38) { line = page.substring(0, sp); page = page.substring(sp + 1); }
        else page = page.substring(line.length());
        M5.Display.setCursor(14, y);
        M5.Display.print(line);
        y += 14;
    }
    if (totalPages > 1) {
        M5.Display.setTextColor(COLOR_TEAL, COLOR_BG);
        M5.Display.setCursor(200, 140);
        M5.Display.printf("<%d/%d>", replyPage + 1, totalPages);
    }

    M5.Display.fillRoundRect(8, 158, 304, 22, 4, 0x1ef8);
    M5.Display.setTextColor(COLOR_WHITE, 0x1ef8);
    M5.Display.setTextSize(1);
    M5.Display.setCursor(14, 164);
    M5.Display.printf("M:%s | LoRA:%s", modelMode.c_str(), activeLora.c_str());

    M5.Display.fillRoundRect(8, 190, 148, 42, 6, COLOR_PURPLE);
    M5.Display.setTextColor(COLOR_WHITE, COLOR_PURPLE);
    M5.Display.setTextSize(2);
    M5.Display.setCursor(30, 202);
    M5.Display.print("TYPE");

    M5.Display.fillRoundRect(164, 190, 148, 42, 6, COLOR_TEAL);
    M5.Display.setTextColor(COLOR_WHITE, COLOR_TEAL);
    M5.Display.setCursor(178, 202);
    M5.Display.print("MIC");
}

void drawSettingsUI() {
    M5.Display.fillScreen(COLOR_BG);
    drawHeader("Settings", COLOR_PURPLE);
    M5.Display.setTextSize(1);
    M5.Display.setTextColor(COLOR_WHITE, COLOR_BG);

    M5.Display.setCursor(12, 42); M5.Display.print("Wi-Fi SSID");
    M5.Display.fillRoundRect(12, 56, 296, 28, 4, COLOR_DARK);
    M5.Display.setCursor(18, 64); M5.Display.print(wifiSsid);

    M5.Display.setCursor(12, 92); M5.Display.print("Wi-Fi Password");
    M5.Display.fillRoundRect(12, 106, 296, 28, 4, COLOR_DARK);
    M5.Display.setCursor(18, 114); M5.Display.print("********");

    M5.Display.setCursor(12, 142); M5.Display.print("PC Server (base URL)");
    M5.Display.fillRoundRect(12, 156, 296, 28, 4, COLOR_DARK);
    M5.Display.setCursor(18, 164); M5.Display.print(serverBase);

    M5.Display.fillRoundRect(12, 200, 140, 36, 6, COLOR_TEAL);
    M5.Display.setTextColor(COLOR_WHITE, COLOR_TEAL);
    M5.Display.setTextSize(2);
    M5.Display.setCursor(40, 210); M5.Display.print("BACK");

    M5.Display.fillRoundRect(168, 200, 140, 36, 6, COLOR_GREEN);
    M5.Display.setTextColor(0, COLOR_GREEN);
    M5.Display.setCursor(185, 210); M5.Display.print("SAVE");
}

const char* rowsL[] = {"1234567890", "qwertyuiop", "asdfghjkl", "zxcvbnm"};
const char* rowsU[] = {"1234567890", "QWERTYUIOP", "ASDFGHJKL", "ZXCVBNM"};
const char* rowsS[] = {"!@#$%^&*()", "-_=+[]{}|\\", ";:'\",.<>/?", "`~"};

void drawKeyboardUI() {
    M5.Display.fillScreen(COLOR_BG);
    M5.Display.fillRect(0, 0, 320, 28, COLOR_TEAL);
    M5.Display.setTextColor(COLOR_WHITE, COLOR_TEAL);
    M5.Display.setTextSize(1);
    M5.Display.setCursor(8, 8);
    const char* ms = kbMode == MODE_LOWER ? "[abc]" : kbMode == MODE_UPPER ? "[ABC]" : "[SYM]";
    M5.Display.printf("%s  %s", keyboardTitle.c_str(), ms);

    M5.Display.drawRoundRect(6, 32, 308, 30, 4, COLOR_PURPLE);
    M5.Display.setTextColor(COLOR_GREEN, COLOR_BG);
    M5.Display.setTextSize(2);
    M5.Display.setCursor(12, 38);
    String shown = keyboardBuffer;
    if (shown.length() > 22) shown = "..." + shown.substring(shown.length() - 19);
    M5.Display.print(shown);

    const char** rows = kbMode == MODE_LOWER ? rowsL : kbMode == MODE_UPPER ? rowsU : rowsS;
    for (int r = 0; r < 4; r++) {
        String row = rows[r];
        int keyW = 300 / row.length();
        int x0 = (320 - keyW * row.length()) / 2;
        for (int c = 0; c < (int)row.length(); c++) {
            int x = x0 + c * keyW, y = 68 + r * 36;
            M5.Display.fillRoundRect(x+1, y+1, keyW-2, 30, 3, COLOR_DARK);
            M5.Display.drawRoundRect(x, y, keyW, 32, 3, COLOR_PURPLE);
            M5.Display.setTextColor(COLOR_WHITE, COLOR_DARK);
            M5.Display.setTextSize(2);
            M5.Display.setCursor(x + keyW/2 - 6, y + 6);
            M5.Display.print(row[c]);
        }
    }
    int y = 210;
    uint16_t sh = (kbMode == MODE_UPPER || shiftHeld) ? COLOR_TEAL : COLOR_DARK;
    M5.Display.fillRoundRect(4, y, 60, 28, 4, sh);
    M5.Display.setTextColor(COLOR_WHITE, sh);
    M5.Display.setTextSize(1);
    M5.Display.setCursor(16, y+8); M5.Display.print("SHIFT");

    M5.Display.fillRoundRect(68, y, 100, 28, 4, COLOR_DARK);
    M5.Display.setTextColor(COLOR_WHITE, COLOR_DARK);
    M5.Display.setCursor(100, y+8); M5.Display.print("SPACE");

    M5.Display.fillRoundRect(172, y, 60, 28, 4, COLOR_DARK);
    M5.Display.setCursor(184, y+8); M5.Display.print(kbMode == MODE_SYMBOLS ? "ABC" : "SYM");

    M5.Display.fillRoundRect(236, y, 40, 28, 4, COLOR_RED);
    M5.Display.setTextColor(COLOR_WHITE, COLOR_RED);
    M5.Display.setCursor(246, y+8); M5.Display.print("<-");

    M5.Display.fillRoundRect(280, y, 36, 28, 4, COLOR_GREEN);
    M5.Display.setTextColor(0, COLOR_GREEN);
    M5.Display.setCursor(288, y+8); M5.Display.print("OK");
}

void drawPictureUI() {
    M5.Display.fillScreen(0);
    if (currentImageBuffer) {
        // Raw RGB565 from PC is little-endian packed; M5GFX often needs swap
        M5.Display.setSwapBytes(true);
        M5.Display.pushImage(0, 0, 320, 240, (uint16_t*)currentImageBuffer);
        M5.Display.setSwapBytes(false);
    } else {
        M5.Display.setTextColor(COLOR_WHITE, 0);
        M5.Display.setTextSize(2);
        M5.Display.setCursor(40, 100);
        M5.Display.print("No image yet");
    }
    // Semi-transparent style hint bar (solid black strip)
    M5.Display.fillRect(0, 210, 320, 30, 0x0000);
    M5.Display.setTextColor(COLOR_WHITE, 0x0000);
    M5.Display.setTextSize(1);
    M5.Display.setCursor(20, 220);
    M5.Display.print("< Swipe Left=Delete   Swipe Right=Save >");
}

void handleKeyboardTouch(int tx, int ty) {
    if (ty < 68) return;
    if (ty < 210) {
        int row = (ty - 68) / 36;
        if (row < 0 || row > 3) return;
        const char** rows = kbMode == MODE_LOWER ? rowsL : kbMode == MODE_UPPER ? rowsU : rowsS;
        String r = rows[row];
        int keyW = 300 / r.length();
        int x0 = (320 - keyW * r.length()) / 2;
        int col = (tx - x0) / keyW;
        if (col >= 0 && col < (int)r.length()) {
            char ch = r[col];
            if (shiftHeld && kbMode == MODE_LOWER && ch >= 'a' && ch <= 'z') {
                ch -= 32; shiftHeld = false;
            }
            keyboardBuffer += ch;
            drawKeyboardUI();
        }
        return;
    }
    if (ty >= 210) {
        if (tx < 64) {
            if (kbMode == MODE_SYMBOLS) kbMode = MODE_LOWER;
            else if (kbMode == MODE_LOWER) { kbMode = MODE_UPPER; shiftHeld = true; }
            else { kbMode = MODE_LOWER; shiftHeld = false; }
            drawKeyboardUI();
        } else if (tx < 168) {
            keyboardBuffer += " "; drawKeyboardUI();
        } else if (tx < 232) {
            kbMode = kbMode == MODE_SYMBOLS ? MODE_LOWER : MODE_SYMBOLS;
            shiftHeld = false; drawKeyboardUI();
        } else if (tx < 276) {
            if (keyboardBuffer.length()) keyboardBuffer.remove(keyboardBuffer.length() - 1);
            drawKeyboardUI();
        } else {
            if (activeEditingField) {
                *activeEditingField = keyboardBuffer;
                currentState = STATE_SETTINGS;
                drawSettingsUI();
            } else {
                String msg = keyboardBuffer;
                keyboardBuffer = "";
                bool img = (keyboardTitle == "Image Prompt");
                currentState = STATE_CHAT;
                drawChatUI();
                if (msg.length()) {
                    if (img) requestImage(msg);
                    else sendChat(msg);
                }
            }
        }
    }
}

// ---------- network ----------
void sendChat(const String& msg) {
    if (WiFi.status() != WL_CONNECTED) {
        isConnected = false;
        lastReply = "Wi-Fi disconnected";
        drawChatUI();
        return;
    }
    isConnected = true;
    isBusy = true;
    drawChatUI();

    WiFiClient client;
    HTTPClient http;
    http.setConnectTimeout(8000);
    http.setTimeout(45000);
    http.setReuse(false);
    http.useHTTP10(true);

    String url = serverBase + "/api/chat";
    if (!http.begin(client, url)) {
        lastReply = "Bad URL";
        isBusy = false;
        drawChatUI();
        return;
    }
    http.addHeader("Content-Type", "application/json");

    JsonDocument body;
    body["message"] = msg;
    String bodyStr;
    serializeJson(body, bodyStr);

    int code = http.POST(bodyStr);
    if (code > 0) {
        String resp = http.getString();
        JsonDocument doc;
        if (!deserializeJson(doc, resp)) {
            if (doc["lora"].is<const char*>()) activeLora = doc["lora"].as<String>();
            if (doc["reply"].is<const char*>()) {
                lastReply = doc["reply"].as<String>();
                replyPage = 0;
                http.end();
                isBusy = false;
                drawChatUI();
                // second request: binary WAV
                fetchAndPlayTTS(lastReply);
                return;
            }
            if (doc["error"].is<const char*>()) lastReply = doc["error"].as<String>();
            else lastReply = "No reply";
        } else {
            lastReply = "Bad JSON";
        }
    } else {
        lastReply = http.errorToString(code);
    }
    http.end();
    isBusy = false;
    drawChatUI();
}

void fetchAndPlayTTS(const String& text) {
    if (text.length() == 0 || WiFi.status() != WL_CONNECTED) return;

    WiFiClient client;
    HTTPClient http;
    http.setConnectTimeout(8000);
    http.setTimeout(45000);
    http.setReuse(false);
    http.useHTTP10(true);

    String url = serverBase + "/api/tts";
    if (!http.begin(client, url)) return;
    http.addHeader("Content-Type", "application/json");

    JsonDocument body;
    body["text"] = text;
    String bodyStr;
    serializeJson(body, bodyStr);

    int code = http.POST(bodyStr);
    if (code != 200) {
        http.end();
        return;
    }

    int sampleRate = 22050;
    if (http.hasHeader("X-Sample-Rate")) {
        int sr = http.header("X-Sample-Rate").toInt();
        if (sr >= 8000) sampleRate = sr;
    }

    // Read entire body into a growable buffer (getSize often -1)
    const int maxBytes = 240000;
    uint8_t* buf = (uint8_t*)malloc(maxBytes);
    if (!buf) {
        http.end();
        return;
    }

    int got = 0;
    WiFiClient* stream = http.getStreamPtr();
    unsigned long deadline = millis() + 40000;
    while (millis() < deadline && got < maxBytes) {
        int avail = stream->available();
        if (avail > 0) {
            int n = stream->readBytes(buf + got, min(avail, maxBytes - got));
            if (n > 0) got += n;
            else break;
        } else if (!http.connected()) {
            // drain any last bytes
            delay(5);
            avail = stream->available();
            if (avail <= 0) break;
        } else {
            delay(2);
        }
    }
    http.end();

    if (got < 512) {
        free(buf);
        return;
    }

    size_t samples = (size_t)got / 2;

    M5.Speaker.end();
    delay(20);
    M5.Speaker.begin();
    M5.Speaker.setVolume(200);
    M5.Speaker.playRaw((const int16_t*)buf, samples, (uint32_t)sampleRate, false);

    // Wait until speaker finishes (or timeout)
    unsigned long playMs = (unsigned long)(samples * 1000UL / (unsigned long)sampleRate) + 400;
    unsigned long start = millis();
    while (M5.Speaker.isPlaying() && (millis() - start) < playMs) {
        delay(20);
        M5.update();
    }
    M5.Speaker.stop();
    free(buf);
}

void requestImage(const String& prompt) {
    if (WiFi.status() != WL_CONNECTED) {
        lastReply = "Wi-Fi disconnected";
        drawChatUI();
        return;
    }
    isBusy = true;
    lastReply = "Generating image...";
    drawChatUI();

    WiFiClient client;
    HTTPClient http;
    http.setConnectTimeout(10000);
    http.setTimeout(120000);
    http.setReuse(false);
    http.useHTTP10(true);

    String url = serverBase + "/api/image";
    if (!http.begin(client, url)) {
        lastReply = "Bad URL";
        isBusy = false;
        drawChatUI();
        return;
    }
    http.addHeader("Content-Type", "application/json");

    JsonDocument body;
    body["prompt"] = prompt;
    body["model_type"] = modelMode;
    if (activeLora != "None") body["lora"] = activeLora;
    String bodyStr;
    serializeJson(body, bodyStr);

    int code = http.POST(bodyStr);
    if (code == 200) {
        // Success = raw RGB565 body
        if (http.hasHeader("X-Lora")) {
            activeLora = http.header("X-Lora");
        }
        const size_t need = 320UL * 240UL * 2UL;
        int len = http.getSize();
        if (len <= 0) len = (int)need;

        if (currentImageBuffer) { free(currentImageBuffer); currentImageBuffer = nullptr; }
        currentImageBuffer = (uint8_t*)malloc(need);
        if (!currentImageBuffer) {
            lastReply = "Out of memory";
            http.end();
            isBusy = false;
            drawChatUI();
            return;
        }

        size_t got = 0;
        WiFiClient* stream = http.getStreamPtr();
        while (http.connected() && got < need) {
            size_t avail = stream->available();
            if (avail) {
                int n = stream->readBytes(currentImageBuffer + got, min(avail, (size_t)(need - got)));
                if (n <= 0) break;
                got += n;
            } else {
                delay(1);
            }
        }
        http.end();

        if (got >= need) {
            currentState = STATE_PICTURE;
            pictureEnterMs = millis();
            touchStartX = -1;  // no active swipe yet
            isBusy = false;
            drawPictureUI();
            return;
        }
        free(currentImageBuffer);
        currentImageBuffer = nullptr;
        lastReply = "Incomplete image";
    } else {
        // Error JSON
        String resp = http.getString();
        JsonDocument doc;
        if (!deserializeJson(doc, resp) && doc["error"].is<const char*>()) {
            lastReply = doc["error"].as<String>();
        } else {
            lastReply = http.errorToString(code);
        }
        http.end();
    }
    isBusy = false;
    currentState = STATE_CHAT;
    drawChatUI();
}

void cycleLora() {
    if (WiFi.status() != WL_CONNECTED) return;
    WiFiClient client;
    HTTPClient http;
    http.setConnectTimeout(4000);
    http.setTimeout(6000);
    http.setReuse(false);
    http.useHTTP10(true);
    if (!http.begin(client, serverBase + "/api/lora")) return;
    http.addHeader("Content-Type", "application/json");
    if (http.POST("{\"action\":\"cycle\"}") == 200) {
        String resp = http.getString();
        JsonDocument doc;
        if (!deserializeJson(doc, resp) && doc["lora"].is<const char*>()) {
            activeLora = doc["lora"].as<String>();
            lastReply = String("LoRA → ") + activeLora;
        }
    }
    http.end();
    if (currentState == STATE_CHAT) drawChatUI();
}

void clearLora() {
    if (WiFi.status() != WL_CONNECTED) return;
    WiFiClient client;
    HTTPClient http;
    http.setConnectTimeout(4000);
    http.setTimeout(6000);
    http.setReuse(false);
    http.useHTTP10(true);
    if (!http.begin(client, serverBase + "/api/lora")) return;
    http.addHeader("Content-Type", "application/json");
    if (http.POST("{\"action\":\"clear\"}") == 200) {
        String resp = http.getString();
        JsonDocument doc;
        if (!deserializeJson(doc, resp) && doc["lora"].is<const char*>()) {
            activeLora = doc["lora"].as<String>();
            lastReply = String("LoRA → ") + activeLora;
        }
    }
    http.end();
    if (currentState == STATE_CHAT) drawChatUI();
}

void startRecording() {
    if (isBusy || isRecording) return;
    if (WiFi.status() != WL_CONNECTED) {
        lastReply = "Wi-Fi disconnected";
        drawChatUI();
        return;
    }

    isRecording = true;
    lastReply = "Recording...";
    drawChatUI();

    const int seconds = RECORD_SECONDS;
    const int rate = SAMPLE_RATE;
    micSamples = rate * seconds;
    if (micBuffer) { free(micBuffer); micBuffer = nullptr; }
    micBuffer = (int16_t*)malloc(micSamples * sizeof(int16_t));
    if (!micBuffer) {
        isRecording = false;
        lastReply = "Out of memory";
        drawChatUI();
        return;
    }

    memset(micBuffer, 0, micSamples * sizeof(int16_t));
    M5.Mic.begin();
    // Blocking record for the duration
    M5.Mic.record(micBuffer, micSamples, rate);
    M5.Mic.end();
    isRecording = false;

    lastReply = "Transcribing...";
    isBusy = true;
    drawChatUI();

    // Build a minimal WAV in memory so /api/stt always gets a valid file
    // 44-byte header + PCM
    size_t pcmBytes = micSamples * 2;
    size_t wavLen = 44 + pcmBytes;
    uint8_t* wav = (uint8_t*)malloc(wavLen);
    if (!wav) {
        free(micBuffer); micBuffer = nullptr;
        isBusy = false;
        lastReply = "Out of memory";
        drawChatUI();
        return;
    }
    // RIFF header
    memcpy(wav + 0, "RIFF", 4);
    uint32_t chunkSize = 36 + pcmBytes;
    memcpy(wav + 4, &chunkSize, 4);
    memcpy(wav + 8, "WAVE", 4);
    memcpy(wav + 12, "fmt ", 4);
    uint32_t sub1 = 16; memcpy(wav + 16, &sub1, 4);
    uint16_t fmt = 1; memcpy(wav + 20, &fmt, 2);          // PCM
    uint16_t ch = 1; memcpy(wav + 22, &ch, 2);
    uint32_t sr = rate; memcpy(wav + 24, &sr, 4);
    uint32_t br = rate * 2; memcpy(wav + 28, &br, 4);
    uint16_t ba = 2; memcpy(wav + 32, &ba, 2);
    uint16_t bps = 16; memcpy(wav + 34, &bps, 2);
    memcpy(wav + 36, "data", 4);
    uint32_t dataSz = pcmBytes; memcpy(wav + 40, &dataSz, 4);
    memcpy(wav + 44, micBuffer, pcmBytes);

    free(micBuffer);
    micBuffer = nullptr;
    micSamples = 0;

    // POST binary WAV to /api/stt
    WiFiClient client;
    HTTPClient http;
    http.setConnectTimeout(8000);
    http.setTimeout(60000);
    http.setReuse(false);
    http.useHTTP10(true);

    String url = serverBase + "/api/stt?sample_rate=" + String(rate);
    if (!http.begin(client, url)) {
        free(wav);
        isBusy = false;
        lastReply = "Bad STT URL";
        drawChatUI();
        return;
    }
    http.addHeader("Content-Type", "audio/wav");
    http.addHeader("Content-Length", String(wavLen));

    int code = http.POST(wav, wavLen);
    free(wav);

    String transcript = "";
    if (code == 200) {
        String resp = http.getString();
        JsonDocument doc;
        if (!deserializeJson(doc, resp) && doc["text"].is<const char*>()) {
            transcript = doc["text"].as<String>();
        } else if (doc["error"].is<const char*>()) {
            lastReply = doc["error"].as<String>();
            http.end();
            isBusy = false;
            drawChatUI();
            return;
        } else {
            lastReply = "STT bad JSON";
            http.end();
            isBusy = false;
            drawChatUI();
            return;
        }
    } else {
        lastReply = http.errorToString(code);
        http.end();
        isBusy = false;
        drawChatUI();
        return;
    }
    http.end();

    if (transcript.length() == 0) {
        isBusy = false;
        lastReply = "Empty transcript";
        drawChatUI();
        return;
    }

    // Show what was heard, then send to chat
    lastReply = String("Heard: ") + transcript;
    drawChatUI();
    delay(400);
    isBusy = false;
    sendChat(transcript);
}

void saveImageToSD() {
    if (!currentImageBuffer || !SD.begin(4)) return;
    if (!SD.exists("/images")) SD.mkdir("/images");
    if (!SD.exists("/images/comfy")) SD.mkdir("/images/comfy");
    String fn = "/images/comfy/img_" + String(millis()) + ".raw";
    File f = SD.open(fn.c_str(), FILE_WRITE);
    if (f) {
        f.write(currentImageBuffer, 320 * 240 * 2);
        f.close();
        lastReply = "Saved " + fn;
    }
}

// ---------- setup / loop ----------
void setup() {
    auto cfg = M5.config();
    cfg.internal_mic = true;
    cfg.internal_spk = true;
    M5.begin(cfg);
    M5.Display.setBrightness(80);
    M5.Speaker.setVolume(200);
    M5.Speaker.begin();
    loadSettings();
    connectWiFi();
    drawChatUI();
}

void loop() {
    M5.update();

    if (M5.BtnA.wasPressed()) {
        if (currentState == STATE_CHAT) {
            activeEditingField = nullptr;
            keyboardTitle = "Image Prompt";
            keyboardBuffer = "";
            kbMode = MODE_LOWER;
            shiftHeld = false;
            currentState = STATE_KEYBOARD;
            drawKeyboardUI();
        } else if (currentState == STATE_PICTURE ||
                   (currentState == STATE_KEYBOARD && keyboardTitle == "Image Prompt")) {
            currentState = STATE_CHAT;
            drawChatUI();
        }
    }

    if (M5.BtnB.wasPressed()) {
        btnBPressStart = millis();
        btnBLongDone = false;
    }
    if (M5.BtnB.isPressed() && !btnBLongDone && millis() - btnBPressStart > 800) {
        btnBLongDone = true;
        clearLora();
    }
    if (M5.BtnB.wasReleased() && !btnBLongDone) cycleLora();

    // Button C = toggle realistic / anime
    if (M5.BtnC.wasPressed()) {
        modelMode = (modelMode == "realistic") ? "anime" : "realistic";
        if (currentState == STATE_CHAT) drawChatUI();
    }

    if (M5.Touch.getCount() > 0) {
        auto t = M5.Touch.getDetail(0);
        int x = t.x, y = t.y;
        if (y >= 240) return;

        if (currentState == STATE_PICTURE) {
            // Ignore leftover touches from keyboard OK / image download wait
            if (millis() - pictureEnterMs < 500) {
                return;
            }
            if (t.wasPressed()) {
                touchStartX = x;
            }
            if (t.wasReleased() && touchStartX >= 0) {
                int d = x - touchStartX;
                touchStartX = -1;
                if (d < -50) {
                    if (currentImageBuffer) { free(currentImageBuffer); currentImageBuffer = nullptr; }
                    lastReply = "Discarded";
                    currentState = STATE_CHAT;
                    drawChatUI();
                } else if (d > 50) {
                    saveImageToSD();
                    if (currentImageBuffer) { free(currentImageBuffer); currentImageBuffer = nullptr; }
                    currentState = STATE_CHAT;
                    drawChatUI();
                }
                // small movement = stay on image (no-op)
            }
            return;
        }

        if (!t.wasPressed()) return;

        if (currentState == STATE_CHAT) {
            if (x > 250 && y < 32) {
                currentState = STATE_SETTINGS;
                drawSettingsUI();
            } else if (x < 160 && y > 185) {
                activeEditingField = nullptr;
                keyboardTitle = "Message";
                keyboardBuffer = "";
                kbMode = MODE_LOWER;
                shiftHeld = false;
                currentState = STATE_KEYBOARD;
                drawKeyboardUI();
            } else if (x >= 160 && y > 185) {
                startRecording();
            } else if (y > 155 && y < 185) {
                // status bar still toggles model too
                modelMode = modelMode == "realistic" ? "anime" : "realistic";
                drawChatUI();
            } else if (y >= 40 && y <= 150) {
                // tap reply box → next page of long text
                int totalPages = (lastReply.length() + CHARS_PER_PAGE - 1) / CHARS_PER_PAGE;
                if (totalPages < 1) totalPages = 1;
                replyPage = (replyPage + 1) % totalPages;
                drawChatUI();
            }
        } else if (currentState == STATE_SETTINGS) {
            if (y > 56 && y < 90) {
                activeEditingField = &wifiSsid;
                keyboardTitle = "Wi-Fi SSID";
                keyboardBuffer = wifiSsid;
                kbMode = MODE_LOWER;
                currentState = STATE_KEYBOARD;
                drawKeyboardUI();
            } else if (y > 106 && y < 140) {
                activeEditingField = &wifiPassword;
                keyboardTitle = "Wi-Fi Password";
                keyboardBuffer = wifiPassword;
                kbMode = MODE_LOWER;
                currentState = STATE_KEYBOARD;
                drawKeyboardUI();
            } else if (y > 156 && y < 190) {
                activeEditingField = &serverBase;
                keyboardTitle = "Server Base URL";
                keyboardBuffer = serverBase;
                kbMode = MODE_SYMBOLS;
                currentState = STATE_KEYBOARD;
                drawKeyboardUI();
            } else if (y > 200) {
                if (x < 160) {
                    currentState = STATE_CHAT;
                    drawChatUI();
                } else {
                    saveSettings();
                    connectWiFi();
                    currentState = STATE_CHAT;
                    drawChatUI();
                }
            }
        } else if (currentState == STATE_KEYBOARD) {
            handleKeyboardTouch(x, y);
        }
    }
    delay(20);
}
