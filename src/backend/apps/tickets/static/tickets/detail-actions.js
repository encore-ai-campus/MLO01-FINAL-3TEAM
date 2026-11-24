const suggestion = document.querySelector("#ticket-quick-reply");
const responseBody = document.querySelector("#ticket-reply-body");

suggestion?.addEventListener("change", () => {
  if (suggestion.value) {
    responseBody.value = suggestion.value;
    responseBody.focus();
  }
});
