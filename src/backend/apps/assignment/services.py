import random

from django.conf import settings
from django.db import connection, transaction

from apps.tickets.action_repository import has_unsettled_reply
from config.database import fetch_rows
from config.http import DeskError

from . import repository as repo


def get_policy(cursor, company_id, staff_uuid):
    """Task 10 can read the policy with its assignment transaction cursor."""
    cursor.execute("""
        SELECT coalesce(p.is_enabled,false) AS enabled, coalesce(p.batch_size,1) AS batch_size,
               coalesce(p.refill_mode,'after_all') AS refill_mode
        FROM staff_user s LEFT JOIN staff_assignment_policy p USING(company_id,staff_user_id)
        WHERE s.company_id=%s AND s.staff_user_id=%s
    """, (company_id, staff_uuid))
    return next(iter(fetch_rows(cursor)), None)


def save_policy(cursor, company_id, staff_uuid, policy):
    cursor.execute("""
        INSERT INTO staff_assignment_policy(company_id,staff_user_id,is_enabled,batch_size,refill_mode)
        VALUES (%s,%s,%s,%s,%s)
        ON CONFLICT (company_id,staff_user_id) DO UPDATE SET
            is_enabled=excluded.is_enabled,batch_size=excluded.batch_size,refill_mode=excluded.refill_mode
    """, (company_id, staff_uuid, policy["enabled"], policy["batch_size"], policy["refill_mode"]))


def refill_team(cursor, company_id, team_id):
    """Serialize staff rotation for a team while allowing concurrent case updates."""
    policies = repo.team_policies(cursor, company_id, team_id)
    if settings.VOC_POC_RANDOM_ASSIGNMENT:
        eligible = [p for p in policies if p["active_count"] < p["batch_size"]
                    and (p["refill_mode"] == "immediate" or not p["active_count"])]
        while eligible:
            policy = random.choice(eligible)
            assigned = repo.fill_staff_batch(cursor, company_id, team_id, policy["staff_user_id"], 1)
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
            repo.fill_staff_batch(cursor, company_id, team_id, policy["staff_user_id"], slots)


def refill_after_completion(ticket_id):
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("""
            SELECT company_id,assigned_team_id FROM visible_ticket
            WHERE ticket_id=%s AND status='답변완료' AND assigned_team_id IS NOT NULL
        """, (ticket_id,))
        team = cursor.fetchone()
        if team:
            refill_team(cursor, *team)


def assignment_result(ticket, outcome):
    return {
        "outcome": outcome,
        "ticket_id": ticket["ticket_id"],
        "category_id": ticket["category_id"],
        "assigned_team_id": ticket["assigned_team_id"],
        "assignee_id": ticket["assignee_id"],
        "review_reason": ticket["category_review_reason"],
    }


def resolve_team(cursor, ticket, category_id):
    if category_id is None:
        return None, "classification_abstained"
    route = repo.category_team(cursor, ticket, category_id)
    if route is None:
        raise DeskError(422, "CATEGORY_OUTSIDE_TICKET_DOMAIN", "티켓 도메인에 속한 카테고리를 선택해 주세요.")
    return route[0], None if route[0] else "no_team_mapping"


def apply_category_result(*, company_id, event_id, preprocess_version, body_version, category_id):
    """The classification worker calls this after mapping its category label to a UUID."""
    version = (body_version, preprocess_version)
    with transaction.atomic(), connection.cursor() as cursor:
        # The Silver trigger can touch the ticket while projecting a new body version.
        current_version = repo.latest_category_version(cursor, company_id, event_id)
        ticket = repo.lock_result_ticket(cursor, company_id, event_id)
        if ticket is None:
            raise DeskError(404, "TICKET_NOT_FOUND", "티켓을 찾을 수 없습니다.")
        if ticket["is_category_manual"]:
            return assignment_result(ticket, "manual_preserved")
        if current_version != version:
            return assignment_result(ticket, "stale_result")
        applied = (ticket["category_applied_body_version"], ticket["category_applied_preprocess_version"])
        if applied == version:
            if ticket["category_id"] != category_id:
                raise DeskError(409, "CATEGORY_RESULT_CONFLICT", "같은 본문 버전의 카테고리 결과가 이미 저장됐습니다.")
            return assignment_result(ticket, "already_applied")

        team_id, review_reason = resolve_team(cursor, ticket, category_id)
        can_assign = (ticket["status"] == "미처리" and ticket["assigned_team_id"] is None
                      and ticket["assignee_id"] is None)
        new_team = team_id if can_assign else ticket["assigned_team_id"]
        ticket_id = ticket["ticket_id"]
        repo.update_category(cursor, ticket_id, category_id, new_team, ticket["assignee_id"],
                             review_reason, version)
        repo.record_event(cursor, ticket_id, "ticket_category_changed", details={
            "category_before": ticket["category_id"], "category_after": category_id,
            "team_before": ticket["assigned_team_id"], "team_after": new_team,
            "review_reason": review_reason, "body_version": body_version, "preprocess_version": preprocess_version,
        })
        if can_assign and new_team is not None:
            refill_team(cursor, company_id, new_team)
        outcome = "review_required" if review_reason else "applied"
        return assignment_result(repo.read_ticket(cursor, company_id, ticket_id), outcome)


def change_analysis(token, ticket_id, *, category_id=None, sentiment=None, team_id=None, reason="",
                    feature_snapshot_id=None, is_urgent=None):
    from apps.tickets.actions import require_assignee, write_ticket

    with write_ticket(token, ticket_id) as (cursor, actor, locked_ticket):
        require_assignee(actor, locked_ticket)
        company_id = actor["company_id"]
        ticket = repo.read_ticket(cursor, company_id, ticket_id)
        team_changed = team_id is not None and team_id != ticket["assigned_team_id"]
        if team_changed:
            if team_id not in {team["team_id"] for team in repo.team_choices(cursor, company_id)}:
                raise DeskError(422, "INVALID_TEAM", "소속 회사의 부서를 선택해 주세요.")
            if has_unsettled_reply(cursor, ticket_id):
                raise DeskError(409, "DELIVERY_UNCERTAIN", "답변 발송 결과를 확인한 뒤 담당 부서를 바꿔 주세요.")

        category_changed = category_id is not None and (
            ticket["category_id"] != category_id or not ticket["is_category_manual"]
            or ticket["category_review_reason"] is not None
        )
        if category_changed:
            categories = repo.category_choices(cursor, ticket["domain_id"])
            if category_id not in {category["category_id"] for category in categories}:
                raise DeskError(422, "CATEGORY_OUTSIDE_TICKET_DOMAIN", "티켓 도메인에 속한 카테고리를 선택해 주세요.")
            repo.save_manual_category(cursor, ticket_id, category_id)

        sentiment_changed = bool(sentiment) and sentiment != ticket["sentiment"]
        if sentiment_changed:
            repo.save_manual_sentiment(cursor, ticket_id, sentiment)
        if team_changed:
            repo.reassign_team(cursor, ticket_id, team_id)
        if not (category_changed or sentiment_changed or team_changed or is_urgent is not None):
            return assignment_result(ticket, "already_applied")
        result = repo.read_ticket(cursor, company_id, ticket_id)
        actor_id = actor["staff_user_id"]
        assignment_changes = {
            "team_before": ticket["assigned_team_id"], "team_after": result["assigned_team_id"],
            "assignee_before": ticket["assignee_id"], "assignee_after": result["assignee_id"],
        }
        if feature_snapshot_id and (category_changed or sentiment_changed or is_urgent is not None):
            cursor.execute("SELECT voc.ticket_feature_uuid(%s)", (ticket_id,))
            current_feature = cursor.fetchone()[0]
            if str(current_feature) != str(feature_snapshot_id):
                feature_snapshot_id = None
        if is_urgent is not None:
            if not feature_snapshot_id:
                raise DeskError(409, "STALE_REVIEW_FEATURE", "입력 버전이 바뀌었습니다. 검수 화면을 다시 열어 주세요.")
            if not reason.strip():
                raise DeskError(422, "REVIEW_REASON_REQUIRED", "긴급 여부를 기록할 때는 검수 사유를 적어 주세요.")
            evidence = repo.record_manual_event(
                cursor, ticket_id, "ticket_training_reviewed", actor=actor_id, reason=reason,
                details={"feature_uuid": feature_snapshot_id, "is_urgent": is_urgent},
            )
            repo.record_training_review(
                cursor, feature_snapshot_id, actor_id, sentiment=sentiment if sentiment_changed else None,
                category_id=category_id if category_id != ticket["category_id"] else None,
                urgent=is_urgent, evidence=evidence,
            )
        if category_changed:
            repo.record_event(cursor, ticket_id, "ticket_category_changed", actor=actor_id, reason=reason, details={
                **assignment_changes, "feature_uuid": feature_snapshot_id,
                "category_before": ticket["category_id"], "category_after": category_id,
                "review_reason": result["category_review_reason"],
            })
        if sentiment_changed:
            repo.record_manual_event(cursor, ticket_id, "ticket_sentiment_changed",
                                     actor=actor_id, reason=reason, details={
                                         "feature_uuid": feature_snapshot_id,
                                         "sentiment_before": ticket["sentiment"], "sentiment_after": sentiment,
                                     })
        if team_changed:
            repo.record_manual_event(cursor, ticket_id, "ticket_team_reassigned",
                                     actor=actor_id, reason=reason, details=assignment_changes)
        return assignment_result(result, "applied")
