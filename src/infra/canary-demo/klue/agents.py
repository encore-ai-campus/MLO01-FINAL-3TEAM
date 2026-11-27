"""Provision three local advisers and commerce routes for the isolated demonstration company."""
import json
import os
from pathlib import Path
import secrets
import sys
from uuid import UUID, uuid5

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from demo import COMPANY, STATE, kube, sql

DOMAIN = "11111111-1111-4111-8111-111111111111"
TEAM = str(uuid5(UUID(COMPANY), "commerce-poc"))
STAFF_IDS = ('7b1f0e63-4c2c-5241-afc5-d2d36e25ff89', '60cb9914-20f6-5c9b-a71a-41fda4743f9e', '168f8926-9838-5f09-8cd4-7f569c3b75c1')
CATEGORIES = {"general": "일반 문의", "item_defective": "상품 불량", "wrong_delivery": "오배송", "delivery": "배송"}

def main():
    secrets_path = STATE/"klue-agent-logins.json"
    if not secrets_path.exists():
        with os.fdopen(os.open(secrets_path, os.O_CREAT|os.O_EXCL|os.O_WRONLY, 0o600), "w") as f:
            json.dump([{"email":f"pocagent{n}@canary.example.test","password":secrets.token_urlsafe(24)}
                       for n in range(1,4)], f, indent=2)
    accounts = json.loads(secrets_path.read_text())
    hashes = json.loads(kube("exec","-i","deployment/canary-desk","-c","django","--","python","-c",
        "import sys,json; from argon2 import PasswordHasher; print(json.dumps([PasswordHasher().hash(a['password']) for a in json.load(sys.stdin)]))",
        data=json.dumps(accounts)))
    setup = ["BEGIN;", """INSERT INTO team(team_id,company_id,name) VALUES (:'team',:'company','PoC 상담팀')
                          ON CONFLICT(team_id) DO NOTHING;"""]
    variables = {"company": COMPANY, "domain":DOMAIN, "team":TEAM}
    for i, (account, hashed) in enumerate(zip(accounts, hashes), 1):
        variables.update({f"staff{i}":STAFF_IDS[i-1], f"email{i}":account["email"], f"hash{i}":hashed})
        setup.append(f"""
          INSERT INTO staff_user(staff_user_id,company_id,email,password_hash,name,role,team_id)
          VALUES (:'staff{i}',:'company',:'email{i}',:'hash{i}','PoC 상담사 {i}','agent',:'team')
          ON CONFLICT(staff_user_id) DO UPDATE SET password_hash=excluded.password_hash,email=excluded.email
          WHERE staff_user.company_id=excluded.company_id AND staff_user.team_id=excluded.team_id;
          INSERT INTO staff_permission(staff_user_id,permission_id)
          SELECT :'staff{i}',permission_id FROM permission WHERE code IN ('ticket.read','ticket.handle') ON CONFLICT DO NOTHING;
          INSERT INTO staff_assignment_policy VALUES (:'company',:'staff{i}',true,100,'immediate')
          ON CONFLICT(company_id,staff_user_id) DO NOTHING;""")
    for i,(label,name) in enumerate(CATEGORIES.items()):
        category = "44444444-4444-4444-8444-444444444444" if label=="delivery" else str(uuid5(UUID(DOMAIN),label))
        variables.update({f"cat{i}":category,f"name{i}":name,f"label{i}":label})
        setup.append(f"""
          INSERT INTO category(category_id,domain_id,name) VALUES (:'cat{i}',:'domain',:'name{i}') ON CONFLICT DO NOTHING;
          INSERT INTO voc.classification_category_map VALUES (:'domain',:'label{i}',:'cat{i}') ON CONFLICT DO NOTHING;
          INSERT INTO ticket_category_team_route(company_id,domain_id,category_id,team_id)
          VALUES (:'company',:'domain',:'cat{i}',:'team') ON CONFLICT DO NOTHING;""")
    setup.append("COMMIT;")
    sql("\n".join(setup), *(v for k,val in variables.items() for v in ("-v",f"{k}={val}")))
    print("Prepared three PoC advisers and four commerce routes.")

if __name__ == "__main__":
    main()
