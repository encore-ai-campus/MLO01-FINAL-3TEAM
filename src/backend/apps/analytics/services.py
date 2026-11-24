from math import ceil
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



def pct(part, total):
    return round(part * 100 / total, 1) if total else None


def difference(value, unit="건"):
    if value == 0:
        return "변동 없음"
    return f"{value:+g}{unit}"


def backlog(items):
    total = sum(value for _, value, _ in items)
    bands, rows = [], []
    previous = 0
    for label, value, color in items:
        end = previous + (360 * value / total if total else 0)
        if end > previous:
            bands.append(f"{color} {previous:.3f}deg {end:.3f}deg")
        previous = end
        rows.append((label, value, pct(value, total) or 0, color))
    return {
        "total": total, "rows": rows,
        "background": f"conic-gradient({', '.join(bands)})" if bands else "#f3f3f3",
    }


def trend(labels, series):
    peak = max(4, 4 * ceil(max(value for _, values, _ in series for value in values) / 4))
    y = lambda value: round(208 - 168 * value / peak, 1)
    points_x = [round(62 + index * 520 / max(1, len(labels) - 1), 1) for index in range(len(labels))]
    lines = []
    for name, values, color in series:
        points = [(x, y(value), f"{label} {name} {value}건") for (label, x), value in zip(zip(labels, points_x), values)]
        lines.append({"name": name, "color": color, "points": points,
                      "line": " ".join(f"{x},{cy}" for x, cy, _ in points)})
    step = ceil(len(labels) / 8)
    return {
        "lines": lines, "labels": list(zip(labels[::step], points_x[::step])),
        "ticks": [(value, y(value)) for value in range(0, peak + 1, peak // 4)],
        "table": list(zip(labels, *(values for _, values, _ in series))),
    }


def label_dates(dates):
    return [day.strftime("%m/%d") for day in dates]



def sentiment_context(cursor, filters):
    data = repository.sentiments(cursor, filters)
    labels = ("긍정", "중립", "부정", "매우 부정")
    rows = [(label, data.get(label, {}).get("selected", 0)) for label in labels]
    total = sum(value for _, value in rows)
    previous_total = sum(row["previous"] for row in data.values())
    negative = sum(data.get(label, {}).get("selected", 0) for label in labels[2:])
    previous_negative = sum(data.get(label, {}).get("previous", 0) for label in labels[2:])
    current_pct, previous_pct = pct(negative, total), pct(previous_negative, previous_total)
    description = f"감성값이 있는 {total}건 중 {negative}건"
    if current_pct is not None and previous_pct is not None:
        description += " · 비교 기간 대비 " + difference(round(current_pct - previous_pct, 1), "%p")
    return (
        ("부정·매우 부정 문의 비율", current_pct if current_pct is not None else "—",
         "%" if current_pct is not None else "", description),
        [(label, value, pct(value, total)) for label, value in rows],
    )


def category_context(cursor, filters, total):
    rows = repository.categories(cursor, filters)
    increases = sorted(
        ((r["name"], r["previous"], r["selected"], r["selected"] - r["previous"])
         for r in rows if r["name"] is not None and r["selected"] > r["previous"]),
        key=lambda row: (-row[3], row[0]),
    )[:3]
    return {
        "category_increases": increases,
        "categories": [(r["name"] or "미분류", r["selected"], pct(r["selected"], total))
                       for r in rows if r["selected"]],
    }


def hourly_chart(cursor, filters):
    rows = repository.hourly_receipts(cursor, filters)
    return trend(
        [f"{hour:02d}시" for hour in range(24)],
        [(label, [rows.get(hour, {}).get(key, 0) for hour in range(24)], color)
         for key, label, color in (("selected", "선택 기간", "#326caa"), ("previous", "비교 기간", "#287e70"))],
    )

def summary_context(cursor, filters):
    receipts = repository.receipts(cursor, filters)
    pending = backlog([
        ("미할당", receipts["unassigned"], "#9baec1"),
        ("담당자 배정", receipts["assigned"], "#326caa"),
        ("보류", receipts["held"], "#c28935"),
    ])
    sentiment_metric, sentiment_rows = sentiment_context(cursor, filters)
    daily = [receipts["daily"].get(day, 0) for day in filters["dates"]]
    return {
        "metrics": [
            ("신규 접수", receipts["received"], "건", "비교 기간 대비 " + difference(receipts["received"] - receipts["previous"])),
            ("24시간 이상 미처리", receipts["aged"], "건", f'전체 미처리 {pending["total"]}건 중 {pct(receipts["aged"], pending["total"]) or 0}%'),
            sentiment_metric,
        ],
        **category_context(cursor, filters, receipts["received"]),
        "sentiments": sentiment_rows,
        "pending": pending, "chart": trend(label_dates(filters["dates"]), [("신규 접수", daily, "#326caa")]),
        "daily_received": daily,
        "hourly": hourly_chart(cursor, filters),
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
    context = {
        "tab": tab, "period": period, "periods": PERIODS, "scope": scope, "scopes": scopes.items(),
        "scope_name": scopes[scope], "bounds": bounds,
    }
    filters = {**bounds, "company": company, "team": None if scope == "org" else scope}
    if tab == "summary":
        context.update(summary_context(cursor, filters))
    return context
