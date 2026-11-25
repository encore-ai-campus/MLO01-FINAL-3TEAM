const displayForm = document.getElementById("prediction-display");
if (displayForm && "IntersectionObserver" in window) {
  const seen = new Set();
  const pending = new Set();
  let frame;
  const report = () => {
    if (!pending.size) return;
    const body = new URLSearchParams(new FormData(displayForm));
    for (const key of pending) body.append("displayed", key);
    pending.clear();
    fetch(displayForm.action, { method: "POST", body, keepalive: true }).catch(() => {});
  };
  const record = (entries) => {
    if (document.visibilityState !== "visible") return;
    for (const entry of entries) {
      if (!entry.isIntersecting || !entry.intersectionRect.width || !entry.intersectionRect.height) continue;
      const key = entry.target.dataset.ticketPrediction;
      if (seen.has(key)) continue;
      seen.add(key);
      pending.add(key);
      observer.unobserve(entry.target);
    }
  };
  const observer = new IntersectionObserver((entries) => {
    record(entries);
    if (frame) cancelAnimationFrame(frame);
    frame = requestAnimationFrame(report);
  });
  const labels = document.querySelectorAll("[data-ticket-prediction]");
  for (const label of labels) observer.observe(label);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      for (const label of labels) {
        if (seen.has(label.dataset.ticketPrediction)) continue;
        observer.unobserve(label);
        observer.observe(label);
      }
    }
  });
  for (const form of document.querySelectorAll("[data-report-predictions]")) {
    form.addEventListener("formdata", (event) => {
      record(observer.takeRecords());
      if (!seen.size) return;
      event.formData.append("display_token", displayForm.elements.token.value);
      for (const key of seen) event.formData.append("displayed", key);
    });
  }
}
