#include <M5Unified.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <mbedtls/base64.h>
#include <Preferences.h>

Preferences preferences;

// ==========================================
// CONFIGURATION & EDITABLE SETTINGS
// ==========================================
const char* WIFI_SSID = "YOUR_WIFI_SSID";
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";
const char* PYTHON_SERVER_URL = "http://192.168.1.157:5000/api/chat"; // Update with your PC's IP
// ==========================================

// Runtime working strings (loaded from preferences or fallbacks above)
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

enum AppState { STATE_CHAT, STATE_SETTINGS, STATE_KEYBOARD };
AppState currentState = STATE_CHAT;

String lastReply = "Ready. Tap screen to chat or record.";
bool isConnected = false;
bool isRecording = false;

// Keyboard editing state
String* activeEditingField = nullptr;
String keyboardBuffer = "";
String keyboardTitle = "";

// Audio Recording Buffers
#define RECORD_TIME_SEC   4
#define SAMPLE_RATE       16000
int16_t* micBuffer = nullptr;
size_t bufferSize = 0;

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

    // Settings Gear Icon / Button Top Right
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

    // Fields
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

    // Header showing what is being edited
    M5.Display.fillRect(0, 0, 320, 30, COLOR_TEAL);
    M5.Display.setTextColor(COLOR_WHITE, COLOR_TEAL);
    M5.Display.setTextSize(1);
    M5.Display.setCursor(10, 10);
    M5.Display.print("Editing: " + keyboardTitle);

    // Input preview box
    M5.Display.drawRect(10, 35, 300, 30, COLOR_PURPLE);
    M5.Display.setTextColor(COLOR_GREEN, COLOR_BG);
    M5.Display.setTextSize(2);
    M5.Display.setCursor(15, 42);
    M5.Display.print(keyboardBuffer);

    // Draw QWERTY Keyboard Grid rows
    const char* rows[] = {
        "1234567890",
        "qwertyuiop",
        "asdfghjkl_",
        "zxcvbnm<OK"
    };

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
            M5.Display.setTextSize(2);
            M5.Display.setCursor(x + 6, y + 8);
            M5.Display.print(rowStr[c]);
        }
    }
}

void handleKeyboardTouch(int touchX, int touchY) {
    const char* rows[] = {
        "1234567890",
        "qwertyuiop",
        "asdfghjkl_",
        "zxcvbnm<OK"
    };

    int startY = 75;
    for (int r = 0; r < 4; r++) {
        String rowStr = rows[r];
        int len = rowStr.length();
        int btnW = 300 / len;
        for (int c = 0; c < len; c++) {
            int x = 10 + (c * btnW);
            int y = startY + (r * 35);

            if (touchX >= x && touchX <= x + btnW && touchY >= y && touchY <= y + 34) {
                char ch = rowStr[c];
                if (ch == '<') { // Backspace
                    if (keyboardBuffer.length() > 0) {
                        keyboardBuffer.remove(keyboardBuffer.length() - 1);
                    }
                } else if (ch == 'O' && rowStr[c+1] == 'K') { // OK / Done
                    *activeEditingField = keyboardBuffer;
                    currentState = STATE_SETTINGS;
                    drawSettingsUI();
                    return;
                } else {
                    keyboardBuffer += ch;
                }
                drawKeyboardUI();
                return;
            }
        }
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
            // First pass to get decoded length using mbedtls
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

void recordAndSendVoice() {
    isRecording = true;
    drawChatUI();

    M5.Mic.begin();
    bufferSize = SAMPLE_RATE * RECORD_TIME_SEC;
    micBuffer = (int16_t*)malloc(bufferSize * sizeof(int16_t));

    if (micBuffer) {
        M5.Mic.record(micBuffer, bufferSize, SAMPLE_RATE);
        uint32_t start = millis();
        while (millis() - start < (RECORD_TIME_SEC * 1000)) {
            M5.update();
            delay(10);
        }
        M5.Mic.end();
    }

    isRecording = false;
    lastReply = "Processing voice...";
    drawChatUI();

    if (micBuffer) {
        // Encode mic PCM buffer using mbedtls base64
        size_t b64Len = 0;
        mbedtls_base64_encode(NULL, 0, &b64Len, (const unsigned char*)micBuffer, bufferSize * sizeof(int16_t));
        
        char* b64Output = (char*)malloc(b64Len + 1);
        size_t actualB64Len = 0;
        if (mbedtls_base64_encode((unsigned char*)b64Output, b64Len, &actualB64Len, (const unsigned char*)micBuffer, bufferSize * sizeof(int16_t)) == 0) {
            b64Output[actualB64Len] = '\0';
            String audioB64 = String(b64Output);
            free(b64Output);
            free(micBuffer);
            micBuffer = nullptr;
            
            sendPayloadToServer("", audioB64);
            return;
        }
        free(b64Output);
        free(micBuffer);
        micBuffer = nullptr;
    }
    lastReply = "Mic buffer failed";
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

    if (M5.Touch.getCount() > 0) {
        auto touch = M5.Touch.getDetail(0);
        if (touch.wasPressed()) {
            int x = touch.x;
            int y = touch.y;

            if (currentState == STATE_CHAT) {
                // Settings button (Top Right)
                if (x >= 270 && x <= 310 && y >= 5 && y <= 30) {
                    currentState = STATE_SETTINGS;
                    drawSettingsUI();
                }
                // Send Text Button
                else if (x >= 10 && x <= 150 && y >= 205 && y <= 240) {
                    sendPayloadToServer("Hello from MantisKAI Core2!");
                }
                // Record Voice Button
                else if (x >= 170 && x <= 310 && y >= 205 && y <= 240) {
                    recordAndSendVoice();
                }
            } 
            else if (currentState == STATE_SETTINGS) {
                // SSID Field
                if (y >= 60 && y <= 85) {
                    activeEditingField = &wifiSsid;
                    keyboardTitle = "Wi-Fi SSID";
                    keyboardBuffer = wifiSsid;
                    currentState = STATE_KEYBOARD;
                    drawKeyboardUI();
                }
                // Password Field
                else if (y >= 110 && y <= 135) {
                    activeEditingField = &wifiPassword;
                    keyboardTitle = "Wi-Fi Password";
                    keyboardBuffer = wifiPassword;
                    currentState = STATE_KEYBOARD;
                    drawKeyboardUI();
                }
                // Server URL Field
                else if (y >= 160 && y <= 185) {
                    activeEditingField = &serverUrl;
                    keyboardTitle = "PC Server URL";
                    keyboardBuffer = serverUrl;
                    currentState = STATE_KEYBOARD;
                    drawKeyboardUI();
                }
                // Back Button
                else if (x >= 10 && x <= 155 && y >= 200 && y <= 235) {
                    currentState = STATE_CHAT;
                    drawChatUI();
                }
                // Save & Connect Button
                else if (x >= 165 && x <= 310 && y >= 200 && y <= 235) {
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
