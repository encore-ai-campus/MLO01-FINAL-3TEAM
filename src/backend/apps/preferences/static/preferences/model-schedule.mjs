const form = document.querySelector("[data-retraining-schedule]");
if (form) {
  function showThresholds() {
    const mode = form.elements.trigger_mode.value;
    for (const [name, inactiveMode] of [["interval_days", "volume"], ["min_new_gold", "period"]]) {
      const input = form.elements[name];
      input.disabled = mode === inactiveMode;
      input.closest(".ops-field").hidden = input.disabled;
    }
  }
  form.addEventListener("change", event => {
    if (event.target.name === "trigger_mode") showThresholds();
  });
  showThresholds();
}
