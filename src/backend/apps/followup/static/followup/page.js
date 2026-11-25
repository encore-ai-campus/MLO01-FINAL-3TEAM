document.querySelectorAll('[data-follow-dialog]').forEach(button => {
  const dialog = document.getElementById(button.dataset.followDialog);
  button.addEventListener('click', () => dialog.showModal());
  dialog.querySelector('[data-close-dialog]').addEventListener('click', () => dialog.close());
  if (dialog.hasAttribute('data-reopen')) dialog.showModal();
});

const alerts = document.querySelectorAll('[data-follow-alert]');
const badges = document.querySelectorAll('[data-scheduled-at]');
const emptyHistory = document.querySelector('[data-follow-empty]');
const sidebarBadge = document.querySelector('[data-followup-badge]');

function updateSchedule() {
  const now = Date.now();
  const unreadTickets = new Set();
  let hasHistory = false;
  for (const alert of alerts) {
    alert.hidden = Date.parse(alert.dataset.followAlert) > now;
    if (!alert.hidden) {
      hasHistory = true;
      if (alert.dataset.unreadTicket) unreadTickets.add(alert.dataset.unreadTicket);
    }
  }
  if (emptyHistory) emptyHistory.hidden = hasHistory;
  if (sidebarBadge) {
    const count = unreadTickets.size;
    sidebarBadge.textContent = count;
    sidebarBadge.setAttribute('aria-label', `업데이트가 있는 추적 티켓 ${count}건`);
    sidebarBadge.hidden = !count;
  }
  for (const badge of badges) {
    const late = Date.parse(badge.dataset.scheduledAt) < now;
    badge.classList.toggle('late', late);
    badge.classList.toggle('waiting', !late);
    badge.textContent = late ? '기한 지남' : '예정';
  }
}

updateSchedule();
window.setInterval(updateSchedule, 1000);
