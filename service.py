def normalize_notice(item):
    required = ('source', 'notice_id', 'title', 'published_at')
    missing = [field for field in required if not item.get(field)]
    if missing:
        raise ValueError(f"missing fields: {', '.join(missing)}")
    return {field: str(item[field]).strip() for field in required}
