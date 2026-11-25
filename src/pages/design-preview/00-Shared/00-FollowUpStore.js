import { readPreview, writePreview } from './00-DeskPreferences.js';
import { demoTicketSubject } from './00-DemoTicketSubjects.js';

export const followUpKey = 'voc.followup.preview.v1';
export const followUpChanged = 'voc-followup-changed';
export const reminderOffsets = [
  [60, '1시간 전'], [30, '30분 전'], [10, '10분 전'], [5, '5분 전'], [0, '정시']
];
export const reminderSummary = reminderOffsets.map(([, label]) => label).join(' · ');
export const followUpTickets = [
  { id: '1048', assignee: '한서윤', status: '할당', events: [
    { id: '1048-memo', label: '김하늘 상담사 메모', time: '2026-11-24T13:20', message: '결제팀에 환불 상태를 확인 중입니다.' },
    { id: '1048-reply', label: '고객 후속 메시지', time: '2026-11-24T14:10', message: '환불 입금 날짜를 한 번 더 확인해 주세요.' }
  ] },
  { id: '1042', assignee: '김하늘', status: '할당', events: [
    { id: '1042-memo', label: '다른 담당자 메모', time: '2026-11-24T10:35', message: '배송팀에서 확인 중입니다.' }
  ] },
  { id: '1031', assignee: '박유진', status: '답변 완료', events: [] },
  { id: '1046', assignee: '한서윤', status: '할당', events: [] }
].map(ticket => ({
  ...ticket, type: '티켓', title: demoTicketSubject(ticket.id), number: '#' + ticket.id
}));
export const followUpSources = [...followUpTickets, {
  id: 'C-218', type: '1:1 상담', number: '채팅 #C-218', title: '배송지 변경 문의'
}];
const memoTime = '2026-11-23T16:50';
const seed = {
  tracks: [
    { sourceId: '1048', started: memoTime },
    { sourceId: '1042', started: memoTime },
    { sourceId: '1031', started: memoTime }
  ],
  contacts: [
    { id: 'contact-1', sourceId: '1048', reason: '환불 입금 예정일 안내', scheduled: '2026-11-25T14:00', assignee: '한서윤' },
    { id: 'contact-2', sourceId: '1031', reason: '취소 승인 결과 안내', scheduled: '2026-11-24T16:00', assignee: '박유진' },
    { id: 'contact-3', sourceId: 'C-218', reason: '배송지 변경 여부 확인', scheduled: '2026-11-26T10:30', assignee: '김하늘' },
    { id: 'contact-4', sourceId: '1046', reason: '예약 확인서 재발송', scheduled: '2026-11-24T11:00', assignee: '한서윤',
      completed: '2026-11-24T11:07', result: '고객에게 예약 확인서를 다시 보내고 수신을 확인했습니다.', completedBy: '한서윤' }
  ]
};

export function loadFollowUp(accountId) {
  return readPreview(followUpKey, {})[accountId] ??
    (accountId === 'sy' ? seed : { tracks: [], contacts: [] });
}

export function saveFollowUp(accountId, next) {
  const stored = writePreview(followUpKey, { ...readPreview(followUpKey, {}), [accountId]: next });
  if (stored) window.dispatchEvent(new Event(followUpChanged));
  return stored;
}

export function trackedEvents(track) {
  return (followUpTickets.find(ticket => ticket.id === track.sourceId)?.events ?? [])
    .filter(event => event.time > track.started).reverse();
}

function activeReminders(state) {
  const active = new Set(state.tracks.map(track => track.contactId));
  return state.contacts.filter(contact => active.has(contact.id) && !contact.completed);
}

export function followUpNotificationCount(state) {
  const alerted = new Set((state.reminders ?? []).map(reminder => reminder.contactId));
  return activeReminders(state).filter(contact => alerted.has(contact.id)).length;
}

export function updateFollowUpReminders(accountId, now = Date.now()) {
  const state = loadFollowUp(accountId);
  const reminders = state.reminders ?? [];
  const recorded = new Set(reminders.map(reminder => reminder.id));
  const due = activeReminders(state).flatMap(contact => {
    const schedule = Date.parse(contact.scheduled + '+09:00');
    return reminderOffsets.flatMap(([minutes, label]) => {
      const id = contact.id + ':' + minutes;
      const time = schedule - minutes * 60_000;
      return time <= now && time >= contact.createdAt && !recorded.has(id)
        ? [{ id, time, label, contactId: contact.id, sourceId: contact.sourceId }] : [];
    });
  });
  if (!due.length) return state;
  const next = { ...state, reminders: [...reminders, ...due] };
  return saveFollowUp(accountId, next) ? next : state;
}

export function followUpHistory(state) {
  const alerts = (state.reminders ?? []).map(reminder => ({
    ...reminder,
    title: '#' + reminder.sourceId + ' ' + demoTicketSubject(reminder.sourceId),
    label: '후속 연락 ' + reminder.label + ' 알림'
  }));
  const changes = state.tracks.flatMap(track => trackedEvents(track).map(event => ({
    id: event.id,
    time: Date.parse(event.time + '+09:00'),
    title: '#' + track.sourceId + ' ' + demoTicketSubject(track.sourceId),
    label: event.label
  })));
  return [...alerts, ...changes].sort((a, b) => b.time - a.time);
}

const clock = new Intl.DateTimeFormat('sv-SE', {
  timeZone: 'Asia/Seoul', dateStyle: 'short', timeStyle: 'short'
});

export function localStamp(date = new Date()) {
  return clock.format(date).replace(' ', 'T');
}
