from urllib.parse import urlencode

from django import forms
from django.conf import settings
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from config.http import DeskError
from .. import model_review, retraining, training
from .training import evaluation_sets, training_actor


MODES = (
    ("period", "기간마다"),
    ("volume", "데이터량 기준"),
    ("either", "둘 중 먼저 충족"),
    ("both", "두 조건 모두 충족"),
)


class ScheduleForm(forms.Form):
    enabled = forms.BooleanField(required=False, label="자동 재학습 사용")
    revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    trigger_mode = forms.ChoiceField(choices=MODES, label="실행 조건", widget=forms.RadioSelect)
    interval_days = forms.IntegerField(required=False, min_value=1, max_value=365, label="재학습 간격")
    min_new_gold = forms.IntegerField(required=False, min_value=1, max_value=1000000, label="새 검수 표본")
    window_start = forms.TimeField(label="시작 시각", widget=forms.TimeInput(format="%H:%M", attrs={"type": "time"}))
    window_end = forms.TimeField(label="종료 시각", widget=forms.TimeInput(format="%H:%M", attrs={"type": "time"}))
    model = forms.ChoiceField(required=False, label="시작 모델")
    eval_dataset = forms.ChoiceField(required=False, label="고정 평가셋")

    def clean(self):
        values = super().clean()
        mode = values.get("trigger_mode")
        for key, inactive_mode in (("interval_days", "volume"), ("min_new_gold", "period")):
            if mode == inactive_mode:
                values[key] = self.initial.get(key)
            elif values.get(key) is None and key not in self.errors:
                self.add_error(key, "실행 기준을 입력해 주세요.")
        if values.get("window_start") and values.get("window_start") == values.get("window_end"):
            self.add_error("window_end", "시작 시각과 다른 종료 시각을 선택해 주세요.")
        if values.get("enabled"):
            for key in ("model", "eval_dataset"):
                if key not in self.errors and not values.get(key):
                    self.add_error(key, "자동 재학습에 사용할 항목을 선택해 주세요.")
        return values


def schedule_summary(policy):
    days = f"{policy['interval_days']}일 경과"
    count = f"새 검수 표본 {policy['min_new_gold']:,}건"
    return {"period": days, "volume": count, "either": f"{days} 또는 {count}", "both": f"{days} 및 {count}"}[policy["trigger_mode"]]


def saved_redirect(name, **params):
    response = redirect(reverse("preferences:" + name) + "?" + urlencode({**params, "saved": "1"}))
    response.status_code = 303
    return response


@require_http_methods(["GET", "POST"])
def schedule(request):
    actor = training_actor(request)
    company = actor["company_id"]
    policy = retraining.read_policy(company)
    notices = []
    models = []
    connected = False
    if settings.MLFLOW_TRACKING_URI:
        try:
            models = training.available_models(company)
            connected = True
        except (OSError, ValueError, KeyError):
            notices.append("MLflow에서 시작 모델을 불러올 수 없습니다.")
    if not models:
        notices.append("선택 가능한 시작 모델을 등록하면 자동 재학습을 켤 수 있습니다.")
    evaluations = evaluation_sets(company)
    if not evaluations:
        notices.append("상담사가 검수한 정답으로 고정 평가셋을 먼저 등록해 주세요.")
    service_pending = not connected or not settings.ARGO_WORKFLOWS_URL
    if service_pending:
        notices.append("예약은 저장할 수 있습니다. 학습은 MLflow와 Argo Workflows가 연결되면 시작합니다.")

    form = ScheduleForm(request.POST if request.method == "POST" else None, initial=policy)
    for key, choices, prompt in (("model", models, "시작 모델 선택"), ("eval_dataset", evaluations, "고정 평가셋 선택")):
        if policy.get(key) and policy[key] not in dict(choices):
            notices.append(f"기존 {prompt} 항목을 읽을 수 없습니다. 예약을 켜려면 다시 선택해 주세요.")
        form.fields[key].choices = [("", prompt), *choices]
    status = 200
    conflict = False
    if request.method == "POST":
        if form.is_valid():
            try:
                retraining.save_policy(actor, form.cleaned_data)
            except retraining.PolicyConflict as error:
                form.add_error(None, str(error))
                conflict = True
            except ValueError as error:
                form.add_error(None, str(error))
            except (OSError, KeyError):
                form.add_error(None, "학습 입력을 다시 확인할 수 없습니다. 연결을 확인한 뒤 저장해 주세요.")
            else:
                return saved_redirect("retraining-schedule")
        status = 409 if conflict else 400
    return render(request, "preferences/model_schedule.html", {
        "active": "model_training", "actor": actor, "form": form, "policy": policy,
        "trigger_summary": schedule_summary(policy), "notices": notices, "conflict": conflict,
        "service_pending": service_pending,
        "saved": request.method == "GET" and request.GET.get("saved") == "1",
    }, status=status)


GROUPS = (
    ("operations", "호출이 안정적인가?", "최근 30분"),
    ("quality", "분류 품질을 유지했는가?", "같은 고정 평가셋"),
    ("review", "상담사 수정이 줄었는가?", "최근 7일"),
    ("budget", "비용이 허용 범위에 드는가?", "최근 7일"),
)


def edit_groups(data, submitted):
    groups = []
    for group_key, title, window in GROUPS:
        fields = []
        for item in data["fields"]:
            if item["group"] != group_key:
                continue
            key = item["key"]
            value = data["policy"].get(key)
            fields.append({**item, "saved_value": value,
                           "input_value": submitted.get(key, value) if submitted is not None else value})
        if fields:
            groups.append({"id": group_key, "title": title, "window": window, "fields": fields})
    return groups


def present_evidence(data):
    datasets = {}
    metrics = []
    for row in data["rows"]:
        if row["label"] == "기존 고정 평가셋":
            datasets["stable"] = row["stable"]
        elif row["label"] == "후보 고정 평가셋":
            datasets["candidate"] = row["candidate"]
        else:
            metrics.append(row)
    rows = metrics.copy()
    if datasets:
        rows.append({"label": "고정 평가셋", "stable": "미선택", "candidate": "미선택",
                     "criterion": "두 버전 동일", "status": "참고", **datasets})

    decision = data["decision"]
    code = decision["code"]
    if code == "different_eval":
        return rows, rows[-1:] if datasets else []
    if code not in ("hold", "rollback", "inspect_baseline", "insufficient", "review_expansion"):
        return rows, []
    # Reuse the evaluator's row grades and reason. The view never grades a metric.
    statuses = ("충족",) if code == "review_expansion" else ("미달", "표본 부족", "미수집")
    for status in statuses:
        matches = [row for row in metrics if row["status"] == status]
        if code == "inspect_baseline":
            matches = [row for row in matches if "오류율" in row["label"]]
        elif code == "rollback":
            matches = [row for row in matches if "오류율" in row["label"] or "p95" in row["label"]]
        if matches:
            matches.sort(key=lambda row: row["label"] not in decision["reason"])
            if code == "review_expansion":
                main = ("고정 평가셋 Macro-F1", "상담사 수정률")
                matches.sort(key=lambda row: row["label"] not in main)
            return rows, matches[:2]
    return rows, []


def review_params(scope, domain, pair):
    params = {"scope": scope}
    if scope == "category" and domain:
        params["domain"] = domain
    if pair == "original":
        params["pair"] = pair
    return params


def original_scores(data):
    by_label = {row["label"]: row for row in data["rows"]}
    by_code = {row["label_code"]: row for row in data["rows"] if "label_code" in row}
    scores = []
    for label in data["labels"]:
        row = by_code[label["code"]]
        scores.append({
            **label, "stable": row.get("stable", "미수집"), "candidate": row.get("candidate", "미수집"),
            "status": row.get("status", "미수집") if label["enabled"] else "미적용",
        })
    return {"macro": by_label["고정 평가셋 Macro-F1"], "negative_lower": by_label.get("부정 재현율 신뢰하한"),
            "samples": f"{data['evaluation_samples']:,}", "scores": scores}


@require_http_methods(["GET", "POST"])
def review(request):
    actor = training_actor(request)
    company = actor["company_id"]
    query = request.POST if request.method == "POST" else request.GET
    scope = query.get("scope", "sentiment")
    domain = query.get("domain") or None
    pair = query.get("pair", "saved")
    action = request.POST.get("action") if request.method == "POST" else None
    if scope not in ("sentiment", "category") or pair not in ("saved", "original"):
        raise DeskError(400, "INVALID_MODEL_SCOPE", "감성 또는 세부유형 업무를 선택해 주세요.")
    error = None
    preview = False
    status = 200
    if action == "save_domains":
        domain_codes = request.POST.getlist("domains")
        try:
            model_review.save_domains(company, domain_codes, request.POST.get("domain_revision"))
        except model_review.PolicyConflict as exc:
            error, status = str(exc), 409
        except ValueError as exc:
            error, status = str(exc), 400
        else:
            if scope == "category" and domain not in domain_codes:
                scope, domain = "sentiment", None
            return saved_redirect("model-review", **review_params(scope, domain, pair), updated="domains")

    try:
        data = model_review.read(company, scope, domain=domain, pair=pair)
    except ValueError as exc:
        if action != "save_domains" or not error:
            raise DeskError(400, "INVALID_MODEL_SCOPE", str(exc)) from exc
        # Another administrator may have removed the displayed domain while the form was open.
        scope, domain, pair = "sentiment", None, "saved"
        data = model_review.read(company, scope)
    domain = data["domain"]
    before = data["decision"]
    submitted = request.POST if request.method == "POST" and action != "save_domains" else None
    if submitted is not None:
        try:
            if pair == "original":
                raise ValueError("교체 기준은 저장된 모델 비교에서 변경해 주세요.")
            if action == "save":
                model_review.save(company, scope, submitted, domain=domain)
                return saved_redirect("model-review", **review_params(scope, domain, pair))
            if action != "preview":
                raise ValueError("요청한 동작을 확인해 주세요.")
            data = model_review.preview(company, scope, submitted, domain=domain)
            preview = True
        except model_review.PolicyConflict as exc:
            error, status = str(exc), 409
        except ValueError as exc:
            error, status = str(exc), 400

    labels = []
    for item in data["labels"]:
        label = dict(item)
        if submitted is not None:
            code = label["code"]
            label.update(enabled=bool(submitted.get("enabled__" + code)),
                         min_recall=submitted.get("recall__" + code, label["min_recall"]),
                         min_samples=submitted.get("samples__" + code, label["min_samples"]),
                         max_missed=submitted.get("missed__" + code, label["max_missed"]))
        labels.append(label)
    selected = dict(data["policy"])
    if submitted is not None:
        for key in ("revision", "baseline_uri", "candidate_uri", "baseline_served", "candidate_served"):
            if key in submitted:
                selected[key] = submitted[key]
    model_names = {model["uri"]: model["label"] for model in data["models"]}
    rows, highlights = present_evidence(data)
    saved_url = "?" + urlencode(review_params(scope, domain, "saved"))
    original_url = "?" + urlencode(review_params(scope, domain, "original"))
    scope_tabs = [{
        "name": item["name"],
        "url": "?" + urlencode(review_params(item["id"], item["domain"], pair)),
        "current": item["id"] == scope and item["domain"] == domain,
    } for item in data["scopes"]]
    grafana = training.browser_url(settings.VOC_GRAFANA_URL)
    grafana_params = urlencode({"var-company": actor["company_id"], "var-scope": data["scope_key"]})
    return render(request, "preferences/model_review.html", {
        "grafana_url": f"{grafana}/d/voc-model-guard/model-guard?{grafana_params}" if grafana else "",
        "active": "model_training", "actor": actor, "data": data, "scope": scope, "labels": labels,
        "domain": domain, "pair": pair, "scope_tabs": scope_tabs,
        "original_url": original_url, "saved_url": saved_url, "current_url": original_url if pair == "original" else saved_url,
        "original_scores": original_scores(data) if pair == "original" else None,
        "rows": rows, "highlights": highlights,
        "edit_labels": request.GET.get("edit") == "labels",
        "baseline_name": model_names.get(data["policy"]["baseline_uri"]),
        "candidate_name": model_names.get(data["policy"]["candidate_uri"]),
        "groups": edit_groups(data, submitted), "selected": selected, "before": before, "preview": preview,
        "error": error, "domain_error": error if action == "save_domains" else None, "conflict": status == 409,
        "saved": request.method == "GET" and request.GET.get("saved") == "1",
        "domains_saved": request.GET.get("updated") == "domains",
    }, status=status)
