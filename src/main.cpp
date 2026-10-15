#include <M5Unified.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <mbedtls/base64.h>
#include <Preferences.h>
#include <SD.h>
#include <SPI.h>
#include <vector>

Preferences preferences;

// ==========================================
// CONFIGURATION & EDITABLE SETTINGS
// ==========================================
const char* WIFI_SSID = "DEFAULT_SSID";
const char* WIFI_PASSWORD = "DEFAULT_PASSWORD";
const char* PYTHON_SERVER_URL = "http://192.168.1.157:5000/api/chat"; 
// ==========================================

String wifiSsid = WIFI_SSID;
String wifiPassword = WIFI_PASSWORD;
String serverUrl = PYTHON_SERVER_URL;

// Color Palette (#007373, #5d005d, #00ff00)
#define COLOR_BG          0x0841  
#define COLOR_TEAL        0x03A6  
#define COLOR_PURPLE      0x580A  
#define COLOR_GREEN       0x07E0  
#define COLOR_WHITE       0xFFFF
#define COLOR_RED         0xF800

enum AppState { STATE_CHAT, STATE_SETTINGS, STATE_KEYBOARD, STATE_PICTURE };
AppState currentState = STATE_CHAT;

String lastReply = "Ready. Tap screen to chat or record.";
bool isConnected = false;
bool isRecording = false;

// Image Generation & Model State
String modelMode = "realistic"; // "realistic" or "anime"
std::vector<String> loraNames = {"detailer.safetensors", "cyberpunk.safetensors"};
int activeLoraIdx = -1; // -1 = No LoRA

unsigned long buttonBPressTime = 0;
bool buttonBHeld = false;

// Keyboard editing state
String* activeEditingField = nullptr;
String keyboardBuffer = "";
String keyboardTitle = "";

enum KeyboardMode { MODE_LOWER, MODE_UPPER, MODE_SYMS };
KeyboardMode kbMode = MODE_LOWER;

// Audio Recording Buffers
#define RECORD_TIME_SEC   4
#define SAMPLE_RATE       16000
int16_t* micBuffer = nullptr;
size_t bufferSize = 0;

// Picture Mode buffer
uint8_t* currentImageBuffer = nullptr;
int touchStartX = 0;
int touchEndX = 0;

void loadSettings() {
    // 1. Try loading from SD Card config file first
    if (SD.begin(4)) {
        if (SD.exists("/wifi_config.json")) {
            File file = SD.open("/wifi_config.json", FILE_READ);
            if (file) {
                StaticJsonDocument<512> doc;
                DeserializationError error = deserializeJson(doc, file);
                if (!error) {
                    if (doc.containsKey("ssid")) wifiSsid = doc["ssid"].as<String>();
                    if (doc.containsKey("pass")) wifiPassword = doc["pass"].as<String>();
                }
                file.close();
            }
        }
    }

    // 2. Fallback to NVS Preferences for Wi-Fi only, force serverUrl to code constant
    preferences.begin("mantiskai", true);
    wifiSsid = preferences.getString("ssid", wifiSsid);
    wifiPassword = preferences.getString("pass", wifiPassword);
    preferences.end();

    // FORCE the server URL to match your hardcoded constant so NVS can't override it with a bad string
    serverUrl = PYTHON_SERVER_URL;
}

void saveSettings() {
    preferences.begin("mantiskai", false);
    preferences.putString("ssid", wifiSsid);
    preferences.putString("pass", wifiPassword);
    preferences.putString("server", serverUrl);
    preferences.end();
}

void connectWiFi() {
    WiFi.begin(wifiSsid.c_str(), wifiPassword.c_str());
    int attempts = 0;
    while (WiFi.status() != WL_CONNECTED && attempts < 15) {
        delay(500);
        attempts++;
    }
    isConnected = (WiFi.status() == WL_CONNECTED);
}

void drawChatUI() {
    M5.Display.fillScreen(COLOR_BG);

    // Header Bar
    M5.Display.fillRect(0, 0, 320, 35, COLOR_TEAL);
    M5.Display.setTextColor(COLOR_WHITE, COLOR_TEAL);
    M5.Display.setTextSize(2);
    M5.Display.setCursor(10, 8);
    M5.Display.print("MantisKAI Chat");

    // Settings Button Top Right
    M5.Display.fillRect(270, 5, 40, 25, COLOR_PURPLE);
    M5.Display.setTextSize(1);
    M5.Display.setCursor(278, 12);
    M5.Display.print("CFG");

    // Status Dot
    M5.Display.fillCircle(250, 18, 6, isConnected ? COLOR_GREEN : COLOR_RED);

    // Chat Box Area
    M5.Display.drawRect(10, 45, 300, 125, COLOR_PURPLE);
    M5.Display.setTextColor(COLOR_WHITE, COLOR_BG);
    M5.Display.setTextSize(1);
    M5.Display.setCursor(15, 52);
    M5.Display.print(isRecording ? "RECORDING VOICE..." : "AI Response:");
    
    M5.Display.setCursor(15, 70);
    M5.Display.setTextSize(2);
    M5.Display.setTextColor(isRecording ? COLOR_RED : COLOR_GREEN, COLOR_BG);
    
    String displayStr = lastReply;
    if (displayStr.length() > 80) displayStr = displayStr.substring(0, 77) + "...";
    M5.Display.println(displayStr);

    // Model & LoRA Status Bar
    M5.Display.fillRect(10, 175, 300, 22, 0x1ef8);
    M5.Display.setTextColor(COLOR_WHITE, 0x1ef8);
    M5.Display.setTextSize(1);
    M5.Display.setCursor(15, 182);
    String loraStatus = (activeLoraIdx >= 0 && activeLoraIdx < (int)loraNames.size()) ? loraNames[activeLoraIdx] : "None";
    M5.Display.print("M: " + modelMode + " | LoRA: " + loraStatus);

    // Bottom Action Buttons
    M5.Display.fillRect(10, 205, 140, 35, COLOR_PURPLE);
    M5.Display.setTextColor(COLOR_WHITE, COLOR_PURPLE);
    M5.Display.setTextSize(1);
    M5.Display.setCursor(35, 217);
    M5.Display.print("SEND TEXT");

    M5.Display.fillRect(170, 205, 140, 35, isRecording ? COLOR_RED : COLOR_TEAL);
    M5.Display.setTextColor(COLOR_WHITE, isRecording ? COLOR_RED : COLOR_TEAL);
    M5.Display.setCursor(195, 217);
    M5.Display.print(isRecording ? "STOP & SEND" : "RECORD VOICE");
}

void drawSettingsUI() {
    M5.Display.fillScreen(COLOR_BG);

    // Header
    M5.Display.fillRect(0, 0, 320, 35, COLOR_PURPLE);
    M5.Display.setTextColor(COLOR_WHITE, COLOR_PURPLE);
    M5.Display.setTextSize(2);
    M5.Display.setCursor(10, 8);
    M5.Display.print("Device Settings");

    M5.Display.setTextSize(1);
    M5.Display.setTextColor(COLOR_WHITE, COLOR_BG);

    // SSID Row
    M5.Display.setCursor(15, 45);
    M5.Display.print("Wi-Fi SSID:");
    M5.Display.fillRect(15, 60, 290, 25, 0x1ef8);
    M5.Display.setCursor(20, 66);
    M5.Display.print(wifiSsid);

    // Password Row
    M5.Display.setCursor(15, 95);
    M5.Display.print("Wi-Fi Password:");
    M5.Display.fillRect(15, 110, 290, 25, 0x1ef8);
    M5.Display.setCursor(20, 116);
    M5.Display.print("********");

    // Server URL Row
    M5.Display.setCursor(15, 145);
    M5.Display.print("PC Server URL:");
    M5.Display.fillRect(15, 160, 290, 25, 0x1ef8);
    M5.Display.setCursor(20, 166);
    M5.Display.print(serverUrl);

    // Bottom Navigation Buttons
    M5.Display.fillRect(10, 200, 145, 35, COLOR_TEAL);
    M5.Display.setTextColor(COLOR_WHITE, COLOR_TEAL);
    M5.Display.setCursor(45, 212);
    M5.Display.print("< BACK");

    M5.Display.fillRect(165, 200, 145, 35, COLOR_GREEN);
    M5.Display.setTextColor(0x0000, COLOR_GREEN);
    M5.Display.setCursor(195, 212);
    M5.Display.print("SAVE & CONNECT");
}

void drawKeyboardUI() {
    M5.Display.fillScreen(COLOR_BG);

    M5.Display.fillRect(0, 0, 320, 30, COLOR_TEAL);
    M5.Display.setTextColor(COLOR_WHITE, COLOR_TEAL);
    M5.Display.setTextSize(1);
    M5.Display.setCursor(10, 10);
    String modeStr = (kbMode == MODE_LOWER) ? "[abc]" : (kbMode == MODE_UPPER) ? "[ABC]" : "[SYM]";
    M5.Display.print("Edit: " + keyboardTitle + " " + modeStr);

    M5.Display.drawRect(10, 35, 300, 30, COLOR_PURPLE);
    M5.Display.setTextColor(COLOR_GREEN, COLOR_BG);
    M5.Display.setTextSize(2);
    M5.Display.setCursor(15, 42);
    M5.Display.print(keyboardBuffer);

    String r0 = "1234567890";
    String r1 = (kbMode == MODE_UPPER) ? "QWERTYUIOP" : "qwertyuiop";
    String r2 = (kbMode == MODE_UPPER) ? "ASDFGHJKL:" : "asdfghjkl;";
    String r3 = (kbMode == MODE_SYMS) ? "-_+=;:/?.,$" : ((kbMode == MODE_UPPER) ? "ZXCVBNM" : "zxcvbnm");

    // Row 0: Numbers
    int len0 = r0.length();
    int btnW0 = 300 / len0;
    for (int c = 0; c < len0; c++) {
        int x = 10 + (c * btnW0);
        int y = 75;
        M5.Display.fillRect(x + 1, y + 1, btnW0 - 2, 32, 0x2a2a);
        M5.Display.drawRect(x, y, btnW0, 34, COLOR_PURPLE);
        M5.Display.setTextColor(COLOR_WHITE, 0x2a2a);
        M5.Display.setTextSize(1);
        M5.Display.setCursor(x + 4, y + 10);
        M5.Display.print(r0[c]);
    }

    // Row 1: QWERTY
    int len1 = r1.length();
    int btnW1 = 300 / len1;
    for (int c = 0; c < len1; c++) {
        int x = 10 + (c * btnW1);
        int y = 110;
        M5.Display.fillRect(x + 1, y + 1, btnW1 - 2, 32, 0x2a2a);
        M5.Display.drawRect(x, y, btnW1, 34, COLOR_PURPLE);
        M5.Display.setTextColor(COLOR_WHITE, 0x2a2a);
        M5.Display.setTextSize(1);
        M5.Display.setCursor(x + 4, y + 10);
        M5.Display.print(r1[c]);
    }

    // Row 2: ASDFGH
    int len2 = r2.length();
    int btnW2 = 300 / len2;
    for (int c = 0; c < len2; c++) {
        int x = 10 + (c * btnW2);
        int y = 145;
        M5.Display.fillRect(x + 1, y + 1, btnW2 - 2, 32, 0x2a2a);
        M5.Display.drawRect(x, y, btnW2, 34, COLOR_PURPLE);
        M5.Display.setTextColor(COLOR_WHITE, 0x2a2a);
        M5.Display.setTextSize(1);
        M5.Display.setCursor(x + 4, y + 10);
        M5.Display.print(r2[c]);
    }

    // Row 3: Special controls (Shift, Sym, Backspace, OK)
    const char* labels[4] = {
        (kbMode == MODE_UPPER) ? "abc" : "ABC", 
        (kbMode == MODE_SYMS) ? "abc" : "SYM", 
        "<-", 
        "OK"
    };
    int bWidth = 300 / 4;
    for (int i = 0; i < 4; i++) {
        int x = 10 + (i * bWidth);
        int y = 180;
        uint16_t btnColor = (i == 3) ? COLOR_GREEN : ((i == 2) ? COLOR_RED : 0x2a2a);
        M5.Display.fillRect(x + 1, y + 1, bWidth - 2, 35, btnColor);
        M5.Display.drawRect(x, y, bWidth, 37, COLOR_PURPLE);
        M5.Display.setTextColor((i == 3) ? 0x0000 : COLOR_WHITE, btnColor);
        M5.Display.setTextSize(1);
        M5.Display.setCursor(x + (bWidth / 2) - 10, y + 12);
        M5.Display.print(labels[i]);
    }

    // Row 4: ZXCVBNM letter keys
    int len4 = r3.length();
    int btnW4 = 300 / len4;
    for (int c = 0; c < len4; c++) {
        int x = 10 + (c * btnW4);
        int y = 220;
        M5.Display.fillRect(x + 1, y + 1, btnW4 - 2, 28, 0x2a2a);
        M5.Display.drawRect(x, y, btnW4, 30, COLOR_PURPLE);
        M5.Display.setTextColor(COLOR_WHITE, 0x2a2a);
        M5.Display.setTextSize(1);
        M5.Display.setCursor(x + 4, y + 8);
        M5.Display.print(r3[c]);
    }
}

void handleKeyboardTouch(int touchX, int touchY) {
    if (touchY >= 75 && touchY <= 109) {
        String r0 = "1234567890";
        int btnW = 300 / r0.length();
        int idx = (touchX - 10) / btnW;
        if (idx >= 0 && idx < (int)r0.length()) {
            keyboardBuffer += r0[idx];
            drawKeyboardUI();
        }
        return;
    }
    if (touchY >= 110 && touchY <= 144) {
        String r1 = (kbMode == MODE_UPPER) ? "QWERTYUIOP" : "qwertyuiop";
        int btnW = 300 / r1.length();
        int idx = (touchX - 10) / btnW;
        if (idx >= 0 && idx < (int)r1.length()) {
            keyboardBuffer += r1[idx];
            drawKeyboardUI();
        }
        return;
    }
    if (touchY >= 145 && touchY <= 179) {
        String r2 = (kbMode == MODE_UPPER) ? "ASDFGHJKL:" : "asdfghjkl;";
        int btnW = 300 / r2.length();
        int idx = (touchX - 10) / btnW;
        if (idx >= 0 && idx < (int)r2.length()) {
            keyboardBuffer += r2[idx];
            drawKeyboardUI();
        }
        return;
    }
    if (touchY >= 180 && touchY <= 216) {
        int bWidth = 300 / 4;
        int idx = (touchX - 10) / bWidth;
        if (idx == 0) { 
            if (kbMode == MODE_UPPER) kbMode = MODE_LOWER;
            else kbMode = MODE_UPPER;
        } else if (idx == 1) { 
            if (kbMode == MODE_SYMS) kbMode = MODE_LOWER;
            else kbMode = MODE_SYMS;
        } else if (idx == 2) { 
            if (keyboardBuffer.length() > 0) keyboardBuffer.remove(keyboardBuffer.length() - 1);
        } else if (idx == 3) { 
            if (activeEditingField) *activeEditingField = keyboardBuffer;
            currentState = STATE_SETTINGS;
            drawSettingsUI();
            return;
        }
        drawKeyboardUI();
        return;
    }
    if (touchY >= 217 && touchY <= 247) {
        String r3 = (kbMode == MODE_SYMS) ? "-_+=;:/?.,$" : ((kbMode == MODE_UPPER) ? "ZXCVBNM" : "zxcvbnm");
        int btnW = 300 / r3.length();
        int idx = (touchX - 10) / btnW;
        if (idx >= 0 && idx < (int)r3.length()) {
            keyboardBuffer += r3[idx];
            drawKeyboardUI();
        }
        return;
    }
}

void drawPictureUI() {
    M5.Display.fillScreen(0x0000);
    if (currentImageBuffer) {
        M5.Display.pushImage(0, 0, 320, 240, (uint16_t*)currentImageBuffer);
    }
    M5.Display.fillRect(0, 210, 320, 30, 0x0000);
    M5.Display.setTextColor(0xFFFF, 0x0000);
    M5.Display.setTextSize(1);
    M5.Display.setCursor(10, 220);
    M5.Display.print("< Swipe Left: Delete  |  Swipe Right: Save >");
}

void saveImageToSD() {
    if (!currentImageBuffer) return;
    if (!SD.begin(4)) return;

    if (!SD.exists("/images")) SD.mkdir("/images");
    if (!SD.exists("/images/comfy")) SD.mkdir("/images/comfy");

    String filename = "/images/comfy/img_" + String(millis()) + ".mg";
    File file = SD.open(filename.c_str(), FILE_WRITE);
    if (file) {
        file.write(currentImageBuffer, 320 * 240 * 2);
        file.close();
        lastReply = "Saved to SD: " + filename;
    }
}


void sendPayloadToServer(String message, String audioBase64 = "") {
    if (WiFi.status() != WL_CONNECTED) {
        isConnected = false;
        lastReply = "Wi-Fi disconnected";
        Serial.println("[NET] Wi-Fi is not connected");
        drawChatUI();
        return;
    }

    isConnected = true;

    // FIX: Instantiate an explicit WiFiClient for stable socket handling
    WiFiClient wifiClient;
    HTTPClient http;
    
    http.setConnectTimeout(5000);
    http.setTimeout(30000);
    http.setReuse(false); // Prevent stale keep-alive connection reuse errors

    Serial.printf("[HTTP] Connecting to %s\n", serverUrl.c_str());

    // FIX: Pass the wifiClient instance explicitly to http.begin()
    if (!http.begin(wifiClient, serverUrl)) {
        lastReply = "Invalid server URL";
        Serial.println("[HTTP] http.begin() failed");
        drawChatUI();
        return;
    }

    http.addHeader("Content-Type", "application/json");

    JsonDocument doc;
    if (message.length() > 0) doc["message"] = message;
    if (audioBase64.length() > 0) doc["audio_base64"] = audioBase64;

    String requestBody;
    serializeJson(doc, requestBody);

    int code = http.POST(requestBody);

    if (code > 0) {
        String body = http.getString();
        JsonDocument responseDoc;
        DeserializationError err = deserializeJson(responseDoc, body);

        if (err) {
            lastReply = "Bad JSON response";
        } else if (code < 200 || code >= 300) {
            lastReply = responseDoc["error"] | "Server returned HTTP error";
        } else if (responseDoc["reply"].is<const char*>()) {
            lastReply = responseDoc["reply"].as<String>();

            String responseAudio = responseDoc["audio_base64"].as<String>();
            if (responseAudio.length() > 0) {
                size_t decodedLen = 0;
                int rc = mbedtls_base64_decode(
                    nullptr, 0, &decodedLen,
                    (const unsigned char*)responseAudio.c_str(),
                    responseAudio.length());

                if (rc == 0 && decodedLen > 0) {
                    auto* audioBytes = (unsigned char*)malloc(decodedLen);
                    if (audioBytes) {
                        size_t actualLen = 0;
                        rc = mbedtls_base64_decode(
                            audioBytes, decodedLen, &actualLen,
                            (const unsigned char*)responseAudio.c_str(),
                            responseAudio.length());

                        if (rc == 0) {
                            M5.Speaker.begin();
                            M5.Speaker.playRaw(
                                (const uint8_t*)audioBytes,
                                actualLen, 22050, false, 1, 0);
                        }
                        free(audioBytes);
                    }
                }
            }
        } else {
            lastReply = responseDoc["error"] | "Missing reply in JSON";
        }
    } else {
        lastReply = http.errorToString(code);
        Serial.printf("[HTTP] Connection failed: %s\n", lastReply.c_str());
    }

    http.end();
    drawChatUI();
}


void setup() {
    auto cfg = M5.config();
    M5.begin(cfg);
    loadSettings();
    Serial.printf("[CONFIG] Server URL: %s\n", serverUrl.c_str());
    connectWiFi();
    drawChatUI();
}

void loop() {
    M5.update();

    if (M5.Touch.getCount() > 0) {
        auto touch = M5.Touch.getDetail(0);
        int x = touch.x;
        int y = touch.y;

        if (y >= 240) {
            if (x >= 214) { 
                if (touch.wasPressed()) {
                    modelMode = (modelMode == "realistic") ? "anime" : "realistic";
                    if (currentState == STATE_CHAT) drawChatUI();
                }
            } 
            else if (x >= 107 && x <= 213) { 
                if (touch.wasPressed()) {
                    buttonBPressTime = millis();
                    buttonBHeld = false;
                }
                if (touch.isPressed() && !buttonBHeld && (millis() - buttonBPressTime > 800)) {
                    activeLoraIdx = -1; 
                    buttonBHeld = true;
                    if (currentState == STATE_CHAT) drawChatUI();
                } 
                else if (touch.wasReleased() && !buttonBHeld) {
                    if (!loraNames.empty()) {
                        activeLoraIdx++;
                        if (activeLoraIdx >= (int)loraNames.size()) {
                            activeLoraIdx = -1; 
                        }
                    }
                    if (currentState == STATE_CHAT) drawChatUI();
                }
            }
        }
    }

    if (currentState == STATE_PICTURE) {
        if (M5.Touch.getCount() > 0) {
            auto touch = M5.Touch.getDetail(0);
            if (touch.wasPressed() && touch.y < 240) touchStartX = touch.x;
            if (touch.wasReleased()) {
                touchEndX = touch.x;
                int diff = touchEndX - touchStartX;
                if (diff < -50) { 
                    if (currentImageBuffer) { free(currentImageBuffer); currentImageBuffer = nullptr; }
                    currentState = STATE_CHAT;
                    drawChatUI();
                } else if (diff > 50) { 
                    saveImageToSD();
                    if (currentImageBuffer) { free(currentImageBuffer); currentImageBuffer = nullptr; }
                    currentState = STATE_CHAT;
                    drawChatUI();
                }
            }
        }
        return;
    }

    if (M5.Touch.getCount() > 0) {
        auto touch = M5.Touch.getDetail(0);
        if (touch.wasPressed()) {
            int x = touch.x;
            int y = touch.y;
            if (y >= 240) return; 

            if (currentState == STATE_CHAT) {
                if (x >= 270 && x <= 310 && y >= 5 && y <= 30) {
                    currentState = STATE_SETTINGS;
                    drawSettingsUI();
                } else if (x >= 10 && x <= 150 && y >= 205 && y <= 240) {
                    sendPayloadToServer("Hello from MantisKAI Core2!");
                }
            } 
            else if (currentState == STATE_SETTINGS) {
                if (y >= 60 && y <= 85) {
                    activeEditingField = &wifiSsid;
                    keyboardTitle = "Wi-Fi SSID";
                    keyboardBuffer = wifiSsid;
                    kbMode = MODE_LOWER;
                    currentState = STATE_KEYBOARD;
                    drawKeyboardUI();
                } else if (y >= 110 && y <= 135) {
                    activeEditingField = &wifiPassword;
                    keyboardTitle = "Wi-Fi Password";
                    keyboardBuffer = wifiPassword;
                    kbMode = MODE_LOWER;
                    currentState = STATE_KEYBOARD;
                    drawKeyboardUI();
                } else if (y >= 160 && y <= 185) {
                    activeEditingField = &serverUrl;
                    keyboardTitle = "PC Server URL";
                    keyboardBuffer = serverUrl;
                    kbMode = MODE_SYMS;
                    currentState = STATE_KEYBOARD;
                    drawKeyboardUI();
                } else if (x >= 10 && x <= 155 && y >= 200 && y <= 235) {
                    currentState = STATE_CHAT;
                    drawChatUI();
                } else if (x >= 165 && x <= 310 && y >= 200 && y <= 235) {
                    saveSettings();
                    connectWiFi();
                    currentState = STATE_CHAT;
                    drawChatUI();
                }
            }
            else if (currentState == STATE_KEYBOARD) {
                handleKeyboardTouch(x, y);
            }
        }
    }
    delay(100);
}
