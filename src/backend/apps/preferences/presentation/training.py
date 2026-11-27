from urllib.error import HTTPError
from uuid import uuid4

from django import forms
from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from apps.accounts.presentation.views import session_token
from apps.accounts.services import current_actor
from config.http import DeskError

from .. import training
from ..training_inputs import evaluation_sets
from ..training_status import OVERALL, points, project


class TrainingForm(forms.Form):
    as_of_at = forms.DateTimeField(
        label=f"학습 기준 시각 ({settings.TIME_ZONE})",
        widget=forms.DateTimeInput(format="%Y-%m-%dT%H:%M", attrs={"type": "datetime-local"}),
        help_text="이 시각까지 확정한 Gold 정답으로 학습셋을 만듭니다.",
    )
    model = forms.ChoiceField(label="시작 모델 버전")
    eval_dataset = forms.ChoiceField(label="고정 평가셋 버전")
    request_id = forms.UUIDField(widget=forms.HiddenInput)

    def clean_as_of_at(self):
        value = self.cleaned_data["as_of_at"]
        if value > timezone.now():
            raise forms.ValidationError("현재 시각 이전으로 지정해 주세요.")
        return value


def training_actor(request):
    actor = current_actor(session_token(request))
    if actor.get("role") != "super_admin":
        raise DeskError(403, "TRAINING_FORBIDDEN", "모델 학습 권한이 필요합니다.")
    return actor


@require_http_methods(["GET", "POST"])
def page(request):
    actor = training_actor(request)
    if request.method == "GET" and request.resolver_match.url_name == "legacy-model-training":
        return redirect("preferences:model-training")
    waiting = project({})
    waiting.update(overall=points(OVERALL, 0), label="접수중")
    context = {"actor": actor, "active": "model_training", "training_snapshot": waiting}
    template = "preferences/model_training.html"
    if not settings.MLFLOW_TRACKING_URI or not settings.ARGO_WORKFLOWS_URL:
        return render(request, template, {
            **context, "notice": "MLflow와 Argo Workflows를 연결하면 학습을 시작할 수 있습니다.",
        }, status=503 if request.method == "POST" else 200)

    try:
        models = training.available_models(actor["company_uuid"])
    except (OSError, ValueError, KeyError):
        return render(request, template, {
            **context, "notice": "MLflow에서 모델 버전을 가져오지 못했습니다.",
        }, status=503)
    evaluations = evaluation_sets(actor["company_uuid"])
    form = TrainingForm(request.POST if request.method == "POST" else None, initial={
        "as_of_at": timezone.localtime(), "request_id": uuid4(),
    })
    form.fields["model"].choices = [("", "시작 모델 선택"), *models]
    form.fields["eval_dataset"].choices = [("", "고정 평가셋 선택"), *evaluations]
    context.update(form=form, can_submit=bool(models and evaluations))

    if not models:
        context["notice"] = "학습에 사용할 수 있는 모델 버전이 없습니다."
    elif not evaluations:
        context["notice"] = "확정된 고정 평가셋이 없습니다."
    if request.method == "GET":
        return render(request, template, context)
    if not form.is_valid():
        return render(request, template, context, status=400)
    try:
        workflow_name = training.submit(actor, form.cleaned_data)
    except training.TrainingBusy as error:
        form.add_error(None, str(error))
        return render(request, template, context, status=409)
    except (OSError, ValueError, KeyError) as error:
        message = "Argo에서 실행을 확인하지 못했습니다. 같은 입력으로 다시 시도해 주세요."
        if isinstance(error, HTTPError) and error.code in (401, 403):
            message = "Argo 제출 인증에 실패했습니다. 서버의 Argo 토큰과 권한을 확인해 주세요."
        form.add_error(None, message)
        return render(request, template, context, status=503)
    response = redirect("preferences:training-detail", workflow_name=workflow_name)
    response.status_code = 303
    return response


@require_GET
def recent(request):
    actor = training_actor(request)
    try:
        rows = training.recent_workflows(actor["company_uuid"])
    except (OSError, ValueError, KeyError):
        return JsonResponse({"error": "최근 실행을 조회하지 못했습니다."}, status=503)
    for row in rows:
        row["url"] = reverse("preferences:training-detail", kwargs={"workflow_name": row["name"]})
    return JsonResponse({"runs": rows})


@require_GET
def detail(request, workflow_name):
    actor = training_actor(request)
    if not training.WORKFLOW_NAME.fullmatch(workflow_name):
        raise DeskError(404, "TRAINING_RUN_NOT_FOUND", "학습 실행을 찾을 수 없습니다.")
    try:
        snapshot = training.snapshot(workflow_name, actor["company_uuid"])
    except (OSError, ValueError, KeyError):
        snapshot = None
    return render(request, "preferences/model_training.html", {
        "actor": actor, "active": "model_training", "workflow_name": workflow_name if snapshot else "",
        "run_name": workflow_name, "training_snapshot": snapshot,
    })


@require_GET
def status(request, workflow_name):
    actor = training_actor(request)
    try:
        return JsonResponse(training.snapshot(workflow_name, actor["company_uuid"]))
    except (OSError, ValueError, KeyError):
        return JsonResponse({"error": "상태 조회 지연"}, status=503)


@require_POST
def stop(request, workflow_name):
    try:
        actor = training_actor(request)
        state = training.request_stop(actor, workflow_name)
    except DeskError as exc:
        return JsonResponse({"error": exc.message, "code": exc.code}, status=exc.status)
    except (OSError, ValueError, KeyError):
        return JsonResponse({"error": "학습 실행을 확인하지 못했습니다. 다시 시도해 주세요."}, status=503)
    return JsonResponse({"stop": state}, status=202 if state["state"] == "requested" else 200)
