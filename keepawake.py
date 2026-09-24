"""Stop Windows going to sleep while the recorder is running.

The documented failure mode here is not a crash - it is the machine sleeping.
Nine hours between 09-21 17:00 and 09-22 13:00 are *thin*, not missing, and a
thin hour is indistinguishable from a quiet market once the data is in Parquet
(see coverage.py). Nothing downstream can recover from that, so it has to be
prevented at the source.

Two ways to prevent it:

    powercfg /change standby-timeout-ac 0                 changes the machine
    SetThreadExecutionState(ES_SYSTEM_REQUIRED | ES_CONTINUOUS)

The second one wins. The request belongs to the calling thread and the kernel
drops it when the process exits - including when it is killed - so the machine
returns to its normal power behaviour the moment recording stops. Editing the
power scheme outlives the project: it silently changes a daily-driver desktop
that its owner will want sleeping again in November, and nothing in this repo
would ever change it back.

ES_DISPLAY_REQUIRED is deliberately not set. A dark monitor costs nothing;
only *system* sleep breaks the capture, and holding a screen lit for five
weeks is both wasteful and conspicuous.

What this does not do: it does not defeat a deliberate sleep - Start > Sleep,
or the power button. It defeats the idle timer, which is what actually stopped
the recording. ES_AWAYMODE_REQUIRED would intercept a deliberate sleep too,
but it converts "sleep" into "keeps running with the screen off", which is a
surprising thing to do to someone's machine without asking. Set
MARKETGUARD_AWAY_MODE=1 if a stray click is judged the bigger risk.

Imports and no-ops cleanly off Windows, so callers need no platform guard.
"""
import ctypes
import os
import sys

# winbase.h
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_AWAYMODE_REQUIRED = 0x00000040

_WINDOWS = sys.platform == "win32"

if _WINDOWS:
    _set_state = ctypes.windll.kernel32.SetThreadExecutionState
    # Declared explicitly because ES_CONTINUOUS has the high bit set: without
    # argtypes, ctypes treats it as a signed int and the call fails.
    _set_state.argtypes = [ctypes.c_uint]
    _set_state.restype = ctypes.c_uint


def _flags() -> int:
    flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED
    if os.environ.get("MARKETGUARD_AWAY_MODE") == "1":
        flags |= ES_AWAYMODE_REQUIRED
    return flags


def hold() -> str:
    """Assert the no-sleep request for the calling thread.

    Must be called from the thread that stays alive for the life of the
    recording - the request dies with its thread, so asserting it from a
    short-lived worker would silently buy nothing.

    Safe to call repeatedly: the call replaces the thread's request rather
    than stacking, so re-asserting on a timer costs one syscall and recovers
    from anything that cleared it.

    Returns a line fit for the log rather than raising, because failing to
    hold off sleep is worth recording but is never a reason to stop
    recording.
    """
    if not _WINDOWS:
        return "keep-awake: not Windows, nothing to hold off"

    away = os.environ.get("MARKETGUARD_AWAY_MODE") == "1"
    if _set_state(_flags()) == 0:
        # Documented failure is away mode on hardware that has no away mode.
        if away and _set_state(ES_CONTINUOUS | ES_SYSTEM_REQUIRED) != 0:
            return "keep-awake: HELD (away mode unsupported, idle sleep only)"
        return "keep-awake: FAILED - the machine may sleep and lose hours"

    mode = "idle sleep + away mode" if away else "idle sleep"
    return f"keep-awake: held ({mode} suppressed)"


def release() -> None:
    """Drop the request. Only useful on a clean shutdown; the kernel does
    this for us on a kill, which is the case that actually matters."""
    if _WINDOWS:
        _set_state(ES_CONTINUOUS)


if __name__ == "__main__":
    print(hold())
    print("holding - Ctrl-C to release")
    try:
        import time

        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        release()
        print("released")
