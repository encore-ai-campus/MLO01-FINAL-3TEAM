from datetime import datetime, time, timedelta
from math import ceil
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

    def y(value):
        return round(208 - 168 * value / peak, 1)

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


def time_label(value):
    if value is None:
        return "—"
    hours, minutes = divmod(round(value), 60)
    return (" ".join(part for part in (f"{hours}시간" if hours else "", f"{minutes}분" if minutes else "") if part)
            or "0분")


def shown(value):
    return "—" if value is None else value


def units(value):
    return "" if value is None else "건"


def summary_context(cursor, filters, history, first_answer):
    received = repository.receipts(cursor, filters)
    pending = backlog([
        ("미할당", received["unassigned"], "#9baec1"),
        ("담당자 배정", received["assigned"], "#326caa"),
        ("보류", received["held"], "#c28935"),
    ])
    sentiment_metric, sentiment_rows = sentiment_context(cursor, filters)
    daily = [received["daily"].get(day, 0) for day in filters["dates"]]
    series = [("신규 접수", daily, "#326caa")]
    completed, comparison, resolution = None, "", None
    if history is not None:
        totals = {row["day"]: row["total"] for row in history["daily"]}
        done = [totals.get(day.isoformat(), 0) for day in filters["dates"]]
        previous = sum(totals.get(day.isoformat(), 0) for day in filters["compare_dates"])
        completed = sum(done)
        comparison = "비교 기간 대비 " + difference(completed - previous)
        resolution = history["resolution"]
        series.append(("처리 완료", done, "#287e70"))
    return {
        "metrics": [
            ("신규 접수", received["received"], "건", "비교 기간 대비 " + difference(received["received"] - received["previous"])),
            ("처리 완료", shown(completed), units(completed), comparison),
            ("24시간 이상 미처리", received["aged"], "건", f'전체 미처리 {pending["total"]}건 중 {pct(received["aged"], pending["total"]) or 0}%'),
            sentiment_metric,
        ],
        **category_context(cursor, filters, received["received"]),
        "sentiments": sentiment_rows, "pending": pending,
        "chart": trend(label_dates(filters["dates"]), series),
        "chart_title": "신규 접수·처리 완료 추이" if completed is not None else "신규 접수 추이",
        "hourly": hourly_chart(cursor, filters),
        "latency": [
            ("첫 답변 시간", "접수부터 첫 고객 답변 발송까지", time_label(first_answer)),
            ("처리 완료 시간", "접수부터 답변 완료까지", time_label(resolution)),
        ],
    }


def counselor_context(cursor, filters, history, first_answers, review_counts, selected_id):
    staff = repository.staff_in_scope(cursor, filters)
    active = repository.active_assignments(cursor, filters)
    rows = history["flow"] if history is not None else []
    staff_flow = {(row["id"], row["day"], row["kind"]): row["total"] for row in rows}
    staff_pending = {row["id"]: row for row in history["pending"]} if history is not None else {}
    engaged = {row["id"] for row in rows}
    agents = []
    for person in staff:
        staff_id = person["id"]
        if not (person["role"] == "counselor" or person["handles_tickets"] or staff_id in active or staff_id in engaged):
            continue
        assigned, done, previous_done = None, None, None
        work = staff_pending.get(staff_id, {})
        if history is not None:
            def day_counts(kind, dates):
                return [staff_flow.get((staff_id, day.isoformat(), kind), 0) for day in dates]

            assigned_daily = day_counts("assigned", filters["dates"])
            done_daily = day_counts("done", filters["dates"])
            assigned, done = sum(assigned_daily), sum(done_daily)
            previous_done = sum(day_counts("done", filters["compare_dates"]))
        agents.append({
            **person, "assigned": assigned, "done": done, "previous_done": previous_done,
            "pending_count": active.get(staff_id, 0),
            "pending_assigned": work.get("assigned", 0) if history is not None else None,
            "pending_transferred": work.get("transferred", 0) if history is not None else None,
            "pending_unknown": work.get("unknown", 0),
            "review_pending": review_counts.get(staff_id, 0) if review_counts is not None else None,
            "first_answer": time_label(first_answers.get(staff_id)),
        })
        if staff_id == selected_id and history is not None:
            agents[-1]["today"] = work.get("today", 0)
            agents[-1]["chart"] = trend(label_dates(filters["dates"]), [
                ("배정", assigned_daily, "#326caa"), ("처리 완료", done_daily, "#287e70"),
            ])

    if not selected_id:
        return {"agents": agents}
    agent = next((entry for entry in agents if entry["id"] == selected_id), None)
    if agent is None:
        raise DeskError(404, "ANALYTICS_AGENT_NOT_FOUND", "조회할 수 있는 상담사가 아닙니다.")
    comparison = "비교 기간 대비 " + difference(agent["done"] - agent["previous_done"]) if agent["done"] is not None else ""
    pending = None
    if history is not None:
        items = [
            ("할당 티켓", agent["pending_assigned"], "#326caa"),
            ("이관", agent["pending_transferred"], "#9baec1"),
        ]
        if agent["pending_unknown"]:
            items.append(("배정 이력 없음", agent["pending_unknown"], "#c28935"))
        pending = backlog(items)
    return {
        "agent": agent, "chart": agent.get("chart"), "pending": pending,
        "metrics": [
            ("기간 내 배정", shown(agent["assigned"]), units(agent["assigned"]),
             f'오늘 할당 {agent["today"]}건' if "today" in agent else ""),
            ("기간 내 처리 완료", shown(agent["done"]), units(agent["done"]), comparison),
            ("현재 미처리 티켓", agent["pending_count"], "건", "조회 시점 기준"),
            ("첫 답변 중앙값", agent["first_answer"], "", "선택 기간 기준"),
            ("미회신 검토 요청", shown(agent["review_pending"]), units(agent["review_pending"]), "조회 시점 기준"),
        ],
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
    sources = repository.available_sources(cursor)
    history = repository.event_history(cursor, filters) if sources["events"] else None
    first_answer, first_answers = repository.first_answer_times(cursor, filters) if sources["first_answers"] else (None, {})
    if tab == "summary":
        context.update(summary_context(cursor, filters, history, first_answer))
    else:
        reviews = repository.review_pending(cursor, filters) if sources["reviews"] else None
        context.update(counselor_context(cursor, filters, history, first_answers, reviews, params.get("agent")))
    context["unavailable"] = [
        label for key, label in (("events", "배정·완료 이력"), ("first_answers", "첫 고객 답변 발송 시각"),
                                 ("reviews", "검토 요청"))
        if not sources[key] and (tab == "agents" or key != "reviews")
    ]
    if history is not None:
        context["missing_completions"] = history["missing_completions"]
    return context
