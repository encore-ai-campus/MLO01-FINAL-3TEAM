from dataclasses import dataclass
from datetime import date
from uuid import UUID

from config.http import DeskError

SORTS = ("default", "updated-desc", "updated-asc", "received-desc", "received-asc")
STATUSES = ("미처리", "보류", "답변완료")
SENTIMENTS = ("긍정", "중립", "부정", "매우 부정")


@dataclass
class TicketQuery:
    scope: str
    search_by: str
    q: str
    sort: str
    page: int
    page_size: int
    days: int | None
    start: date | None
    end: date | None
    filters: dict[str, list[str]]


def parse_query(data):
    try:
        scope = data.get("scope", "all")
        search_by = data.get("search_by", "title")
        q = data.get("q", "")
        page = int(data.get("page", 1))
        page_size = int(data.get("page_size", 10))
        days = int(data["days"]) if "days" in data else None
        start = date.fromisoformat(data["start"]) if "start" in data else None
        end = date.fromisoformat(data["end"]) if "end" in data else None
        if scope not in ("all", "mine") or search_by not in ("title", "customer", "id") or len(q) > 50 or page < 1:
            raise ValueError
    except (ValueError, OverflowError):
        raise DeskError(422, "INVALID_REQUEST", "조회 조건의 형식과 범위를 확인해 주세요.") from None
    sort = data.get("sort", "default")
    if sort not in SORTS or page_size not in (10, 20, 30, 50):
        raise DeskError(422, "INVALID_PAGE", "정렬 또는 페이지 크기를 확인해 주세요.")
    if (start is None) != (end is None) or (days is not None and start is not None):
        raise DeskError(422, "INVALID_PERIOD", "최근 기간 또는 시작일·종료일 한 쌍을 선택해 주세요.")
    filters = {key: data.getlist(key) for key in ("team", "assignee", "category", "status", "sentiment")}
    try:
        for key in ("team", "assignee", "category"):
            filters[key] = [value if value == "unassigned" and key != "category" else str(UUID(value))
                            for value in filters[key]]
        if not set(filters["status"]).issubset(STATUSES) or not set(filters["sentiment"]).issubset(SENTIMENTS):
            raise ValueError
    except ValueError:
        raise DeskError(422, "INVALID_FILTER", "필터 값을 확인해 주세요.") from None
    return TicketQuery(scope, search_by, q.strip(), sort, page, page_size, days, start, end, filters)
