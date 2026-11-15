# Blink LED from Python

UNO Q App Lab app. Python on the Linux side toggles a boolean once per second. The STM32 sketch receives it over the Router Bridge and drives `LED_BUILTIN`.

This folder matches Arduino's example `core-and-foundational/01-led-blink/03-blinking-an-led-from-python` at `arduino/app-bricks-examples` tag `0.13.2`: `app.yaml`, `python/main.py`, `sketch/sketch.ino`, and `sketch/sketch.yaml` (`platform: arduino:zephyr`). No bricks and no Python requirements. `arduino.app_utils` comes from the App Lab runtime on the board.

`LED_BUILTIN` on UNO Q is the red channel of MCU RGB LED 3 (STM32 port H pin 10 in core `arduino:zephyr` 0.56.0). The LED is active-low: `true` writes `LOW`, `false` writes `HIGH`.

No board was attached while this was added. A real UNO Q run is still required before merge. Copy this folder to the board's app directory (or import it in App Lab) and run it there. LED 3 should blink red about once a second. Do not install system packages from the unprivileged dev account.

## Native test on a PC

`test-native/` is a separate PlatformIO project so App Lab still sees the official app layout at this folder's root. The test compiles `sketch/sketch.ino` directly. It does not copy the LED logic. A stub `Arduino_RouterBridge.h` in that project records `Bridge.provide` and leaves `Bridge.begin()` empty. ArduinoFake supplies `pinMode` and `digitalWrite`.

On a PC, with no board:

```bash
pip install platformio
cd uno-q-blink/test-native
pio test -e native
```

The test calls `setup()`, checks that `LED_BUILTIN` is an output and that `set_led_state` was provided, then calls that handler with `true` and `false`. It expects `digitalWrite(LED_BUILTIN, LOW)` and then `digitalWrite(LED_BUILTIN, HIGH)`. ArduinoFake 0.4.0 defines `LED_BUILTIN` as 13. The UNO Q core defines the same macro as the onboard red LED. This test checks the macro the sketch writes and the active-low order. It does not flash hardware.
