"""Select source sentences for the agent using the local Ollama summary model."""
import json
import re
import textwrap
from http.client import HTTPException
from urllib.request import ProxyHandler, Request, build_opener

from django.conf import settings

from config.http import DeskError

MODEL = "voc-ticket-summary:qwen2.5-1.5b"
SENTENCE_BREAK = re.compile(r"(?<=[.!?。！？])\s+|\n+")
GREETING = re.compile(r"(?:안녕하세(?:요|십니까)|감사합니다|고맙습니다|수고하세요)[.!。！\s]*")
SIGN_OFF = re.compile(
    r"(?:답장은 여기로 주시면 됩니다|(?:답변|진행 상황을 알려)\s*주시면\s*"
    r"(?:(?:내부|일행|담당자|팀)(?:에|에게|에도|에게도)?\s*)?"
    r"(?:공유|전달)하겠습니다)[.!。！]*"
)
CONDITION = re.compile(
    r"\d[\d,.]*(?:\s*(?:원|달러|만원|건|개|회|일|월|시|분|%))"
    r"|(?:까지|대신|말고|한\s*건만|원하지\s*않|신청한\s*적[은이]?\s*없)"
    r"|아직.{0,45}(?:않|못|없)|(?:것[은이]?|건|게)\s*아(?:니|닙)|(?:했는데|했지만|했더니)"
    r"|(?:주세요|주십시오|부탁드|원합니다|주실\s*수\s*있)"
)
MAIL_HISTORY = re.compile(r"(?im)^\s*(?:-{2,}\s*(?:original message|forwarded message|전달된 메시지)\s*-{2,}|On [^\n]+ wrote:)\s*$")
OLLAMA = build_opener(ProxyHandler({}))


def end_window(sentences):
    window = []
    remaining = 800
    for sentence in sentences:
        if len(window) == 12 or len(sentence) + 1 > remaining:
            break
        window.append(sentence)
        remaining -= len(sentence) + 1
    return window


def select_sentences(sentences):
    schema = {
        "type": "object", "additionalProperties": False, "required": ["issue", "request"],
        "properties": {
            "issue": {"type": "string", "enum": sentences},
            "request": {"type": "string", "enum": ["", *sentences]},
        },
    }
    payload = {"model": MODEL, "stream": False, "format": schema, "keep_alive": "5m",
               "messages": [{"role": "user", "content": "\n".join(sentences)}]}
    request = Request(settings.VOC_OLLAMA_URL.rstrip("/") + "/api/chat",
                      data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    try:
        with OLLAMA.open(request, timeout=15) as response:
            result = json.load(response)
        selected = json.loads(result["message"]["content"])
        if (result.get("done_reason") == "stop" and selected["issue"] in sentences
                and selected["request"] in ["", *sentences]):
            return selected["issue"], selected["request"]
    except (OSError, HTTPException):
        raise DeskError(503, "SUMMARY_UNAVAILABLE", "요약 모델에 연결할 수 없습니다.") from None
    except (ValueError, KeyError, TypeError, AttributeError):
        pass
    raise DeskError(502, "SUMMARY_INVALID", "모델이 원문에서 유효한 요약을 고르지 못했습니다.")


def summarize(body, channel):
    text = body.strip()
    if channel == "gmail":
        current = MAIL_HISTORY.split(text, maxsplit=1)[0]
        text = "\n".join(line for line in current.splitlines() if not line.lstrip().startswith(">")).strip()
        if not text:
            raise DeskError(422, "SUMMARY_QUOTED_ONLY", "인용된 메일을 제외하면 요약할 본문이 없습니다.")

    sentences = []
    for sentence in SENTENCE_BREAK.split(text):
        sentence = sentence.strip()
        if not sentence or GREETING.fullmatch(sentence) or SIGN_OFF.fullmatch(sentence):
            continue
        sentences.extend(textwrap.wrap(sentence, width=200, expand_tabs=False, replace_whitespace=False, break_on_hyphens=False)
                         if len(sentence) > 240 else [sentence])
    # A short greeting is still a valid on-demand inference input.
    sentences = list(dict.fromkeys(sentences)) or [text[:200]]
    # Keep the problem and the final request within the model's local context budget.
    partial = len(sentences) > 24 or sum(map(len, sentences)) > 1600
    if partial:
        sentences = end_window(sentences) + list(reversed(end_window(reversed(sentences))))

    selected = select_sentences(sentences)
    excerpt = [s for s in sentences if s in selected or CONDITION.search(s)]
    summary = " ".join(excerpt)
    if not partial and (len(excerpt) > 3 or len(summary) >= 0.9 * len(text)):
        return {"mode": "original", "text": text, "partial": False}
    return {"mode": "excerpt", "text": summary, "partial": partial}
