"""Exception hierarchy. Every error the runner handles specially lives here."""


class HarnessError(Exception):
    """Base class for harness errors."""


class ConfigError(HarnessError):
    pass


class EmergencyStop(HarnessError):
    """Raised from any check point once the emergency stop has been triggered."""


class UserAbort(HarnessError):
    """The operator chose Abort at a manual checkpoint."""


class WindowNotFound(HarnessError):
    pass


class CaptureBlocked(HarnessError):
    """The viewer window asks Windows to exclude it from capture.

    The harness deliberately does not try to circumvent this.
    """


class PlatformUnsupported(HarnessError):
    pass


class ResumeMismatch(HarnessError):
    pass


class FocusLost(HarnessError):
    """The viewer could not be brought to the foreground (someone else has focus)."""
