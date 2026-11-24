import React, { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { DeskLayout, defaultAccountName } from '../00-Shared/00-DeskLayout.jsx';
import { requestGroups, ticketProfiles } from './05-InboxData.js';
import { loadReplies, readReply, saveReply } from './05-ReplyStore.js';

const tabs = { transfer: '받은 이관', review: '받은 검토 요청', reply: '받은 검토 답변' };
const columns = { transfer: '이관 메시지', review: '검토 메모', reply: '답변' };
const allRequests = Object.values(requestGroups).flat();
const requestsById = new Map(allRequests.map(request => [request.requestId, request]));
const params = new URLSearchParams(location.search);
const account = defaultAccountName;
const initialTab = Object.hasOwn(tabs, params.get('tab')) ? params.get('tab') : 'transfer';
const emptyDemo = params.get('empty') === '1';
const readKey = 'voc.inbox.confirmedRequests';

function readConfirmed() {
  try {
    const stored = JSON.parse(sessionStorage.getItem(readKey) || '[]');
    return new Set(Array.isArray(stored) ? stored : []);
  } catch {
    return new Set();
  }
}

const sentimentClasses = {
  긍정: 'positive', 중립: 'neutral', 부정: 'negative', '매우 부정': 'very-negative',
  '분석 중': 'pending', '분석 실패': 'negative'
};

function formatReceivedTime(timestamp) {
  const date = new Date(timestamp);
  const pad = number => String(number).padStart(2, '0');
  return `${pad(date.getMonth() + 1)}/${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function Drawer({ title, onClose, children, footer }) {
  const dialog = useRef(null);
  const heading = useRef(null);
  useEffect(() => { dialog.current.showModal(); }, []);
  useEffect(() => { heading.current.focus(); }, [title]);

  return <dialog ref={dialog} className="inbox-drawer" aria-labelledby="inbox-drawer-title"
    onCancel={event => { event.preventDefault(); onClose(); }}
    onClick={event => { if (event.target === event.currentTarget) onClose(); }}>
    <section className="inbox-drawer-panel">
      <header className="inbox-drawer-header">
        <h2 ref={heading} tabIndex={-1} id="inbox-drawer-title">{title}</h2>
        <button className="desk-button" type="button" onClick={onClose}>닫기</button>
      </header>
      <div className="inbox-drawer-body" key={title}>{children}</div>
      {footer && <footer className="inbox-drawer-footer">{footer}</footer>}
    </section>
  </dialog>;
}

function TicketLink({ id }) {
  return <a className="desk-button desk-button-solid" href={`../04-TicketDetailPage/04-TicketDetailPage.html?ticket=${encodeURIComponent(id)}`}>티켓 열기</a>;
}

function RequestDetail({ request, reply, ready, onSend, onClose }) {
  const [view, setView] = useState('detail');
  const [draft, setDraft] = useState('');
  const [error, setError] = useState('');
  const [sending, setSending] = useState(false);
  const pending = useRef(false);
  const isReview = request.requestId.startsWith('review-');
  const profile = ticketProfiles[request.id];
  const detailTitle = isReview ? '받은 검토 요청 상세' : '받은 이관 상세';
  const noteTitle = isReview ? '검토 메모' : '이관 메시지';

  async function submit(event) {
    event.preventDefault();
    if (!draft.trim() || reply || pending.current || !ready) return;
    pending.current = true;
    setSending(true);
    setError('');
    try {
      await onSend(request, draft.trim());
      setDraft('');
      setView('detail');
    } catch {
      setError('답변을 보내지 못했습니다. 다시 시도해 주세요.');
    } finally {
      pending.current = false;
      setSending(false);
    }
  }

  function close() {
    if (!pending.current) onClose();
  }

  if (view === 'history') {
    const currentTransfer = [account, profile.transferredAt, request.memo];
    return <Drawer title="이관 이력" onClose={close} footer={<TicketLink id={request.id} />}>
      <div className="inbox-history-heading">
        <p className="inbox-ticket-meta">#{request.id} · {request.title}</p>
        <button type="button" className="desk-button" onClick={() => setView('detail')}>돌아가기</button>
      </div>
      <ol className="inbox-history">
        {[currentTransfer, ...profile.priorTransfers].map(([owner, time, message]) => <li key={time} className="inbox-history-card">
          <dl className="inbox-detail-meta"><dt>담당자</dt><dd>{owner}</dd><dt>일시</dt><dd>{time}</dd></dl>
          <h3>이관 메시지</h3>
          <p>{message}</p>
        </li>)}
      </ol>
    </Drawer>;
  }

  if (view === 'write' && !reply) {
    return <Drawer title="검토 답변 작성" onClose={close} footer={<>
      <button type="button" className="desk-button" disabled={sending} onClick={() => setView('detail')}>취소</button>
      <button className="desk-button desk-button-solid" form="inbox-reply-form" type="submit"
        disabled={!draft.trim() || sending}>{sending ? '보내는 중…' : '보내기'}</button>
    </>}>
      <dl className="inbox-detail-meta"><dt>받는 사람</dt><dd>{request.sender}</dd></dl>
      <form id="inbox-reply-form" onSubmit={submit}>
        <label htmlFor="inbox-reply-text">답변</label>
        <textarea id="inbox-reply-text" value={draft} onChange={event => setDraft(event.target.value)}
          placeholder="검토한 내용을 입력해 주세요." rows={7} disabled={sending} />
        <p className="inbox-form-help">요청당 답변을 한 번 보낼 수 있습니다.</p>
        {error && <p className="inbox-send-error" role="alert">{error}</p>}
      </form>
    </Drawer>;
  }

  return <Drawer title={detailTitle} onClose={close} footer={<>
    {isReview && !reply && <button type="button" className="desk-button" disabled={!ready}
      onClick={() => { setError(''); setView('write'); }}>회신하기</button>}
    <TicketLink id={request.id} />
  </>}>
    <p className="inbox-ticket-meta">#{request.id}</p>
    <h3>{request.title}</h3>
    <dl className="inbox-detail-meta">
      <dt>보낸 사람</dt><dd>{request.sender}</dd>
      <dt>접수일자</dt><dd>{profile.receivedAt}</dd>
      <dt>받은 시각</dt><dd>{request.time}</dd>
      <dt>상태</dt><dd>확인</dd>
      <dt>티켓 처리 상태</dt><dd>{request.status}</dd>
      <dt>감성</dt><dd><span className={`inbox-sentiment inbox-sentiment-${sentimentClasses[request.sentiment] ?? 'pending'}`}>{request.sentiment}</span></dd>
    </dl>
    <section className="inbox-note">
      <div className="inbox-note-heading">
        <h3>{noteTitle}</h3>
        {!isReview && <button type="button" className="desk-button" onClick={() => setView('history')}>이관 이력</button>}
      </div>
      <p>{request.memo}</p>
    </section>
    {request.extra && <section className="inbox-reference">
      <h3>{isReview ? '참고 자료' : '전달된 답변'}</h3>
      <p className={isReview ? 'inbox-file' : 'inbox-shared-reply'}>{request.extra}</p>
    </section>}
    {reply && <section className="inbox-sent-reply" aria-live="polite">
      <h3>보낸 답변</h3><p>{reply.message}</p>
    </section>}
    {isReview && !ready && <p className="inbox-send-error" role="status">답변 저장 상태를 확인할 수 없습니다. 화면을 새로 고쳐 주세요.</p>}
  </Drawer>;
}

function ReplyDetail({ reply, request, onClose }) {
  const [showRequest, setShowRequest] = useState(false);
  return <Drawer title="받은 검토 답변 상세" onClose={onClose} footer={<TicketLink id={request.id} />}>
    <p className="inbox-ticket-meta">#{request.id}</p><h3>{request.title}</h3>
    <dl className="inbox-detail-meta">
      <dt>보낸 사람</dt><dd>{reply.sender}</dd>
      <dt>받은 시각</dt><dd>{formatReceivedTime(reply.sentAt)}</dd>
      <dt>상태</dt><dd>{reply.read ? '확인' : '미확인'}</dd>
    </dl>
    <section className="inbox-note"><h3>답변 내용</h3><p>{reply.message}</p></section>
    <section className="inbox-reference">
      <button className="desk-button" type="button" aria-expanded={showRequest} aria-controls="inbox-original-request"
        onClick={() => setShowRequest(value => !value)}>원래 검토 요청 보기</button>
      <div className="inbox-shared-reply" id="inbox-original-request" hidden={!showRequest}>
        <h3>원래 검토 요청</h3><p>{request.memo}</p>
      </div>
    </section>
  </Drawer>;
}

function InboxPage() {
  const saved = history.state?.inbox;
  const [tab, setTab] = useState(saved?.tab ?? initialTab);
  const [scope, setScope] = useState(saved?.scope ?? 'all');
  const [input, setInput] = useState(saved?.input ?? '');
  const [query, setQuery] = useState(saved?.query ?? '');
  const [selectedId, setSelectedId] = useState(saved ? saved.selectedId : params.get('request'));
  const [read, setRead] = useState(readConfirmed);
  const [replies, setReplies] = useState([]);
  const [storeState, setStoreState] = useState('loading');
  const [replyError, setReplyError] = useState('');
  const focusedRow = useRef(null);
  const listHeading = useRef(null);
  const searchInput = useRef(null);

  useEffect(() => {
    let active = true;
    const refresh = () => loadReplies().then(records => {
      if (active) { setReplies(records); setStoreState('ready'); }
    }).catch(() => {
      if (active) setStoreState('error');
    });
    refresh();
    window.addEventListener('focus', refresh);
    return () => {
      active = false;
      window.removeEventListener('focus', refresh);
    };
  }, []);

  const showRequests = !emptyDemo;
  const selectedRequest = showRequests ? requestsById.get(selectedId) : null;
  const receivedReplies = emptyDemo ? [] : replies.filter(reply => reply.recipient === account)
    .filter(reply => requestsById.has(reply.requestId))
    .sort((a, b) => b.sentAt - a.sentAt);
  const selectedReply = receivedReplies.find(reply => 'reply:' + reply.requestId === selectedId);

  useEffect(() => {
    if (!selectedRequest?.unread) return;
    setRead(previous => {
      if (previous.has(selectedRequest.requestId)) return previous;
      const updated = new Set(previous).add(selectedRequest.requestId);
      try { sessionStorage.setItem(readKey, JSON.stringify([...updated])); } catch {}
      return updated;
    });
  }, [selectedRequest]);

  useEffect(() => {
    history.replaceState({ ...history.state, inbox: { tab, scope, input, query, selectedId } }, '');
  }, [tab, scope, input, query, selectedId]);

  function updateReply(record) {
    setReplies(previous => [...previous.filter(reply => reply.requestId !== record.requestId), record]);
  }
  async function sendResponse(request, message) {
    const record = {
      requestId: request.requestId, sender: account, recipient: request.sender,
      message, sentAt: Date.now(), read: false
    };
    try {
      await saveReply(record);
      updateReply(record);
    } catch (error) {
      if (error?.name !== 'ConstraintError') throw error;
      const existing = (await loadReplies()).find(reply => reply.requestId === request.requestId);
      if (!existing) throw error;
      updateReply(existing);
    }
  }

  function openRow(event, row) {
    focusedRow.current = event.currentTarget;
    setSelectedId(row.key);
    setReplyError('');
    if (tab === 'reply' && row.unread) {
      readReply(row.requestId, account).then(record => { if (record) updateReply(record); })
        .catch(() => setReplyError('답변의 확인 상태를 저장하지 못했습니다.'));
    }
  }
  function selectTab(next) {
    setTab(next);
    setInput('');
    setQuery('');
  }
  function closeDetail() {
    setSelectedId(null);
    requestAnimationFrame(() => {
      const trigger = focusedRow.current;
      if (trigger?.isConnected) trigger.focus();
      else if (searchInput.current) searchInput.current.focus();
      else listHeading.current?.focus();
    });
  }

  const isUnread = request => request.unread && !read.has(request.requestId);
  const received = tab === 'reply' ? receivedReplies.map(reply => {
    const request = requestsById.get(reply.requestId);
    return { ...request, key: 'reply:' + request.requestId, summary: reply.message, sender: reply.sender,
      time: formatReceivedTime(reply.sentAt), unread: !reply.read };
  }) : (showRequests ? requestGroups[tab] : []).map(request => ({
    ...request, key: request.requestId, unread: isUnread(request)
  }));
  const visible = tab === 'reply' ? received : received.filter(row =>
    (scope !== 'unread' || row.unread) && [row.title, '#' + row.id, row.sender]
      .some(value => value.toLocaleLowerCase().includes(query))
  );
  const emptyMessage = tab === 'reply'
    ? ({ loading: '받은 검토 답변을 불러오는 중입니다.', error: '받은 검토 답변을 불러오지 못했습니다.' }[storeState] ?? '받은 검토 답변이 없습니다.')
    : !received.length ? '받은 요청이 없습니다.' : query ? '검색 결과가 없습니다.' : '미확인 요청이 없습니다.';

  return <DeskLayout active="inbox">
    <section className="inbox-page" aria-labelledby="inbox-title">
      <h1 id="inbox-title" ref={listHeading} tabIndex={-1}>받은 요청</h1>
      <nav className="inbox-tabs" aria-label="받은 요청 유형">
        {Object.entries(tabs).map(([key, label]) => <button key={key} type="button"
          className="desk-button" aria-pressed={tab === key} onClick={() => selectTab(key)}>
          {label}<span className="inbox-count" aria-label="미확인 건수">
            {key === 'reply' ? receivedReplies.filter(reply => !reply.read).length : showRequests ? requestGroups[key].filter(isUnread).length : 0}
          </span>
        </button>)}
      </nav>
      {tab !== 'reply' && <>
        <div className="inbox-toolbar">
          <form className="inbox-search" onSubmit={event => { event.preventDefault(); setQuery(input.trim().toLocaleLowerCase()); }}>
            <input ref={searchInput} type="search" value={input} onChange={event => setInput(event.target.value)}
              aria-label="제목, 티켓번호, 보낸 사람으로 검색" placeholder="제목, 티켓번호, 보낸 사람으로 검색" />
            <button className="desk-button desk-button-solid" type="submit">검색</button>
            {query && <button className="desk-button" type="button" onClick={() => { setInput(''); setQuery(''); }}>검색 초기화</button>}
          </form>
          <div className="inbox-scopes" role="group" aria-label="확인 상태">
            <button className="desk-button" type="button" aria-pressed={scope === 'all'} onClick={() => setScope('all')}>전체</button>
            <button className="desk-button" type="button" aria-pressed={scope === 'unread'} onClick={() => setScope('unread')}>미확인</button>
          </div>
        </div>
        <p className="inbox-sort">받은 시각 최신순</p>
      </>}
      {replyError && <p className="inbox-send-error" role="alert">{replyError}</p>}
      <div className={`inbox-table-wrap${tab === 'reply' ? ' inbox-reply-table' : ''}`}>
        <table className="inbox-table">
          <colgroup><col className="inbox-col-read"/><col className="inbox-col-ticket"/><col className="inbox-col-note"/><col className="inbox-col-category"/><col className="inbox-col-sender"/><col className="inbox-col-date"/></colgroup>
          <thead><tr>{['상태', '티켓', columns[tab], '카테고리', '보낸 사람', '받은 시각'].map(label => <th key={label} scope="col">{label}</th>)}</tr></thead>
          <tbody>{visible.map(row => <tr key={row.key}>
            <td><span className="inbox-read-label">{row.unread ? '미확인' : '확인'}</span>{row.unread && <span className="inbox-unread-dot" aria-hidden="true"/>}</td>
            <th scope="row">
              <button className="inbox-row-open" type="button" onClick={event => openRow(event, row)}>{row.title}</button>
              <div className="inbox-ticket-meta">#{row.id}</div>
            </th>
            <td className="inbox-summary"><span>{row.summary}</span></td>
            <td>{row.category}</td>
            <td>{row.sender}</td>
            <td className="inbox-received">{row.time}<span aria-hidden="true">›</span></td>
          </tr>)}</tbody>
        </table>
        {!visible.length && <div className="inbox-empty" role="status">
          <strong>{emptyMessage}</strong>
          {tab !== 'reply' && query && received.length > 0 && <p>다른 검색어를 입력하거나 검색을 초기화해 주세요.</p>}
        </div>}
      </div>
      {selectedRequest && <RequestDetail key={selectedRequest.requestId} request={selectedRequest}
        ready={storeState === 'ready'} reply={replies.find(reply => reply.requestId === selectedRequest.requestId && reply.sender === account)}
        onSend={sendResponse} onClose={closeDetail} />}
      {selectedReply && <ReplyDetail key={selectedReply.requestId} reply={selectedReply}
        request={requestsById.get(selectedReply.requestId)} onClose={closeDetail} />}
    </section>
  </DeskLayout>;
}

createRoot(document.getElementById('app')).render(<InboxPage />);
