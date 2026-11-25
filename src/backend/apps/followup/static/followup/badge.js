const badge = document.querySelector("[data-followup-badge]");

async function refreshFollowupBadge() {
  try {
    const response = await fetch(badge.dataset.followupBadge);
    if (!response.ok) return;
    const { ticket_count: count } = await response.json();
    badge.textContent = count ? String(count) : "";
    badge.setAttribute("aria-label", `업데이트가 있는 추적 티켓 ${count}건`);
    badge.hidden = !count;
  } catch {
    // The next page load or window focus can retry a transient failure.
  }
}

if (badge) {
  refreshFollowupBadge();
  window.addEventListener("focus", refreshFollowupBadge);
}
