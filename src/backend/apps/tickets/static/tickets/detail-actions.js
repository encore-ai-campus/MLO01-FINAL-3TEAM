const suggestion = document.querySelector("#ticket-quick-reply");
const responseBody = document.querySelector("#ticket-reply-body");
const composer = responseBody?.form;
if (composer) {
  const modes = composer.querySelectorAll('input[type="radio"][name="message_type"]');
  const drafts = { reply: "", memo: "" };
  let mode = composer.elements.message_type.value;

  for (const input of modes) {
    input.addEventListener("change", () => {
      drafts[mode] = responseBody.value;
      mode = input.value;
      responseBody.value = drafts[mode];
      responseBody.setCustomValidity("");
    });
  }

  responseBody.addEventListener("input", () => responseBody.setCustomValidity(""));
  composer.addEventListener("submit", (event) => {
    const tooLong = Array.from(responseBody.value).length > 2000;
    if (tooLong) {
      event.preventDefault();
      responseBody.setCustomValidity("본문은 2,000자까지 입력할 수 있습니다.");
      responseBody.reportValidity();
    }
  });

  suggestion?.addEventListener("change", () => {
    if (!suggestion.value) return;
    modes[0].checked = true;
    modes[0].dispatchEvent(new Event("change"));
    responseBody.value = suggestion.value;
    responseBody.focus();
  });
}
