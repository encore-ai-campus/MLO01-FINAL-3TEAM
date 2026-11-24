import { loadReplies, readReply, saveReply } from './reply-store.js';

const page = document.getElementById('inbox');
const rows = document.getElementById('inbox-rows');
const empty = document.getElementById('inbox-empty');
const drawer = document.getElementById('request-drawer');
const account = page.dataset.account;
const readKey = 'voc.inbox.confirmed.' + page.dataset.owner;
const requests = new Map(JSON.parse(document.getElementById('preview-review-requests').textContent).map(item => [item.requestId, item]));
const unseen = JSON.parse(document.getElementById('preview-unread-ids').textContent);
const read = new Set();
try {
  const stored = JSON.parse(sessionStorage.getItem(readKey));
  if (Array.isArray(stored)) stored.forEach(id => read.add(id));
} catch { /* Browser storage may be unavailable. */ }

if (drawer) {
  read.add(drawer.dataset.selected);
  try { sessionStorage.setItem(readKey, JSON.stringify([...read])); } catch { /* Stay usable in memory. */ }
  drawer.close();
  drawer.showModal();
  const heading = drawer.querySelector('h2');
  const detailTitle = heading.textContent;
  heading.focus();
  function changeView(view) {
    for (const name of ['detail', 'write', 'history']) {
      const panel = document.getElementById('request-' + name);
      if (panel) panel.hidden = view !== name;
    }
    document.getElementById('detail-actions').hidden = view === 'write';
    const writeActions = document.getElementById('write-actions');
    if (writeActions) writeActions.hidden = view !== 'write';
    heading.textContent = { detail: detailTitle, write: '검토 답변 작성', history: '이관 이력' }[view];
    (view === 'write' ? document.getElementById('inbox-reply-text') : heading).focus();
  }
  drawer.addEventListener('click', event => {
    const button = event.target.closest('[data-view]');
    if (button) changeView(button.dataset.view);
    if (event.target === drawer) document.getElementById('drawer-close').click();
  });
  drawer.addEventListener('cancel', event => {
    event.preventDefault();
    document.getElementById('drawer-close').click();
  });
  const form = document.getElementById('request-write');
  if (form) {
    const textarea = form.querySelector('textarea');
    const submit = drawer.querySelector('button[form="request-write"]');
    submit.disabled = true;
    textarea.addEventListener('input', () => { submit.disabled = !textarea.value.trim(); });
    form.addEventListener('submit', async event => {
      event.preventDefault();
      const item = requests.get(drawer.dataset.selected);
      const message = textarea.value.trim();
      if (!item || !message || submit.disabled) return;
      submit.disabled = true;
      document.getElementById('inbox-status').textContent = '';
      try {
        await saveReply({ requestId: item.requestId, sender: account, recipient: item.sender, sentAt: Date.now(), read: false, message });
        await refresh();
        changeView('detail');
      } catch (error) {
        if (error.name === 'ConstraintError') {
          await refresh();
          changeView('detail');
        } else {
          document.getElementById('inbox-status').textContent = '답변을 보내지 못했습니다. 다시 시도해 주세요.';
          submit.disabled = false;
        }
      }
    });
  }
}

for (const span of page.querySelectorAll('[data-count]')) {
  span.textContent = unseen[span.dataset.count].filter(id => !read.has(id)).length;
}
for (const row of rows.querySelectorAll('[data-id]')) {
  const unread = row.dataset.unread === '1' && !read.has(row.dataset.id);
  row.querySelector('.inbox-read-label').textContent = unread ? '미확인' : '확인';
  const dot = row.querySelector('.inbox-unread-dot');
  if (dot) dot.hidden = !unread;
  row.hidden = page.dataset.scope === 'unread' && !unread;
}
empty.hidden = !!rows.querySelector('tr:not([hidden])');

function formatTime(timestamp) {
  const parts = new Intl.DateTimeFormat('ko-KR', { timeZone: 'Asia/Seoul', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).formatToParts(timestamp);
  const byType = Object.fromEntries(parts.map(p => [p.type, p.value]));
  return byType.month + '/' + byType.day + ' ' + byType.hour + ':' + byType.minute;
}

const replyDrawer = document.getElementById('reply-drawer');
replyDrawer.querySelector('[data-close-reply]').addEventListener('click', () => replyDrawer.close());
async function showReply(item, reply) {
  replyDrawer.querySelector('.inbox-ticket-meta').textContent = '#' + item.id;
  replyDrawer.querySelector('[data-title]').textContent = item.title;
  replyDrawer.querySelector('[data-byline]').textContent = reply.sender + ' · ' + formatTime(reply.sentAt);
  replyDrawer.querySelector('[data-message]').textContent = reply.message;
  replyDrawer.querySelector('[data-original]').textContent = item.memo;
  replyDrawer.querySelector('details').open = false;
  replyDrawer.querySelector('[data-ticket-link]').href = '/inbox/sample-ticket/' + encodeURIComponent(item.id) + '/';
  replyDrawer.showModal();
  if (!reply.read) {
    try {
      await readReply(reply.requestId, account);
      await refresh();
    } catch { replyDrawer.querySelector('[data-byline]').textContent += ' · 확인 상태 저장 실패'; }
  }
}

function drawReply(reply) {
  const item = requests.get(reply.requestId);
  const row = rows.insertRow();
  row.insertCell().textContent = reply.read ? '확인' : '미확인';
  const title = row.insertCell();
  const open = document.createElement('button');
  open.type = 'button';
  open.className = 'inbox-row-open';
  open.textContent = item.title;
  open.addEventListener('click', () => showReply(item, reply));
  const id = document.createElement('div');
  id.className = 'inbox-ticket-meta';
  id.textContent = '#' + item.id;
  title.append(open, id);
  for (const value of [reply.message, item.category, reply.sender, formatTime(reply.sentAt)]) row.insertCell().textContent = value;
}

async function refresh() {
  try {
    const records = await loadReplies();
    const received = records.filter(record => record.recipient === account && requests.has(record.requestId)).sort((a, b) => b.sentAt - a.sentAt);
    document.getElementById('reply-count').textContent = received.filter(r => !r.read).length;
    if (page.dataset.tab === 'reply') {
      rows.replaceChildren();
      received.forEach(drawReply);
      empty.hidden = received.length > 0;
    }
    const write = document.getElementById('write-button');
    if (write && drawer) {
      const saved = records.find(r => r.requestId === drawer.dataset.selected && r.sender === account);
      write.hidden = !!saved;
      write.disabled = false;
      if (saved) {
        const sent = document.getElementById('sent-reply');
        sent.querySelector('p').textContent = saved.message;
        sent.hidden = false;
      }
    }
  } catch {
    if (page.dataset.tab === 'reply') {
      empty.textContent = '받은 검토 답변을 불러오지 못했습니다.';
      empty.hidden = false;
    }
    if (drawer) document.getElementById('inbox-status').textContent = '답변 저장 상태를 확인할 수 없습니다.';
  }
}
window.addEventListener('focus', refresh);
refresh();
