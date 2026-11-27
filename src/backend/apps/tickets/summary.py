"""Generate a concise Korean intent title with the local Ollama ticket model."""
import json
import re
import textwrap
from urllib.request import ProxyHandler, Request, build_opener

MODEL = "voc-ticket-summary:qwen2.5-1.5b-v2"
TITLE_LIMIT = 60
CHUNK_CHARS = 1800  # Leave room for the system prompt and output in the 8K context.
MAX_CHUNKS = 4
OLLAMA = build_opener(ProxyHandler({}))
MAIL_HISTORY = re.compile(r"(?im)^\s*(?:-{2,}\s*(?:original message|forwarded message|전달된 메시지)\s*-{2,}|On [^\n]+ wrote:)\s*$")
AMOUNT = re.compile(r"\d[\d,]*(?:\.\d+)?\s*(?:만\s*원|원|달러)")
DUE = re.compile(r"(?:(?:이번|다음)\s*주\s*)?(?:[월화수목금토일]요일|오늘|내일|모레|\d{1,2}월\s*\d{1,2}일)(?:\s*오[전후])?(?:\s*\d{1,2}시)?\s*까지")
SENTENCE_BREAK = re.compile(r"(?<=[.!?。！？])\s+|\n+")
REQUEST_WORDS = re.compile(r"주세요|주십시오|부탁드|원합니다|고 싶|좋겠|좋을 것 같|궁금|[?？]")
ASKS_HOW = re.compile(r"(?:방법(?:이|은)? (?:있|없)|방법을 (?:알고|알려)|어떻게.{0,25}(?:나요|까요))")
TITLE_FORMAT = {
    "type": "object", "additionalProperties": False, "required": ["summary"],
    "properties": {"summary": {"type": "string", "minLength": 1, "maxLength": TITLE_LIMIT}},
}


class EmptyInquiry(ValueError):
    pass


def amounts(text):
    return {re.sub(r"[,\s]", "", amount) for amount in AMOUNT.findall(text)}


def request_title(messages, url):
    payload = {"model": MODEL, "stream": False, "format": TITLE_FORMAT, "keep_alive": "5m",
               "messages": messages}
    request = Request(url.rstrip("/") + "/api/chat", data=json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json"})
    with OLLAMA.open(request, timeout=15) as response:
        result = json.load(response)
    if result.get("done_reason") != "stop":
        raise ValueError("incomplete summary")
    return json.loads(result["message"]["content"])["summary"]


def generate_title(text, url):
    messages = [{"role": "user", "content": text}]
    dates = set(DUE.findall(text))
    last_request = next((s for s in reversed(SENTENCE_BREAK.split(text)) if REQUEST_WORDS.search(s)), "")
    for attempt in range(2):
        try:
            title = request_title(messages, url)
        except (ValueError, KeyError, TypeError, AttributeError):
            title = None
        title = title.strip() if isinstance(title, str) else ""
        if (not title or len(title) > TITLE_LIMIT or len(title.splitlines()) != 1
                or amounts(title) - amounts(text) or title in {"문의", "요청", "제보", "방법 문의"}
                or re.search(r"(?:[?？]|(?:요|습니다|입니다)[.!…]?)$", title)):
            messages = [{"role": "user", "content":
                         "아래 문의를 핵심 대상과 의도만 담은 짧은 명사구로 다시 써라. "
                         "원문에 없는 금액을 쓰지 마라.\n\n고객 문의:\n" + text}]
            continue
        if title.endswith(" 요청") and ASKS_HOW.search(last_request):
            topic = title.removesuffix(" 요청").removesuffix(" 방법")
            if len(topic + " 방법 문의") <= TITLE_LIMIT:
                return topic + " 방법 문의"
        if attempt == 0 and len(dates) == 1 and title.endswith("요청"):
            due = next(iter(dates))
            if due.replace(" ", "") not in title.replace(" ", ""):
                messages = [*messages, {"role": "assistant", "content": json.dumps({"summary": title}, ensure_ascii=False)},
                            {"role": "user", "content": f"'{due}'가 이번 요청의 기한이면 제목 앞에 그대로 붙여 한 줄로 쓰세요."}]
                continue
        return title
    raise ValueError("invalid summary")


def summarize_inquiry(body, channel, url):
    text = body.strip()
    if channel == "gmail":
        current = MAIL_HISTORY.split(text, maxsplit=1)[0]
        text = "\n".join(line for line in current.splitlines() if not line.lstrip().startswith(">")).strip()
    if not text:
        raise EmptyInquiry("no current inquiry")

    sentences = list(dict.fromkeys(s.strip() for s in SENTENCE_BREAK.split(text) if s.strip()))
    text = " ".join(sentences)
    parts = [text] if len(text) <= CHUNK_CHARS else textwrap.wrap(text, width=CHUNK_CHARS, break_on_hyphens=False)
    partial = len(parts) > MAX_CHUNKS
    if partial:
        # Bound interactive work for exceptionally large emails and sample across the whole inquiry.
        parts = [parts[i * (len(parts) - 1) // (MAX_CHUNKS - 1)] for i in range(MAX_CHUNKS)]
    titles = [generate_title(part, url) for part in parts]
    title = titles[0]
    if len(titles) > 1:
        # The final pass also gets direct evidence, so the customer's request can survive lossy chunk summaries.
        requests = [s for s in sentences if REQUEST_WORDS.search(s)]
        evidence = "\n".join(requests[-3:])[-600:]
        context = "구간별 메모:\n" + "\n".join(titles)
        if evidence:
            context += "\n고객의 실제 문의·요청 원문:\n" + evidence
        title = generate_title(context + "\n이 고객이 원하는 일을 한 줄 제목으로 요약하세요.", url)

    return {"mode": "summary", "text": title, "partial": partial}
