import React, { useEffect, useRef, useState } from 'react';
import { hasStaffRight } from '../00-Shared/00-StaffAccess.js';

export function HandoffDialog({ kind, people, previousReplies, onClose, onSubmit, saving, error }) {
  const dialog = useRef(null);
  const transfer = kind === 'transfer';
  const [query, setQuery] = useState('');
  const [selected, select] = useState(null);
  const [open, show] = useState(false);
  const [highlight, setHighlight] = useState(-1);
  const [memo, setMemo] = useState('');
  const [files, setFiles] = useState([]);
  const results = people.filter(person => transfer ? hasStaffRight(person, 'ticket') : person.account === 'active')
    .filter(person => [person.name, person.team].some(value => value.toLowerCase().includes(query.trim().toLowerCase())));
  useEffect(() => {
    const node = dialog.current;
    node.showModal();
    return () => node.close();
  }, []);

  function choose(person) {
    select(person);
    setQuery(person.name);
    show(false);
    setHighlight(-1);
  }
  function keys(event) {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      show(true);
      const move = event.key === 'ArrowDown' ? 1 : -1;
      setHighlight(index => Math.max(0, Math.min(results.length - 1, index + move)));
    }
    if (event.key === 'Enter' && open && results[highlight]) {
      event.preventDefault();
      choose(results[highlight]);
    }
    if (event.key === 'Escape' && open) {
      event.preventDefault();
      show(false);
    }
  }
  function submit(event) {
    event.preventDefault();
    if (!selected || !memo.trim() || saving) return;
    const { reference } = Object.fromEntries(new FormData(event.currentTarget));
    onSubmit({ recipientId: selected.id, recipient: selected.name, message: memo.trim(), reference, files });
  }

  return <dialog className="detail-dialog" ref={dialog} aria-labelledby="handoff-title"
    onCancel={event => { event.preventDefault(); if (!saving) onClose(); }}>
    <form onSubmit={submit}>
      <h2 id="handoff-title">{transfer ? '이관하기' : '검토 요청'}</h2>
      <div className="detail-recipient" onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) show(false); }}>
        <label htmlFor="handoff-recipient">{transfer ? '새 담당자' : '검토 담당자'} (필수)</label>
        <input id="handoff-recipient" role="combobox" aria-autocomplete="list" aria-controls="handoff-results"
          aria-expanded={open} aria-activedescendant={open && results[highlight] ? 'handoff-' + results[highlight].id : undefined}
          value={query} placeholder="이름 또는 팀 검색" autoComplete="off" autoFocus onKeyDown={keys}
          onFocus={() => show(true)} onChange={event => { select(null); setQuery(event.target.value); setHighlight(-1); show(true); }} />
        {open && <div className="detail-recipient-menu" id="handoff-results" role="listbox">
          {results.map((person, index) => <button type="button" role="option" key={person.id}
            id={'handoff-' + person.id} aria-selected={highlight === index} onClick={() => choose(person)}>
            <strong>{person.name}</strong><span>{person.team}</span>
          </button>)}
          {!results.length && <p>검색 결과가 없습니다.</p>}
        </div>}
      </div>
      <label>{transfer ? '이관 메시지' : '검토 메모'} (필수)
        <textarea required rows={3} value={memo} maxLength={2000} onChange={event => setMemo(event.target.value)} />
      </label>
      {transfer ? <label>이전 답변 첨부 (선택)<select name="reference" defaultValue="">
        <option value="">이전 답변 선택</option>
        {previousReplies.map(reply => <option key={reply.eventId} value={reply.message}>{reply.message}</option>)}
      </select></label> : <label>참고 내용 (선택)<textarea name="reference" rows={2} maxLength={2000} /></label>}
      <div className="detail-attachments">
        <label className="detail-file-label">첨부파일 (선택)
          <input type="file" multiple onChange={event => {
            const added = Array.from(event.target.files);
            setFiles(current => [...current, ...added]);
            event.target.value = '';
          }} />
        </label>
        {!!files.length && <ul>{files.map((file, index) => <li key={index}>
          <span>{file.name}</span><button type="button" className="desk-button" aria-label={file.name + ' 제거'}
            onClick={() => setFiles(current => current.filter((_, position) => position !== index))}>제거</button>
        </li>)}</ul>}
      </div>
      {error && <p className="detail-error" role="alert">{error}</p>}
      <div className="detail-actions">
        <button type="button" className="desk-button" onClick={onClose} disabled={saving}>취소</button>
        <button type="submit" className="desk-button desk-button-solid" disabled={!selected || !memo.trim() || saving}>
          {saving ? '저장 중…' : transfer ? '이관하기' : '요청 보내기'}
        </button>
      </div>
    </form>
  </dialog>;
}
