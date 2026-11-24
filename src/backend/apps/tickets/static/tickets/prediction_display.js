const displayForm = document.getElementById("prediction-display");
if (displayForm && "IntersectionObserver" in window) {
  const displayed = new Set();
  let frame;
  const report = () => {
    if (!displayed.size) return;
    const body = new URLSearchParams(new FormData(displayForm));
    for (const key of displayed) body.append("displayed", key);
    displayed.clear();
    fetch(displayForm.action, { method: "POST", body, keepalive: true }).catch(() => {});
  };
  const observer = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (entry.isIntersecting && document.visibilityState === "visible") {
        displayed.add(entry.target.dataset.ticketPrediction);
        observer.unobserve(entry.target);
      }
    }
    if (frame) cancelAnimationFrame(frame);
    frame = requestAnimationFrame(report);
  });
  const labels = document.querySelectorAll("[data-ticket-prediction]");
  for (const label of labels) observer.observe(label);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      for (const label of labels) {
        observer.unobserve(label);
        observer.observe(label);
      }
    }
  });
}
