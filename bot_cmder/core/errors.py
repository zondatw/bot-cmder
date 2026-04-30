class BotCmderError(Exception):
    """Base error for bot-cmder."""


class CommandNotFound(BotCmderError):
    pass


class AuthDenied(BotCmderError):
    pass


class OTPRequired(BotCmderError):
    """Raised when a privileged command needs an OTP step (Phase 2)."""


class OTPInvalid(BotCmderError):
    """Raised when an OTP code does not validate (Phase 2)."""


class HandlerError(BotCmderError):
    """Wraps unexpected exceptions raised by command handlers."""
