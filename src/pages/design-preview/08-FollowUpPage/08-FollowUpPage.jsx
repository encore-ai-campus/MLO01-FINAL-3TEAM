import React, { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { DeskLayout } from '../00-Shared/00-DeskLayout.jsx';
import { currentStaff, withStaffPreview } from '../00-Shared/00-StaffAccess.js';
import {
  followUpKey, followUpSources, followUpTickets, loadFollowUp,
  localStamp, reminderSummary, saveFollowUp, trackedEvents
} from '../00-Shared/00-FollowUpStore.js';

const tabs = ['추적 티켓', '연락 예정', '연락 완료'];
const sources = Object.fromEntries(followUpSources.map(source => [source.id, source]));
const ticketUrl = '../04-TicketDetailPage/04-TicketDetailPage.html';
const timestamp = value => value?.replace('T', ' ') ?? '—';

function Badge({ children, tone = '' }) {
  return <span className={'follow-badge ' + tone}>{children}</span>;
}

function ContactList({ rows, tab, selectedId, now, onSelect }) {
  return <div className="follow-table-wrap">
    <table className="follow-table" aria-label={tabs[tab] + ' 목록'}>
      <thead><tr>
        <th scope="col">문의</th>
        <th scope="col">{['담당자 / 상태', '담당자 / 연락 사유', '처리자 / 연락 결과'][tab]}</th>
        <th scope="col">{['추적 일시', '예정 일시', '연락 일시'][tab]}</th>
      </tr></thead>
      <tbody>
        {rows.map(row => {
          const { source } = row;
          return <tr key={row.id} data-selected={selectedId === row.id || undefined}>
            <th scope="row"><button className="follow-row-button" type="button" onClick={() => onSelect(row.id)} aria-pressed={selectedId === row.id}>
              <small>{source.number}</small><strong>{source.title}</strong>
            </button></th>
            <td>{tab === 0 ? <>
              <strong>{source.assignee}</strong><Badge>{source.status}</Badge>
            </> : <><strong>{tab === 1 ? row.assignee : row.completedBy}</strong><small className="follow-summary">{tab === 1 ? row.reason : row.result}</small></>}</td>
            <td>{tab === 0 ? <>
              <strong>{timestamp(row.scheduled)}</strong>
              {row.events[0] && <small>{row.events[0].label}</small>}
            </> : <>
              <strong>{timestamp(tab === 1 ? row.scheduled : row.completed)}</strong>
              {tab === 1 && <Badge tone={row.scheduled < now ? 'late' : 'waiting'}>{row.scheduled < now ? '기한 지남' : '예정'}</Badge>}
            </>}</td>
          </tr>;
        })}
        {!rows.length && <tr><td className="follow-empty" colSpan={3}>표시할 {tabs[tab]} 항목이 없습니다.</td></tr>}
      </tbody>
    </table>
  </div>;
}

function FollowUpDetail({ row, tab, now, onUntrack, onFinish }) {
  const { source } = row;
  const scheduled = tab === 1;
  return <aside className="follow-detail" aria-labelledby="follow-detail-title">
    <h2 id="follow-detail-title">{tabs[tab]} 상세</h2>
    <div className="follow-detail-body">
      <p className="follow-muted">{source.number}</p>
      <h3>{source.title}</h3>
      {tab === 0 ? <>
        <p className="follow-meta">{source.assignee} · {source.status}</p>
        <dl><dt>추적 일시</dt><dd>{timestamp(row.scheduled)}</dd></dl>
        <h4>최근 변경</h4>
        {row.events.length ? <ol className="follow-events">
          {row.events.map(event => <li key={event.id}>
            <div><strong>{event.label}</strong><time dateTime={event.time + '+09:00'}>{timestamp(event.time)}</time></div>
            <p>{event.message}</p>
          </li>)}
        </ol> : <p className="follow-muted">추적을 시작한 뒤 변경된 내용이 없습니다.</p>}
        <div className="follow-actions">
          <button className="desk-button" type="button" onClick={() => onUntrack(source.id)}>추적 해제</button>
        </div>
      </> : <>
        {scheduled && <Badge tone={row.scheduled < now ? 'late' : 'waiting'}>{row.scheduled < now ? '기한 지남' : '예정'}</Badge>}
        <dl>
          <dt>{scheduled ? '연락 사유' : '예약 사유'}</dt><dd>{row.reason}</dd>
          <dt>예정 일시</dt><dd>{timestamp(row.scheduled)}</dd>
          <dt>예약 담당자</dt><dd>{row.assignee}</dd>
          {!scheduled && <><dt>실제 연락 일시</dt><dd>{timestamp(row.completed)}</dd><dt>처리자</dt><dd>{row.completedBy}</dd></>}
        </dl>
        {scheduled ? <button className="desk-button desk-button-solid" type="button" onClick={() => onFinish(row)}>연락 완료 처리</button> :
          <><h4>연락 결과</h4><p className="follow-result">{row.result}</p></>}
      </>}
      {source.type === '티켓' && <div className="follow-origin">
        <a className="desk-button" href={withStaffPreview(ticketUrl + '?ticket=' + encodeURIComponent(source.id))}>원본 티켓 열기 ›</a>
      </div>}
    </div>
  </aside>;
}

function FollowUpDialog({ modal, available, error, onClose, onSave }) {
  const dialog = useRef(null);
  const finish = modal.kind === 'finish';
  const title = finish ? '연락 완료 처리' : '추적 등록';
  useEffect(() => {
    const node = dialog.current;
    node.showModal();
    return () => node.close();
  }, []);
  function submit(event) {
    event.preventDefault();
    onSave(Object.fromEntries(new FormData(event.currentTarget)));
  }
  return <dialog className="follow-dialog" aria-labelledby="follow-dialog-title" ref={dialog} onCancel={event => { event.preventDefault(); onClose(); }}>
    <form onSubmit={submit}>
      <h2 id="follow-dialog-title">{title}</h2>
      {finish ? <>
        <p className="follow-result">{sources[modal.contact.sourceId].number} {sources[modal.contact.sourceId].title}<br />예정 {timestamp(modal.contact.scheduled)} · {modal.contact.assignee}</p>
        <label>실제 연락 일시<input type="datetime-local" name="completed" required defaultValue={localStamp()} /></label>
        <label>연락 결과<textarea required name="result" maxLength={1000} rows={4} /></label>
      </> : <>
        <label>티켓
          <select name="sourceId" required autoFocus>
            <option value="">선택해 주세요</option>
            {available.map(source => <option value={source.id} key={source.id}>{source.number} {source.title}</option>)}
          </select>
        </label>
        <label>추적 날짜 및 시각<input type="datetime-local" name="scheduled" required min={localStamp(new Date(Date.now() + 60_000))} /></label>
        <p className="follow-muted">자동 알림 · {reminderSummary}</p>
      </>}
      {error && <p className="follow-error" role="alert">{error}</p>}
      <div className="follow-actions">
        <button className="desk-button" type="button" onClick={onClose}>취소</button>
        <button className="desk-button desk-button-solid" type="submit" disabled={!finish && !available.length}>
          {finish ? '완료 처리' : '추적 등록'}
        </button>
      </div>
    </form>
  </dialog>;
}

function FollowUpPage({ person }) {
  const [data, setData] = useState(() => loadFollowUp(person.id));
  const [tab, setTab] = useState(0);
  const [selectedId, select] = useState('');
  const [query, setQuery] = useState('');
  const [modal, setModal] = useState(null);
  const [notice, setNotice] = useState('');
  const [error, setError] = useState('');
  const [now, setNow] = useState(localStamp);
  useEffect(() => {
    const timer = setInterval(() => setNow(localStamp()), 1000 * 60);
    const refresh = () => setData(loadFollowUp(person.id));
    const sync = event => { if (event.key === followUpKey || event.key === null) refresh(); };
    window.addEventListener('storage', sync);
    window.addEventListener('pageshow', refresh);
    return () => {
      clearInterval(timer);
      window.removeEventListener('storage', sync);
      window.removeEventListener('pageshow', refresh);
    };
  }, [person.id]);

  const trackRows = data.tracks.map(track => ({
    ...track, id: track.sourceId, source: sources[track.sourceId], events: trackedEvents(track),
    scheduled: data.contacts.find(contact => contact.id === track.contactId)?.scheduled
  })).sort((a, b) => (b.events[0]?.time ?? b.started).localeCompare(a.events[0]?.time ?? a.started));
  const contacts = data.contacts.filter(contact => tab === 1 ? !contact.completed : contact.completed)
    .map(contact => ({ ...contact, source: sources[contact.sourceId] }))
    .sort((a, b) => tab === 1 ? a.scheduled.localeCompare(b.scheduled) : b.completed.localeCompare(a.completed));
  const term = query.trim().toLowerCase();
  const rows = (tab === 0 ? trackRows : contacts).filter(row => row.source &&
    (row.source.number.toLowerCase().includes(term) || row.source.title.toLowerCase().includes(term)));
  const detail = rows.find(row => row.id === selectedId) ?? rows[0];
  const available = followUpTickets.filter(source => !data.tracks.some(track => track.sourceId === source.id));

  function switchTab(index, id = '') {
    setTab(index);
    select(id);
    setQuery('');
  }
  function commit(next, message) {
    if (!saveFollowUp(person.id, next)) return false;
    setData(next);
    setNotice(message);
    return true;
  }
  function showModal(kind, fields = {}) {
    setError('');
    setNotice('');
    setModal({ kind, ...fields });
  }
  function untrack(sourceId) {
    const fresh = loadFollowUp(person.id);
    const tracks = fresh.tracks.filter(track => track.sourceId !== sourceId);
    if (!commit({ ...fresh, tracks }, '추적을 해제했습니다.')) {
      setNotice('브라우저에 변경사항을 저장할 수 없습니다.');
    }
  }
  function save(fields) {
    const fresh = loadFollowUp(person.id);
    if (modal.kind === 'track') {
      if (sources[fields.sourceId]?.type !== '티켓') return setError('추적할 티켓을 선택해 주세요.');
      if (fresh.tracks.some(track => track.sourceId === fields.sourceId)) return setError('이미 추적 중인 티켓입니다.');
      const createdAt = Date.now();
      if (!(Date.parse(fields.scheduled + '+09:00') > createdAt)) return setError('현재보다 늦은 추적 날짜와 시각을 입력해 주세요.');
      const contact = {
        id: crypto.randomUUID(), sourceId: fields.sourceId, reason: '추적 후속 연락',
        scheduled: fields.scheduled, assignee: person.name, createdAt
      };
      const tracks = [...fresh.tracks, { sourceId: contact.sourceId, started: localStamp(), contactId: contact.id }];
      if (!commit({ ...fresh, tracks, contacts: [...fresh.contacts, contact] }, '추적을 등록했습니다.')) return setError('브라우저에 저장할 수 없습니다.');
      switchTab(0, fields.sourceId);
    } else {
      if (!fields.result?.trim() || !fields.completed) return setError('실제 연락 일시와 결과를 입력해 주세요.');
      const current = fresh.contacts.find(contact => contact.id === modal.contact.id && !contact.completed);
      if (!current) return setError('이미 완료된 연락입니다.');
      const next = { ...current, completed: fields.completed, result: fields.result.trim(), completedBy: person.name };
      const contacts = fresh.contacts.map(contact => contact.id === next.id ? next : contact);
      if (!commit({ ...fresh, contacts }, '연락 완료를 저장했습니다.')) return setError('브라우저에 저장할 수 없습니다.');
      switchTab(2, next.id);
    }
    setModal(null);
  }

  return <DeskLayout active="followup">
    <section className="follow-page" aria-labelledby="follow-title">
      <h1 id="follow-title">팔로업</h1>
      <p className="follow-muted">추적 중인 문의와 연락 일정을 확인합니다.</p>
      <div className="follow-top">
        <nav className="follow-actions" aria-label="팔로업 보기">
          {tabs.map((name, index) => <button className={'desk-button' + (tab === index ? ' desk-button-solid' : '')} type="button"
            key={name} aria-pressed={tab === index} onClick={() => switchTab(index)}>{name}</button>)}
        </nav>
        {tab === 0 && <button className="desk-button desk-button-solid" type="button" onClick={() => showModal('track')}>추적 등록</button>}
      </div>
      {notice && <p className="follow-notice" role="status">{notice}</p>}
      <div className="follow-grid">
        <div className="follow-list">
          <div className="follow-filter">
            <input type="search" aria-label="티켓번호 또는 제목 검색" placeholder="티켓번호 또는 제목으로 검색" value={query} onChange={event => setQuery(event.target.value)} />
          </div>
          <p className="follow-count">조회 결과 {rows.length}건</p>
          <ContactList rows={rows} tab={tab} selectedId={detail?.id} now={now} onSelect={select} />
        </div>
        {detail && <FollowUpDetail row={detail} tab={tab} now={now} onUntrack={untrack} onFinish={contact => showModal('finish', { contact })} />}
      </div>
    </section>
    {modal && <FollowUpDialog modal={modal} available={available} error={error} onClose={() => setModal(null)} onSave={save} />}
  </DeskLayout>;
}

const person = currentStaff();
createRoot(document.getElementById('app')).render(person?.account === 'active'
  ? <FollowUpPage person={person} />
  : <DeskLayout active="followup"><h1>팔로업</h1><p role="status">활성 계정이 필요합니다.</p></DeskLayout>);
