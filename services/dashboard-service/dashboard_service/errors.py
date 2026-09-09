"""Error semantics shared by every API surface (contracts.md §5)."""
from datetime import datetime, timezone
from uuid import UUID, uuid4


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable


def require(condition, status: int = 422, code: str = "invalid_input", message: str = "Invalid request",
            *, retryable: bool = False):
    if not condition:
        raise ApiError(status, code, message, retryable=retryable)
    return True


def now() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid4())


def is_uuid(value) -> bool:
    if not isinstance(value, str) or len(value) != 36:
        return False
    try:
        return str(UUID(value)) == value.lower()
    except (ValueError, AttributeError):
        return False


def require_uuid(value, field: str = "id") -> str:
    require(is_uuid(value), message=f"{field} must be a UUID")
    return value.lower()


def to_rfc3339(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_rfc3339(value, field: str = "time"):
    """Accept only timezone-aware absolute times; store/emit UTC."""
    require(isinstance(value, str) and value, code="invalid_time", message=f"{field} must be an RFC3339 timestamp")
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00") if text.endswith(("Z", "z")) else text)
    except ValueError:
        raise ApiError(422, "invalid_time", f"{field} must be an RFC3339 timestamp") from None
    require(parsed.tzinfo is not None and parsed.utcoffset() is not None,
            code="invalid_time", message=f"{field} requires an explicit timezone")
    return parsed.astimezone(timezone.utc)


def text_field(data: dict, name: str, maximum: int, *, required: bool = False) -> str:
    value = data.get(name)
    require(isinstance(value, str), message=f"{name} must be a string")
    require(len(value) <= maximum, message=f"{name} must be at most {maximum} characters")
    if required:
        require(bool(value.strip()), message=f"{name} must not be blank")
    return value
