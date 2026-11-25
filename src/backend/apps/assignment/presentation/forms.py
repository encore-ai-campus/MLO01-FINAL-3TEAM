from django import forms

from apps.tickets.presentation.forms import SENTIMENTS


class AssignmentPolicyForm(forms.Form):
    enabled = forms.BooleanField(label="자동 배정 사용", required=False)
    batch_size = forms.IntegerField(
        label="배정 건수", min_value=1, max_value=2147483647,
        widget=forms.NumberInput(attrs={"list": "assignment-batch-sizes"}),
        help_text="1, 5, 10 중에서 선택하거나 건수를 직접 입력하세요.",
    )
    refill_mode = forms.ChoiceField(label="재배정 시점", choices=(
        ("immediate", "소진 즉시 보충"),
        ("after_all", "전체 소진 후 재배정"),
    ))


class CategoryRouteForm(forms.Form):
    category_uuid = forms.UUIDField()
    team_uuid = forms.UUIDField(required=False)


class CategoryChangeForm(forms.Form):
    category_uuid = forms.UUIDField()
    reason = forms.CharField(max_length=1000, required=False)


class AnalysisChangeForm(CategoryChangeForm):
    category_uuid = forms.UUIDField(required=False)
    sentiment = forms.ChoiceField(choices=[(value, value) for value in SENTIMENTS], required=False)
    team_uuid = forms.UUIDField(required=False)


class CategoryResultForm(forms.Form):
    company_uuid = forms.UUIDField()
    event_id = forms.UUIDField()
    preprocess_version = forms.IntegerField(min_value=1, max_value=2147483647)
    body_version = forms.IntegerField(min_value=1, max_value=2147483647)
    category_uuid = forms.UUIDField(required=False)
