import datetime


def to_json_safe(value):
    """Convert any Python object into a JSON-serializable structure."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value

    if isinstance(value, (list, tuple)):
        return [to_json_safe(v) for v in value]

    if isinstance(value, dict):
        return {str(k): to_json_safe(v) for k, v in value.items()}

    if isinstance(value, set):
        return [to_json_safe(v) for v in value]

    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()

    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")

    # Home Assistant objects → 文字列化
    if hasattr(value, "__class__"):
        return str(value)

    # fallback
    return str(value)
