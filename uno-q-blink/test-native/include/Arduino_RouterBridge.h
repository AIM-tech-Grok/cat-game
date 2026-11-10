#pragma once

// Test-only stand-in for Arduino_RouterBridge. The sketch includes this
// header by name. The native test puts this file on the include path.
// App Lab on a UNO Q uses the real library instead.

using BridgeBoolHandler = void (*)(bool);

struct BridgeClass {
    const char *provided_name;
    BridgeBoolHandler provided_handler;
    int provide_count;

    BridgeClass()
        : provided_name(nullptr), provided_handler(nullptr), provide_count(0) {}

    void begin() {}

    void provide(const char *name, BridgeBoolHandler handler) {
        provided_name = name;
        provided_handler = handler;
        provide_count++;
    }

    void reset() {
        provided_name = nullptr;
        provided_handler = nullptr;
        provide_count = 0;
    }
};

inline BridgeClass Bridge;
