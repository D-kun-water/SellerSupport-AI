from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation


class DomainError(Exception):
    def __init__(self, code, message, status=422):
        self.code, self.message, self.status = code, message, status


def timestamp(value):
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            raise ValueError()
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError):
        raise DomainError('VALIDATION_ERROR', '时间必须为带时区的 ISO 8601')


def now():
    return datetime.now(timezone.utc).isoformat()


def money(value):
    try:
        d = Decimal(str(value))
        if not d.is_finite() or d < 0 or d != d.quantize(Decimal('0.01')):
            raise InvalidOperation()
        return format(d, '.2f')
    except (InvalidOperation, ValueError):
        raise DomainError('VALIDATION_ERROR', '金额必须为非负、最多两位小数的十进制数')


TRANSITIONS = {
    'new': {'in_progress', 'escalated'},
    'in_progress': {'waiting_customer', 'waiting_approval', 'waiting_execution', 'escalated', 'resolved'},
    'waiting_customer': {'in_progress', 'escalated'},
    'waiting_approval': {'in_progress', 'waiting_execution', 'escalated'},
    'waiting_execution': {'in_progress', 'escalated', 'resolved'},
    'escalated': {'in_progress', 'resolved'},
    'resolved': {'closed', 'reopened'},
    'closed': {'reopened'},
    'reopened': {'in_progress', 'escalated'},
}
