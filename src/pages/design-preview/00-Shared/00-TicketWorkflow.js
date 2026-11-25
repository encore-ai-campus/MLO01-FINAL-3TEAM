import { openStore } from '../05-InboxPage/05-ReplyStore.js';

const storeName = 'ticketEvents';
const updatedKey = 'voc.ticket-events.update';
export const closeAfter = 72 * 60 * 60 * 1000;

export function ticketState(events, now = Date.now()) {
  const latestReply = events.filter(event => event.kind === 'reply').reduce((latest, event) => Math.max(latest, event.time), 0);
  const latestTransfer = events.filter(event => event.kind === 'transfer').sort((a, b) => b.time - a.time)[0];
  const latestUpdate = events.reduce((latest, event) => Math.max(latest, event.time), 0);
  return {
    ...(latestUpdate && { updated: new Date(latestUpdate) }),
    ...(latestTransfer && { assignee: latestTransfer.recipient }),
    ...(latestReply ? { status: now >= latestReply + closeAfter ? '문의 종료' : '답변완료', closeAt: latestReply + closeAfter } :
      latestTransfer ? { status: '할당' } : {})
  };
}

export async function loadTicketEvents(ticketId) {
  const db = await openStore();
  return new Promise((resolve, reject) => {
    const transaction = db.transaction(storeName, 'readonly');
    const store = transaction.objectStore(storeName);
    const request = ticketId ? store.index('ticketId').getAll(ticketId) : store.getAll();
    transaction.oncomplete = () => resolve(request.result);
    transaction.onabort = () => reject(request.error ?? transaction.error);
  });
}

export async function appendTicketEvent(fields) {
  const db = await openStore();
  const record = await new Promise((resolve, reject) => {
    const transaction = db.transaction(storeName, 'readwrite');
    const store = transaction.objectStore(storeName);
    const request = store.index('ticketId').getAll(fields.ticketId);
    let saved;
    let error;
    request.onsuccess = () => {
      const time = Date.now();
      if (ticketState(request.result, time).status === '문의 종료') {
        error = new Error('문의가 종료되었습니다.');
        transaction.abort();
        return;
      }
      saved = { ...fields, eventId: crypto.randomUUID(), time };
      store.add(saved);
    };
    transaction.oncomplete = () => resolve(saved);
    transaction.onabort = () => reject(error ?? transaction.error ?? request.error);
  });
  try { localStorage.setItem(updatedKey, record.eventId); } catch {}
  return record;
}

export function subscribeTicketEvents(refresh) {
  const sync = event => { if (event.key === updatedKey) refresh(); };
  window.addEventListener('storage', sync);
  window.addEventListener('focus', refresh);
  window.addEventListener('pageshow', refresh);
  return () => {
    window.removeEventListener('storage', sync);
    window.removeEventListener('focus', refresh);
    window.removeEventListener('pageshow', refresh);
  };
}

const seoul = new Intl.DateTimeFormat('sv-SE', {
  timeZone: 'Asia/Seoul', dateStyle: 'short', timeStyle: 'short'
});
export const ticketDate = value => seoul.format(new Date(value));
