import json
from datetime import datetime, timezone
from uuid import uuid4


class ApiError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message
        super().__init__(message)


def require(condition, status=422, code='invalid_input', message='Invalid request'):
    if not condition:
        raise ApiError(status, code, message)


def now():
    return datetime.now(timezone.utc)


def timestamp(value=None):
    return (value or now()).isoformat().replace('+00:00', 'Z')


def parse_time(value):
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        require(parsed.tzinfo is not None, message='Dates require an explicit timezone')
        require(parsed.utcoffset().total_seconds() == 0, message='Dates must be UTC')
        return parsed
    except (ValueError, TypeError, AttributeError):
        raise ApiError(422, 'invalid_input', 'Expected an RFC3339 UTC timestamp') from None


def identifier():
    return str(uuid4())


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def revision(data, expected):
    require(type(expected) is int, message='expected_revision is required')
    require(data['revision'] == expected, 409, 'revision_conflict', 'Resource changed; read it again')


def text_field(data, name, maximum, required=False):
    value = data.get(name, '')
    require(isinstance(value, str) and len(value) <= maximum, message=f'Invalid {name}')
    if required:
        require(bool(value.strip()), message=f'{name} is required')
    return value


def actions(role):
    return {'viewer':['read'], 'editor':['read','write'], 'owner':['read','write','manage']}.get(role, [])
