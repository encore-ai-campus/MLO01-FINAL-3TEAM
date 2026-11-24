from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.utils import timezone

from config.http import DeskError

from . import repository


KOREA = ZoneInfo("Asia/Seoul")
PERIODS = (("current", "최근 7일"), ("previous", "직전 7일"))


def period_bounds(period, now):
    today = now.astimezone(KOREA).date()
    last_day = today - timedelta(days=7 if period == "previous" else 0)
    first_day = last_day - timedelta(days=6)
    compare_day = first_day - timedelta(days=7)
    return {
        "now": now,
        "start": datetime.combine(first_day, time.min, KOREA),
        "end": datetime.combine(last_day + timedelta(days=1), time.min, KOREA) if period == "previous" else now,
        "compare_start": datetime.combine(compare_day, time.min, KOREA),
        "dates": [first_day + timedelta(days=index) for index in range(7)],
        "compare_dates": [compare_day + timedelta(days=index) for index in range(7)],
    }


def analytics_context(cursor, actor, params):
    scopes = {"org": "조직 전체"}
    company = actor["company_uuid"]
    scopes.update((team["id"], team["name"]) for team in repository.teams(cursor, company))
    scope = params.get("scope", "org")
    period = params.get("period", "current")
    tab = params.get("tab", "summary")
    if scope not in scopes or period not in dict(PERIODS) or tab not in {"summary", "agents"}:
        raise DeskError(400, "INVALID_ANALYTICS_FILTER", "조회 조건을 확인해 주세요.")
    bounds = period_bounds(period, timezone.now())
    return {
        "tab": tab, "period": period, "periods": PERIODS, "scope": scope, "scopes": scopes.items(),
        "scope_name": scopes[scope], "bounds": bounds,
    }
