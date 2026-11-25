from django import forms


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
