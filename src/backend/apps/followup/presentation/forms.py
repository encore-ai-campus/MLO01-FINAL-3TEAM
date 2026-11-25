from django import forms
from django.utils import timezone


class ContactQueryForm(forms.Form):
    state = forms.ChoiceField(choices=(("scheduled", "예정"), ("completed", "완료")), required=False)
    source = forms.ChoiceField(choices=(("ticket", "티켓"), ("chat", "1:1 문의")), required=False)
    q = forms.CharField(max_length=100, required=False)


class ContactForm(forms.Form):
    ticket_uuid = forms.UUIDField(required=False)
    chat_uuid = forms.UUIDField(required=False)
    assignee_uuid = forms.UUIDField()
    reason = forms.CharField(max_length=1000)
    scheduled_at = forms.DateTimeField()

    def clean(self):
        data = super().clean()
        if bool(data.get("ticket_uuid")) == bool(data.get("chat_uuid")):
            raise forms.ValidationError("원본 티켓과 상담 중 하나를 선택해 주세요.")
        return data


class CompleteForm(forms.Form):
    contacted_at = forms.DateTimeField()
    result = forms.CharField(max_length=1000)


class TrackScheduleForm(forms.Form):
    ticket_uuid = forms.UUIDField()
    scheduled_at = forms.DateTimeField()

    def clean_scheduled_at(self):
        scheduled = self.cleaned_data["scheduled_at"]
        if scheduled <= timezone.now():
            raise forms.ValidationError("현재보다 늦은 추적 날짜와 시각을 입력해 주세요.")
        return scheduled
