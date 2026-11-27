from uuid import UUID

from django import forms
from django.db import DatabaseError
from django.shortcuts import render
from django.views.decorators.http import require_GET

from apps.accounts.presentation.views import session_token
from apps.accounts.services import current_actor, require_settings_admin

from .. import lineage


class LineageSearchForm(forms.Form):
    view = forms.ChoiceField(choices=(("flow", "데이터 흐름"), ("event", "이벤트 추적"), ("model", "모델 학습 이력")))
    q = forms.CharField(label="티켓 번호 또는 이벤트 UUID", max_length=36, required=False)
    page = forms.IntegerField(min_value=1, max_value=100000, initial=1)

    def clean_q(self):
        query = self.cleaned_data["q"]
        number = query.removeprefix("#")
        if number.isascii() and number.isdigit() and len(number) <= 8:
            return number.zfill(8)
        if query:
            try:
                return str(UUID(query))
            except ValueError:
                raise forms.ValidationError("티켓 번호 또는 올바른 이벤트 UUID를 입력해 주세요.") from None
        return query


@require_GET
def page(request):
    actor = current_actor(session_token(request))
    is_operator = actor.get("role") == "super_admin"
    if not is_operator:
        require_settings_admin(actor)
    form = LineageSearchForm({"view": request.GET.get("view", "flow"),
                              "q": request.GET.get("q", ""), "page": request.GET.get("page", "1"),
                              "company": request.GET.get("company", "")})
    context = {"actor": actor, "active": "settings", "form": form, "flow": lineage.FLOW}
    if is_operator:
        try:
            choices = [(row["id"], row["name"]) for row in lineage.companies()]
        except DatabaseError:
            return render(request, "preferences/lineage.html", {
                **context, "notice": "고객사 목록을 조회할 수 없습니다. 데이터 연결 상태를 확인해 주세요.",
            }, status=503)
        form.fields["company"] = forms.ChoiceField(label="고객사", required=False,
                                                   choices=[("", "고객사 선택"), *choices])
    status = 200
    if form.is_valid():
        selection = form.cleaned_data
        context.update(selection)
        company = selection.get("company") if is_operator else actor["company_uuid"]
        context["previous_page"] = selection["page"] - 1
        context["next_page"] = selection["page"] + 1
        try:
            if is_operator and not company and selection["view"] != "flow":
                context["notice"] = "조회할 고객사를 선택해 주세요."
            elif selection["view"] == "event":
                context.update(lineage.event_trace(company, selection["q"], selection["page"]))
            elif selection["view"] == "model":
                context.update(lineage.model_history(company, selection["page"]))
        except DatabaseError:
            context["notice"] = "리니지 데이터를 조회할 수 없습니다. 데이터 연결 및 스키마 준비 상태를 확인해 주세요."
            status = 503
    else:
        status = 400
    return render(request, "preferences/lineage.html", context, status=status)
