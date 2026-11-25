import { mountFollowUp } from '../../pages/design-preview/08-FollowUpPage/08-FollowUpPage.jsx';
import { localStamp, reminderOffsets } from '../../pages/design-preview/00-Shared/00-FollowUpStore.js';
import { sidebarLabels } from '../../pages/design-preview/00-Shared/00-DeskPreferences.js';

const config = JSON.parse(document.getElementById('followup-bootstrap').textContent);
const empty = { tickets: [], tracks: [], contacts: [], sources: {} };
const stamp = value => value ? localStamp(new Date(value)) : undefined;

async function request(url, method = 'GET', body) {
  const response = await fetch(url, {
    method,
    headers: method === 'GET' ? {} : {
      'X-CSRFToken': config.csrfToken,
      ...(body ? { 'Content-Type': 'application/json' } : {}),
    },
    body: body && JSON.stringify(body),
  });
  if (!response.ok) {
    const data = await response.json().catch(() => null);
    throw new Error(data?.detail?.message ?? '팔로업 요청을 처리할 수 없습니다.');
  }
  return response;
}

function source(row) {
  return {
    id: row.ticket_uuid ?? row.chat_uuid,
    type: row.ticket_uuid ? '티켓' : '1:1 상담',
    number: row.ticket_uuid ? '#' + row.ticket_number : '채팅 #' + row.chat_uuid.slice(0, 8),
    title: row.title || '1:1 상담',
    url: row.source_url,
    assignee: row.assignee || '미지정',
    status: row.status === '답변완료' ? '답변 완료' : row.status,
  };
}

function normalize(payload) {
  const sources = {};
  const byTicket = Map.groupBy(payload.notifications, row => row.ticket_uuid);
  const contacts = payload.contacts.map(row => {
    const item = source(row);
    sources[item.id] = item;
    return {
      id: row.contact_uuid, sourceId: item.id, reason: row.reason, assignee: row.assignee,
      scheduled: stamp(row.scheduled_at), completed: stamp(row.contacted_at),
      scheduledAt: Date.parse(row.scheduled_at), remindersStarted: Date.parse(row.reminders_started_at),
      remindersStopped: Date.parse(row.reminders_stopped_at), completedBy: row.completed_by,
      result: row.result, completeUrl: row.complete_url,
    };
  });
  const bySource = Map.groupBy(contacts, row => row.sourceId);
  const tracks = payload.tracks.map(row => {
    const item = source(row);
    sources[item.id] = item;
    const candidates = bySource.get(item.id) ?? [];
    const planned = candidates.find(contact => contact.remindersStarted >= Date.parse(row.tracked_at))
      ?? candidates.find(contact => !contact.completed);
    return {
      sourceId: item.id, started: stamp(row.tracked_at), contactId: planned?.id, removeUrl: row.remove_url,
      events: (byTicket.get(item.id) ?? []).map(event => ({
        id: event.event_uuid,
        label: event.kind === 'memo' ? event.author + ' 상담사 메모' : '고객 후속 메시지',
        message: event.body, time: stamp(event.occurred_at), occurredAt: Date.parse(event.occurred_at),
      })),
    };
  });
  return { tracks, contacts, sources, tickets: payload.tickets.map(source) };
}

function alerts(data, now) {
  const updated = new Set();
  const active = new Set(data.tracks.map(track => track.sourceId));
  const title = sourceId => {
    const item = data.sources[sourceId];
    return item.number + ' ' + item.title;
  };
  const history = data.tracks.flatMap(track => track.events.map(event => {
    updated.add(track.sourceId);
    return { ...event, id: track.sourceId + ':' + event.id, time: event.occurredAt, title: title(track.sourceId) };
  }));
  for (const contact of data.contacts) {
    if (!Number.isFinite(contact.remindersStarted)) continue;
    for (const [offset, label] of reminderOffsets) {
      const time = contact.scheduledAt - offset * 60_000;
      if (time < contact.remindersStarted || time > now || time > contact.remindersStopped) continue;
      history.push({
        id: contact.id + ':' + offset, time, title: title(contact.sourceId),
        label: '후속 연락 ' + label + ' 알림',
      });
      if (active.has(contact.sourceId) && !Number.isFinite(contact.remindersStopped)) updated.add(contact.sourceId);
    }
  }
  return { count: updated.size, history: history.sort((a, b) => b.time - a.time) };
}

mountFollowUp(document.getElementById('app'), {
  ...config,
  sidebar: Object.fromEntries(Object.keys(sidebarLabels).map(key => [key, config.sidebar?.includes(key) ?? true])),
  empty,
  alerts,
  async load() { return normalize(await (await request(config.dataUrl)).json()); },
  register: fields => request(config.registerUrl, 'POST', {
    ticket_uuid: fields.sourceId, scheduled_at: fields.scheduled + '+09:00',
  }),
  remove: url => request(url, 'DELETE'),
  complete: (url, fields) => request(url, 'POST', {
    contacted_at: fields.completed + '+09:00', result: fields.result,
  }),
  logout(event) {
    event.preventDefault();
    const form = document.createElement('form');
    form.method = 'post';
    form.action = config.links.logout;
    const token = document.createElement('input');
    token.type = 'hidden';
    token.name = 'csrfmiddlewaretoken';
    token.value = config.csrfToken;
    form.append(token);
    document.body.append(form);
    form.submit();
  },
});
