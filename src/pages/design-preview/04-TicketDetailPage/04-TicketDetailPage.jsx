import React, { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { DeskLayout } from '../00-Shared/00-DeskLayout.jsx';
import { currentStaff, hasStaffRight, loadStaff, withStaffPreview } from '../00-Shared/00-StaffAccess.js';
import { findDemoTicket } from '../00-Shared/00-DemoTicketCatalog.js';
import { appendTicketEvent, loadTicketEvents, subscribeTicketEvents, ticketDate, ticketState } from '../00-Shared/00-TicketWorkflow.js';
import { HandoffDialog } from './04-HandoffDialog.jsx';

const params = new URLSearchParams(location.search);
const listUrl = new URL(withStaffPreview('../03-TicketListPage/03-TicketListPage.html'), location.href);
const listHref = listUrl.href + (params.get('tab') === 'assigned' ? '#03-02' : '');
const agent = currentStaff();
const ticket = findDemoTicket(params.get('ticket') ?? '1048');
const formatDate = value => ticketDate(value).replaceAll('-', '/');
const targetLabel = { Gmail: 'Gmail 원본', Discord: 'Discord 원본' };
const previewState = ['loading', 'error', 'empty'].includes(params.get('similar')) ? params.get('similar') : '';
const basicAttachments = [{ name: '환불 신청 확인.pdf' }];

function returnToList(event) {
  if (!params.has('ticket') || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  const currentIndex = window.navigation?.currentEntry?.index;
  const url = window.navigation?.entries().find(entry => entry.index === currentIndex - 1)?.url || document.referrer;
  if (!url) return;
  const previous = new URL(url);
  if (previous.origin === listUrl.origin && previous.pathname === listUrl.pathname && history.length > 1) {
    event.preventDefault();
    history.back();
  }
}

function conversationSample(ticket) {
  const isRefund = ticket.id === '1048';
  const channel = isRefund ? 'Gmail' : ['Gmail', 'Discord', 'CSV'][Number(ticket.id) % 3];
  const message = (offset, author, body, kind, origin) => ({
    eventId: 'sample-' + offset, time: ticket.received.getTime() + offset * 60_000,
    author, message: body, kind, origin
  });
  const messages = isRefund ? [
    message(0, '민지', '어제 신청한 환불이 아직 입금되지 않았어요.', 'customer', 'A'),
    message(8, '민지', '다른 상품 환불 예정일도 함께 확인해 주세요.', 'customer', 'B'),
    message(23, '한서윤', '환불 신청 내역과 처리 일정을 확인해 보겠습니다.', 'reply', 'A'),
    message(33, '김하늘', '결제팀에서 처리 일정을 확인 중입니다.', 'transfer'),
    message(88, '박유진', '신청 순서대로 환불 처리 중입니다.', 'reply', 'A')
  ] : [message(0, '고객', ticket.title, 'customer', 'A')];
  return {
    channel, messages, attachments: isRefund ? basicAttachments : [],
    summary: isRefund ? '환불 입금 예정일을 확인해 달라는 문의입니다.' : ticket.title,
    related: isRefund ? ['1031', '1046'] : [],
    similar: isRefund ? ['1045', '1042'] : []
  };
}

function Message({ entry, channel }) {
  const { kind, author, time, message, origin } = entry;
  const labels = { reply: author === agent?.name ? '내 답변' : '고객 답변', transfer: '이관 메모', memo: '내부 메모', csv: '처리 기록' };
  const label = kind === 'customer' ? channel : labels[kind];
  return <li className={'detail-message ' + kind}>
    <p>{author} · {label}{origin && channel !== 'CSV' ? ' 원본 ' + origin : ''} · <time dateTime={new Date(time).toISOString()}>{formatDate(time).slice(11)}</time></p>
    <div>{message}</div>
  </li>;
}

function ToolPanel({ sample, onCollapse, canEdit, onAction }) {
  const noSimilar = { loading: '유사 티켓을 찾는 중…', error: '유사 티켓을 불러오지 못했습니다.', empty: '유사 티켓이 없습니다.' };
  function tickets(ids) {
    return <ul>{ids.map(id => {
      const item = findDemoTicket(id);
      return <li key={id}><a href={withStaffPreview('?ticket=' + id)}>#{id} {item.title}</a>
        <small>{item.category} · {item.status} · {formatDate(item.updated)} 업데이트</small></li>;
    })}</ul>;
  }
  return <aside className="detail-tools detail-card" aria-label="티켓 도구">
    <div className="detail-heading"><h2>티켓 도구</h2><button className="desk-button" type="button" onClick={onCollapse}>접기</button></div>
    {canEdit && <div className="detail-actions">
      <button className="desk-button" type="button" onClick={() => onAction('transfer')}>이관하기</button>
      <button className="desk-button" type="button" onClick={() => onAction('review')}>검토 요청</button>
    </div>}
    <details open><summary>이전 문의 · 동일 고객</summary>{sample.related.length ? tickets(sample.related) : <p>이전 문의가 없습니다.</p>}</details>
    <details open><summary>유사 티켓</summary>{previewState || !sample.similar.length
      ? <p role="status">{noSimilar[previewState || 'empty']}</p> : tickets(sample.similar)}</details>
    {sample.attachments.length > 0 && <details open><summary>첨부파일</summary>
      <ul>{sample.attachments.map(file => <li key={file.name}>{file.name}</li>)}</ul>
    </details>}
  </aside>;
}

function Composer({ channel, origins, height, onResize, onSubmit, saving, error }) {
  const [mode, setMode] = useState('reply');
  const [drafts, setDrafts] = useState({ reply: '', memo: '' });
  const [origin, setOrigin] = useState(origins.at(-1)?.origin ?? '');
  const drag = useRef(null);
  const draft = drafts[mode];
  function submit(event) {
    event.preventDefault();
    if (saving || !draft.trim()) return;
    const kind = mode === 'memo' ? 'memo' : channel === 'CSV' ? 'csv' : 'reply';
    onSubmit({ kind, message: draft.trim(), ...(kind === 'reply' && { channel, origin }) }).then(success => {
      if (success) setDrafts(current => ({ ...current, [mode]: '' }));
    });
  }
  return <form className="detail-composer detail-card" onSubmit={submit} style={{ '--composer-size': height + 'px' }}>
    <div className="detail-grip" role="separator" aria-label="답변 작성 패널 높이" aria-orientation="horizontal"
      aria-valuemin={205} aria-valuemax={420} aria-valuenow={height} tabIndex={0}
      onPointerDown={event => {
        event.currentTarget.setPointerCapture(event.pointerId);
        drag.current = { start: event.clientY, height };
      }}
      onPointerMove={event => {
        if (drag.current) onResize(drag.current.height + drag.current.start - event.clientY);
      }}
      onPointerUp={() => { drag.current = null; }} onLostPointerCapture={() => { drag.current = null; }}
      onKeyDown={event => {
        if (event.key === 'ArrowUp' || event.key === 'ArrowDown') {
          event.preventDefault();
          onResize(height + (event.key === 'ArrowUp' ? 16 : -16));
        }
      }}><span /></div>
    <div className="detail-actions detail-modes">
      <button className="desk-button" type="button" aria-pressed={mode === 'reply'} onClick={() => setMode('reply')}>{channel === 'CSV' ? '처리 기록' : '고객 답변'}</button>
      <button className="desk-button" type="button" aria-pressed={mode === 'memo'} onClick={() => setMode('memo')}>내부 메모</button>
    </div>
    {mode === 'reply' && channel !== 'CSV' && <label className="detail-origin">회신 대상
      <select value={origin} onChange={event => setOrigin(event.target.value)}>{origins.map(entry =>
        <option value={entry.origin} key={entry.origin}>{targetLabel[channel]} {entry.origin} · {formatDate(entry.time).slice(11)}</option>
      )}</select>
    </label>}
    {mode === 'memo' && <p className="detail-help">내부 담당자에게 표시됩니다.</p>}
    <textarea aria-label={mode === 'memo' ? '내부 메모 내용' : '답변 내용'} maxLength={2000} required value={draft}
      onChange={event => setDrafts({ ...drafts, [mode]: event.target.value })}
      placeholder={mode === 'memo' ? '내부 메모를 입력해 주세요.' : '답변을 입력해 주세요.'} />
    {error && <p role="alert" className="detail-error">{error}</p>}
    <div className="detail-actions detail-commit"><small>{draft.length.toLocaleString('ko-KR')} / 2,000</small>
      <button type="submit" className="desk-button desk-button-solid" disabled={saving || !draft.trim()}>
        {saving ? '저장 중…' : mode === 'memo' ? '메모 저장' : channel === 'CSV' ? '처리 기록 저장' : error ? '다시 보내기' : '답변 보내기'}
      </button>
    </div>
  </form>;
}

function TicketDetailPage() {
  const [events, setEvents] = useState([]);
  const [ready, setReady] = useState(false);
  const [loadError, setLoadError] = useState(false);
  const [now, setNow] = useState(Date.now);
  const [panel, showPanel] = useState(true);
  const [height, setHeight] = useState(238);
  const [dialog, setDialog] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [collaborationError, setCollaborationError] = useState('');
  const [saving, setSaving] = useState(false);
  const pending = useRef(false);
  const failOnce = useRef(params.get('delivery') === 'fail');
  const chat = useRef(null);
  useEffect(() => {
    if (!ticket) return;
    let alive = true;
    const reload = () => loadTicketEvents(ticket.id).then(records => {
      if (alive) {
        setEvents(records); setReady(true); setLoadError(false); setNow(Date.now());
      }
    }).catch(() => { if (alive) setLoadError(true); });
    reload();
    const off = subscribeTicketEvents(reload);
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => { alive = false; off(); clearInterval(timer); };
  }, []);
  useEffect(() => {
    if (events.length && chat.current) chat.current.scrollTop = chat.current.scrollHeight;
  }, [events.length]);

  const state = ticketState(events, now);
  const status = state.status ?? ticket?.status;
  const closed = status === '문의 종료';
  const ticketRight = hasStaffRight(agent, 'ticket');
  const receivedReview = agent?.account === 'active' && events.some(item => item.kind === 'review' && item.recipientId === agent.id);
  const canEdit = ticketRight && !closed && ready && !loadError;
  const sample = ticket && conversationSample(ticket);
  const messages = sample ? [...sample.messages, ...events.filter(item => item.kind !== 'review')].sort((a, b) => a.time - b.time) : [];
  const previousReplies = messages.filter(item => item.kind === 'reply');

  async function commit(fields) {
    if (!canEdit || pending.current) return false;
    pending.current = true;
    setSaving(true);
    setError('');
    setCollaborationError('');
    setNotice('');
    try {
      if (fields.kind === 'reply' && failOnce.current) {
        failOnce.current = false;
        setError('연결 문제로 답변을 보내지 못했습니다. 다시 보내 주세요.');
        return false;
      }
      const record = await appendTicketEvent({
        ...fields, ticketId: ticket.id, actorId: agent.id, author: agent.name,
        ticket: { id: ticket.id, title: ticket.title, category: ticket.category, sentiment: ticket.emotion,
          status, receivedAt: ticketDate(ticket.received) }
      });
      setEvents(current => [...current.filter(item => item.eventId !== record.eventId), record]);
      setNow(Date.now());
      setNotice({ reply: '답변을 보냈습니다.', csv: '처리 기록을 저장했습니다.', memo: '내부 메모를 저장했습니다.',
        review: '선택한 담당자의 받은 검토 요청으로 보냈습니다.', transfer: '새 담당자의 받은 이관으로 보냈습니다.' }[fields.kind]);
      return true;
    } catch (failure) {
      const message = failure.message === '문의가 종료되었습니다.' ? failure.message : '저장하지 못했습니다. 다시 시도해 주세요.';
      if (dialog) setCollaborationError(message);
      else setError(message);
      setNow(Date.now());
      return false;
    } finally {
      setSaving(false);
      pending.current = false;
    }
  }

  return <DeskLayout active="tickets"><section className="ticket-detail" aria-labelledby="ticket-detail-title">
    <header className="detail-header">
      <a className="desk-button" href={listHref} onClick={returnToList}>‹ 티켓 목록</a>
      <h1 id="ticket-detail-title">{ticket && (ticketRight || receivedReview) ? '#' + ticket.id + ' ' + ticket.title : '티켓 상세'}</h1>
      {ticket && (ticketRight || receivedReview) && <div className="detail-meta">
        <span data-ticket-status={status}>{status === '답변완료' ? '답변 완료' : status}</span>
        <span>{sample.channel}</span><span>{ticket.category}</span>
        <span data-emotion={ticket.emotion}>{ticket.emotion}</span>
        <p>담당 {state.assignee ?? (ticket.assignee || '미할당')} · 접수 {formatDate(ticket.received)}</p>
      </div>}
      {notice && <p className="detail-notice" role="status">{notice}</p>}
      {loadError && <p role="alert" className="detail-error">저장된 티켓 기록을 불러오지 못했습니다. 화면을 새로 고쳐 주세요.</p>}
    </header>
    {!ticket ? <p role="status">티켓을 찾을 수 없습니다.</p>
      : !ticketRight && !receivedReview ? <p role="status">{!ready && !loadError && agent?.account === 'active' ? '티켓을 불러오는 중입니다.' : '이 티켓에 접근할 권한이 필요합니다.'}</p>
      : <div className={'detail-layout' + (panel ? '' : ' detail-full')}>
        <div className="detail-column">
          {!panel && <button className="desk-button detail-expand" type="button" onClick={() => showPanel(true)}>보조 패널 펼치기</button>}
          <div className="detail-chat detail-card">
            <h2>문의 대화</h2>
            <p className="detail-summary"><strong>AI 요약</strong> {ticket.category === '분석 실패' || params.get('summary') === 'fail'
              ? '요약을 불러오지 못했습니다.' : ticket.category === '분석 중' ? '요약하는 중…' : sample.summary}</p>
            <ol className="detail-messages" ref={chat}>
              {messages.filter(item => ticketRight || item.kind === 'customer' || item.kind === 'reply').map(entry =>
                <Message key={entry.eventId} entry={entry} channel={sample.channel} />)}
            </ol>
          </div>
          {closed && <footer className="detail-closed detail-card">
            <strong>문의 종료</strong><time dateTime={new Date(state.closeAt).toISOString()}>{formatDate(state.closeAt)}에 종료되었습니다.</time>
          </footer>}
          {canEdit && <Composer channel={sample.channel} origins={sample.messages.filter(item => item.kind === 'customer')}
            height={height} onResize={value => setHeight(Math.max(205, Math.min(420, Math.round(value))))}
            onSubmit={commit} saving={saving} error={error} />}
        </div>
        {panel && <ToolPanel sample={sample} onCollapse={() => showPanel(false)} canEdit={canEdit}
          onAction={kind => { setCollaborationError(''); setDialog(kind); }} />}
      </div>}
    {dialog && canEdit && <HandoffDialog kind={dialog} people={loadStaff().filter(person => person.id !== agent.id)}
      previousReplies={previousReplies} onClose={() => setDialog('')} saving={saving} error={collaborationError}
      onSubmit={fields => commit({ ...fields, kind: dialog }).then(success => { if (success) setDialog(''); })} />}
  </section></DeskLayout>;
}

createRoot(document.getElementById('app')).render(<TicketDetailPage />);
