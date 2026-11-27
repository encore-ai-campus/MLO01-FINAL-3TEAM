"""Company-local scheduling over the same Gold inputs and Argo submission as manual training."""
import logging
import re
from datetime import datetime, time, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from config.http import DeskError
from . import training, training_inputs


SEOUL = ZoneInfo("Asia/Seoul")
DEFAULTS = dict(enabled=False, trigger_mode="either", interval_days=7, min_new_gold=100,
                window_start=time(2), window_end=time(5), model="", eval_dataset="")
logger = logging.getLogger(__name__)


class PolicyConflict(ValueError):
    pass


def quiet_window(now, start, end):
    """Return the opening instant of the current window, including a window crossing midnight."""
    local = now.astimezone(SEOUL)
    clock = local.time()
    opened = datetime.combine(local.date(), start, SEOUL)
    if start < end:
        return opened if start <= clock < end else None
    if clock >= start:
        return opened
    return opened - timedelta(days=1) if clock < end else None


def due(policy, now, cutoff, count):
    if not cutoff or not count:
        return False
    period = now >= cutoff + timedelta(days=policy["interval_days"])
    volume = count >= policy["min_new_gold"]
    return {"period":period, "volume":volume, "either":period or volume,
            "both":period and volume}[policy["trigger_mode"]]


def _one(cursor):
    row = cursor.fetchone()
    return dict(zip((col.name for col in cursor.description), row)) if row is not None else None


def _policy(cursor, company, *, lock=False):
    query = "SELECT * FROM voc.retraining_policy WHERE company_uuid=%s"
    cursor.execute(query + (" FOR UPDATE" if lock else ""), (company,))
    return _one(cursor)


def _last(cursor, company):
    cursor.execute("""SELECT d.*, s.state AS stop_state FROM voc.retraining_dispatch d
        LEFT JOIN voc.training_stop_request s USING (company_uuid,workflow_name)
        WHERE d.company_uuid=%s ORDER BY d.cutoff DESC LIMIT 1""", (company,))
    return _one(cursor)


def _completed_cutoff(cursor, company, until):
    cursor.execute("""SELECT max(r.label_cutoff) FROM voc.training_run r JOIN voc.model_version v USING(workflow_ref)
        JOIN voc.training_dataset d ON d.dataset_uuid=r.train_dataset_uuid
        WHERE d.company_uuid=%s AND r.label_cutoff<=%s""", (company,until))
    return cursor.fetchone()[0]


def _watermark(cursor, policy, until):
    # A stop suppresses automatic resubmission of the same Gold. The data remains eligible
    # for training. If natural completion wins the race, its actual completion cutoff applies.
    company = policy["company_uuid"]
    cursor.execute("""SELECT max(requested_at) FROM voc.training_stop_request
        WHERE company_uuid=%s AND state IN ('requested','stopped') AND requested_at<=%s""", (company,until))
    stop_cutoff = cursor.fetchone()[0]
    times = [policy["counting_since"], _completed_cutoff(cursor, company, until), stop_cutoff]
    return max((value for value in times if value is not None), default=None)


def _new_gold(cursor, policy, since, until):
    if since is None:
        return 0
    # Gold for two heads on the same input contributes one training example. Exclude the
    # frozen evaluation conversations/texts, as the training dataset builder does.
    cursor.execute("""WITH frozen AS MATERIALIZED (
          SELECT s.feature_uuid,s.source_conversation,lower(btrim(regexp_replace(s.body,'\\s+',' ','g'))) AS body
          FROM voc.training_dataset d JOIN voc.training_dataset_item i USING(dataset_uuid)
          JOIN voc.feature_snapshot s USING(feature_uuid)
          WHERE d.company_uuid=%s AND d.dataset_version=%s AND d.purpose='eval'
        )
        SELECT count(DISTINCT f.feature_uuid) FROM voc.gold_label g JOIN voc.feature_snapshot f USING(feature_uuid)
        WHERE f.company_uuid=%s AND g.confirmed_at>%s AND g.confirmed_at<=%s
          AND g.label_origin='counselor' AND f.available_at<=g.as_of_at
          AND (g.label_kind<>'category' OR EXISTS(SELECT FROM public.category c
               WHERE c.category_uuid=g.category_uuid AND c.domain_uuid=f.domain_uuid))
          AND NOT EXISTS (SELECT FROM frozen e WHERE e.feature_uuid=f.feature_uuid
               OR (e.source_conversation<>'' AND e.source_conversation=f.source_conversation)
               OR e.body=lower(btrim(regexp_replace(f.body,'\\s+',' ','g'))))""",
        (policy["company_uuid"], policy["eval_dataset"], policy["company_uuid"], since, until))
    return cursor.fetchone()[0]


def _status(policy, last, cutoff, count, now):
    if not policy["enabled"]:
        return "예약 꺼짐"
    if last and last["state"] in ("pending","running"):
        if last["stop_state"] == "requested":
            return "중지 요청 중"
        return last["note"] or ("학습 진행 중" if last["state"]=="running" else "학습 제출 대기")
    opened = quiet_window(now,policy["window_start"],policy["window_end"])
    if last and last["state"]=="failed" and last["revision"]==policy["revision"] and (
            not opened or last["finished_at"] >= opened):
        return "학습 실패 · 다음 허용 시간대에 재시도"
    if not count:
        return "신규 검수 데이터 대기"
    if not due(policy, now, cutoff, count):
        return "기간·데이터 조건 대기"
    return "학습 제출 대기" if opened else "허용 시간대 대기"


def read_policy(company, now=None):
    now = now or timezone.now()
    with connection.cursor() as cursor:
        policy = _policy(cursor,company)
        if not policy:
            return {**DEFAULTS, "revision":0, "last_cutoff":None, "last_workflow":None,
                    "new_gold":0, "status":"예약 꺼짐"}
        last = _last(cursor,company)
        cutoff = _watermark(cursor,policy,now)
        count = _new_gold(cursor,policy,cutoff,now)
    return {**policy, "last_cutoff":cutoff, "new_gold":count,
            "last_workflow":last["workflow_name"] if last and last["argo_uid"] else None,
            "status":_status(policy,last,cutoff,count,now)}


def _validated(current, data):
    values = {field:data.get(field,current[field]) for field in DEFAULTS}
    if not isinstance(values["enabled"], bool) or values["trigger_mode"] not in ("period","volume","either","both"):
        raise ValueError("예약 사용 여부와 실행 조건을 확인해 주세요.")
    for field,limit in (("interval_days",365),("min_new_gold",1000000)):
        if type(values[field]) is not int or not 1 <= values[field] <= limit:
            raise ValueError("기간은 1~365일, 신규 검수량은 1~1,000,000건으로 입력해 주세요.")
    for field in ("window_start","window_end"):
        value = values[field]
        if isinstance(value,str):
            value = time.fromisoformat(value)
        if not isinstance(value,time) or value.tzinfo or value.second or value.microsecond:
            raise ValueError("한국시간을 시와 분으로 입력해 주세요.")
        values[field] = value
    if values["window_start"] == values["window_end"]:
        raise ValueError("허용 시간대의 시작과 종료 시각을 다르게 지정해 주세요.")
    values["model"] = values["model"] or ""
    values["eval_dataset"] = values["eval_dataset"] or ""
    if values["enabled"] and (not re.fullmatch(r"models:/[A-Za-z0-9_.-]+/[1-9][0-9]*", values["model"]) or not values["eval_dataset"]):
        raise ValueError("예약 학습에 사용할 시작 모델과 고정 평가셋을 선택해 주세요.")
    return values


def save_policy(actor, data):
    if actor.get("role") != "super_admin":
        raise DeskError(403,"TRAINING_FORBIDDEN","모델 학습 권한이 필요합니다.")
    company = actor["company_uuid"]
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended('voc-retraining-policy/'||%s::text,0))",(company,))
        old = _policy(cursor,company,lock=True)
        revision = old["revision"] if old else 0
        if data.get("revision") != revision:
            raise PolicyConflict("다른 관리자가 설정을 수정했습니다. 최신 값을 다시 확인해 주세요.")
        values = _validated(old or DEFAULTS,data)
        if values["enabled"]:
            if not settings.MLFLOW_TRACKING_URI or values["model"] not in dict(training.available_models(company)):
                raise ValueError("MLflow에 등록되어 가중치를 읽을 수 있는 시작 모델을 선택해 주세요.")
            if not training_inputs.evaluation_sets(company, values["eval_dataset"]):
                raise ValueError("저장소에서 검증된 회사의 고정 평가셋을 선택해 주세요.")
        since = old["counting_since"] if old else None
        if values["enabled"] and not since:
            now = timezone.now()
            since = _completed_cutoff(cursor,company,now) or now
        cursor.execute("""INSERT INTO voc.retraining_policy
            (company_uuid,requested_by,revision,counting_since,enabled,trigger_mode,interval_days,min_new_gold,
             window_start,window_end,model,eval_dataset)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (company_uuid) DO UPDATE SET
             (requested_by,revision,counting_since,enabled,trigger_mode,interval_days,min_new_gold,window_start,window_end,model,eval_dataset)=
             (EXCLUDED.requested_by,EXCLUDED.revision,EXCLUDED.counting_since,EXCLUDED.enabled,EXCLUDED.trigger_mode,
              EXCLUDED.interval_days,EXCLUDED.min_new_gold,EXCLUDED.window_start,EXCLUDED.window_end,EXCLUDED.model,EXCLUDED.eval_dataset)""",
            (company,actor["staff_user_uuid"],revision+1,since,*(values[k] for k in DEFAULTS)))
    return read_policy(company)


def _reserve(company,now):
    with transaction.atomic(), connection.cursor() as cursor:
        policy = _policy(cursor,company,lock=True)
        if not policy:
            return None
        last = _last(cursor,company)
        if last and last["state"] in ("pending","running"):
            return last["request_id"]
        opened = quiet_window(now,policy["window_start"],policy["window_end"])
        if not policy["enabled"] or not opened:
            return None
        if last and last["state"]=="failed" and last["revision"]==policy["revision"] and last["finished_at"]>=opened:
            return None
        cutoff = _watermark(cursor,policy,now)
        if not due(policy,now,cutoff,_new_gold(cursor,policy,cutoff,now)):
            return None
        request = uuid4()
        actor = {"company_uuid":company, "staff_user_uuid":policy["requested_by"]}
        name = training.workflow_name(actor,{"request_id":request})
        cursor.execute("""INSERT INTO voc.retraining_dispatch
            (request_id,company_uuid,revision,requested_by,model,eval_dataset,cutoff,workflow_name,state)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'pending')""",
            (request,company,policy["revision"],policy["requested_by"],policy["model"],policy["eval_dataset"],now,name))
        return request


def _set_dispatch(cursor, req, state, note, *, uid=None, finished=None):
    cursor.execute("""UPDATE voc.retraining_dispatch SET state=%s,note=%s,argo_uid=coalesce(%s,argo_uid),finished_at=%s
        WHERE request_id=%s""",(state,note,uid,finished,req))


def _record_remote(cursor, request, flow, now):
    phase = flow.get("status",{}).get("phase")
    state = {"Succeeded":"succeeded","Failed":"failed","Error":"failed"}.get(phase,"running")
    remote_uid = flow["metadata"]["uid"]
    if training.stop_outcome(flow) == "stopped":
        cursor.execute("SELECT 1 FROM voc.model_version WHERE workflow_ref=%s", (remote_uid,))
        state = "succeeded" if cursor.fetchone() else "stopped"
    note = "학습 중단" if state == "stopped" else ""
    _set_dispatch(cursor,request["request_id"],state,note,uid=remote_uid,
                  finished=now if state!="running" else None)
    if state in ("succeeded", "stopped"):
        # This also covers a stop initiated directly in Argo, for which there is no TiCats audit.
        cursor.execute("""UPDATE voc.retraining_policy SET counting_since=greatest(counting_since,%s)
            WHERE company_uuid=%s""",(request["cutoff"],request["company_uuid"]))
    return state


def _reconcile_stop_requests(company):
    if not settings.ARGO_WORKFLOWS_URL:
        return
    with connection.cursor() as cursor:
        cursor.execute("""SELECT workflow_name FROM voc.training_stop_request
            WHERE company_uuid=%s AND state='requested' ORDER BY requested_at""", (company,))
        names = [row[0] for row in cursor.fetchall()]
    for name in names:
        try:
            training.reconcile_stop_request(name, company)
        except (OSError, ValueError, KeyError, DeskError):
            logger.warning("학습 중지 상태를 다시 확인합니다. workflow=%s", name)


def process_company(company, now=None):
    """Reserve a durable idempotency key before any network operation."""
    # Manual runs may have a pending stop even if the company never configured a schedule.
    _reconcile_stop_requests(company)
    req = _reserve(company, now or timezone.now())
    if not req:
        return "waiting"
    with transaction.atomic(), connection.cursor() as cursor:
        policy = _policy(cursor,company,lock=True)
        cursor.execute("""SELECT d.*, s.state AS stop_state, s.workflow_uid AS stop_uid
            FROM voc.retraining_dispatch d LEFT JOIN voc.training_stop_request s USING (company_uuid,workflow_name)
            WHERE d.request_id=%s FOR UPDATE OF d""",(req,))
        request = _one(cursor)
        if request["state"] not in ("pending","running"):
            return request["state"]
        if not settings.ARGO_WORKFLOWS_URL:
            _set_dispatch(cursor,req,request["state"],"Argo 연결 대기")
            return "unavailable"
        try:
            try:
                flow = training.get_workflow(request["workflow_name"],company)
            except DeskError as error:
                if error.status != 404:
                    raise
                flow = None
            moment = now or timezone.now()
            expected_uid = request["argo_uid"] or request["stop_uid"]
            if flow and (expected_uid is None or flow["metadata"]["uid"] == expected_uid):
                return _record_remote(cursor,request,flow,moment)
            if request["stop_state"]:
                # A request from the desk is only created for a workflow that actually existed.
                # Never resubmit its name after Argo retention removes the workflow.
                stop_state, uid = request["stop_state"], request["stop_uid"]
                cursor.execute("SELECT 1 FROM voc.model_version WHERE workflow_ref=%s",(uid,))
                if cursor.fetchone():
                    return _record_remote(cursor,request,{"metadata":{"uid":uid},"status":{"phase":"Succeeded"}},moment)
                if stop_state == "stopped":
                    _set_dispatch(cursor,req,"stopped","학습 중단",uid=uid,finished=moment)
                    return "stopped"
                if stop_state == "completed":
                    _set_dispatch(cursor,req,"failed","실행 종료 · 등록 모델 없음",uid=uid,finished=moment)
                    return "failed"
                _set_dispatch(cursor,req,"cancelled","실행 기록 보관 종료 · 최종 상태 확인 불가",uid=uid,finished=moment)
                return "cancelled"
            if request["state"]=="running":
                # Argo may have removed a completed workflow under its retention policy.
                cursor.execute("SELECT 1 FROM voc.model_version WHERE workflow_ref=%s",(request["argo_uid"],))
                phase = "Succeeded" if cursor.fetchone() else "Failed"
                return _record_remote(cursor,request,{"metadata":{"uid":request["argo_uid"]},"status":{"phase":phase}},moment)
            cursor.execute("""SELECT 1 FROM public.staff_user WHERE staff_user_uuid=%s AND company_uuid=%s
                AND role='super_admin'""",(request["requested_by"],company))
            if not cursor.fetchone() or not policy["enabled"] or policy["revision"]!=request["revision"]:
                _set_dispatch(cursor,req,"cancelled","예약 설정 또는 담당자 권한 변경",finished=moment)
                return "cancelled"
            if not quiet_window(moment,policy["window_start"],policy["window_end"]):
                _set_dispatch(cursor,req,"pending","허용 시간대 대기")
                return "waiting"
            if not settings.MLFLOW_TRACKING_URI or request["model"] not in dict(training.available_models(company)):
                _set_dispatch(cursor,req,"pending","등록된 시작 모델 연결 대기")
                return "unavailable"
            if not training_inputs.evaluation_sets(company, request["eval_dataset"]):
                _set_dispatch(cursor, req, "pending", "회사의 고정 평가셋 확인 필요")
                return "unavailable"
            # Fresh data may have been consumed by a manual run while this request was waiting.
            watermark = _watermark(cursor,policy,moment)
            if not due(policy,request["cutoff"],watermark,_new_gold(cursor,policy,watermark,request["cutoff"])):
                _set_dispatch(cursor,req,"cancelled","최근 학습 완료 후 조건 재계산",finished=moment)
                return "cancelled"
            checked_at = now or timezone.now()
            opening = quiet_window(checked_at,policy["window_start"],policy["window_end"])
            if opening is None:
                _set_dispatch(cursor,req,"pending","허용 시간대 대기")
                return "waiting"
            closing = datetime.combine(opening.date(),policy["window_end"],SEOUL)
            if closing <= opening:
                closing += timedelta(days=1)
            actor = {"company_uuid":company,"staff_user_uuid":request["requested_by"],"role":"super_admin",
                     "scheduled":True,"submit_before":closing}
            selection = {"request_id":req,"as_of_at":request["cutoff"],"model":request["model"],"eval_dataset":request["eval_dataset"]}
            training.submit(actor,selection)
            # Retain pending until a successful read supplies the authoritative Argo uid.
            _set_dispatch(cursor,req,"pending","Argo 제출 완료 · 실행 확인 중")
            return "submitted"
        except training.TrainingBusy:
            _set_dispatch(cursor,req,request["state"],"기존 학습 종료 대기")
            return "busy"
        except (OSError,ValueError,KeyError,DeskError):
            logger.warning("예약 학습 외부 서비스 확인 지연 (request=%s)",req)
            _set_dispatch(cursor,req,request["state"],"외부 서비스 응답 대기 · 동일 요청으로 재확인")
            return "unavailable"
