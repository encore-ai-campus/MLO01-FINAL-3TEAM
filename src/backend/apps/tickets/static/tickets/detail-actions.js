const responseBody = document.querySelector("#ticket-reply-body");
const composer = responseBody?.form;
if (composer) {
  const suggestion = document.querySelector("#ticket-quick-reply");
  const memoToggle = document.querySelector("#detail-memo-toggle");
  const type = composer.elements.message_type;
  const send = composer.querySelector('[data-send-label]');
  function setMemo(enabled) {
    type.value = enabled ? "memo" : "reply";
    memoToggle.setAttribute("aria-pressed", String(enabled));
    send.textContent = !enabled && send.hasAttribute('data-retry') ? '다시 보내기' : '전송';
    responseBody.setAttribute("aria-label", enabled ? "내부 메모 내용" : "메시지 내용");
  }
  memoToggle?.addEventListener("click", () => setMemo(type.value !== "memo"));
  suggestion?.addEventListener("change", () => {
    if (!suggestion.value) return;
    setMemo(false);
    responseBody.value = suggestion.value;
    responseBody.focus();
  });
}

const summaryToggle = document.querySelector('#ticket-summary-toggle');
if (summaryToggle && composer) {
  const panel = document.querySelector('#ticket-summary-panel');
  const status = panel.querySelector('[role="status"]');
  const retry = panel.querySelector('button');
  let pending = null;

  async function requestSummary() {
    const response = await fetch(summaryToggle.dataset.url, {
      method: 'POST',
      credentials: 'same-origin',
      headers: {'X-CSRFToken': composer.elements.csrfmiddlewaretoken.value, 'Accept': 'application/json'},
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail?.message || '요약을 불러오지 못했습니다.');
    if (data.partial) return '문의 앞뒤 일부에서 발췌\n' + data.text;
    if (data.mode === 'original') return '문의 원문\n' + data.text;
    return data.text;
  }

  async function showSummary() {
    panel.hidden = !summaryToggle.checked;
    retry.hidden = true;
    if (panel.hidden) {
      status.textContent = '';
      return;
    }
    status.textContent = '문의 내용을 요약하고 있습니다…';
    if (pending) return;
    pending = requestSummary();
    try {
      const text = await pending;
      if (summaryToggle.checked) status.textContent = text;
    } catch (error) {
      if (summaryToggle.checked) {
        status.textContent = error instanceof TypeError || error instanceof SyntaxError
          ? '요약을 불러오지 못했습니다.' : error.message;
        retry.hidden = false;
      }
    } finally {
      pending = null;
    }
  }

  function resetSummary() {
    summaryToggle.checked = false;
    panel.hidden = true;
    status.textContent = '';
    retry.hidden = true;
  }
  resetSummary();
  window.addEventListener('pageshow', event => { if (event.persisted) resetSummary(); });
  summaryToggle.addEventListener('change', showSummary);
  retry.addEventListener('click', showSummary);
}

const grip = document.querySelector('.detail-grip');
if (grip && responseBody) {
  let drag = null;
  const resize = pixels => { responseBody.style.height = Math.max(64, Math.min(420, pixels)) + 'px'; };
  grip.addEventListener('pointerdown', event => {
    drag = {y: event.clientY, height: responseBody.offsetHeight};
    grip.setPointerCapture(event.pointerId);
  });
  grip.addEventListener('pointermove', event => {
    if (drag) resize(drag.height + drag.y - event.clientY);
  });
  grip.addEventListener('lostpointercapture', () => { drag = null; });
  grip.addEventListener('keydown', event => {
    if (event.key !== 'ArrowUp' && event.key !== 'ArrowDown') return;
    event.preventDefault();
    resize(responseBody.offsetHeight + (event.key === 'ArrowUp' ? 16 : -16));
  });
}
