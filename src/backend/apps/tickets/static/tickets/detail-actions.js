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
