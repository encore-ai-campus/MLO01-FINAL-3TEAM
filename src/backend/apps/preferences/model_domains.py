"""Company-specific review domains. The routing domain of an incoming ticket is independent."""
from django.db import connection, transaction


DOMAIN_NAMES = {"commerce": "커머스", "finance": "금융", "food_delivery": "외식배달"}
DOMAIN_ALIASES = {
    "commerce": {"commerce", "커머스", "이커머스", "전자상거래"},
    "finance": {"finance", "금융"},
    "food_delivery": {"food_delivery", "외식배달", "외식·배달", "음식배달"},
}
STANDARD_LABELS = {
    "commerce": {"delivery_delay": "배송 지연", "item_defective": "상품 불량",
                 "no_issue": "특이사항 없음", "wrong_delivery": "오배송"},
    "finance": {"balance_limit_inquiry": "잔액·한도 문의", "card_loss": "카드 분실",
                "transaction_history_inquiry": "거래 내역 문의", "transfer_inquiry": "이체 문의"},
    "food_delivery": {"food_foreign_object": "음식 이물질", "no_issue": "특이사항 없음",
                      "order_item_missing": "주문 상품 누락", "payment_failed": "결제 실패"},
}


def company_domains(cur, company):
    cur.execute("""SELECT c.domain_uuid,d.name,p.codes,p.revision
        FROM public.company c LEFT JOIN public.domain d USING(domain_uuid)
        LEFT JOIN voc.model_review_company_domains p USING(company_uuid)
        WHERE c.company_uuid=%s""", (company,))
    row = cur.fetchone()
    if row is None:
        raise ValueError("회사를 확인해 주세요.")
    home, home_name, enabled, revision = row
    cur.execute("SELECT code,domain_uuid FROM voc.model_review_domain")
    ids = dict(cur.fetchall())
    options = {code: {"code": code, "name": name, "uuid": ids[code]}
               for code, name in DOMAIN_NAMES.items()}
    default = None
    if home is not None:
        default = next((code for code, value in ids.items() if value == home), None)
        if default is None:
            name = (home_name or "").replace(" ", "").lower()
            default = next((code for code, aliases in DOMAIN_ALIASES.items() if name in aliases), "company_domain")
        # Some older companies use a separate UUID for the same business domain.
        options[default] = {"code": default, "name": DOMAIN_NAMES.get(default, home_name), "uuid": home}
    codes = {default} if enabled is None else set(enabled)
    for code, option in options.items():
        option["enabled"] = code in codes
    return options, revision or 0


def lock_selection(cur, company, *, shared):
    function = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
    cur.execute(f"SELECT {function}(hashtextextended(%s,0))", (f"model-review-domains/{company}",))


class PolicyConflict(ValueError):
    pass


def save_domains(company, codes, revision):
    with transaction.atomic(), connection.cursor() as cur:
        lock_selection(cur, company, shared=False)
        options, current = company_domains(cur, company)
        if str(revision) != str(current):
            raise PolicyConflict("다른 관리자가 사용 도메인을 변경했습니다. 최신 설정을 다시 확인해 주세요.")
        requested = set(codes)
        if not requested.issubset(options):
            raise ValueError("이 회사에서 사용할 수 있는 도메인을 선택해 주세요.")
        selected = [code for code in options if code in requested]
        if current == 0 or selected != [code for code, item in options.items() if item["enabled"]]:
            cur.execute("""INSERT INTO voc.model_review_company_domains(company_uuid,codes,revision)
                    VALUES (%s,%s::text[],1)
                ON CONFLICT (company_uuid) DO UPDATE SET codes=excluded.codes,
                    revision=voc.model_review_company_domains.revision+1""", (company, selected))
        return selected
