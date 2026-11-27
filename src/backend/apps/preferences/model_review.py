"""Company/domain-specific replacement advice. Reading or saving a policy never changes routing."""
import json
import math
from decimal import Decimal, InvalidOperation

from .model_domains import STANDARD_LABELS, PolicyConflict, company_domains, lock_selection, save_domains


# key, label, unit, group, minimum, maximum, step, default
RULES = (
    ("min_calls","모델별 최소 완료 호출","건","operations",1,1000000,1,100),
    ("error_limit","모델별 오류율 상한","%","operations",0,100,0.01,1),
    ("latency_limit","기존 모델 대비 후보 p95 상한","배","operations",0.01,100,0.01,1.5),
    ("absolute_p95","후보 p95 절대 상한","ms","operations",1,3600000,1,None),
    ("min_f1_gain_pp","최소 Macro-F1 개선 폭","%p","quality",-100,100,0.01,0),
    ("min_negative","최소 부정 정답 표본","건","quality",1,1000000,1,120),
    ("min_lower","부정 재현율 단측 95% 신뢰하한","%","quality",0,100,0.01,82.64),
    ("min_review","모델별 최소 상담사 검수량","건","review",1,1000000,1,100),
    ("min_gain_pp","최소 상담사 수정률 개선 폭","%p","review",-100,100,0.01,2),
    ("cost_limit","후보 추론 비용 상한","원/1,000건","budget",0,1000000000,0.01,None),
)
IDENTITIES = ("baseline_uri","candidate_uri","baseline_served","candidate_served")
SENTIMENT = {"negative":"부정","neutral":"중립","positive":"긍정"}
LABEL_FLOORS = {"negative":90,"wrong_delivery":90,"item_defective":90,"card_loss":95,
                "food_foreign_object":95,"transfer_inquiry":90,"order_item_missing":90,"payment_failed":90}


def number(value, label, low, high, step, optional=False):
    if value in (None,"") and optional:
        return None
    try:
        n = Decimal(str(value))
        if not n.is_finite() or not Decimal(str(low)) <= n <= Decimal(str(high)) or n % Decimal(str(step)):
            raise ValueError
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{label} 값을 {low}~{high} 범위와 단위에 맞게 입력해 주세요.") from None
    return int(n) if step == 1 else float(n)


def fields(scope):
    return [dict(key=k,label=name,unit=u,group=g,min=lo,max=hi,step=step,optional=default is None,default=default)
            for k,name,u,g,lo,hi,step,default in RULES if scope=="sentiment" or k not in ("min_lower","min_negative")]


def as_object(value):
    return json.loads(value) if isinstance(value,str) else (value or {})


def catalog(cur, company, scope, domain_code=None):
    if scope not in ("sentiment","category"):
        raise ValueError("지원하는 평가 업무를 선택해 주세요.")
    options, revision = company_domains(cur, company)
    cur.execute("""SELECT to_regclass('voc.classification_job') IS NOT NULL,
                          to_regclass('voc.classification_category_map') IS NOT NULL,statement_timestamp()""")
    jobs_ready, map_ready, as_of = cur.fetchone()
    domain, code, scope_name, key = None, "", "공통 감성", "sentiment"
    names = SENTIMENT
    if scope == "category":
        code = domain_code or next((k for k,v in options.items() if v["enabled"]), "")
        if code not in options or not options[code]["enabled"]:
            raise ValueError("회사에서 사용하는 도메인을 선택해 주세요.")
        domain = options[code]["uuid"]
        scope_name = options[code]["name"] + " 세부유형"
        key = f"category/{domain}"
        names = dict(STANDARD_LABELS.get(code, {}))
        if map_ready:
            cur.execute("""SELECT coalesce(m.label,c.category_id::text),c.name FROM public.category c
                LEFT JOIN voc.classification_category_map m USING(domain_id,category_id)
                WHERE c.domain_id=%s ORDER BY c.name,c.category_id,m.label""",(domain,))
        else:
            cur.execute("SELECT category_id::text,name FROM public.category WHERE domain_id=%s ORDER BY name,category_id",(domain,))
        names.update(cur.fetchall())
    versions = registered_versions(cur, company, scope, domain)
    return dict(scope=scope,domain=domain,code=code,scope_name=scope_name,jobs_ready=jobs_ready,
                map_ready=map_ready,names=names,versions=versions,as_of=as_of,key=key,
                domains=list(options.values()),domain_revision=revision)


def selected_counts(raw, scope, domain):
    by_domain = as_object(raw)
    if scope == "category":
        return by_domain.get(str(domain), {}).get("category", {})
    combined = {}
    if scope == "sentiment":
        for tasks in by_domain.values():
            for label, values in tasks.get("sentiment", {}).items():
                total = combined.setdefault(label, {"n": 0, "tp": 0, "predicted": 0})
                for metric in total:
                    total[metric] += values[metric]
    return combined


def registered_versions(cur, company, scope, domain):
    cur.execute("""SELECT v.artifact_ref,v.model_name,v.registry_version,r.eval_dataset_id,r.sentiment_macro_f1,
        r.category_macro_f1,r.review_counts,e.dataset_version FROM voc.model_version v
        JOIN voc.training_run r USING(workflow_ref) JOIN voc.training_dataset t ON t.dataset_id=r.train_dataset_id
        JOIN voc.training_dataset e ON e.dataset_id=r.eval_dataset_id
        WHERE t.company_id=%s AND e.company_id=t.company_id AND e.finalized_at IS NOT NULL
        UNION ALL
        SELECT 'models:/' || x.model_name || '/' || x.registry_version,x.model_name,x.registry_version,
               x.eval_dataset_id,NULL,NULL,x.review_counts,e.dataset_version
        FROM voc.external_model_evaluation x JOIN voc.training_dataset e ON e.dataset_id=x.eval_dataset_id
        WHERE x.company_id=%s AND e.company_id=x.company_id AND e.purpose='eval' AND e.finalized_at IS NOT NULL
          AND NOT EXISTS (SELECT FROM voc.model_version v JOIN voc.training_run r USING(workflow_ref)
              JOIN voc.training_dataset t ON t.dataset_id=r.train_dataset_id
              WHERE t.company_id=x.company_id AND v.model_name=x.model_name AND v.registry_version=x.registry_version)
        ORDER BY 2,3 DESC""", (company, company))
    return {uri:dict(uri=uri,label=f"{model} · v{version}",dataset=str(ds),dataset_name=ds_name,
                         counts=selected_counts(counts,scope,domain),
                         overall=sent if scope=="sentiment" else cat)
                for uri,model,version,ds,sent,cat,counts,ds_name in cur.fetchall()}


ORIGINAL_MODELS = {"klue_bert_v1": "원본 KLUE-BERT v1", "ax_encoder": "원본 A.X"}


def original_versions(cur, company, task):
    cur.execute("""SELECT model_code,row_count,macro_f1,label_counts,split_sha256,report_sha256
        FROM voc.model_review_benchmark WHERE company_id=%s AND task_code=%s""", (company,task))
    versions = {}
    for model, n, f1, labels, split, report in cur.fetchall():
        uri = f"benchmark:{model}"
        versions[uri] = dict(uri=uri,label=ORIGINAL_MODELS[model],counts=as_object(labels),overall=f1,
                             dataset=f"{task}/{report}/{split}",dataset_name=f"원본 고정 평가셋 · {n:,}건",
                             original=True,row_count=n)
    return versions


def policy_state(cur, company, ctx):
    cur.execute("SELECT revision,settings FROM voc.model_review_policy WHERE company_id=%s AND scope_key=%s",
                (company,ctx["key"]))
    row = cur.fetchone()
    saved = as_object(row[1]) if row else {}
    values = {f["key"]:saved.get(f["key"],f["default"]) for f in fields(ctx["scope"])}
    values.update({key:saved.get(key,"") for key in IDENTITIES})
    values["revision"] = row[0] if row else 0
    labels = []
    for code,name in ctx["names"].items():
        rule = {"enabled":True,"min_recall":LABEL_FLOORS.get(code,85),"min_samples":100,"max_missed":None}
        rule.update(saved.get("labels",{}).get(code,{}))
        labels.append(dict(code=code,name=name,**rule))
    return values,labels


def draft_values(post, ctx, values, labels):
    for field in fields(ctx["scope"]):
        key = field["key"]
        values[key] = number(post.get(key,values[key]),field["label"],field["min"],field["max"],
                             field["step"],field["optional"])
    for key in IDENTITIES:
        value = str(post.get(key,values[key])).strip()
        if key.endswith("_uri") and value and value not in ctx["versions"]:
            raise ValueError("현재 회사에서 등록한 모델 버전을 선택해 주세요.")
        if key.endswith("_served") and (len(value)>100 or any(ord(c)<32 for c in value)):
            raise ValueError("서빙 버전은 100자 이내로 입력해 주세요.")
        values[key] = value
    for suffix in ("uri","served"):
        if values["baseline_"+suffix] and values["baseline_"+suffix]==values["candidate_"+suffix]:
            raise ValueError("기존 모델과 후보 모델의 버전을 구분해 주세요.")
    for rule in labels:
        code=rule["code"]
        if post.get("labels_present"):
            rule["enabled"]=str(post.get("enabled__"+code,"")).lower() in ("1","on","true")
        for prefix,key,low,high,step,optional in (
            ("recall","min_recall",0,100,0.01,False),("samples","min_samples",1,1000000,1,False),
            ("missed","max_missed",0,1000000,1,True)):
            rule[key]=number(post.get(prefix+"__"+code,rule[key]),rule["name"],low,high,step,optional)


def lane_stats(cur, company, ctx, values):
    stats = {"stable":{}, "canary":{}}
    if not ctx["jobs_ready"] or not all(values[k] for k in ("baseline_served","candidate_served")):
        return stats
    args = dict(company=company,domain=ctx["domain"],task=ctx["scope"],at=ctx["as_of"],
                stable=values["baseline_served"],canary=values["candidate_served"])
    # The seven-day cost cohort and 30-minute HTTP cohort use the same tenant, domain, route and cutoff.
    cur.execute("""WITH attempts AS (
        SELECT j.event_id,target.route,target.model_version,a.finished_at,a.outcome,a.served_version,
            (to_jsonb(a)->>'http_ms')::float AS http_ms,(to_jsonb(a)->>'cost_krw')::numeric AS cost,
            a.started_at>%(at)s::timestamptz-interval '30 minutes' AS recent,
            target.model_version=CASE target.route WHEN 'stable' THEN %(stable)s ELSE %(canary)s END AS selected
        FROM voc.classification_job j JOIN voc.classification_attempt a USING(request_id)
        JOIN public.ticket t ON t.company_id=j.company_id AND t.source_event_id=j.event_id
        JOIN voc.bronze_message b USING(event_id)
        CROSS JOIN LATERAL (SELECT coalesce(to_jsonb(a)->>'assigned_route',j.route) AS route,
            coalesce(to_jsonb(a)->>'assigned_version',j.model_version) AS model_version) target
        WHERE j.company_id=%(company)s AND (%(domain)s::uuid IS NULL OR t.domain_id=%(domain)s::uuid)
          AND j.analysis_kind=%(task)s
          AND a.started_at>%(at)s::timestamptz-interval '7 days' AND a.started_at<=%(at)s
          AND b.context_json->>'synthetic' IS DISTINCT FROM 'true' AND b.origin IS DISTINCT FROM 'synthetic-canary'
    ), completed AS (
        SELECT * FROM attempts WHERE selected AND finished_at<=%(at)s
    ), sequences AS (
        SELECT event_id,outcome,count(*) FILTER(WHERE outcome='succeeded') OVER (ORDER BY finished_at
            RANGE UNBOUNDED PRECEDING) AS seq FROM completed WHERE recent AND route='canary'
    ), runs AS (SELECT count(DISTINCT event_id) AS n FROM sequences WHERE outcome IN ('failed','expired') GROUP BY seq)
    SELECT route,count(*) FILTER(WHERE recent AND selected AND finished_at<=%(at)s) AS calls,
        count(*) FILTER(WHERE recent AND selected AND finished_at<=%(at)s AND outcome IN ('failed','expired')) AS errors,
        count(http_ms) FILTER(WHERE recent AND selected AND finished_at<=%(at)s) AS timed,
        percentile_cont(.95) WITHIN GROUP (ORDER BY http_ms) FILTER(WHERE recent AND selected AND finished_at<=%(at)s) AS p95,
        count(*) FILTER(WHERE recent AND selected AND (finished_at IS NULL OR finished_at>%(at)s)) AS pending,
        bool_or(recent AND (NOT selected OR (finished_at<=%(at)s AND served_version IS NOT NULL
            AND served_version<>model_version))) AS mixed,
        CASE WHEN count(cost) FILTER(WHERE selected AND finished_at<=%(at)s AND
            (served_version IS NULL OR served_version=model_version))=count(*) FILTER(WHERE selected AND finished_at<=%(at)s)
            THEN avg(cost) FILTER(WHERE selected AND finished_at<=%(at)s)*1000 END AS cost,
        coalesce((SELECT max(n) FROM runs),0) FROM attempts GROUP BY route""",args)
    for route,calls,errors,timed,p95,pending,mixed,cost,run in cur.fetchall():
        stats[route].update(calls=calls,errors=errors,error_pct=100*errors/calls if calls else None,
                            p95=p95 if timed==calls else None,pending=pending,mixed=mixed,
                            cost=float(cost) if cost is not None else None,streak=run if route=="canary" else 0)
    # Compare the actual model answer with the first final counselor label for that same input version.
    # category_id is stable across renames; j.label is the domain-specific serving code.
    category_join = ("LEFT JOIN voc.classification_category_map m ON m.domain_id=f.domain_id AND m.label=j.label"
                     if ctx["scope"]=="category" and ctx["map_ready"] else "")
    if ctx["scope"]=="category" and not category_join:
        return stats
    mismatch = ("(CASE WHEN j.label='매우 부정' THEN '부정' ELSE j.label END) IS DISTINCT FROM g.sentiment_value"
                if ctx["scope"]=="sentiment" else "m.category_id IS DISTINCT FROM g.category_id")
    cur.execute(f"""WITH reviews AS (
        SELECT DISTINCT ON (j.request_id) j.route,{mismatch} AS corrected
        FROM voc.feature_snapshot_label g JOIN voc.feature_snapshot f USING(feature_snapshot_id)
        JOIN voc.classification_job j ON j.company_id=f.company_id AND j.event_id=f.silver_event_id
            AND j.body_version=f.body_version AND j.preprocess_version=f.preprocess_version AND j.analysis_kind=g.label_kind
        JOIN voc.bronze_message b ON b.event_id=j.event_id {category_join}
        WHERE f.company_id=%(company)s AND (%(domain)s::uuid IS NULL OR f.domain_id=%(domain)s::uuid)
          AND g.label_kind=%(task)s AND g.label_origin='counselor'
          AND g.confirmed_at>%(at)s::timestamptz-interval '7 days' AND g.confirmed_at<=%(at)s
          AND j.state='succeeded' AND j.applied AND j.finished_at<=g.as_of_at AND g.reviewer_id IS NOT NULL
          AND j.model_version=CASE j.route WHEN 'stable' THEN %(stable)s ELSE %(canary)s END
          AND b.context_json->>'synthetic' IS DISTINCT FROM 'true' AND b.origin IS DISTINCT FROM 'synthetic-canary'
        ORDER BY j.request_id,g.confirmed_at,g.feature_snapshot_label_id)
        SELECT route,count(*),100.0*count(*) FILTER(WHERE corrected)/count(*) FROM reviews GROUP BY route""",args)
    for route,n,rate in cur.fetchall():
        stats[route].update(reviews=n,correction=float(rate))
    return stats


def fmt(value, unit=""):
    return "미수집" if value is None else f"{value:,.2f}".rstrip("0").rstrip(".")+unit


def lower_recall(data):
    if not data or not data["n"]:
        return None
    n, z = data["n"], 1.6448536269514722
    p = data["tp"]/n
    return 100*(p+z*z/(2*n)-z*math.sqrt(p*(1-p)/n+z*z/(4*n*n)))/(1+z*z/n)


def domain_f1(model):
    if not model:
        return None
    if model.get("original"):
        return model["overall"]*100
    if model["counts"]:
        scores = [2*x["tp"]/(x["n"]+x["predicted"]) for x in model["counts"].values() if x["n"]+x["predicted"]]
        return sum(scores)/len(scores)*100 if scores else None
    if model.get("single_domain") and model["overall"] is not None:
        return model["overall"]*100
    return None


def assess(values, labels, versions, stats, scope, *, offline=False):
    rows = []
    findings = []
    def check(label, a, b, goal, result, *, unit="",required=True):
        status = "선택" if not required else ("미수집" if result is None else ("충족" if result else "미달"))
        rows.append(dict(label=label,stable=fmt(a,unit),candidate=fmt(b,unit),criterion=goal,status=status))
        if required and result is not True:
            findings.append((label,status))
    def verdict(code,title,reason):
        return dict(code=code,title=title,reason=reason),rows

    base, cand = versions.get(values["baseline_uri"]), versions.get(values["candidate_uri"])
    paired = bool(base and cand and base["dataset"]==cand["dataset"])
    b,c = stats["stable"],stats["canary"]
    calls_b,calls_c = b.get("calls"),c.get("calls")
    baseline_ready = (calls_b or 0) >= values["min_calls"]
    candidate_ready = (calls_c or 0) >= values["min_calls"]
    volume_ok = baseline_ready and candidate_ready
    check("최근 30분 완료 호출",calls_b,calls_c,f"각 {values['min_calls']}건 이상",
          volume_ok if calls_b is not None and calls_c is not None else None,unit="건")
    eb,ec = b.get("error_pct"),c.get("error_pct")
    baseline_incident = baseline_ready and eb is not None and eb > values["error_limit"]
    candidate_incident = candidate_ready and ec is not None and ec > values["error_limit"]
    errors_ok = None
    if baseline_incident or candidate_incident:
        errors_ok = False
    elif volume_ok and None not in (eb, ec):
        errors_ok = True
    check("최근 30분 오류율",eb,ec,f"각 {values['error_limit']}% 이하",
          errors_ok,unit="%")
    pb,pc = b.get("p95"),c.get("p95")
    ratio = pc/pb if pb and pc is not None else None
    check("HTTP 응답시간 p95",pb,pc,f"기존 모델의 {values['latency_limit']}배 이하",
          ratio<=values["latency_limit"] if volume_ok and ratio is not None else None,unit="ms")
    absolute=values["absolute_p95"]
    check("후보 p95 절대 상한",pb,pc,fmt(absolute,"ms 이하") if absolute is not None else "미설정",
          pc<=absolute if absolute is not None and candidate_ready and pc is not None else None,unit="ms",required=absolute is not None)
    quality_start = len(rows)
    f1b,f1c=domain_f1(base),domain_f1(cand)
    check("고정 평가셋 Macro-F1",f1b,f1c,f"{values['min_f1_gain_pp']}%p 이상 개선",
          f1c-f1b+1e-9>=values["min_f1_gain_pp"] if paired and None not in (f1b,f1c) else None,unit="%")
    counts_b=base["counts"] if base else {}
    counts_c=cand["counts"] if cand else {}
    if scope=="sentiment":
        nb,nc=counts_b.get("negative",{}),counts_c.get("negative",{})
        lo=lower_recall(nc)
        sufficient = paired and nb.get("n") == nc.get("n") and nc.get("n",0) >= values["min_negative"]
        check("부정 재현율 신뢰하한",lower_recall(nb),lo,f"정답 {values['min_negative']}건 · 하한 {values['min_lower']}% 이상",
              lo>=values["min_lower"] if sufficient and lo is not None else None,unit="%")
        if paired and 0 < nc.get("n",0) < values["min_negative"]:
            rows[-1]["status"]="표본 부족"
    for rule in labels:
        if not rule["enabled"] and not offline:
            continue
        old,new=counts_b.get(rule["code"],{}),counts_c.get(rule["code"],{})
        recall=lambda v: 100*v["tp"]/v["n"] if v.get("n") else None
        rb,rc=recall(old),recall(new)
        criterion=f"{rule['min_samples']}건 이상 · 재현율 {rule['min_recall']}% 이상"
        max_missed=rule["max_missed"]
        if max_missed is not None:
            criterion+=f" · 누락 {max_missed}건 이하"
        grade=None
        if paired and old.get("n") and old.get("n")==new.get("n") and new["n"]>=rule["min_samples"]:
            grade=rc+1e-9>=rule["min_recall"] and (max_missed is None or new["n"]-new["tp"]<=max_missed)
        check(rule["name"]+" 재현율",rb,rc,criterion,grade,unit="%")
        rows[-1]["label_code"] = rule["code"]
        for key,rec,data in (("stable",rb,old),("candidate",rc,new)):
            if rec is not None:
                rows[-1][key]+=f" ({data['tp']}/{data['n']}건)"
        if grade is None and paired and new.get("n",0)>0 and new["n"]<rule["min_samples"]:
            rows[-1]["status"]="표본 부족"
        if not rule["enabled"]:
            rows[-1].update(status="미적용", criterion="미적용")
    quality_failures = [row["label"] for row in rows[quality_start:] if row["status"] == "미달"]
    if offline:
        rows = rows[quality_start:]
        if not paired:
            return verdict("different_eval", "동일 평가셋 필요", "두 모델이 평가된 원본 표본이 일치하지 않습니다.")
        if quality_failures:
            return verdict("offline_hold", "품질 기준 미달", "기준 미달 · " + ", ".join(quality_failures))
        insufficient = [row["label"] for row in rows if row["status"] == "표본 부족"]
        if insufficient:
            return verdict("offline_insufficient", "평가 표본 부족", "최소 표본 미달 · " + ", ".join(insufficient))
        if any(row["status"] == "미수집" for row in rows):
            return verdict("offline_missing", "추가 평가 필요", "선택한 기준을 검증할 평가 결과가 더 필요합니다.")
        return verdict("offline_pass", "품질 기준 충족", "원본 고정 평가셋에서 적용한 품질 기준을 모두 충족했습니다.")
    rvb,rvc=b.get("reviews"),c.get("reviews")
    check("최근 7일 상담사 검수",rvb,rvc,f"각 {values['min_review']}건 이상",
          min(rvb,rvc)>=values["min_review"] if None not in (rvb,rvc) else None,unit="건")
    mb,mc=b.get("correction"),c.get("correction")
    check("상담사 수정률",mb,mc,f"{values['min_gain_pp']}%p 이상 개선",
          mb-mc+1e-9>=values["min_gain_pp"] if None not in (rvb,rvc,mb,mc) and min(rvb,rvc)>=values["min_review"] else None,unit="%")
    cap=values["cost_limit"]
    cost=c.get("cost")
    check("1,000호출당 추론 비용",b.get("cost"),cost,fmt(cap,"원 이하") if cap is not None else "미설정",
          cost<=cap if None not in (cost,cap) else None,unit="원",required=cap is not None)
    if not base or not cand:
        return verdict("select_pair","비교 모델 선택","같은 회사에서 등록한 기존 모델과 후보 버전을 선택해 주세요.")
    if not all(values[k] for k in ("baseline_served","candidate_served")):
        if paired and quality_failures:
            return verdict("hold", "기존 모델 유지", "고정 평가셋에서 기준 미달 · " + ", ".join(quality_failures))
        return verdict("map_serving","서빙 버전 연결 필요","배포 시 지정한 두 모델의 실제 서빙 버전을 연결해 주세요.")
    if b.get("mixed") or c.get("mixed"):
        return verdict("mixed_versions","배포 버전 점검","현재 30분 구간의 배정 버전과 선택한 서빙 버전이 일치하는지 확인해 주세요.")
    if c.get("streak",0)>=3:
        return verdict("stop_consecutive_failures","복귀 권고","후보의 서로 다른 문의 3건 이상이 연속 실패했습니다.")
    if baseline_incident:
        return verdict("inspect_baseline","기존 모델 점검","기존 모델의 오류율이 설정한 상한을 초과했습니다.")
    if candidate_ready and (candidate_incident or
                      (baseline_ready and ratio is not None and ratio>values["latency_limit"]) or
                      (pc is not None and absolute is not None and pc>absolute)):
        return verdict("rollback","복귀 권고","후보 모델이 운영 오류율 또는 응답시간 상한을 초과했습니다.")
    if not paired:
        return verdict("different_eval","동일 평가셋 필요","두 등록 버전의 고정 평가셋이 달라 품질 개선을 판정할 수 없습니다.")
    failures = [name for name,status in findings if status=="미달" and name not in ("최근 30분 완료 호출","최근 7일 상담사 검수")]
    if failures:
        return verdict("hold","기존 모델 유지","기준 미달 · "+", ".join(failures))
    if not labels and scope=="category":
        return verdict("missing_taxonomy","검토 대기","회사 도메인의 세부유형 매핑을 확인해 주세요.")
    if findings or b.get("pending") or c.get("pending"):
        reason=", ".join(name for name,status in findings[:3]) or "진행 중인 호출"
        return verdict("insufficient","추가 검증 필요","측정값과 표본 확인 · "+reason)
    return verdict("review_expansion","교체 검토 권고","설정한 품질·운영·상담사 검수 기준을 모두 충족했습니다. 운영 배포를 검토해 주세요.")


def result(cur, company, ctx, values, labels, pair):
    versions = ctx["versions"]
    original = original_versions(cur, company, ctx["code"] or "sentiment")
    original_available = len(original) == len(ORIGINAL_MODELS)
    if pair == "original":
        if not original_available:
            raise ValueError("이 회사와 업무에 연결한 원본 v1·A.X 평가 결과가 없습니다.")
        versions = original
        values = {**values, "baseline_uri": "benchmark:klue_bert_v1", "candidate_uri": "benchmark:ax_encoder",
                  "baseline_served": "", "candidate_served": ""}
    # Older runs only recorded company-wide F1. It is usable for a single domain if all evaluation
    # examples came from that domain. Per-label counts can only come from newly instrumented runs.
    for uri in (values["baseline_uri"],values["candidate_uri"]):
        model=versions.get(uri)
        if model and not model["counts"] and not model.get("original"):
            if ctx["scope"] == "sentiment":
                model["single_domain"] = True  # The stored aggregate already covers the whole company.
                continue
            cur.execute("""SELECT bool_and(f.domain_id=%s) FROM voc.training_dataset_item i
                    JOIN voc.feature_snapshot f USING(feature_snapshot_id) WHERE i.dataset_id=%s""",
                        (ctx["domain"], model["dataset"]))
            model["single_domain"]=cur.fetchone()[0] is True
    stats = {"stable": {}, "canary": {}} if pair == "original" else lane_stats(cur,company,ctx,values)
    decision,rows=assess(values,labels,versions,stats,ctx["scope"],offline=pair=="original")
    for key in ("baseline_uri","candidate_uri"):
        model=versions.get(values[key])
        if model:
            rows.insert(0,dict(label="기존 고정 평가셋" if key=="baseline_uri" else "후보 고정 평가셋",
                stable=model["dataset_name"] if key=="baseline_uri" else "—",
                candidate=model["dataset_name"] if key=="candidate_uri" else "—",criterion="두 버전 동일",status="참고"))
    domains = [{key:d[key] for key in ("code","name","enabled")} for d in ctx["domains"]]
    scopes = [dict(id="sentiment",domain="",name="공통 감성")]
    scopes.extend(dict(id="category",domain=d["code"],name=d["name"]+" 세부유형") for d in domains if d["enabled"])
    samples = versions[values["baseline_uri"]]["row_count"] if pair == "original" else None
    return dict(scope=ctx["scope"],scope_key=ctx["key"],scope_name=ctx["scope_name"],scopes=scopes,domain=ctx["code"],
                domains=domains,domain_revision=ctx["domain_revision"],pair=pair,
                original_available=original_available,evaluation_samples=samples,
                policy=values,fields=fields(ctx["scope"]),models=[dict(uri=k,label=v["label"]) for k,v in versions.items()],
                labels=labels,decision=decision,rows=rows,as_of=ctx["as_of"])


def review(company,scope,post,write,domain=None,pair="saved"):
    from django.db import connection, transaction

    with transaction.atomic(),connection.cursor() as cur:
        if pair not in ("saved", "original") or (pair == "original" and post is not None):
            raise ValueError("지원하는 모델 비교를 선택해 주세요.")
        if write:
            lock_selection(cur, company, shared=True)
        ctx=catalog(cur,company,scope,domain)
        if write:
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",(f"model-policy/{company}/{ctx['key']}",))
        values,labels=policy_state(cur,company,ctx)
        if post is not None:
            if write and str(values["revision"])!=str(post.get("revision","")):
                raise PolicyConflict("다른 관리자가 기준을 변경했습니다. 최신 기준을 다시 확인해 주세요.")
            draft_values(post,ctx,values,labels)
        if write:
            settings={k:v for k,v in values.items() if k!="revision"}
            settings["labels"]={r["code"]:{k:v for k,v in r.items() if k not in ("code","name")} for r in labels}
            cur.execute("""INSERT INTO voc.model_review_policy(company_id,scope_key,revision,settings) VALUES (%s,%s,1,%s::jsonb)
                ON CONFLICT(company_id,scope_key) DO UPDATE SET settings=excluded.settings,
                    revision=voc.model_review_policy.revision+1,updated_at=statement_timestamp() RETURNING revision""",
                        (company,ctx["key"],json.dumps(settings)))
            values["revision"]=cur.fetchone()[0]
        return result(cur,company,ctx,values,labels,pair)


def read(company_id,scope="sentiment",domain=None,pair="saved"):
    return review(company_id,scope,None,False,domain,pair)


def preview(company_id,scope,post,domain=None):
    return review(company_id,scope,post,False,domain)


def save(company_id,scope,post,domain=None):
    return review(company_id,scope,post,True,domain)
