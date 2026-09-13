"""Device registry and health watchdog (spec 13)."""

from app.modules.devices.watchdog import (
    SilenceState,
    WatchdogConfig,
    WatchdogVerdict,
    evaluate_silence,
)

__all__ = ["SilenceState", "WatchdogConfig", "WatchdogVerdict", "evaluate_silence"]
