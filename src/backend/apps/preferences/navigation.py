SIDEBAR_ITEMS = (
    ("home", "업무 홈"), ("tickets", "티켓 목록"), ("inbox", "받은 요청"),
    ("followup", "팔로업"), ("analytics", "분석 대시보드"),
)


def available_sidebar_items(actor):
    return [(key, label) for key, label in SIDEBAR_ITEMS if key != "analytics" or actor["can_read_dashboard"]]


def visible_sidebar(actor):
    saved = actor["display_preferences_json"].get("sidebar")
    return [key for key, _ in available_sidebar_items(actor) if saved is None or key in saved]
