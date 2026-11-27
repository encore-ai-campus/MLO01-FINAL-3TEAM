import random

from django.conf import settings
from django.db import connection, transaction

from apps.tickets.action_repository import has_unsettled_reply
from config.database import fetch_rows
from config.http import DeskError

from . import repository as repo


def get_policy(cursor, company_uuid, staff_uuid):
    """Task 10 can read the policy with its assignment transaction cursor."""
    cursor.execute("""
        SELECT coalesce(p.enabled,false) AS enabled, coalesce(p.batch_size,1) AS batch_size,
               coalesce(p.refill_mode,'after_all') AS refill_mode
        FROM staff_user s LEFT JOIN staff_assignment_policy p USING(company_uuid,staff_user_uuid)
        WHERE s.company_uuid=%s AND s.staff_user_uuid=%s
    """, (company_uuid, staff_uuid))
    return next(iter(fetch_rows(cursor)), None)


def save_policy(cursor, company_uuid, staff_uuid, policy):
    cursor.execute("""
        INSERT INTO staff_assignment_policy(company_uuid,staff_user_uuid,enabled,batch_size,refill_mode)
        VALUES (%s,%s,%s,%s,%s)
        ON CONFLICT (company_uuid,staff_user_uuid) DO UPDATE SET
            enabled=excluded.enabled,batch_size=excluded.batch_size,refill_mode=excluded.refill_mode
    """, (company_uuid, staff_uuid, policy["enabled"], policy["batch_size"], policy["refill_mode"]))


def refill_team(cursor, company_uuid, team_uuid):
    """Serialize staff rotation for a team while allowing concurrent case updates."""
    policies = repo.team_policies(cursor, company_uuid, team_uuid)
    if settings.VOC_POC_RANDOM_ASSIGNMENT:
        eligible = [p for p in policies if p["active_count"] < p["batch_size"]
                    and (p["refill_mode"] == "immediate" or not p["active_count"])]
        while eligible:
            policy = random.choice(eligible)
            assigned = repo.fill_staff_batch(cursor, company_uuid, team_uuid, policy["staff_user_uuid"], 1)
            if not assigned:
                break
            policy["active_count"] += 1
            if policy["active_count"] == policy["batch_size"]:
                eligible.remove(policy)
        return
    for policy in policies:
        active = policy["active_count"]
        if policy["refill_mode"] == "after_all" and active:
            continue
        slots = policy["batch_size"] - active
        if slots > 0:
            repo.fill_staff_batch(cursor, company_uuid, team_uuid, policy["staff_user_uuid"], slots)


def refill_after_completion(ticket_uuid):
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("""
            SELECT company_uuid,assigned_team_uuid FROM visible_ticket
            WHERE ticket_uuid=%s AND status='답변완료' AND assigned_team_uuid IS NOT NULL
        """, (ticket_uuid,))
        team = cursor.fetchone()
        if team:
            refill_team(cursor, *team)


def assignment_result(ticket, outcome):
    return {
        "outcome": outcome,
        "ticket_uuid": ticket["ticket_uuid"],
        "category_uuid": ticket["category_uuid"],
        "assigned_team_uuid": ticket["assigned_team_uuid"],
        "assignee_uuid": ticket["assignee_uuid"],
        "review_reason": ticket["category_review_reason"],
    }


def resolve_team(cursor, ticket, category_uuid):
    if category_uuid is None:
        return None, "classification_abstained"
    route = repo.category_team(cursor, ticket, category_uuid)
    if route is None:
        raise DeskError(422, "CATEGORY_OUTSIDE_TICKET_DOMAIN", "티켓 도메인에 속한 카테고리를 선택해 주세요.")
    return route[0], None if route[0] else "no_team_mapping"


def apply_category_result(*, company_uuid, event_id, preprocess_version, body_version, category_uuid):
    """The classification worker calls this after mapping its category label to a UUID."""
    version = (body_version, preprocess_version)
    with transaction.atomic(), connection.cursor() as cursor:
        # The Silver trigger can touch the ticket while projecting a new body version.
        current_version = repo.latest_category_version(cursor, company_uuid, event_id)
        ticket = repo.lock_result_ticket(cursor, company_uuid, event_id)
        if ticket is None:
            raise DeskError(404, "TICKET_NOT_FOUND", "티켓을 찾을 수 없습니다.")
        if ticket["category_manually_set"]:
            return assignment_result(ticket, "manual_preserved")
        if current_version != version:
            return assignment_result(ticket, "stale_result")
        applied = (ticket["category_applied_body_version"], ticket["category_applied_preprocess_version"])
        if applied == version:
            if ticket["category_uuid"] != category_uuid:
                raise DeskError(409, "CATEGORY_RESULT_CONFLICT", "같은 본문 버전의 카테고리 결과가 이미 저장됐습니다.")
            return assignment_result(ticket, "already_applied")

        team_uuid, review_reason = resolve_team(cursor, ticket, category_uuid)
        can_assign = (ticket["status"] == "미처리" and ticket["assigned_team_uuid"] is None
                      and ticket["assignee_uuid"] is None)
        new_team = team_uuid if can_assign else ticket["assigned_team_uuid"]
        ticket_uuid = ticket["ticket_uuid"]
        repo.update_category(cursor, ticket_uuid, category_uuid, new_team, ticket["assignee_uuid"],
                             review_reason, version)
        repo.record_event(cursor, ticket_uuid, "ticket_category_changed", details={
            "category_before": ticket["category_uuid"], "category_after": category_uuid,
            "team_before": ticket["assigned_team_uuid"], "team_after": new_team,
            "review_reason": review_reason, "body_version": body_version, "preprocess_version": preprocess_version,
        })
        if can_assign and new_team is not None:
            refill_team(cursor, company_uuid, new_team)
        outcome = "review_required" if review_reason else "applied"
        return assignment_result(repo.read_ticket(cursor, company_uuid, ticket_uuid), outcome)


def change_analysis(token, ticket_uuid, *, category_uuid=None, sentiment=None, team_uuid=None, reason="",
                    feature_uuid=None, is_urgent=None):
    from apps.tickets.actions import require_assignee, write_ticket

    with write_ticket(token, ticket_uuid) as (cursor, actor, locked_ticket):
        require_assignee(actor, locked_ticket)
        company_uuid = actor["company_uuid"]
        ticket = repo.read_ticket(cursor, company_uuid, ticket_uuid)
        team_changed = team_uuid is not None and team_uuid != ticket["assigned_team_uuid"]
        if team_changed:
            if team_uuid not in {team["team_uuid"] for team in repo.team_choices(cursor, company_uuid)}:
                raise DeskError(422, "INVALID_TEAM", "소속 회사의 부서를 선택해 주세요.")
            if has_unsettled_reply(cursor, ticket_uuid):
                raise DeskError(409, "DELIVERY_UNCERTAIN", "답변 발송 결과를 확인한 뒤 담당 부서를 바꿔 주세요.")

        category_changed = category_uuid is not None and (
            ticket["category_uuid"] != category_uuid or not ticket["category_manually_set"]
            or ticket["category_review_reason"] is not None
        )
        if category_changed:
            categories = repo.category_choices(cursor, ticket["domain_uuid"])
            if category_uuid not in {category["category_uuid"] for category in categories}:
                raise DeskError(422, "CATEGORY_OUTSIDE_TICKET_DOMAIN", "티켓 도메인에 속한 카테고리를 선택해 주세요.")
            repo.save_manual_category(cursor, ticket_uuid, category_uuid)

        sentiment_changed = bool(sentiment) and sentiment != ticket["sentiment"]
        if sentiment_changed:
            repo.save_manual_sentiment(cursor, ticket_uuid, sentiment)
        if team_changed:
            repo.reassign_team(cursor, ticket_uuid, team_uuid)
        if not (category_changed or sentiment_changed or team_changed or is_urgent is not None):
            return assignment_result(ticket, "already_applied")
        result = repo.read_ticket(cursor, company_uuid, ticket_uuid)
        actor_uuid = actor["staff_user_uuid"]
        assignment_changes = {
            "team_before": ticket["assigned_team_uuid"], "team_after": result["assigned_team_uuid"],
            "assignee_before": ticket["assignee_uuid"], "assignee_after": result["assignee_uuid"],
        }
        if feature_uuid and (category_changed or sentiment_changed or is_urgent is not None):
            cursor.execute("SELECT voc.ticket_feature_uuid(%s)", (ticket_uuid,))
            current_feature = cursor.fetchone()[0]
            if str(current_feature) != str(feature_uuid):
                feature_uuid = None
        if is_urgent is not None:
            if not feature_uuid:
                raise DeskError(409, "STALE_REVIEW_FEATURE", "입력 버전이 바뀌었습니다. 검수 화면을 다시 열어 주세요.")
            if not reason.strip():
                raise DeskError(422, "REVIEW_REASON_REQUIRED", "긴급 여부를 기록할 때는 검수 사유를 적어 주세요.")
            evidence = repo.record_manual_event(
                cursor, ticket_uuid, "ticket_training_reviewed", actor=actor_uuid, reason=reason,
                details={"feature_uuid": feature_uuid, "is_urgent": is_urgent},
            )
            repo.record_training_review(
                cursor, feature_uuid, actor_uuid, sentiment=sentiment if sentiment_changed else None,
                category_uuid=category_uuid if category_uuid != ticket["category_uuid"] else None,
                urgent=is_urgent, evidence=evidence,
            )
        if category_changed:
            repo.record_event(cursor, ticket_uuid, "ticket_category_changed", actor=actor_uuid, reason=reason, details={
                **assignment_changes, "feature_uuid": feature_uuid,
                "category_before": ticket["category_uuid"], "category_after": category_uuid,
                "review_reason": result["category_review_reason"],
            })
        if sentiment_changed:
            repo.record_manual_event(cursor, ticket_uuid, "ticket_sentiment_changed",
                                     actor=actor_uuid, reason=reason, details={
                                         "feature_uuid": feature_uuid,
                                         "sentiment_before": ticket["sentiment"], "sentiment_after": sentiment,
                                     })
        if team_changed:
            repo.record_manual_event(cursor, ticket_uuid, "ticket_team_reassigned",
                                     actor=actor_uuid, reason=reason, details=assignment_changes)
        return assignment_result(result, "applied")
