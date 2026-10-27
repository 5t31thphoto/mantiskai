#include <M5Unified.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <mbedtls/base64.h>
#include <Preferences.h>
#include <SD.h>
#include <vector>
#include <string.h>
#include "sprites.h"
#include "reel_sprites.h"

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

enum AppState {
    STATE_CHAT, STATE_IMAGE, STATE_SETTINGS, STATE_KEYBOARD, STATE_PICTURE,
    STATE_SLOTS, STATE_SLOTS_REWARD, STATE_TROPHY
};
AppState currentState = STATE_CHAT;
AppState stateBeforeSettings = STATE_CHAT;

String lastReply = "Ready. Type/MIC to chat, or press A for Image mode.";
bool isConnected = false;
bool isBusy      = false;
bool isRecording = false;

String modelMode  = "realistic";
String activeLora = "None";

// Last prompt used for image generation (needed for regenerate)
String lastImagePrompt = "";

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

// Long-press regenerate confirm on picture
unsigned long picturePressStart = 0;
bool pictureLongDone = false;
bool showingRegenConfirm = false;
bool regenConfirmSkipRelease = false;  // ignore the release that ended the long-press

unsigned long btnBPressStart = 0;
bool btnBLongDone = false;
unsigned long btnCPressStart = 0;
bool btnCLongDone = false;

// Busy-wait sprite animation (bottom-right)
enum SpriteAnim { SPRITE_NONE = 0, SPRITE_ANIM_THINK, SPRITE_ANIM_PAINT };
SpriteAnim activeSprite = SPRITE_NONE;
int spriteFrame = 0;
unsigned long spriteLastMs = 0;
const unsigned long SPRITE_FRAME_MS = 180;

// ---------- Slots economy + reels (art: lemon, 7, cherry, girl, jackpot) ----------
enum SlotSym { SYM_LEMON = 0, SYM_SEVEN, SYM_CHERRY, SYM_GIRL, SYM_JACKPOT, SYM_COUNT };
static const char* SYM_NAME[] = { "LEMON", "SEVEN", "CHERRY", "GIRL", "JACKPOT" };
static const int SYM_PAIR_MULT[]   = { 2, 4, 5, 10, 20 };
static const int SYM_TRIPLE_MULT[] = { 10, 25, 40, 80, 200 };
static const int SYM_WEIGHT[]      = { 28, 24, 22, 16, 10 };  // JACKPOT rarest

int slotCredits = 100;
int slotBet = 5;
const int SLOT_BETS[] = { 1, 5, 10, 25 };
int slotBetIdx = 1;
int reelSym[3] = { 0, 0, 0 };
int reelPos[3] = { 0, 0, 0 };
bool slotSpinning = false;
bool slotCelebrating = false;
int slotCelebFrame = 0;
String slotStatus = "Long-press C anytime. Spin to play!";
String slotLastPrompt = "";
bool slotRewardFromSlots = false;
bool slotGameOverPending = false;
int trophyIndex = 0;
int trophyCount = 0;
static const int TROPHY_MAX = 64;
String trophyFiles[TROPHY_MAX];

// Word lists — defaults in flash; overridden by /slots_words.json on SD
static const int WL_MAX = 24;
static const int WL_LEN = 48;
char wl_descriptive[WL_MAX][WL_LEN];
char wl_subject[WL_MAX][WL_LEN];
char wl_general[WL_MAX][WL_LEN];
char wl_setting[WL_MAX][WL_LEN];
char wl_action[WL_MAX][WL_LEN];
char wl_style_mod[WL_MAX][WL_LEN];
char wl_style[WL_MAX][WL_LEN];
char wl_quality[WL_MAX][WL_LEN];
char wl_jackpot_pose[WL_MAX][WL_LEN];
char wl_gameover[WL_MAX][WL_LEN];
int n_descriptive = 0, n_subject = 0, n_general = 0, n_setting = 0;
int n_action = 0, n_style_mod = 0, n_style = 0, n_quality = 0;
int n_jackpot_pose = 0, n_gameover = 0;

static void wlCopy(char dest[][WL_LEN], int& n, const char* const* src, int count) {
    n = 0;
    for (int i = 0; i < count && n < WL_MAX; i++) {
        strncpy(dest[n], src[i], WL_LEN - 1);
        dest[n][WL_LEN - 1] = 0;
        n++;
    }
}

void initDefaultWordLists() {
    static const char* d[] = { "cute", "adorable", "tiny", "elegant", "cheerful", "mysterious", "brave", "sparkly" };
    static const char* s[] = { "mantis girl", "anime girl", "fox girl", "robot maid", "witch apprentice", "space pilot girl", "cat girl", "dragon girl" };
    static const char* g[] = { "with big expressive eyes", "in a frilly outfit", "wearing a hoodie", "with glowing accessories", "soft pastel hair" };
    static const char* se[] = { "in a sunny meadow", "on a spaceship bridge", "by a quiet lake", "in a neon city alley", "in an enchanted forest", "at a seaside dock" };
    static const char* a[] = { "fishing with a bamboo rod", "dancing joyfully", "piloting a starfighter", "reading a spellbook", "sipping tea", "riding a bicycle", "painting on a canvas" };
    static const char* sm[] = { "soft shading", "cel shading", "watercolor edges", "clean lineart", "glowing highlights" };
    static const char* st[] = { "anime illustration", "pixiv trending style", "studio ghibli inspired", "modern anime key visual", "chibi style" };
    static const char* q[] = { "Pixel perfect, high levels of vivid detail", "masterpiece, best quality, ultra detailed", "sharp focus, rich colors" };
    static const char* jp[] = { "celebrating with confetti", "victory pose arms raised", "blowing a kiss", "spinning happily", "holding a golden trophy" };
    static const char* go[] = { "sitting alone looking sad", "empty wallet tears", "rainy window wistful", "hugging knees on the floor" };
    wlCopy(wl_descriptive, n_descriptive, d, 8);
    wlCopy(wl_subject, n_subject, s, 8);
    wlCopy(wl_general, n_general, g, 5);
    wlCopy(wl_setting, n_setting, se, 6);
    wlCopy(wl_action, n_action, a, 7);
    wlCopy(wl_style_mod, n_style_mod, sm, 5);
    wlCopy(wl_style, n_style, st, 5);
    wlCopy(wl_quality, n_quality, q, 3);
    wlCopy(wl_jackpot_pose, n_jackpot_pose, jp, 5);
    wlCopy(wl_gameover, n_gameover, go, 4);
}

static const char* pickWL(char list[][WL_LEN], int n) {
    if (n <= 0) return "cute character";
    return list[random(n)];
}

void loadWordListsFromSD() {
    initDefaultWordLists();
    if (!SD.begin(4) || !SD.exists("/slots_words.json")) return;
    File f = SD.open("/slots_words.json", FILE_READ);
    if (!f) return;
    JsonDocument doc;
    if (deserializeJson(doc, f)) { f.close(); return; }
    f.close();
    auto loadArr = [&](const char* key, char dest[][WL_LEN], int& n) {
        if (!doc[key].is<JsonArray>()) return;
        n = 0;
        for (JsonVariant v : doc[key].as<JsonArray>()) {
            if (n >= WL_MAX) break;
            const char* s = v.as<const char*>();
            if (!s) continue;
            strncpy(dest[n], s, WL_LEN - 1);
            dest[n][WL_LEN - 1] = 0;
            n++;
        }
    };
    loadArr("descriptive", wl_descriptive, n_descriptive);
    loadArr("subject", wl_subject, n_subject);
    loadArr("general", wl_general, n_general);
    loadArr("setting", wl_setting, n_setting);
    loadArr("action", wl_action, n_action);
    loadArr("style_mod", wl_style_mod, n_style_mod);
    loadArr("style", wl_style, n_style);
    loadArr("quality", wl_quality, n_quality);
    loadArr("jackpot_pose", wl_jackpot_pose, n_jackpot_pose);
    loadArr("gameover", wl_gameover, n_gameover);
    Serial.printf("[SLOTS] wordlists loaded subj=%d action=%d\n", n_subject, n_action);
}

void drawChatUI();
void drawImageUI();
void drawSettingsUI();
void drawKeyboardUI();
void drawPictureUI();
void drawBusySprite();
void drawSlotsUI();
void drawTrophyUI();
void drawSlotsRewardUI();
void connectWiFi();
void sendChat(const String& msg);
void fetchAndPlayTTS(const String& text);
void requestImage(const String& prompt);
void cycleLora();
void clearLora();
void startRecording(bool forImage);
void loadSlotCredits();
void saveSlotCredits();
void spinSlots();
void evaluateSlots();
String buildSlotPrompt(int sym);
void saveImageToSlots();
void scanTrophyRoom();
int pickWeightedSym();
void drawReelSymbol(int x, int y, int w, int h, int sym, int animFrame = 0);
String buildGameOverPrompt();

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
    slotCredits  = preferences.getInt("credits", 100);
    slotBetIdx   = preferences.getInt("betIdx", 1);
    if (slotBetIdx < 0 || slotBetIdx > 3) slotBetIdx = 1;
    slotBet = SLOT_BETS[slotBetIdx];
    preferences.end();
    if (serverBase.endsWith("/")) serverBase.remove(serverBase.length() - 1);
}

void saveSettings() {
    preferences.begin("mantiskai", false);
    preferences.putString("ssid", wifiSsid);
    preferences.putString("pass", wifiPassword);
    preferences.putString("server", serverBase);
    preferences.putInt("credits", slotCredits);
    preferences.putInt("betIdx", slotBetIdx);
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

// ---------- Slots helpers ----------
int pickWeightedSym() {
    int total = 0;
    for (int i = 0; i < SYM_COUNT; i++) total += SYM_WEIGHT[i];
    int r = random(total);
    for (int i = 0; i < SYM_COUNT; i++) {
        r -= SYM_WEIGHT[i];
        if (r < 0) return i;
    }
    return 0;
}

void drawReelSymbol(int x, int y, int w, int h, int sym, int animFrame) {
    if (sym < 0) sym = 0;
    if (sym >= REEL_SYMS) sym = REEL_SYMS - 1;
    if (animFrame < 0) animFrame = 0;
    if (animFrame >= REEL_ANIMS) animFrame = animFrame % REEL_ANIMS;

    M5.Display.fillRoundRect(x, y, w, h, 4, COLOR_DARK);
    M5.Display.drawRoundRect(x, y, w, h, 4, COLOR_TEAL);

    // Center the 48x48 sprite in the reel window
    int ox = x + (w - REEL_W) / 2;
    int oy = y + (h - REEL_H) / 2;
    if (ox < x) ox = x;
    if (oy < y) oy = y;
    const uint16_t* src = REEL_FRAMES[sym][animFrame];
    static uint16_t line[REEL_W];
    M5.Display.setSwapBytes(true);
    for (int row = 0; row < REEL_H; row++) {
        if (oy + row < y || oy + row >= y + h) continue;
        for (int col = 0; col < REEL_W; col++) {
            uint16_t c = pgm_read_word(&src[row * REEL_W + col]);
            line[col] = (c == REEL_TRANSPARENT) ? COLOR_DARK : c;
        }
        M5.Display.pushImage(ox, oy + row, REEL_W, 1, line);
    }
    M5.Display.setSwapBytes(false);
}

// Recipe format:
//   {quality}, full body view of ({descriptive}) ({subject}) ({general}) ({setting}) ({action}) ({style_mod}) ({style})
// Jackpot swaps action for jackpot_pose; game-over uses sad recipe.
String buildSlotPrompt(int sym) {
    const char* quality = pickWL(wl_quality, n_quality);
    const char* desc = pickWL(wl_descriptive, n_descriptive);
    const char* subj = pickWL(wl_subject, n_subject);
    const char* gen = pickWL(wl_general, n_general);
    const char* set = pickWL(wl_setting, n_setting);
    const char* act = (sym >= SYM_GIRL)
        ? pickWL(wl_jackpot_pose, n_jackpot_pose)
        : pickWL(wl_action, n_action);
    const char* smod = pickWL(wl_style_mod, n_style_mod);
    const char* sty = pickWL(wl_style, n_style);

    // Per-symbol recipe spice (hardcoded fragments, slots still randomize lists)
    String extra = "";
    if (sym == SYM_LEMON) extra = ", bright cheerful colors";
    else if (sym == SYM_SEVEN) extra = ", lucky golden accents";
    else if (sym == SYM_CHERRY) extra = ", sweet playful mood";
    else if (sym == SYM_GIRL) extra = ", cute mantis girl energy, celebratory";
    else if (sym == SYM_JACKPOT) extra = ", legendary win moment, sparkling effects";

    String p = String(quality) + ", full body view of " + desc + " " + subj +
               " " + gen + " " + set + " " + act + " " + smod + " " + sty + extra;
    return p;
}

String buildGameOverPrompt() {
    const char* quality = pickWL(wl_quality, n_quality);
    const char* desc = pickWL(wl_descriptive, n_descriptive);
    const char* subj = pickWL(wl_subject, n_subject);
    const char* go = pickWL(wl_gameover, n_gameover);
    const char* sty = pickWL(wl_style, n_style);
    return String(quality) + ", full body view of " + desc + " " + subj +
           " " + go + ", rainy melancholy atmosphere, " + sty;
}

void loadSlotCredits() {
    preferences.begin("mantiskai", true);
    slotCredits = preferences.getInt("credits", slotCredits);
    preferences.end();
}

void saveSlotCredits() {
    preferences.begin("mantiskai", false);
    preferences.putInt("credits", slotCredits);
    preferences.putInt("betIdx", slotBetIdx);
    preferences.end();
}

void drawSlotsUI() {
    M5.Display.fillScreen(COLOR_BG);
    M5.Display.fillRect(0, 0, 320, 28, 0x580A);
    M5.Display.setTextColor(COLOR_WHITE, 0x580A);
    M5.Display.setTextSize(1);
    M5.Display.setCursor(8, 8);
    M5.Display.printf("SLOTS  CR:%d  BET:%d", slotCredits, slotBet);

    // Three reels
    const int rw = 90, rh = 70, gap = 8;
    const int rx0 = (320 - (rw * 3 + gap * 2)) / 2;
    const int ry = 40;
    for (int i = 0; i < 3; i++) {
        int x = rx0 + i * (rw + gap);
        M5.Display.drawRoundRect(x - 2, ry - 2, rw + 4, rh + 4, 6, COLOR_TEAL);
        int show = slotSpinning ? ((reelPos[i] / 8) % SYM_COUNT) : reelSym[i];
        drawReelSymbol(x, ry, rw, rh, show);
    }

    // Status
    M5.Display.setTextColor(COLOR_GREEN, COLOR_BG);
    M5.Display.setTextSize(1);
    M5.Display.setCursor(8, 120);
    String st = slotStatus;
    if (st.length() > 50) st = st.substring(0, 47) + "...";
    M5.Display.print(st);

    // Buttons
    M5.Display.fillRoundRect(8, 150, 70, 36, 6, COLOR_DARK);
    M5.Display.setTextColor(COLOR_WHITE, COLOR_DARK);
    M5.Display.setCursor(22, 162); M5.Display.print("BET-");

    M5.Display.fillRoundRect(86, 150, 70, 36, 6, COLOR_DARK);
    M5.Display.setCursor(100, 162); M5.Display.print("BET+");

    M5.Display.fillRoundRect(164, 150, 70, 36, 6, COLOR_TEAL);
    M5.Display.setTextColor(COLOR_WHITE, COLOR_TEAL);
    M5.Display.setCursor(180, 162); M5.Display.print("SPIN");

    M5.Display.fillRoundRect(242, 150, 70, 36, 6, 0xFDA0);
    M5.Display.setTextColor(0, 0xFDA0);
    M5.Display.setCursor(250, 162); M5.Display.print("TROPHY");

    M5.Display.fillRoundRect(8, 198, 148, 36, 6, COLOR_PURPLE);
    M5.Display.setTextColor(COLOR_WHITE, COLOR_PURPLE);
    M5.Display.setTextSize(2);
    M5.Display.setCursor(40, 208); M5.Display.print("BACK");

    M5.Display.fillRoundRect(164, 198, 148, 36, 6, COLOR_DARK);
    M5.Display.setTextColor(COLOR_WHITE, COLOR_DARK);
    M5.Display.setTextSize(1);
    M5.Display.setCursor(190, 212); M5.Display.print("Paytable: pair/triple");
}

void celebrateWin(int sym) {
    // Loop reel animation frames for the winning symbol on all three reels
    slotCelebrating = true;
    const int rw = 90, rh = 70, gap = 8;
    const int rx0 = (320 - (rw * 3 + gap * 2)) / 2;
    const int ry = 40;
    for (int loop = 0; loop < 3; loop++) {
        for (int f = 0; f < REEL_ANIMS; f++) {
            for (int i = 0; i < 3; i++) {
                int x = rx0 + i * (rw + gap);
                drawReelSymbol(x, ry, rw, rh, sym, f);
            }
            M5.update();
            delay(60);
        }
    }
    slotCelebrating = false;
}

void evaluateSlots() {
    int a = reelSym[0], b = reelSym[1], c = reelSym[2];
    if (a == b && b == c) {
        int win = slotBet * SYM_TRIPLE_MULT[a];
        slotCredits += win;
        saveSlotCredits();
        slotStatus = String("TRIPLE ") + SYM_NAME[a] + "! +" + String(win);
        drawSlotsUI();
        celebrateWin(a);
        slotStatus = String("TRIPLE ") + SYM_NAME[a] + "! generating art...";
        drawSlotsUI();
        slotLastPrompt = buildSlotPrompt(a);
        lastImagePrompt = slotLastPrompt;
        slotRewardFromSlots = true;
        requestImage(slotLastPrompt);
        if (currentState != STATE_PICTURE) {
            slotRewardFromSlots = false;
            slotStatus = "Generation failed. Credits kept.";
            currentState = STATE_SLOTS;
            drawSlotsUI();
        }
        return;
    }
    if (a == b || b == c || a == c) {
        int sym = (a == b) ? a : c;
        int win = slotBet * SYM_PAIR_MULT[sym];
        slotCredits += win;
        saveSlotCredits();
        slotStatus = String("Pair ") + SYM_NAME[sym] + "! +" + String(win) + " credits";
        // short celebrate on the matched symbol
        const int rw = 90, rh = 70, gap = 8;
        const int rx0 = (320 - (rw * 3 + gap * 2)) / 2;
        for (int f = 0; f < REEL_ANIMS; f++) {
            for (int i = 0; i < 3; i++) {
                int x = rx0 + i * (rw + gap);
                int show = (reelSym[i] == sym) ? sym : reelSym[i];
                int af = (reelSym[i] == sym) ? f : 0;
                drawReelSymbol(x, 40, rw, rh, show, af);
            }
            delay(40);
        }
    } else {
        slotStatus = "No match. Try again!";
    }
    // Broke → game over art then soft reset credits
    if (slotCredits < SLOT_BETS[0]) {
        slotStatus = "GAME OVER — out of credits...";
        drawSlotsUI();
        slotLastPrompt = buildGameOverPrompt();
        lastImagePrompt = slotLastPrompt;
        slotRewardFromSlots = true;
        slotGameOverPending = true;
        requestImage(slotLastPrompt);
        if (currentState != STATE_PICTURE) {
            slotRewardFromSlots = false;
            slotGameOverPending = false;
            slotCredits = 100;
            saveSlotCredits();
            slotStatus = "Credits refreshed to 100. Spin again!";
            currentState = STATE_SLOTS;
            drawSlotsUI();
        }
        return;
    }
    drawSlotsUI();
}

void spinSlots() {
    if (slotSpinning || isBusy || slotCelebrating) return;
    if (slotCredits < slotBet) {
        slotStatus = "Not enough credits!";
        drawSlotsUI();
        return;
    }
    slotCredits -= slotBet;
    saveSlotCredits();
    // Decide finals first (RNG before animation) — strip scrolls through symbols
    for (int i = 0; i < 3; i++) reelSym[i] = pickWeightedSym();
    slotSpinning = true;
    slotStatus = "Spinning...";
    drawSlotsUI();

    // Scroll symbols upward (cycle SYM indices) then staggered stop
    for (int frame = 0; frame < 40; frame++) {
        for (int i = 0; i < 3; i++) {
            int stopAt = 20 + i * 6;
            if (frame < stopAt) reelPos[i] = (reelPos[i] + 1 + i) % REEL_SYMS;
        }
        const int rw = 90, rh = 70, gap = 8;
        const int rx0 = (320 - (rw * 3 + gap * 2)) / 2;
        const int ry = 40;
        for (int i = 0; i < 3; i++) {
            int x = rx0 + i * (rw + gap);
            int show = (frame < 20 + i * 6) ? reelPos[i] : reelSym[i];
            drawReelSymbol(x, ry, rw, rh, show, 0);
        }
        M5.update();
        delay(30);
    }
    slotSpinning = false;
    evaluateSlots();
}

void scanTrophyRoom() {
    trophyCount = 0;
    trophyIndex = 0;
    if (!SD.begin(4)) return;
    if (!SD.exists("/images/slots")) SD.mkdir("/images/slots");
    File dir = SD.open("/images/slots");
    if (!dir || !dir.isDirectory()) return;
    File f = dir.openNextFile();
    while (f && trophyCount < TROPHY_MAX) {
        String n = f.name();
        if (n.endsWith(".raw") || n.endsWith(".RAW")) {
            // store full path
            if (!n.startsWith("/")) n = String("/images/slots/") + n;
            // some SD libs return full path already
            if (n.indexOf("slots") < 0) n = String("/images/slots/") + n;
            trophyFiles[trophyCount++] = n;
        }
        f = dir.openNextFile();
    }
    dir.close();
}

void saveImageToSlots() {
    if (!currentImageBuffer || !SD.begin(4)) return;
    if (!SD.exists("/images")) SD.mkdir("/images");
    if (!SD.exists("/images/slots")) SD.mkdir("/images/slots");
    String fn = "/images/slots/slot_" + String(millis()) + ".raw";
    File f = SD.open(fn.c_str(), FILE_WRITE);
    if (!f) return;
    f.write(currentImageBuffer, 320UL * 240UL * 2UL);
    f.close();
    slotStatus = "Bought! Saved to trophy room.";
}

void drawTrophyUI() {
    M5.Display.fillScreen(COLOR_BG);
    M5.Display.fillRect(0, 0, 320, 28, 0xFDA0);
    M5.Display.setTextColor(0, 0xFDA0);
    M5.Display.setTextSize(1);
    M5.Display.setCursor(8, 8);
    M5.Display.printf("TROPHY ROOM  %d/%d", trophyCount ? trophyIndex + 1 : 0, trophyCount);

    if (trophyCount == 0) {
        M5.Display.setTextColor(COLOR_WHITE, COLOR_BG);
        M5.Display.setCursor(40, 100);
        M5.Display.print("No trophies yet. Hit jackpots!");
    } else {
        // Try load raw RGB565
        String path = trophyFiles[trophyIndex];
        File f = SD.open(path.c_str(), FILE_READ);
        if (f) {
            size_t need = 320UL * 240UL * 2UL;
            uint8_t* buf = (uint8_t*)malloc(need);
            if (buf) {
                size_t got = f.read(buf, need);
                f.close();
                if (got >= need) {
                    M5.Display.setSwapBytes(true);
                    M5.Display.pushImage(0, 28, 320, 180, (uint16_t*)buf);  // letterbox-ish
                    M5.Display.setSwapBytes(false);
                }
                free(buf);
            } else f.close();
        }
    }

    M5.Display.fillRect(0, 210, 320, 30, COLOR_DARK);
    M5.Display.setTextColor(COLOR_WHITE, COLOR_DARK);
    M5.Display.setCursor(10, 220);
    M5.Display.print("< Prev    BACK=exit    Next >");
}

void drawBusySprite() {
    // Fills the entire reply/prompt content box (not a corner pin).
    if (activeSprite == SPRITE_NONE) return;

    const int nframes = (activeSprite == SPRITE_ANIM_PAINT)
        ? SPRITE_PAINT_FRAMES : SPRITE_THINK_FRAMES;

    unsigned long now = millis();
    if (now - spriteLastMs >= SPRITE_FRAME_MS) {
        spriteLastMs = now;
        spriteFrame = (spriteFrame + 1) % nframes;
    }

    const uint16_t* src = (activeSprite == SPRITE_ANIM_PAINT)
        ? SPRITE_PAINT[spriteFrame]
        : SPRITE_THINK[spriteFrame];

    // Match the reply box interior: chat uses tall box, image slightly shorter
    const int boxX = 10;
    const int boxY = 48;
    const int boxW = 300;
    const int boxH = (currentState == STATE_IMAGE) ? 100 : 128;

    static uint16_t line[300];
    M5.Display.setSwapBytes(true);
    for (int dy = 0; dy < boxH; dy++) {
        int sy = dy * SPRITE_H / boxH;
        for (int dx = 0; dx < boxW; dx++) {
            int sx = dx * SPRITE_W / boxW;
            uint16_t c = pgm_read_word(&src[sy * SPRITE_W + sx]);
            line[dx] = (c == SPRITE_TRANSPARENT) ? COLOR_BG : c;
        }
        M5.Display.pushImage(boxX, boxY + dy, boxW, 1, line);
    }
    M5.Display.setSwapBytes(false);
}

void drawChatUI() {
    M5.Display.fillScreen(COLOR_BG);
    drawHeader("MantisKAI Chat");

    M5.Display.fillRoundRect(255, 4, 30, 24, 4, COLOR_PURPLE);
    M5.Display.setTextSize(1);
    M5.Display.setCursor(261, 10);
    M5.Display.print("CFG");

    // Reply area — taller now that model/LoRA bar is image-mode only
    M5.Display.drawRoundRect(8, 40, 304, 140, 6, COLOR_PURPLE);

    if (isBusy) {
        // Full reply-box animation (think cycle) — no text under it
        drawBusySprite();
    } else {
        M5.Display.setTextColor(COLOR_WHITE, COLOR_BG);
        M5.Display.setTextSize(1);
        M5.Display.setCursor(14, 46);
        M5.Display.print("AI Reply:");

        int totalPages = (lastReply.length() + CHARS_PER_PAGE - 1) / CHARS_PER_PAGE;
        if (totalPages < 1) totalPages = 1;
        if (replyPage >= totalPages) replyPage = totalPages - 1;
        if (replyPage < 0) replyPage = 0;
        int start = replyPage * CHARS_PER_PAGE;
        String page = lastReply.substring(start, start + CHARS_PER_PAGE);

        M5.Display.setTextColor(COLOR_GREEN, COLOR_BG);
        int y = 62;
        while (page.length() && y < 170) {
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
            M5.Display.setCursor(200, 170);
            M5.Display.printf("<%d/%d>", replyPage + 1, totalPages);
        }
    }

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

void drawImageUI() {
    M5.Display.fillScreen(COLOR_BG);
    drawHeader("MantisKAI Image");

    M5.Display.fillRoundRect(255, 4, 30, 24, 4, COLOR_PURPLE);
    M5.Display.setTextSize(1);
    M5.Display.setCursor(261, 10);
    M5.Display.print("CFG");

    M5.Display.drawRoundRect(8, 40, 304, 110, 6, COLOR_PURPLE);

    if (isRecording || (isBusy && (lastReply == "Transcribing..." || lastReply == "Recording..."))) {
        M5.Display.fillRoundRect(12, 44, 296, 20, 4, COLOR_TEAL);
        M5.Display.setTextColor(COLOR_WHITE, COLOR_TEAL);
        M5.Display.setTextSize(1);
        M5.Display.setCursor(18, 50);
        M5.Display.print(isRecording ? "RECORDING AUDIO..." : "TRANSCRIBING AUDIO...");
    }

    if (isBusy) {
        // Full prompt-box paint animation
        drawBusySprite();
    } else {
        M5.Display.setTextColor(COLOR_WHITE, COLOR_BG);
        M5.Display.setTextSize(1);
        M5.Display.setCursor(14, 46);
        M5.Display.print("Image Prompt:");

        M5.Display.setTextColor(COLOR_GREEN, COLOR_BG);
        int y = 62;
        String page = lastImagePrompt.length() ? lastImagePrompt : "Type or record a prompt.";
        while (page.length() && y < 140) {
            String line = page.substring(0, 38);
            int sp = line.lastIndexOf(' ');
            if (sp > 20 && page.length() > 38) { line = page.substring(0, sp); page = page.substring(sp + 1); }
            else page = page.substring(line.length());
            M5.Display.setCursor(14, y);
            M5.Display.print(line);
            y += 14;
        }
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

    if (slotRewardFromSlots) {
        // Jackpot reward: BUY (credits → /images/slots) or DELETE
        M5.Display.fillRect(0, 0, 320, 36, 0x0000);
        M5.Display.setTextColor(COLOR_WHITE, 0x0000);
        M5.Display.setTextSize(1);
        M5.Display.setCursor(8, 12);
        M5.Display.printf("JACKPOT art!  Buy=20cr (you have %d)", slotCredits);

        M5.Display.fillRoundRect(20, 170, 130, 50, 8, COLOR_GREEN);
        M5.Display.setTextColor(0, COLOR_GREEN);
        M5.Display.setTextSize(2);
        M5.Display.setCursor(50, 186);
        M5.Display.print("BUY");

        M5.Display.fillRoundRect(170, 170, 130, 50, 8, COLOR_RED);
        M5.Display.setTextColor(COLOR_WHITE, COLOR_RED);
        M5.Display.setCursor(185, 186);
        M5.Display.print("DELETE");
    } else if (showingRegenConfirm) {
        // Dim overlay strip + two big action buttons
        M5.Display.fillRect(0, 0, 320, 50, 0x0000);
        M5.Display.setTextColor(COLOR_WHITE, 0x0000);
        M5.Display.setTextSize(1);
        M5.Display.setCursor(40, 18);
        M5.Display.print("Regenerate with same prompt?");

        // Green = Save then regen
        M5.Display.fillRoundRect(20, 80, 130, 80, 8, COLOR_GREEN);
        M5.Display.setTextColor(0, COLOR_GREEN);
        M5.Display.setTextSize(2);
        M5.Display.setCursor(40, 100);
        M5.Display.print("SAVE");
        M5.Display.setCursor(28, 122);
        M5.Display.print("& REGEN");

        // Red = Discard then regen
        M5.Display.fillRoundRect(170, 80, 130, 80, 8, COLOR_RED);
        M5.Display.setTextColor(COLOR_WHITE, COLOR_RED);
        M5.Display.setCursor(188, 100);
        M5.Display.print("DELETE");
        M5.Display.setCursor(178, 122);
        M5.Display.print("& REGEN");

        // Cancel hint
        M5.Display.fillRect(0, 210, 320, 30, 0x0000);
        M5.Display.setTextColor(COLOR_WHITE, 0x0000);
        M5.Display.setTextSize(1);
        M5.Display.setCursor(70, 220);
        M5.Display.print("Tap elsewhere to cancel");
    } else {
        // Semi-transparent style hint bar (solid black strip)
        M5.Display.fillRect(0, 210, 320, 30, 0x0000);
        M5.Display.setTextColor(COLOR_WHITE, 0x0000);
        M5.Display.setTextSize(1);
        M5.Display.setCursor(8, 220);
        M5.Display.print("< Del   Hold 2s=Regen   Save >");
    }
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
                if (img) {
                    currentState = STATE_IMAGE;
                    drawImageUI();
                    if (msg.length()) requestImage(msg);
                } else {
                    currentState = STATE_CHAT;
                    drawChatUI();
                    if (msg.length()) sendChat(msg);
                }
            }
        }
    }
}

// ---------- network (non-blocking wait so sprites can animate) ----------
bool parseServerUrl(String& host, uint16_t& port) {
    // Expect "http://host:port" or "http://host"
    host = "";
    port = 80;
    String s = serverBase;
    if (s.startsWith("http://")) s = s.substring(7);
    else if (s.startsWith("https://")) s = s.substring(8);
    int slash = s.indexOf('/');
    if (slash >= 0) s = s.substring(0, slash);
    int colon = s.indexOf(':');
    if (colon >= 0) {
        host = s.substring(0, colon);
        port = (uint16_t)s.substring(colon + 1).toInt();
        if (port == 0) port = 80;
    } else {
        host = s;
    }
    return host.length() > 0;
}

void pumpBusy() {
    M5.update();
    if (isBusy && activeSprite != SPRITE_NONE) drawBusySprite();
    delay(20);
}

// POST JSON, wait up to timeoutMs while pumping sprite. Returns HTTP code or <=0 on fail.
// On success, responseBody holds the body text.
int httpPostJsonPump(const char* path, const String& jsonBody, String& responseBody, unsigned long timeoutMs) {
    responseBody = "";
    String host;
    uint16_t port;
    if (!parseServerUrl(host, port)) return -1;

    WiFiClient client;
    client.setTimeout(2000);
    if (!client.connect(host.c_str(), port)) return -1;

    String req;
    req.reserve(jsonBody.length() + 160);
    req += "POST ";
    req += path;
    req += " HTTP/1.0\r\nHost: ";
    req += host;
    req += "\r\nContent-Type: application/json\r\nContent-Length: ";
    req += String(jsonBody.length());
    req += "\r\nConnection: close\r\n\r\n";
    req += jsonBody;
    client.print(req);

    unsigned long deadline = millis() + timeoutMs;
    // Wait for headers
    String headers;
    headers.reserve(512);
    bool gotHeaders = false;
    while (millis() < deadline && !gotHeaders) {
        pumpBusy();
        while (client.available()) {
            char c = (char)client.read();
            headers += c;
            if (headers.length() >= 4 &&
                headers.endsWith("\r\n\r\n")) {
                gotHeaders = true;
                break;
            }
            if (headers.length() > 4096) break;
        }
        if (!client.connected() && !client.available() && !gotHeaders) break;
    }
    if (!gotHeaders) {
        client.stop();
        return -11;  // timeout
    }

    int code = 0;
    int sp = headers.indexOf(' ');
    if (sp > 0) code = headers.substring(sp + 1).toInt();

    // Read body
    responseBody.reserve(2048);
    while (millis() < deadline) {
        pumpBusy();
        int avail = client.available();
        if (avail > 0) {
            while (client.available()) {
                responseBody += (char)client.read();
                if (responseBody.length() > 32000) break;
            }
        } else if (!client.connected()) {
            delay(5);
            if (!client.available()) break;
        }
    }
    client.stop();
    return code > 0 ? code : -11;
}

// POST JSON, stream binary body into buf (up to maxLen) while pumping. Returns bytes read or <0.
int httpPostBinaryPump(const char* path, const String& jsonBody,
                       uint8_t* buf, size_t maxLen, unsigned long timeoutMs,
                       String* outLora = nullptr) {
    String host;
    uint16_t port;
    if (!parseServerUrl(host, port)) return -1;

    WiFiClient client;
    client.setTimeout(2000);
    if (!client.connect(host.c_str(), port)) return -1;

    String req;
    req.reserve(jsonBody.length() + 160);
    req += "POST ";
    req += path;
    req += " HTTP/1.0\r\nHost: ";
    req += host;
    req += "\r\nContent-Type: application/json\r\nContent-Length: ";
    req += String(jsonBody.length());
    req += "\r\nConnection: close\r\n\r\n";
    req += jsonBody;
    client.print(req);

    unsigned long deadline = millis() + timeoutMs;
    String headers;
    headers.reserve(512);
    bool gotHeaders = false;
    while (millis() < deadline && !gotHeaders) {
        pumpBusy();
        while (client.available()) {
            char c = (char)client.read();
            headers += c;
            if (headers.length() >= 4 && headers.endsWith("\r\n\r\n")) {
                gotHeaders = true;
                break;
            }
            if (headers.length() > 4096) break;
        }
        if (!client.connected() && !client.available() && !gotHeaders) break;
    }
    if (!gotHeaders) {
        client.stop();
        return -11;
    }

    int code = 0;
    int sp = headers.indexOf(' ');
    if (sp > 0) code = headers.substring(sp + 1).toInt();

    if (outLora) {
        int p = headers.indexOf("X-Lora:");
        if (p < 0) p = headers.indexOf("x-lora:");
        if (p >= 0) {
            int e = headers.indexOf("\r\n", p);
            if (e > p) {
                String v = headers.substring(p + 7, e);
                v.trim();
                *outLora = v;
            }
        }
    }

    if (code != 200) {
        // Drain any error JSON into a temp (caller may not need it)
        while (millis() < deadline && (client.connected() || client.available())) {
            pumpBusy();
            while (client.available()) client.read();
        }
        client.stop();
        return code > 0 ? -code : -11;  // negative HTTP code = server error
    }

    size_t got = 0;
    while (millis() < deadline && got < maxLen) {
        pumpBusy();
        int avail = client.available();
        if (avail > 0) {
            int n = client.readBytes(buf + got, min((size_t)avail, maxLen - got));
            if (n > 0) got += n;
        } else if (!client.connected()) {
            delay(5);
            if (!client.available()) break;
        }
    }
    client.stop();
    return (int)got;
}

void sendChat(const String& msg) {
    if (isBusy) return;  // never stack GPU/LLM work
    if (WiFi.status() != WL_CONNECTED) {
        isConnected = false;
        lastReply = "Wi-Fi disconnected";
        drawChatUI();
        return;
    }
    isConnected = true;
    isBusy = true;
    activeSprite = SPRITE_ANIM_THINK;
    spriteFrame = 0;
    spriteLastMs = millis();
    drawChatUI();

    JsonDocument body;
    body["message"] = msg;
    String bodyStr;
    serializeJson(body, bodyStr);

    String resp;
    int code = httpPostJsonPump("/api/chat", bodyStr, resp, 120000);

    if (code == 200) {
        JsonDocument doc;
        if (!deserializeJson(doc, resp)) {
            if (doc["lora"].is<const char*>()) activeLora = doc["lora"].as<String>();
            if (doc["reply"].is<const char*>()) {
                lastReply = doc["reply"].as<String>();
                replyPage = 0;
                isBusy = false;
                activeSprite = SPRITE_NONE;
                drawChatUI();
                fetchAndPlayTTS(lastReply);
                return;
            }
            if (doc["error"].is<const char*>()) lastReply = doc["error"].as<String>();
            else lastReply = "No reply";
        } else {
            lastReply = "Bad JSON";
        }
    } else if (code == 503) {
        lastReply = "Server busy – wait for current job.";
    } else if (code == -11 || code == 0) {
        lastReply = "Timeout – LLM still thinking? Try again.";
    } else if (code < 0) {
        lastReply = "Connection failed";
    } else {
        // non-200 with body maybe error JSON
        JsonDocument doc;
        if (!deserializeJson(doc, resp) && doc["error"].is<const char*>())
            lastReply = doc["error"].as<String>();
        else
            lastReply = String("HTTP ") + code;
    }
    isBusy = false;
    activeSprite = SPRITE_NONE;
    drawChatUI();
}

// Fetch one TTS chunk into buf. Returns bytes, sets sampleRate/more. <0 on fail.
static int fetchTtsChunk(const String& text, int chunkIdx, uint8_t* buf, int maxBytes,
                         int& sampleRate, int& more) {
    sampleRate = 22050;
    more = 0;
    WiFiClient client;
    HTTPClient http;
    http.setConnectTimeout(10000);
    http.setTimeout(45000);
    http.setReuse(false);
    http.useHTTP10(true);

    if (!http.begin(client, serverBase + "/api/tts")) return -1;
    http.addHeader("Content-Type", "application/json");

    JsonDocument body;
    body["text"] = text;
    body["chunk"] = chunkIdx;
    String bodyStr;
    serializeJson(body, bodyStr);

    int code = http.POST(bodyStr);
    if (code == 204) { http.end(); return 0; }
    if (code != 200) { http.end(); return -1; }

    if (http.hasHeader("X-Sample-Rate")) {
        int sr = http.header("X-Sample-Rate").toInt();
        if (sr >= 8000) sampleRate = sr;
    }
    more = 1;
    if (http.hasHeader("X-More")) more = http.header("X-More").toInt();

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
            delay(3);
            if (stream->available() <= 0) break;
        } else {
            delay(1);
        }
    }
    http.end();
    return got;
}

void fetchAndPlayTTS(const String& text) {
    // Seamless chunked playback: prefetch next chunk while current plays.
    // No artificial gaps, no busy animation during speech.
    if (text.length() == 0 || WiFi.status() != WL_CONNECTED) return;

    activeSprite = SPRITE_NONE;  // never animate between TTS chunks

    const int maxBytes = 120000;
    uint8_t* bufA = (uint8_t*)malloc(maxBytes);
    uint8_t* bufB = (uint8_t*)malloc(maxBytes);
    if (!bufA) { if (bufB) free(bufB); return; }
    if (!bufB) { free(bufA); return; }

    int sampleRate = 22050, more = 0;
    int got = fetchTtsChunk(text, 0, bufA, maxBytes, sampleRate, more);
    if (got < 512) {
        free(bufA); free(bufB);
        return;
    }

    int chunkIdx = 0;
    uint8_t* playBuf = bufA;
    uint8_t* nextBuf = bufB;
    int playBytes = got;
    int playRate = sampleRate;
    int hasMore = more;

    M5.Speaker.end();
    delay(10);
    M5.Speaker.begin();
    M5.Speaker.setVolume(200);

    while (true) {
        size_t samples = (size_t)playBytes / 2;
        M5.Speaker.playRaw((const int16_t*)playBuf, samples, (uint32_t)playRate, false);

        // Prefetch next chunk while this one plays — eliminates gaps
        int nextGot = 0, nextRate = playRate, nextMore = 0;
        bool prefetched = false;
        if (hasMore) {
            nextGot = fetchTtsChunk(text, chunkIdx + 1, nextBuf, maxBytes, nextRate, nextMore);
            prefetched = (nextGot >= 512);
        }

        // Never advance on an estimated duration: wait for the hardware queue
        // to finish this complete chunk before moving to the next one.
        while (M5.Speaker.isPlaying()) {
            delay(10);
            M5.update();
        }
        if (!hasMore || !prefetched) break;

        // Swap — next chunk already in nextBuf, play immediately
        uint8_t* tmp = playBuf;
        playBuf = nextBuf;
        nextBuf = tmp;
        playBytes = nextGot;
        playRate = nextRate;
        hasMore = nextMore;
        chunkIdx++;
    }

    free(bufA);
    free(bufB);
}

void requestImage(const String& prompt) {
    if (isBusy) return;  // never stack Comfy / GPU work
    if (WiFi.status() != WL_CONNECTED) {
        lastReply = "Wi-Fi disconnected";
        if (slotRewardFromSlots) {
            currentState = STATE_SLOTS;
            drawSlotsUI();
        } else {
            currentState = STATE_IMAGE;
            drawImageUI();
        }
        return;
    }
    lastImagePrompt = prompt;
    isBusy = true;
    activeSprite = SPRITE_ANIM_PAINT;
    spriteFrame = 0;
    spriteLastMs = millis();
    showingRegenConfirm = false;
    lastReply = "Generating image...";
    // Jackpot keeps slots context; normal path clears reward flag
    if (!slotRewardFromSlots) {
        currentState = STATE_IMAGE;
        drawImageUI();
    } else {
        // brief status on slots screen then paint anim in image-sized box
        currentState = STATE_IMAGE;
        drawImageUI();
    }

    JsonDocument body;
    body["prompt"] = prompt;
    body["model_type"] = modelMode;
    if (activeLora != "None") body["lora"] = activeLora;
    String bodyStr;
    serializeJson(body, bodyStr);

    const size_t need = 320UL * 240UL * 2UL;
    if (currentImageBuffer) { free(currentImageBuffer); currentImageBuffer = nullptr; }
    currentImageBuffer = (uint8_t*)malloc(need);
    if (!currentImageBuffer) {
        lastReply = "Out of memory";
        isBusy = false;
        activeSprite = SPRITE_NONE;
        drawImageUI();
        return;
    }

    String loraHdr;
    int got = httpPostBinaryPump("/api/image", bodyStr, currentImageBuffer, need, 150000, &loraHdr);

    if (got >= (int)need) {
        if (loraHdr.length()) activeLora = loraHdr;
        currentState = STATE_PICTURE;
        pictureEnterMs = millis();
        picturePressStart = 0;
        pictureLongDone = false;
        showingRegenConfirm = false;
        regenConfirmSkipRelease = false;
        touchStartX = -1;
        isBusy = false;
        activeSprite = SPRITE_NONE;
        drawPictureUI();
        return;
    }

    free(currentImageBuffer);
    currentImageBuffer = nullptr;
    if (got == -503)
        lastReply = "Server busy – wait for current job.";
    else if (got == -11)
        lastReply = "Timeout – image still generating? Try again.";
    else if (got < 0)
        lastReply = "Image request failed";
    else
        lastReply = "Incomplete image";
    isBusy = false;
    activeSprite = SPRITE_NONE;
    currentState = STATE_IMAGE;
    drawImageUI();
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
    else if (currentState == STATE_IMAGE) drawImageUI();
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
    else if (currentState == STATE_IMAGE) drawImageUI();
}

void startRecording(bool forImage) {
    if (isBusy || isRecording) return;
    if (WiFi.status() != WL_CONNECTED) {
        lastReply = "Wi-Fi disconnected";
        if (forImage) { currentState = STATE_IMAGE; drawImageUI(); }
        else { currentState = STATE_CHAT; drawChatUI(); }
        return;
    }

    isRecording = true;
    lastReply = "Recording...";
    if (forImage) { currentState = STATE_IMAGE; drawImageUI(); }
    else { currentState = STATE_CHAT; drawChatUI(); }

    const int seconds = RECORD_SECONDS;
    const int rate = SAMPLE_RATE;
    micSamples = rate * seconds;
    if (micBuffer) { free(micBuffer); micBuffer = nullptr; }
    micBuffer = (int16_t*)malloc(micSamples * sizeof(int16_t));
    if (!micBuffer) {
        isRecording = false;
        lastReply = "Out of memory";
        if (forImage) drawImageUI(); else drawChatUI();
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
    if (forImage) drawImageUI(); else drawChatUI();

    // Build a minimal WAV in memory so /api/stt always gets a valid file
    // 44-byte header + PCM
    size_t pcmBytes = micSamples * 2;
    size_t wavLen = 44 + pcmBytes;
    uint8_t* wav = (uint8_t*)malloc(wavLen);
    if (!wav) {
        free(micBuffer); micBuffer = nullptr;
        isBusy = false;
        lastReply = "Out of memory";
        if (forImage) drawImageUI(); else drawChatUI();
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
    http.setConnectTimeout(15000);
    http.setTimeout(90000);  // Whisper can spike under load
    http.setReuse(false);
    http.useHTTP10(true);

    String url = serverBase + "/api/stt?sample_rate=" + String(rate);
    if (!http.begin(client, url)) {
        free(wav);
        isBusy = false;
        lastReply = "Bad STT URL";
        if (forImage) drawImageUI(); else drawChatUI();
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
            if (forImage) drawImageUI(); else drawChatUI();
            return;
        } else {
            lastReply = "STT bad JSON";
            http.end();
            isBusy = false;
            if (forImage) drawImageUI(); else drawChatUI();
            return;
        }
    } else {
        lastReply = http.errorToString(code);
        http.end();
        isBusy = false;
        if (forImage) drawImageUI(); else drawChatUI();
        return;
    }
    http.end();

    if (transcript.length() == 0) {
        isBusy = false;
        lastReply = "Empty transcript";
        if (forImage) drawImageUI(); else drawChatUI();
        return;
    }

    // Show what was heard, then route to image or chat
    lastReply = String("Heard: ") + transcript;
    if (forImage) {
        lastImagePrompt = transcript;
        drawImageUI();
        delay(400);
        isBusy = false;
        requestImage(transcript);
    } else {
        drawChatUI();
        delay(400);
        isBusy = false;
        sendChat(transcript);
    }
}

void appendComfyIndex(const char* filename) {
    const char* dir = "/images/comfy";
    const char* ipath = "/images/comfy/.mg_index";

    std::vector<String> names;
    if (SD.exists(ipath)) {
        File f = SD.open(ipath, FILE_READ);
        if (f) {
            String magic = f.readStringUntil('\n'); magic.trim();
            if (magic == "MGIDX1") {
                f.readStringUntil('\n'); // old count
                while (f.available()) {
                    String line = f.readStringUntil('\n');
                    line.trim();
                    if (line.length() && line[0] != '.') names.push_back(line);
                }
            }
            f.close();
        }
    }
    // avoid dup if same name somehow
    for (auto& n : names) if (n == filename) return;
    names.push_back(String(filename));

    File f = SD.open(ipath, FILE_WRITE);
    if (!f) return;
    f.println("MGIDX1");
    f.println((int)names.size());
    for (auto& n : names) f.println(n);
    f.close();
}

void saveImageToSD() {
    if (!currentImageBuffer || !SD.begin(4)) return;
    if (!SD.exists("/images")) SD.mkdir("/images");
    if (!SD.exists("/images/comfy")) SD.mkdir("/images/comfy");

    String base = String("img_") + String(millis()) + ".mg";
    String fn = String("/images/comfy/") + base;

    File f = SD.open(fn.c_str(), FILE_WRITE);
    if (!f) return;
    f.write((const uint8_t*)"MG01", 4);
    uint16_t w = 320, h = 240;
    f.write((uint8_t*)&w, 2);
    f.write((uint8_t*)&h, 2);
    f.write(currentImageBuffer, 320 * 240 * 2);
    f.close();

    appendComfyIndex(base.c_str());
    lastReply = "Saved " + fn;
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
    randomSeed(esp_random());
    loadSettings();
    loadWordListsFromSD();
    connectWiFi();
    drawChatUI();
}

void loop() {
    M5.update();

    // BtnA: toggle Chat ↔ Image mode (or exit picture / slots / cancel image keyboard)
    if (M5.BtnA.wasPressed()) {
        if (currentState == STATE_CHAT) {
            currentState = STATE_IMAGE;
            drawImageUI();
        } else if (currentState == STATE_IMAGE) {
            currentState = STATE_CHAT;
            drawChatUI();
        } else if (currentState == STATE_PICTURE) {
            showingRegenConfirm = false;
            if (slotRewardFromSlots) {
                // leaving without buy = delete
                if (currentImageBuffer) {
                    free(currentImageBuffer);
                    currentImageBuffer = nullptr;
                }
                slotRewardFromSlots = false;
                currentState = STATE_SLOTS;
                drawSlotsUI();
            } else {
                currentState = STATE_IMAGE;
                drawImageUI();
            }
        } else if (currentState == STATE_SLOTS || currentState == STATE_TROPHY) {
            currentState = STATE_CHAT;
            drawChatUI();
        } else if (currentState == STATE_KEYBOARD && keyboardTitle == "Image Prompt") {
            currentState = STATE_IMAGE;
            drawImageUI();
        }
    }

    // BtnB: cycle LoRA (short) / clear LoRA (long) — works in chat & image
    if (M5.BtnB.wasPressed()) {
        btnBPressStart = millis();
        btnBLongDone = false;
    }
    if (M5.BtnB.isPressed() && !btnBLongDone && millis() - btnBPressStart > 800) {
        btnBLongDone = true;
        clearLora();
    }
    if (M5.BtnB.wasReleased() && !btnBLongDone) cycleLora();

    // BtnC: short = model toggle; long (>800ms) = open SLOTS
    if (M5.BtnC.wasPressed()) {
        btnCPressStart = millis();
        btnCLongDone = false;
    }
    if (M5.BtnC.isPressed() && !btnCLongDone && millis() - btnCPressStart > 800) {
        btnCLongDone = true;
        if (currentState != STATE_SLOTS && currentState != STATE_TROPHY &&
            currentState != STATE_PICTURE && !isBusy) {
            currentState = STATE_SLOTS;
            slotStatus = "Match 2 = credits. Match 3 = jackpot art!";
            drawSlotsUI();
        }
    }
    if (M5.BtnC.wasReleased() && !btnCLongDone) {
        modelMode = (modelMode == "realistic") ? "anime" : "realistic";
        if (currentState == STATE_IMAGE) drawImageUI();
        else if (currentState == STATE_CHAT) drawChatUI();
        else if (currentState == STATE_SLOTS) drawSlotsUI();
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

            // Jackpot / game-over reward from slots: BUY or DELETE
            if (slotRewardFromSlots) {
                if (t.wasReleased()) {
                    auto finishSlotsReward = [&](bool bought) {
                        if (currentImageBuffer) {
                            free(currentImageBuffer);
                            currentImageBuffer = nullptr;
                        }
                        slotRewardFromSlots = false;
                        if (slotGameOverPending) {
                            slotGameOverPending = false;
                            slotCredits = 100;
                            saveSlotCredits();
                            slotStatus = bought ? "Game over art kept. Credits reset to 100."
                                                : "Credits reset to 100. Spin again!";
                        } else {
                            slotStatus = bought ? "Trophy saved!" : "Deleted. Spin again!";
                        }
                        currentState = STATE_SLOTS;
                        drawSlotsUI();
                    };
                    if (x >= 20 && x <= 150 && y >= 170 && y <= 220) {
                        if (slotGameOverPending) {
                            // Game over image is free to keep
                            saveImageToSlots();
                            finishSlotsReward(true);
                        } else if (slotCredits >= 20) {
                            slotCredits -= 20;
                            saveSlotCredits();
                            saveImageToSlots();
                            finishSlotsReward(true);
                        } else {
                            finishSlotsReward(false);
                            slotStatus = "Can't afford buy (20cr). Deleted.";
                            drawSlotsUI();
                        }
                    } else if (x >= 170 && x <= 300 && y >= 170 && y <= 220) {
                        finishSlotsReward(false);
                    }
                }
                return;
            }

            // --- Long-press → show Save&Regen / Delete&Regen confirm ---
            if (t.wasPressed()) {
                touchStartX = x;
                picturePressStart = millis();
                pictureLongDone = false;
            }
            if (t.isPressed() && !pictureLongDone && !showingRegenConfirm &&
                picturePressStart > 0 && (millis() - picturePressStart > 2000)) {
                pictureLongDone = true;
                showingRegenConfirm = true;
                regenConfirmSkipRelease = true;  // don't treat this lift as a button tap
                drawPictureUI();
                return;
            }

            if (t.wasReleased()) {
                if (showingRegenConfirm) {
                    // First release after long-press only dismisses the hold, not a choice
                    if (regenConfirmSkipRelease) {
                        regenConfirmSkipRelease = false;
                        touchStartX = -1;
                        return;
                    }
                    // Green button region: SAVE & REGEN
                    if (x >= 20 && x <= 150 && y >= 80 && y <= 160) {
                        showingRegenConfirm = false;
                        if (currentImageBuffer) {
                            saveImageToSD();
                            // keep buffer until new image arrives; free happens in requestImage
                        }
                        if (lastImagePrompt.length()) {
                            requestImage(lastImagePrompt);
                        } else {
                            currentState = STATE_IMAGE;
                            drawImageUI();
                        }
                        return;
                    }
                    // Red button region: DELETE & REGEN
                    if (x >= 170 && x <= 300 && y >= 80 && y <= 160) {
                        showingRegenConfirm = false;
                        if (currentImageBuffer) {
                            free(currentImageBuffer);
                            currentImageBuffer = nullptr;
                        }
                        if (lastImagePrompt.length()) {
                            requestImage(lastImagePrompt);
                        } else {
                            currentState = STATE_IMAGE;
                            drawImageUI();
                        }
                        return;
                    }
                    // Tap elsewhere → cancel confirm
                    showingRegenConfirm = false;
                    drawPictureUI();
                    touchStartX = -1;
                    return;
                }

                // Normal swipe handling (only when not in confirm)
                if (touchStartX >= 0 && !pictureLongDone) {
                    int d = x - touchStartX;
                    touchStartX = -1;
                    if (d < -50) {
                        if (currentImageBuffer) { free(currentImageBuffer); currentImageBuffer = nullptr; }
                        lastReply = "Discarded";
                        currentState = STATE_IMAGE;
                        drawImageUI();
                    } else if (d > 50) {
                        saveImageToSD();
                        if (currentImageBuffer) { free(currentImageBuffer); currentImageBuffer = nullptr; }
                        currentState = STATE_IMAGE;
                        drawImageUI();
                    }
                    // small movement = stay on image (no-op)
                }
                touchStartX = -1;
            }
            return;
        }

        if (!t.wasPressed()) return;

        if (currentState == STATE_SLOTS) {
            if (y >= 150 && y < 190) {
                if (x < 80) {
                    if (slotBetIdx > 0) { slotBetIdx--; slotBet = SLOT_BETS[slotBetIdx]; saveSlotCredits(); }
                    drawSlotsUI();
                } else if (x < 160) {
                    if (slotBetIdx < 3) { slotBetIdx++; slotBet = SLOT_BETS[slotBetIdx]; saveSlotCredits(); }
                    drawSlotsUI();
                } else if (x < 240) {
                    spinSlots();
                } else {
                    scanTrophyRoom();
                    currentState = STATE_TROPHY;
                    drawTrophyUI();
                }
            } else if (y >= 198) {
                if (x < 160) {
                    currentState = STATE_CHAT;
                    drawChatUI();
                }
            }
        } else if (currentState == STATE_TROPHY) {
            if (y >= 210) {
                if (x < 100) {
                    if (trophyCount > 0) {
                        trophyIndex = (trophyIndex - 1 + trophyCount) % trophyCount;
                        drawTrophyUI();
                    }
                } else if (x > 220) {
                    if (trophyCount > 0) {
                        trophyIndex = (trophyIndex + 1) % trophyCount;
                        drawTrophyUI();
                    }
                } else {
                    currentState = STATE_SLOTS;
                    drawSlotsUI();
                }
            }
        } else if (currentState == STATE_CHAT) {
            if (x > 250 && y < 32) {
                stateBeforeSettings = STATE_CHAT;
                currentState = STATE_SETTINGS;
                drawSettingsUI();
            } else if (x < 160 && y > 185) {
                if (isBusy) return;  // no new chat while LLM busy
                activeEditingField = nullptr;
                keyboardTitle = "Message";
                keyboardBuffer = "";
                kbMode = MODE_LOWER;
                shiftHeld = false;
                currentState = STATE_KEYBOARD;
                drawKeyboardUI();
            } else if (x >= 160 && y > 185) {
                if (isBusy) return;
                startRecording(false);
            } else if (y >= 40 && y <= 180) {
                // tap reply box → next page of long text
                int totalPages = (lastReply.length() + CHARS_PER_PAGE - 1) / CHARS_PER_PAGE;
                if (totalPages < 1) totalPages = 1;
                replyPage = (replyPage + 1) % totalPages;
                drawChatUI();
            }
        } else if (currentState == STATE_IMAGE) {
            if (y >= 40 && y <= 150 && lastImagePrompt.length() > 0 && !isBusy) {
                activeEditingField = nullptr;
                keyboardTitle = "Image Prompt";
                keyboardBuffer = lastImagePrompt;
                kbMode = MODE_LOWER;
                shiftHeld = false;
                currentState = STATE_KEYBOARD;
                drawKeyboardUI();
            } else if (x > 250 && y < 32) {
                stateBeforeSettings = STATE_IMAGE;
                currentState = STATE_SETTINGS;
                drawSettingsUI();
            } else if (x < 160 && y > 185) {
                if (isBusy) return;  // no new image while GPU busy
                activeEditingField = nullptr;
                keyboardTitle = "Image Prompt";
                keyboardBuffer = "";
                kbMode = MODE_LOWER;
                shiftHeld = false;
                currentState = STATE_KEYBOARD;
                drawKeyboardUI();
            } else if (x >= 160 && y > 185) {
                if (isBusy) return;
                startRecording(true);
            } else if (y > 155 && y < 185) {
                // model/LoRA status bar — tap toggles model
                modelMode = modelMode == "realistic" ? "anime" : "realistic";
                drawImageUI();
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
                    currentState = stateBeforeSettings;
                    if (currentState == STATE_IMAGE) drawImageUI();
                    else drawChatUI();
                } else {
                    saveSettings();
                    connectWiFi();
                    currentState = stateBeforeSettings;
                    if (currentState == STATE_IMAGE) drawImageUI();
                    else drawChatUI();
                }
            }
        } else if (currentState == STATE_KEYBOARD) {
            handleKeyboardTouch(x, y);
        }
    }
    delay(20);
}
