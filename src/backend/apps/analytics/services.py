import math

from .repository import preview_data


PREVIEW_ROLES = {
    "administrator": (["org", "support", "tech"], True),
    "team-lead": (["support"], True),
    "overview-only": (["org", "support", "tech"], False),
}




def trend(dates, opened, done, label):
    peak = max(4, 4 * math.ceil(max(*opened, *done) / 4))
    def y(value):
        return round(208 - 168 * value / peak, 1)
    dates_x = [(date, round(62 + index * 520 / (len(dates) - 1), 1)) for index, date in enumerate(dates)]
    lines = []
    for name, series, color in ((label, opened, "#326caa"), ("처리 완료", done, "#287e70")):
        points = [(x, y(v), f"{date} {name} {v}건") for (date, x), v in zip(dates_x, series)]
        lines.append({"name": name, "color": color, "points": points,
                      "line": " ".join(f"{x},{cy}" for x, cy, _ in points)})
    return {"label": label, "lines": lines, "dates": dates_x, "ticks": [(n, y(n)) for n in range(0, peak + 1, peak // 4)]}


def backlog(counts, names):
    total = sum(counts)
    shares = [round(100 * c / total) if total else 0 for c in counts]
    if total:
        shares[counts.index(max(counts))] += 100 - sum(shares)
    colors = ["#326caa", "#9baec1"] if len(counts) == 2 else ["#9baec1", "#326caa", "#c28935"]
    start = 0
    bands = []
    for count, color in zip(counts, colors):
        end = start + (360 * count / total if total else 0)
        if end > start:
            bands.append(f"{color} {start:.2f}deg {end:.2f}deg")
        start = end
    return {"total": total, "items": list(zip(names, counts, shares, colors)),
            "background": f"conic-gradient({', '.join(bands)})" if bands else "#f3f3f3"}


def difference(value, unit):
    return ("+" if value > 0 else "−") + str(abs(value)) + unit if value else "변동 없음"


def duration(minutes):
    hours, remainder = divmod(minutes, 60)
    return ((f"{hours}시간 " if hours else "") + (f"{remainder}분" if remainder or not hours else "")).strip()


def analytics_context(params):
    data = preview_data()
    preview = params.get("preview", "")
    if preview not in PREVIEW_ROLES:
        return {}
    scopes, individual = PREVIEW_ROLES[preview]
    period = params.get("period") if params.get("period") in data["periods"] else "current"
    scope = params.get("scope") if params.get("scope") in scopes else scopes[0]
    tab = "agents" if params.get("tab") == "agents" and individual else "summary"
    period_data = data["periods"][period]
    teams = ["support", "tech"] if scope == "org" else [scope]
    result = {"preview": preview, "individual": individual, "scopes": [(s, data["scopeNames"][s]) for s in scopes],
              "scope": scope, "periods": data["periods"].items(), "period": period, "tab": tab, "date_range": period_data}
    if tab == "agents":
        rows = []
        for team in teams:
            for name, assigned, closed, response in data["reports"][team][period]:
                work = data["pendingWork"][name]
                rows.append({"name": name, "team": data["scopeNames"][team], "assigned": assigned, "closed": closed,
                             "response": response, "work": work})
                if params.get("agent") == name:
                    series = data["individualTrends"][team][period][name]
                    result.update(agent=rows[-1], metrics=[
                        ("기간 내 배정", assigned, "건", "선택 기간 기준", "", ""),
                        ("기간 내 처리 완료", closed, "건", "선택 기간 기준", "", ""),
                        ("현재 미처리 티켓", work["assigned"] + work["transfer"], "건", "조회 시점 기준", "", ""),
                        ("첫 답변 중앙값", response, "분", "선택 기간 기준", "", ""),
                        ("미회신 검토 요청", work["review"], "건", "조회 시점 기준 · 티켓과 별도", "", "")],
                        chart=trend(period_data["dates"], series["assigned"], series["done"], "배정"),
                        backlog=backlog([work["assigned"], work["transfer"]], ["할당 티켓", "이관"]))
        result["agents"] = rows
        return result
    group = data["sample"][scope]
    current = group[period]
    previous = group["previous" if period == "current" else "earlier"]
    opened, closed = sum(current["opened"]), sum(current["done"])
    compare = period_data["compare"]
    def total(value):
        return sum(value) if isinstance(value, list) else value
    def percent(pair):
        return math.floor(100 * pair[0] / pair[1] + .5) if pair[1] else 0
    failed = 0 if scope == "tech" else 1
    b = backlog(group["pending"], ["미할당", "담당자 배정", "보류"])
    result.update(metrics=[
        ("신규 접수", opened, "건", compare, difference(opened - total(previous["opened"]), "건"), ""),
        ("처리 완료", closed, "건", compare, difference(closed - total(previous["done"]), "건"), ""),
        ("24시간 이상 미처리", group["aged"], "건", "조회 시점 기준", f'전체 미처리 {b["total"]}건 중 {percent((group["aged"], b["total"]))}%', ""),
        ("부정·매우 부정 문의 비율", percent(current["negative"]), "%", "분석 완료 {}건 중 {}건".format(*reversed(current["negative"])),
         compare + " " + difference(percent(current["negative"]) - percent(previous["negative"]), "%p"),
         f'분석 중 {opened - current["negative"][1] - failed}건 · 실패 {failed}건'),
        ("고객 만족도 (CSAT)", percent(current["csat"]), "%", "설문 응답 {}건 중 만족 {}건".format(*reversed(current["csat"])),
         compare + " " + difference(percent(current["csat"]) - percent(previous["csat"]), "%p"), "5점 설문 중 4·5점 응답")
    ], backlog=b, chart=trend(period_data["dates"], current["opened"], current["done"], "신규 접수"),
        latency=[("첫 답변 시간", "접수부터 첫 고객 답변까지", duration(current["reply"])),
                 ("처리 완료 시간", "접수부터 처리 완료까지", duration(current["resolve"]))],
        categories=sorted([(n, before, after, after - before) for n, before, after
                           in zip(["환불", "계정", "배송"], previous["categories"], current["categories"])
                           if after > before], key=lambda v: -v[3]))
    return result
