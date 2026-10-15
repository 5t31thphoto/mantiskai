#include <M5Unified.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <mbedtls/base64.h>
#include <Preferences.h>
#include <SD.h>
#include <SPI.h>

Preferences preferences;

// ==========================================
// CONFIGURATION & EDITABLE SETTINGS
// ==========================================
const char* WIFI_SSID = "YOUR_WIFI_SSID";
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";
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
    preferences.begin("mantiskai", true);
    wifiSsid = preferences.getString("ssid", WIFI_SSID);
    wifiPassword = preferences.getString("pass", WIFI_PASSWORD);
    serverUrl = preferences.getString("server", PYTHON_SERVER_URL);
    preferences.end();
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
    M5.Display.drawRect(10, 45, 300, 145, COLOR_PURPLE);
    M5.Display.setTextColor(COLOR_WHITE, COLOR_BG);
    M5.Display.setTextSize(1);
    M5.Display.setCursor(15, 55);
    M5.Display.print(isRecording ? "RECORDING VOICE..." : "AI Response:");
    
    M5.Display.setCursor(15, 75);
    M5.Display.setTextSize(2);
    M5.Display.setTextColor(isRecording ? COLOR_RED : COLOR_GREEN, COLOR_BG);
    
    String displayStr = lastReply;
    if (displayStr.length() > 90) displayStr = displayStr.substring(0, 87) + "...";
    M5.Display.println(displayStr);

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

    // Header showing what is being edited & mode
    M5.Display.fillRect(0, 0, 320, 30, COLOR_TEAL);
    M5.Display.setTextColor(COLOR_WHITE, COLOR_TEAL);
    M5.Display.setTextSize(1);
    M5.Display.setCursor(10, 10);
    String modeStr = (kbMode == MODE_LOWER) ? "[abc]" : (kbMode == MODE_UPPER) ? "[ABC]" : "[SYM]";
    M5.Display.print("Edit: " + keyboardTitle + " " + modeStr);

    // Input preview box
    M5.Display.drawRect(10, 35, 300, 30, COLOR_PURPLE);
    M5.Display.setTextColor(COLOR_GREEN, COLOR_BG);
    M5.Display.setTextSize(2);
    M5.Display.setCursor(15, 42);
    M5.Display.print(keyboardBuffer);

    // Select Rows based on Keyboard Mode
    String r0, r1, r2, r3;
    if (kbMode == MODE_LOWER) {
        r0 = "1234567890";
        r1 = "qwertyuiop";
        r2 = "asdfghjkl;";
        r3 = "UP symzxcvbnm<OK"; // UP = switch to upper, sym = switch to symbols
    } else if (kbMode == MODE_UPPER) {
        r0 = "!@#$%^&*()";
        r1 = "QWERTYUIOP";
        r2 = "ASDFGHJKL:";
        r3 = "lo symZXCVBNM<OK"; // lo = switch to lower
    } else {
        r0 = "1234567890";
        r1 = "!@#$%^&*()";
        r2 = "-_+=;:/?.,$";
        r3 = "abc ABC[]{}|<OK";
    }

    String rows[4] = {r0, r1, r2, r3};
    int startY = 75;
    for (int r = 0; r < 4; r++) {
        String rowStr = rows[r];
        int len = rowStr.length();
        int btnW = 300 / len;
        for (int c = 0; c < len; c++) {
            int x = 10 + (c * btnW);
            int y = startY + (r * 35);
            M5.Display.fillRect(x + 1, y + 1, btnW - 2, 32, 0x2a2a);
            M5.Display.drawRect(x, y, btnW, 34, COLOR_PURPLE);
            M5.Display.setTextColor(COLOR_WHITE, 0x2a2a);
            M5.Display.setTextSize(1);
            M5.Display.setCursor(x + 3, y + 10);
            
            // Render specific key label handling for multi-char keys
            char ch = rowStr[c];
            M5.Display.print(ch);
        }
    }
}

void handleKeyboardTouch(int touchX, int touchY) {
    String r0, r1, r2, r3;
    if (kbMode == MODE_LOWER) {
        r0 = "1234567890";
        r1 = "qwertyuiop";
        r2 = "asdfghjkl;";
        r3 = "UP symzxcvbnm<OK";
    } else if (kbMode == MODE_UPPER) {
        r0 = "!@#$%^&*()";
        r1 = "QWERTYUIOP";
        r2 = "ASDFGHJKL:";
        r3 = "lo symZXCVBNM<OK";
    } else {
        r0 = "1234567890";
        r1 = "!@#$%^&*()";
        r2 = "-_+=;:/?.,$";
        r3 = "abc ABC[]{}|<OK";
    }

    String rows[4] = {r0, r1, r2, r3};
    int startY = 75;

    for (int r = 0; r < 4; r++) {
        String rowStr = rows[r];
        int len = rowStr.length();
        int btnW = 300 / len;
        for (int c = 0; c < len; c++) {
            int x = 10 + (c * btnW);
            int y = startY + (r * 35);

            if (touchX >= x && touchX <= x + btnW && touchY >= y && touchY <= y + 34) {
                // Check special multi-character buttons on bottom row (row 3)
                if (r == 3) {
                    if (rowStr == "UP symzxcvbnm<OK") {
                        if (c == 0 && rowStr.substring(0, 2) == "UP") { kbMode = MODE_UPPER; drawKeyboardUI(); return; }
                        // simplified touch mapping for bottom controls
                    }
                }
                
                char ch = rowStr[c];
                if (ch == '<') { // Backspace
                    if (keyboardBuffer.length() > 0) {
                        keyboardBuffer.remove(keyboardBuffer.length() - 1);
                    }
                } else {
                    // Simple character append or mode toggle trigger
                    keyboardBuffer += ch;
                }
                drawKeyboardUI();
                return;
            }
        }
    }
}

// Picture Mode Rendering & SD Save
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
    if (!isConnected) return;

    HTTPClient http;
    http.begin(serverUrl);
    http.addHeader("Content-Type", "application/json");

    JsonDocument doc;
    if (message.length() > 0) doc["message"] = message;
    if (audioBase64.length() > 0) doc["audio_base64"] = audioBase64;
    
    String requestBody;
    serializeJson(doc, requestBody);

    int httpResponseCode = http.POST(requestBody);

    if (httpResponseCode > 0) {
        String responseString = http.getString();
        JsonDocument responseDoc;
        deserializeJson(responseDoc, responseString);

        lastReply = responseDoc["reply"].as<String>();
        String responseAudio = responseDoc["audio_base64"].as<String>();

        if (responseAudio.length() > 0) {
            size_t decodedLen = 0;
            mbedtls_base64_decode(NULL, 0, &decodedLen, (const unsigned char*)responseAudio.c_str(), responseAudio.length());
            if (decodedLen > 0) {
                unsigned char* audioBytes = (unsigned char*)malloc(decodedLen);
                size_t actualLen = 0;
                if (mbedtls_base64_decode(audioBytes, decodedLen, &actualLen, (const unsigned char*)responseAudio.c_str(), responseAudio.length()) == 0) {
                    M5.Speaker.begin();
                    M5.Speaker.playRaw((const uint8_t*)audioBytes, actualLen, 22050, false, 1, 0);
                }
                free(audioBytes);
            }
        }
    } else {
        lastReply = "Server Error / Unreachable";
    }
    http.end();
    drawChatUI();
}

void setup() {
    auto cfg = M5.config();
    M5.begin(cfg);
    loadSettings();
    connectWiFi();
    drawChatUI();
}

void loop() {
    M5.update();

    if (currentState == STATE_PICTURE) {
        if (M5.Touch.getCount() > 0) {
            auto touch = M5.Touch.getDetail(0);
            if (touch.wasPressed()) touchStartX = touch.x;
            if (touch.wasReleased()) {
                touchEndX = touch.x;
                int diff = touchEndX - touchStartX;
                if (diff < -50) { // Swipe Left -> Delete
                    if (currentImageBuffer) { free(currentImageBuffer); currentImageBuffer = nullptr; }
                    currentState = STATE_CHAT;
                    drawChatUI();
                } else if (diff > 50) { // Swipe Right -> Save
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

            if (currentState == STATE_CHAT) {
                if (x >= 270 && x <= 310 && y >= 5 && y <= 30) {
                    currentState = STATE_SETTINGS;
                    drawSettingsUI();
                } else if (x >= 10 && x <= 150 && y >= 205 && y <= 240) {
                    sendPayloadToServer("Hello from MantisKAI Core2!");
                } else if (x >= 170 && x <= 310 && y >= 205 && y <= 240) {
                    // record voice...
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
