const filters = document.getElementById('ticket-filters');
const dialog = document.getElementById('filter-dialog');
const opener = document.getElementById('open-filters');
const clear = document.getElementById('clear-filters');
const cancel = document.getElementById('cancel-filters');
const custom = document.getElementById('custom-period');
const dates = document.getElementById('custom-dates');
const start = filters.elements.start;
const end = filters.elements.end;

function updatePeriod() {
  dates.hidden = !custom.checked;
  start.disabled = end.disabled = !custom.checked;
  start.required = end.required = custom.checked;
  end.setCustomValidity('');
  if (custom.checked && start.value && end.value) {
    const days = (end.valueAsNumber - start.valueAsNumber) / 86400000 + 1;
    if (days < 1) end.setCustomValidity('종료일은 시작일보다 빠를 수 없습니다.');
    if (days > 365) end.setCustomValidity('최대 365일까지 조회할 수 있습니다.');
  }
}
if (typeof dialog.showModal === 'function') {
  dialog.append(filters);
  document.getElementById('inline-filters').hidden = true;
  opener.hidden = clear.hidden = cancel.hidden = false;
  opener.addEventListener('click', () => {
    filters.reset();
    updatePeriod();
    dialog.showModal();
  });
  cancel.addEventListener('click', () => dialog.close());
  clear.addEventListener('click', () => {
    for (const checkbox of filters.querySelectorAll('input[type=checkbox]')) checkbox.checked = false;
    filters.querySelector('input[name="days"]').checked = true;
    start.value = end.value = '';
    updatePeriod();
  });
}
filters.addEventListener('input', updatePeriod);
filters.addEventListener('formdata', event => {
  for (const name of ['days', 'start', 'end']) {
    if (!event.formData.get(name)) event.formData.delete(name);
  }
});
updatePeriod();
document.getElementById('ticket-page-size').addEventListener('change', event => event.target.form.requestSubmit());

// Keep server-rendered relative dates current while the same page remains open.
const kst = new Intl.DateTimeFormat('sv-SE', {
  timeZone: 'Asia/Seoul', year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', hourCycle: 'h23'
});
function refreshTimes() {
  const now = Date.now();
  const today = kst.format(now);
  for (const element of document.querySelectorAll('time[data-relative]')) {
    const instant = Date.parse(element.dateTime);
    const minutes = Math.floor((now - instant) / 60000);
    const hours = Math.floor(minutes / 60);
    const days = Math.floor(hours / 24);
    const date = kst.format(instant);
    let text;
    if (minutes < 1) text = '1분 미만';
    else if (minutes < 60) text = minutes + '분 전';
    else if (hours < 24) text = hours + '시간 전';
    else if (days <= 7) text = days + '일 전';
    else if (date.slice(0, 4) !== today.slice(0, 4)) text = '지난해';
    else if (date.slice(5, 7) !== today.slice(5, 7)) text = '지난달';
    else text = '지난주';
    element.textContent = text;
  }
  for (const element of document.querySelectorAll('time.ticket-received-date')) {
    const date = kst.format(new Date(element.dateTime));
    const label = date.slice(0, 10) === today.slice(0, 10)
      ? date.slice(11) : date.slice(date.slice(0, 4) === today.slice(0, 4) ? 5 : 0, 10).replaceAll('-', '/');
    element.textContent = '접수 ' + label;
  }
}
setInterval(refreshTimes, 30000);
