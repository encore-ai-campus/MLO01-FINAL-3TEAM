"""Exercise real HTTP inference, weighted routing, ticket projection and PoC assignment."""
from collections import Counter
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from demo import COMPANY, STATE, bucket, enqueue, kube, outcomes, sql
from route import set_percent

CASES = ["주문한 상품이 사흘째 도착하지 않았어요.", "주문한 제품이 깨진 채로 도착했습니다.",
         "빠른 배송 감사합니다. 제품이 마음에 들어요."]
DOMAIN = "11111111-1111-4111-8111-111111111111"
# Execute inside the demo desk so requests use the same ClusterIP services as the worker.
PROBE = """
import json, sys, time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4
cases,domain=json.load(sys.stdin)
rows=[]
for version in ("klue-v1","klue-v2"):
    base="http://voc-classification-"+version+":8080"
    with urlopen(base+"/readyz",timeout=10) as r:
        assert json.load(r)=={"status":"ready"}
    def call(kind,text,domain_id=domain):
        req={"analysis_kind":kind,"request_id":str(uuid4()),"model_text":text,"domain_id":domain_id}
        start=time.monotonic()
        with urlopen(Request(base+"/v1/predict",json.dumps(req).encode(),{"Content-Type":"application/json"}),timeout=10) as r:
            data=json.load(r)
        assert data["request_id"]==req["request_id"] and data["analysis_kind"]==kind and data["model_version"]==version
        return {"request":req,"response":data,"milliseconds":round((time.monotonic()-start)*1000,1)}
    for text in cases:
        for kind in (("sentiment","category") if version=="klue-v1" else ("sentiment",)):
            result=call(kind,text)
            label=result["response"]["label"]
            assert label in ({"긍정","중립","부정"} if kind=="sentiment" else {"general","item_defective","wrong_delivery","delivery"})
            assert 0 <= result["response"]["score"] <= 1
            rows.append(result)
    if version=="klue-v1":
        assert call("category",cases[0],str(uuid4()))["response"]["label"] is None
    else:
        try: call("category",cases[0])
        except HTTPError as exc: assert exc.code==422
        else: raise AssertionError("Canary category unexpectedly enabled")
print(json.dumps(rows))
"""
def assignment_rows(run):
    data = sql("""
        SELECT coalesce(json_agg(x),'[]') FROM (
          SELECT t.source_event_id AS event,t.ticket_number,t.ticket_uuid,s.name,s.email,
                 t.category_applied_body_version AS body,j.assignment_checked_at IS NOT NULL AS delivered,
                 (SELECT count(*) FROM ticket_event e JOIN event_type et USING(event_type_uuid)
                  WHERE e.ticket_uuid=t.ticket_uuid AND et.code='assigned') AS recorded,
                 s.company_uuid=t.company_uuid AND s.team_uuid=t.assigned_team_uuid AS correct_team
          FROM ticket t JOIN voc.bronze_messages b ON b.event_id=t.source_event_id
          JOIN voc.classification_job j ON j.event_id=b.event_id AND j.analysis_kind='category'
          LEFT JOIN staff_user s ON s.staff_user_uuid=t.assignee_uuid
          WHERE b.origin='synthetic-canary' AND split_part(b.source_message_id,'/',1)=:'run'
        ) x;
    """, "-v", f"run={run}")
    return json.loads(data)


def main():
    run = str(uuid4())
    probes = json.loads(kube("exec","-i","deployment/canary-desk","-c","django","--","python","-c",PROBE,
                             data=json.dumps([CASES,DOMAIN])))
    labels = {(r["response"]["model_version"],r["request"]["analysis_kind"],r["request"]["model_text"]):
              r["response"]["label"] for r in probes}
    assigned_emails = {f"pocagent{i}@canary.example.test" for i in range(1,4)}
    expected, phases = {}, []
    try:
        for percent in (0,10,50,0):
            revision = set_percent(percent)
            for i,text in enumerate(CASES):
                for event in enqueue(run,(i,20+i,80+i),text):
                    expected[event] = (text, percent)
            until = time.monotonic()+90
            while True:
                rows = outcomes(run)
                advisers = assignment_rows(run)
                if any(r["state"] in ("failed","skipped") for r in rows):
                    raise RuntimeError(f"Model job failed for demonstration {run}")
                if len(rows)==2*len(expected) and all(r["state"]=="succeeded" for r in rows) and len(advisers)==len(expected) and all(r["delivered"] for r in advisers):
                    break
                if time.monotonic() > until:
                    raise RuntimeError(f"Timed out waiting for classified, assigned demonstration tickets: {run}")
                time.sleep(.3)
            for r in rows:
                text,pct = expected[r["event_id"]]
                kind = r["analysis_kind"]
                target = "klue-v2" if kind=="sentiment" and bucket(r["event_id"]) < pct else "klue-v1"
                assert r["model_version"]==r["served_version"]==target
                assert r["applied"] and r["outcome"]=="succeeded" and r["label"]==labels[target,kind,text]
                assert r["attempt_count"]==1 and r["seconds"]<=30
                if kind=="sentiment": assert r["sentiment"]==r["label"]
                else: assert r["category_uuid"] is not None
            assert all(r["email"] in assigned_emails and r["correct_team"] and r["recorded"]==1 and r["body"]==1 for r in advisers)
            phases.append({"canary_percent":percent,"cumulative_tickets":len(expected),"chart_revision":revision})
        assert {r["email"] for r in advisers}==assigned_emails
        counts=Counter(r["analysis_kind"]+"/"+r["served_version"] for r in rows)
        report={"run":run,"phases":phases,"probes":probes,"tickets":len(expected),"jobs":dict(counts),
                "max_queue_to_result_seconds":max(r["seconds"] for r in rows),
                "assignments":dict(Counter(r["name"] for r in advisers)),"assigned_tickets":advisers}
        (STATE/"klue-verification.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")
        print(json.dumps({k:v for k,v in report.items() if k not in ("probes","assigned_tickets")},ensure_ascii=False))
    finally:
        set_percent(0)

if __name__=="__main__":
    main()
