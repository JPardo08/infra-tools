"""Fail-closed errors for the synology-mcp implementation."""


class AdapterError(RuntimeError):
    """Raised when the MCP backend cannot complete a health read."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class TargetResolutionError(AdapterError):
    """Raised when a targetRef cannot be resolved uniquely."""
