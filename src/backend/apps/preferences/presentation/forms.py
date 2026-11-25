from django import forms

HOME_CARDS = ("tickets", "requests", "performance")
SIDEBAR_ITEMS = (
    ("home", "업무 홈"), ("tickets", "티켓 목록"), ("inbox", "받은 요청"),
    ("followup", "팔로업"), ("analytics", "분석 대시보드"),
)
REPLY_SCOPES = (("personal", "내 신속답변"), ("team", "팀 신속답변"))
PROVIDERS = (("slack", "Slack"), ("discord", "Discord"))


class SidebarForm(forms.Form):
    sidebar = forms.MultipleChoiceField(
        label="좌측 패널 표시 항목", choices=SIDEBAR_ITEMS, required=False, widget=forms.CheckboxSelectMultiple,
    )


class HomeCardsForm(forms.Form):
    cards = forms.JSONField()

    def clean_cards(self):
        cards = self.cleaned_data["cards"]
        if (not isinstance(cards, list) or len(cards) != len(HOME_CARDS)
                or any(not isinstance(card, dict) or card.keys() != {"id", "visible"}
                       or card["id"] not in HOME_CARDS or type(card["visible"]) is not bool for card in cards)
                or len({card["id"] for card in cards}) != len(HOME_CARDS)):
            raise forms.ValidationError("업무 홈 카드의 표시 항목과 순서를 확인해 주세요.")
        return cards


class ReplySearchForm(forms.Form):
    scope = forms.ChoiceField(label="사용 범위", choices=REPLY_SCOPES, required=False)
    q = forms.CharField(label="이름 검색", max_length=80, required=False)
    category = forms.ChoiceField(label="카테고리", required=False)

    def __init__(self, *args, categories, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].choices = [("", "전체"), *categories]


class QuickReplyForm(forms.Form):
    name = forms.CharField(label="이름", max_length=80)
    category_uuid = forms.ChoiceField(label="카테고리")
    scope = forms.ChoiceField(label="사용 범위", choices=REPLY_SCOPES)
    body = forms.CharField(label="답변 내용", max_length=2000, widget=forms.Textarea(attrs={"rows": 6}))

    def __init__(self, *args, categories, editing, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category_uuid"].choices = [("", "선택"), *categories]
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
