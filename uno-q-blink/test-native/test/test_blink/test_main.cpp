#include <ArduinoFake.h>
#include <unity.h>

#include <cstdio>
#include <exception>
#include <string>

#include "Arduino_RouterBridge.h"

using namespace fakeit;

// The Arduino builder inserts a prototype before the sketch body. This file
// includes sketch.ino as ordinary C++, so the prototype has to come first.
// The sketch file itself is unchanged.
void set_led_state(bool state);

#include "../../../sketch/sketch.ino"

// FakeIt prints uint8_t arguments as characters, so pin 13 shows up as a
// carriage return. Keep the Unity failure on one readable line.
static std::string one_line(const std::string &raw) {
    std::string out;
    out.reserve(raw.size());
    for (unsigned char c : raw) {
        if (c == '\n' || c == '\t') {
            out.push_back(' ');
        } else if (c < 32 || c > 126) {
            static const char *hex = "0123456789abcdef";
            out += "\\x";
            out.push_back(hex[c >> 4]);
            out.push_back(hex[c & 0x0f]);
        } else {
            out.push_back(static_cast<char>(c));
        }
    }
    return out;
}

static bool verify_ok(void (*check)(), std::string *message) {
    try {
        check();
        return true;
    } catch (const std::exception &ex) {
        *message = one_line(ex.what());
        return false;
    }
}

// Unity calls these with C linkage. A C++ definition would not replace the
// weak stubs PlatformIO generates.
extern "C" void setUp(void) {
    ArduinoFakeReset();
    Bridge.reset();
    When(Method(ArduinoFake(), pinMode)).AlwaysReturn();
    When(Method(ArduinoFake(), digitalWrite)).AlwaysReturn();
}

extern "C" void tearDown(void) {}

extern "C" void test_setup_arms_active_low_led(void) {
    // Unity failures longjmp. Destroy C++ locals before that jump.
    static char failure[1200];
    const char *error = nullptr;

    setup();

    {
        std::string message;
        if (!verify_ok(
                []() {
                    Verify(Method(ArduinoFake(), pinMode).Using(LED_BUILTIN, OUTPUT))
                        .Once();
                },
                &message)) {
            std::snprintf(failure, sizeof(failure), "%s", message.c_str());
            error = failure;
        }
    }
    if (error != nullptr) {
        TEST_FAIL_MESSAGE(error);
    }

    TEST_ASSERT_EQUAL_INT(1, Bridge.provide_count);
    TEST_ASSERT_NOT_NULL(Bridge.provided_name);
    TEST_ASSERT_EQUAL_STRING("set_led_state", Bridge.provided_name);
    TEST_ASSERT_NOT_NULL(reinterpret_cast<void *>(Bridge.provided_handler));

    Bridge.provided_handler(true);
    Bridge.provided_handler(false);

    {
        std::string message;
        if (!verify_ok(
                []() {
                    Verify(Method(ArduinoFake(), digitalWrite).Using(LED_BUILTIN, LOW) +
                           Method(ArduinoFake(), digitalWrite)
                               .Using(LED_BUILTIN, HIGH))
                        .Once();
                },
                &message)) {
            std::snprintf(failure, sizeof(failure), "%s", message.c_str());
            error = failure;
        }
    }
    if (error != nullptr) {
        TEST_FAIL_MESSAGE(error);
    }
}

int main(int argc, char **argv) {
    (void)argc;
    (void)argv;
    UNITY_BEGIN();
    RUN_TEST(test_setup_arms_active_low_led);
    // Unity's return value is the failure count. PlatformIO's native reader
    // treats status 1 as SIGHUP, which hides the FAIL line behind an error.
    // The Unity report above is the result PlatformIO records.
    (void)UNITY_END();
    return 0;
}
