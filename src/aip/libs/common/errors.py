"""平台错误码与 RFC 9457 Problem Details（docs/api/error-codes.md 为登记表，测试校验两者一致）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    PRECONDITION_FAILED = "PRECONDITION_FAILED"
    NOT_FOUND = "NOT_FOUND"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    TOKEN_EXPIRED = "TOKEN_EXPIRED"
    ASSERTION_INVALID = "ASSERTION_INVALID"
    ASSERTION_EXPIRED = "ASSERTION_EXPIRED"
    ASSERTION_REPLAYED = "ASSERTION_REPLAYED"
    MFA_REQUIRED = "MFA_REQUIRED"
    ACCOUNT_LOCKED = "ACCOUNT_LOCKED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    USER_NOT_BOUND = "USER_NOT_BOUND"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    CLASSIFICATION_DENIED = "CLASSIFICATION_DENIED"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
    CONFIRMATION_MISMATCH = "CONFIRMATION_MISMATCH"
    RATE_LIMITED = "RATE_LIMITED"
    DOWNSTREAM_AUTH_EXPIRED = "DOWNSTREAM_AUTH_EXPIRED"
    DOWNSTREAM_UNAVAILABLE = "DOWNSTREAM_UNAVAILABLE"
    DOWNSTREAM_TIMEOUT = "DOWNSTREAM_TIMEOUT"
    TLS_UNTRUSTED = "TLS_UNTRUSTED"
    TLS_EXPIRED = "TLS_EXPIRED"
    TLS_HOSTNAME_MISMATCH = "TLS_HOSTNAME_MISMATCH"
    TLS_PIN_MISMATCH = "TLS_PIN_MISMATCH"
    TLS_FINGERPRINT_CHANGED = "TLS_FINGERPRINT_CHANGED"
    SCHEME_MISMATCH = "SCHEME_MISMATCH"
    REDIRECT_BLOCKED = "REDIRECT_BLOCKED"
    TRANSPORT_NOT_ALLOWLISTED = "TRANSPORT_NOT_ALLOWLISTED"
    TRANSPORT_EXCEPTION_EXPIRED = "TRANSPORT_EXCEPTION_EXPIRED"
    BUSINESS_RULE_VIOLATION = "BUSINESS_RULE_VIOLATION"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"
    MAINTENANCE_WINDOW = "MAINTENANCE_WINDOW"
    SANDBOX_WRITE_BLOCKED = "SANDBOX_WRITE_BLOCKED"
    POLICY_UNAVAILABLE = "POLICY_UNAVAILABLE"
    INTERNAL = "INTERNAL"


# code -> (HTTP status, retryable)
ERROR_META: dict[ErrorCode, tuple[int, bool]] = {
    ErrorCode.INVALID_ARGUMENT: (400, False),
    ErrorCode.IDEMPOTENCY_CONFLICT: (409, False),
    ErrorCode.PRECONDITION_FAILED: (412, False),
    ErrorCode.NOT_FOUND: (404, False),
    ErrorCode.UNAUTHENTICATED: (401, False),
    ErrorCode.TOKEN_EXPIRED: (401, False),
    ErrorCode.ASSERTION_INVALID: (401, False),
    ErrorCode.ASSERTION_EXPIRED: (401, False),
    ErrorCode.ASSERTION_REPLAYED: (401, False),
    ErrorCode.MFA_REQUIRED: (401, False),
    ErrorCode.ACCOUNT_LOCKED: (423, False),
    ErrorCode.PERMISSION_DENIED: (403, False),
    ErrorCode.USER_NOT_BOUND: (403, False),
    ErrorCode.OUT_OF_SCOPE: (403, False),
    ErrorCode.CLASSIFICATION_DENIED: (403, False),
    ErrorCode.CONFIRMATION_REQUIRED: (202, False),
    ErrorCode.CONFIRMATION_MISMATCH: (409, False),
    ErrorCode.RATE_LIMITED: (429, True),
    ErrorCode.DOWNSTREAM_AUTH_EXPIRED: (502, True),
    ErrorCode.DOWNSTREAM_UNAVAILABLE: (503, True),
    ErrorCode.DOWNSTREAM_TIMEOUT: (504, True),
    ErrorCode.TLS_UNTRUSTED: (502, False),
    ErrorCode.TLS_EXPIRED: (502, False),
    ErrorCode.TLS_HOSTNAME_MISMATCH: (502, False),
    ErrorCode.TLS_PIN_MISMATCH: (502, False),
    ErrorCode.TLS_FINGERPRINT_CHANGED: (502, False),
    ErrorCode.SCHEME_MISMATCH: (500, False),
    ErrorCode.REDIRECT_BLOCKED: (502, False),
    ErrorCode.TRANSPORT_NOT_ALLOWLISTED: (502, False),
    ErrorCode.TRANSPORT_EXCEPTION_EXPIRED: (502, False),
    ErrorCode.BUSINESS_RULE_VIOLATION: (422, False),
    ErrorCode.OUTCOME_UNKNOWN: (202, False),
    ErrorCode.CIRCUIT_OPEN: (503, False),
    ErrorCode.MAINTENANCE_WINDOW: (503, False),
    ErrorCode.SANDBOX_WRITE_BLOCKED: (403, False),
    ErrorCode.POLICY_UNAVAILABLE: (503, False),
    ErrorCode.INTERNAL: (500, False),
}


@dataclass
class PlatformError(Exception):
    """所有对外可见的错误都用它抛出；detail 面向人，不得包含下游内部细节或凭据。"""

    code: ErrorCode
    detail: str = ""
    errors: list[dict[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        super().__init__(f"{self.code}: {self.detail}")

    @property
    def status(self) -> int:
        return ERROR_META[self.code][0]

    @property
    def retryable(self) -> bool:
        return ERROR_META[self.code][1]

    def problem(self, instance: str = "", trace_id: str = "") -> dict[str, Any]:
        body: dict[str, Any] = {
            "type": f"https://errors.aip.internal/{self.code}",
            "title": self.code.replace("_", " ").capitalize(),
            "status": self.status,
            "code": str(self.code),
            "detail": self.detail,
        }
        if instance:
            body["instance"] = instance
        if trace_id:
            body["trace_id"] = trace_id
        if self.errors:
            body["errors"] = self.errors
        return body
