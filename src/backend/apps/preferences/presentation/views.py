import json

from django.db import connection, transaction
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from apps.accounts.presentation.views import session_token
from apps.accounts.repository import company_staff, dashboard_allowed, work_permissions
from apps.accounts.services import DASHBOARD_READ, TEAM_REPLY_EDIT, current_actor, require_settings_admin, require_work_permission
from config.http import DeskError

from .. import repository
from .forms import (
    HOME_CARD_LABELS, PROVIDERS, SIDEBAR_ITEMS, DisplayForm, HomeCardsForm, MessengerForm, QuickReplyForm, ReplySearchForm,
)


@require_GET
def index(request):
    actor = current_actor(session_token(request))
    return render(request, "preferences/index.html", {"actor": actor, "active": "settings"})


@require_http_methods(["GET", "POST"])
def display(request):
    saving = request.method == "POST"
    settings_url = reverse("preferences:index") + "#personal"
    return_url = reverse("workspace:home") if request.GET.get("from") == "home" else settings_url
    with connection.cursor() as cursor:
        actor = current_actor(session_token(request), cursor)
        defaults = [{"id": key, "visible": True} for key in HOME_CARD_LABELS]
        saved = actor["display_preferences"]
        cards = saved.get("cards", defaults)
        sidebar = saved.get("sidebar", [key for key, _ in SIDEBAR_ITEMS])
        show_analytics = dashboard_allowed(cursor, actor, DASHBOARD_READ)
        form = DisplayForm(request.POST if saving else None, initial={"cards": cards, "sidebar": sidebar})
        if saving and form.is_valid():
            if not show_analytics and ("analytics" in sidebar) != ("analytics" in form.cleaned_data["sidebar"]):
                selected = [key for key in form.cleaned_data["sidebar"] if key != "analytics"]
                if "analytics" in sidebar:
                    selected.append("analytics")
                form.cleaned_data["sidebar"] = selected
            repository.save_display(cursor, actor, form.cleaned_data)
            return redirect(return_url)
    entered = getattr(form, "cleaned_data", {})
    cards = entered.get("cards", cards)
    sidebar_selection = entered.get("sidebar", sidebar)
    card_rows = [(card, *HOME_CARD_LABELS[card["id"]]) for card in cards]
    return render(request, "preferences/display.html", {
        "actor": actor, "active": "settings", "form": form, "cards_json": json.dumps(cards), "card_rows": card_rows,
        "card_order": list(HOME_CARD_LABELS), "use_local_cards": "cards" not in saved and not saving,
        "sidebar": sidebar_selection, "sidebar_items": SIDEBAR_ITEMS, "show_analytics": show_analytics,
        "settings_url": settings_url, "return_url": return_url,
    }, status=400 if saving else 200)


@require_POST
def home_cards(request):
    with connection.cursor() as cursor:
        actor = current_actor(session_token(request), cursor)
        form = HomeCardsForm(request.POST)
        if not form.is_valid():
            raise DeskError(400, "INVALID_HOME_CARDS", "업무 홈 카드 설정을 확인해 주세요.")
        repository.save_display(cursor, actor, form.cleaned_data)
    return JsonResponse({"cards": form.cleaned_data["cards"]})


@require_GET
def replies(request):
    with connection.cursor() as cursor:
        actor = current_actor(session_token(request), cursor)
        company, staff = actor["company_uuid"], actor["staff_user_uuid"]
        categories = [(str(key), name) for key, name in repository.categories(cursor, company)]
        form = ReplySearchForm(request.GET, categories=categories)
        valid = form.is_valid()
        rows = []
        if valid:
            filters = form.cleaned_data
            rows = repository.visible_replies(cursor, actor, scope=filters["scope"] or "personal",
                                              query=filters["q"], category=filters["category"] or None)
        can_edit_team = bool(work_permissions(cursor, company, staff, (TEAM_REPLY_EDIT,)))
    return render(request, "preferences/replies.html", {
        "actor": actor, "active": "settings", "form": form, "replies": rows, "can_edit_team": can_edit_team,
    }, status=200 if valid else 400)


@require_http_methods(["GET", "POST"])
def reply(request, reply_uuid=None):
    saving = request.method == "POST"
    with transaction.atomic(), connection.cursor() as cursor:
        actor = current_actor(session_token(request), cursor)
        company = actor["company_uuid"]
        existing = None
        if reply_uuid:
            rows = repository.visible_replies(cursor, actor, reply_uuid=reply_uuid, lock=saving)
            if not rows:
                raise DeskError(404, "QUICK_REPLY_NOT_FOUND", "신속답변을 찾을 수 없습니다.")
            existing = rows[0]
            if existing["scope"] == "team":
                require_work_permission(cursor, actor, TEAM_REPLY_EDIT)
            if saving and request.POST.get("action") == "delete":
                repository.delete_reply(cursor, company, reply_uuid)
                return redirect("preferences:replies")
        categories = [(str(key), name) for key, name in repository.categories(cursor, company)]
        initial = existing or {"scope": "team" if request.GET.get("scope") == "team" else "personal"}
        form = QuickReplyForm(request.POST if saving else None, initial=initial,
                              categories=categories, editing=bool(existing))
        if not saving and not existing:
            can_edit_team = actor.get("team_uuid") and work_permissions(
                cursor, company, actor["staff_user_uuid"], (TEAM_REPLY_EDIT,),
            )
            if not can_edit_team:
                form.fields["scope"].choices = [("personal", "내 신속답변")]
                form.initial["scope"] = "personal"
        if saving and form.is_valid():
            data = form.cleaned_data
            if existing:
                saved = repository.update_reply(cursor, actor, reply_uuid, data)
            else:
                shared = data["scope"] == "team"
                team = actor.get("team_uuid") if shared else None
                if shared:
                    require_work_permission(cursor, actor, TEAM_REPLY_EDIT)
                    if team not in {str(key) for key, _ in repository.company_teams(cursor, company)}:
                        raise DeskError(400, "TEAM_REQUIRED", "소속 팀이 있어야 팀 신속답변을 등록할 수 있습니다.")
                reply_uuid = repository.create_reply(cursor, actor, data, team)
                saved = bool(reply_uuid)
            if saved:
                return redirect("preferences:reply-edit", reply_uuid=reply_uuid)
            form.add_error("category_uuid", "현재 회사에서 사용할 수 있는 카테고리를 다시 선택해 주세요.")
    return render(request, "preferences/form.html", {
        "actor": actor, "active": "settings", "title": "신속답변 편집" if existing else "신속답변 등록",
        "form": form, "back_url": "preferences:replies", "can_delete": bool(existing),
    }, status=400 if saving else 200)


@require_http_methods(["GET", "POST"])
def messenger(request):
    saving = request.method == "POST"
    with transaction.atomic(), connection.cursor() as cursor:
        actor = current_actor(session_token(request), cursor)
        require_settings_admin(actor)
        company = actor["company_uuid"]
        active_provider = repository.active_messenger(cursor, company)
        requested = (request.POST if saving else request.GET).get("provider")
        provider = requested if requested in dict(PROVIDERS) else (active_provider or "discord")
        routes = repository.messenger_routes(cursor, company, provider)
        default = next((row["channel"] for row in routes if not row["team_uuid"] and not row["staff_user_uuid"]), "")
        targets = {
            "default": [],
            "team": [(str(key), name) for key, name in repository.company_teams(cursor, company)],
            "staff": [(str(row["staff_user_uuid"]), f'{row["name"]} ({row["email"]})')
                      for row in company_staff(cursor, company)],
        }
        destination = request.POST.get("destination") if saving else None
        if saving and destination not in targets:
            raise DeskError(400, "INVALID_DESTINATION", "수신 대상 구분을 확인해 주세요.")
        forms = {}
        for kind, choices in targets.items():
            initial = {"provider": provider, "destination": kind, "channel": default if kind == "default" else ""}
            forms[kind] = MessengerForm(
                request.POST if destination == kind else None,
                destination=kind, targets=choices, initial=initial, auto_id=f"{kind}_%s",
            )
        if saving and forms[destination].is_valid():
            repository.save_messenger_route(cursor, company, forms[destination].cleaned_data)
            return redirect(f"{reverse('preferences:messenger')}?provider={provider}")
    return render(request, "preferences/messenger.html", {
        "actor": actor, "active": "settings", "provider": provider, "providers": PROVIDERS,
        "active_provider": active_provider, "forms": forms, "routes": routes,
    }, status=400 if saving else 200)
