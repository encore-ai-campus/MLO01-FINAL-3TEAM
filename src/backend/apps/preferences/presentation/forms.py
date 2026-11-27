from django import forms

from ..navigation import SIDEBAR_ITEMS

HOME_CARD_LABELS = {
    "tickets": ("내게 할당된 티켓", "오늘 배정된 티켓과 처리가 남은 티켓"),
    "requests": ("받은 요청", "나에게 들어온 검토 요청"),
    "performance": ("내 실적", "오늘과 전날의 처리 건수"),
}
REPLY_SCOPES = (("personal", "내 신속답변"), ("team", "팀 신속답변"))
PROVIDERS = (("slack", "Slack"), ("discord", "Discord"))


class HomeCardsForm(forms.Form):
    cards = forms.JSONField(widget=forms.HiddenInput)

    def clean_cards(self):
        cards = self.cleaned_data["cards"]
        if (not isinstance(cards, list) or len(cards) != len(HOME_CARD_LABELS)
                or any(not isinstance(card, dict) or card.keys() != {"id", "visible"}
                       or not isinstance(card["id"], str) or card["id"] not in HOME_CARD_LABELS
                       or type(card["visible"]) is not bool for card in cards)
                or len({card["id"] for card in cards}) != len(HOME_CARD_LABELS)):
            raise forms.ValidationError("업무 홈 카드의 표시 항목과 순서를 확인해 주세요.")
        return cards


class DisplayForm(HomeCardsForm):
    sidebar = forms.MultipleChoiceField(label="좌측 패널 표시 항목", choices=SIDEBAR_ITEMS, required=False)


class ReplySearchForm(forms.Form):
    scope = forms.ChoiceField(label="사용 범위", choices=REPLY_SCOPES, required=False)
    q = forms.CharField(label="이름 검색", max_length=80, required=False)
    category = forms.ChoiceField(label="카테고리", required=False)

    def __init__(self, *args, categories, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].choices = [("", "전체"), *categories]


class QuickReplyForm(forms.Form):
    name = forms.CharField(label="이름", max_length=80)
    category_id = forms.ChoiceField(label="카테고리")
    scope = forms.ChoiceField(label="사용 범위", choices=REPLY_SCOPES)
    body = forms.CharField(label="답변 내용", max_length=2000, widget=forms.Textarea(attrs={"rows": 6}))

    def __init__(self, *args, categories, editing, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category_id"].choices = [("", "선택"), *categories]
        self.fields["scope"].disabled = editing


class MessengerForm(forms.Form):
    provider = forms.ChoiceField(label="메신저", choices=PROVIDERS)
    destination = forms.ChoiceField(widget=forms.HiddenInput, choices=(
        ("default", "기본"), ("team", "팀"), ("staff", "직원"),
    ))
    target = forms.ChoiceField(required=False, label="수신 대상")
    channel = forms.CharField(label="수신 채널", max_length=100, required=False)

    def __init__(self, *args, destination, targets=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["destination"].initial = destination
        self.fields["provider"].widget = forms.HiddenInput()
        if destination == "default":
            self.fields["target"].widget = forms.HiddenInput()
        else:
            self.fields["target"].required = True
            self.fields["target"].choices = [("", "선택"), *targets]
