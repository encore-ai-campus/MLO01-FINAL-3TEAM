import React, { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { DeskLayout, settingsUrl } from '../00-Shared/00-DeskLayout.jsx';
import { loadOperations, OpinionIntake, saveOperations, StaffOperations } from './07-StaffOperations.jsx';

import { currentStaff, hasStaffRight, loadStaff, roleDefaults, rights, saveStaff, staffRoles as roles, teams, withStaffPreview } from '../00-Shared/00-StaffAccess.js';

const actor = '최지민';
const accountLabels = { active: '활성', pending: '설정 대기', inactive: '비활성' };
function logChange(item, before = null, after = null) {
  const stamp = new Intl.DateTimeFormat('sv-SE', {
    timeZone: 'Asia/Seoul', dateStyle: 'short', timeStyle: 'short'
  }).format(new Date());
  return { time: stamp, actor, item, before, after };
}

const permissionValue = enabled => enabled ? '허용' : '제한';
const describeRight = (value, override) =>
  permissionValue(value) + ' · ' + (override ? '개인 예외' : '역할 기본값');

function permissionChanges(person, fields) {
  return [
    ['소속 팀', person.team, fields.team],
    ['역할', person.role, fields.role],
    ...Object.entries(rights).map(([key, label]) => [label,
      describeRight(person.effective[key], Object.hasOwn(person.overrides, key)),
      describeRight(fields.overrides[key] ?? roleDefaults(fields.role)[key], Object.hasOwn(fields.overrides, key))])
  ].filter(([, before, after]) => before !== after).map(([item, before, after]) => logChange(item, before, after));
}

function Badge({ children, variant = '' }) {
  return <span className={'staff-badge ' + variant}>{children}</span>;
}

function StaffList({ staff, filters, setFilters, openPerson, onIssue }) {
  const query = filters.query.trim().toLowerCase();
  const rows = staff.filter(person =>
    (person.name.toLowerCase().includes(query) || person.email.toLowerCase().includes(query)) &&
    (!filters.team || person.team === filters.team) && (!filters.account || person.account === filters.account)
  );
  function filter(key, value) {
    setFilters({ ...filters, [key]: value });
  }
  return <>
    <div className="staff-section-head"><h2>사원 목록</h2><button type="button" className="desk-button desk-button-solid" onClick={onIssue}>사원 계정 발급</button></div>
    <div className="staff-filters">
      <input type="search" aria-label="이름 또는 회사 이메일 검색" placeholder="이름 또는 회사 이메일 검색" value={filters.query} onChange={event => filter('query', event.target.value)} />
      <select aria-label="소속 팀" value={filters.team} onChange={event => filter('team', event.target.value)}>
        <option value="">팀 전체</option>{teams.map(team => <option key={team}>{team}</option>)}
      </select>
      <select aria-label="계정 상태" value={filters.account} onChange={event => filter('account', event.target.value)}>
        <option value="">계정 상태 전체</option>{Object.entries(accountLabels).map(([key, label]) => <option value={key} key={key}>{label}</option>)}
      </select>
    </div>
    <div className="staff-table-wrap">
      <table className="staff-table" aria-label="사원 목록">
        <thead><tr>{['이름', '회사 이메일', '소속 팀', '역할', '계정 상태'].map(label => <th scope="col" key={label}>{label}</th>)}</tr></thead>
        <tbody>
          {rows.map(person => {
            return <tr key={person.id}>
              <th scope="row"><button type="button" className="staff-name" onClick={() => openPerson(person.id)}>{person.name}</button></th>
              <td>{person.email}</td><td>{person.team}</td><td>{person.role}</td>
              <td><Badge variant={person.account}>{accountLabels[person.account]}</Badge></td>
            </tr>;
          })}
          {rows.length === 0 && <tr><td colSpan="5" className="staff-no-results">검색 조건과 일치하는 사원이 없습니다.</td></tr>}
        </tbody>
      </table>
    </div>
  </>;
}

function StaffDetail({ person, back, open }) {
  const heading = useRef(null);
  useEffect(() => { heading.current?.focus(); }, [person.id]);
  return <>
    <button type="button" className="desk-button" onClick={back}>‹ 사원 목록</button>
    <div className="staff-section-head staff-profile-head">
      <div>
        <div className="staff-name-line"><h2 tabIndex={-1} ref={heading}>{person.name}</h2><Badge variant={person.account}>계정 {accountLabels[person.account]}</Badge></div>
        <p className="desk-sub">{person.team} · {person.role}</p>
      </div>
      <div className="staff-actions">
        <button className="desk-button" type="button" disabled={person.account === 'inactive'} onClick={() => open('reset')}>비밀번호 초기화</button>
        <button className="desk-button" type="button" disabled={person.account === 'inactive'} onClick={() => open('deactivate')}>계정 비활성화</button>
      </div>
    </div>
    <div className="staff-detail-grid">
      <section className="staff-card"><h3>기본 정보</h3>
        <dl><div><dt>회사 이메일</dt><dd>{person.email}</dd></div><div><dt>소속 팀 / 역할</dt><dd>{person.team} / {person.role}</dd></div></dl>
      </section>
      <section className="staff-card">
        <div className="staff-card-head"><h3>업무 권한</h3><button type="button" className="desk-button" onClick={() => open('permissions')}>소속·권한 변경</button></div>
        <dl>{Object.entries(rights).map(([key, label]) => <div key={key}><dt>{label}</dt><dd>{describeRight(person.effective[key], Object.hasOwn(person.overrides, key))}</dd></div>)}</dl>
      </section>
      <section className="staff-card staff-history-card"><h3>변경 이력</h3>
        {person.history.length ? <div className="staff-table-wrap"><table className="staff-table" aria-label="변경 이력">
          <thead><tr>{['일시', '변경한 사람', '변경 항목', '변경 전', '변경 후'].map(label => <th key={label} scope="col">{label}</th>)}</tr></thead>
          <tbody>{person.history.map((item, index) => <tr key={index}>
            <td>{item.time}</td><td>{item.actor}</td><th scope="row">{item.item ?? item.message}</th>
            <td>{item.before ?? '—'}</td><td>{item.after ?? '—'}</td>
          </tr>)}</tbody>
        </table></div> : <p className="desk-sub">변경 이력이 없습니다.</p>}
      </section>
    </div>
  </>;
}

const dialogs = {
  issue: { title: '사원 계정 발급', action: '계정 발급' },
  permissions: { title: '소속·권한 변경', action: '변경 저장' },
  reset: { title: '비밀번호 초기화', action: '초기화' },
  deactivate: { title: '계정 비활성화', action: '비활성화' }
};

function StaffDialog({ kind, person, staff, close, save, openPerson }) {
  const dialog = useRef(null);
  const [name, setName] = useState('');
  const [email, setEmail] = useState('');
  const [team, setTeam] = useState(person?.team ?? '');
  const [role, setRole] = useState(person?.role ?? roles[0]);
  const [overrides, setOverrides] = useState(person?.overrides ?? {});
  const [error, setError] = useState('');
  const [duplicate, setDuplicate] = useState(null);
  const { title, action } = dialogs[kind];
  const hasWork = kind === 'deactivate' && (person.tickets > 0 || person.reviews > 0);
  useEffect(() => {
    const node = dialog.current;
    node.showModal();
    return () => node.close();
  }, []);
  function submit(event) {
    event.preventDefault();
    if (hasWork) return;
    if (kind === 'issue') {
      const emailValue = email.trim().toLowerCase();
      if (!name.trim()) return setError('사원 이름을 입력해 주세요.');
      const existing = staff.find(entry => entry.email.trim().toLowerCase() === emailValue);
      if (existing) {
        setDuplicate(existing);
        setError('이미 등록된 회사 이메일입니다.');
        return;
      }
      save({ name: name.trim(), email: emailValue, team, role });
      return;
    }
    save({ team, role, overrides });
  }
  return <dialog className="staff-dialog" ref={dialog} aria-labelledby="staff-dialog-title" onCancel={event => { event.preventDefault(); close(); }}>
    <form onSubmit={submit}>
      <div className="staff-dialog-main">
        <h2 id="staff-dialog-title">{title}</h2>
        {kind === 'issue' && <>
          <p className="desk-sub">회사 이메일과 소속을 입력해 사원 계정을 발급합니다.</p>
          <div className="staff-fields">
            <label>이름<input required autoComplete="off" maxLength={60} placeholder="예: 박지우" value={name} onChange={event => { setName(event.target.value); setError(''); }} /></label>
            <label>회사 이메일<input required type="email" autoComplete="off" maxLength={254} placeholder="name@example.com" value={email} onChange={event => { setEmail(event.target.value); setError(''); setDuplicate(null); }} /></label>
          </div>
        </>}
        {kind === 'permissions' || kind === 'deactivate' ? <p className="desk-sub">{person.name} · {person.email}</p> : null}
        {kind === 'issue' || kind === 'permissions' ? <div className="staff-fields staff-fields-pair">
          <label>소속 팀<select required value={team} onChange={event => setTeam(event.target.value)}><option value="">팀 선택</option>{teams.map(entry => <option key={entry}>{entry}</option>)}</select></label>
          <label>역할<select value={role} onChange={event => setRole(event.target.value)}>{roles.map(entry => <option key={entry}>{entry}</option>)}</select></label>
        </div> : null}
        {kind === 'permissions' && team !== person.team && <div className="staff-callout">
          <strong>인수인계 대상</strong>
          <dl><div><dt>미처리 티켓</dt><dd>{person.tickets}건</dd></div><div><dt>미회신 검토 요청</dt><dd>{person.reviews}건</dd></div></dl>
        </div>}
        {kind === 'permissions' && <fieldset className="staff-permissions"><legend>업무 권한</legend>
          <div className="staff-table-wrap"><table className="staff-table" aria-label="권한 변경 비교">
            <thead><tr>{['권한', '새 역할 기본값', '현재', '변경 후'].map(label => <th key={label} scope="col">{label}</th>)}</tr></thead>
            <tbody>{Object.entries(rights).map(([key, label]) => <tr key={key}>
              <th scope="row">{label}</th><td>{permissionValue(roleDefaults(role)[key])}</td>
              <td>{describeRight(person.effective[key], Object.hasOwn(person.overrides, key))}</td>
              <td><select aria-label={label} value={Object.hasOwn(overrides, key) ? String(overrides[key]) : 'inherit'}
                onChange={event => {
                  const next = { ...overrides };
                  if (event.target.value === 'inherit') delete next[key];
                  else next[key] = event.target.value === 'true';
                  setOverrides(next);
                }}>
                <option value="inherit">역할 기본값</option><option value="true">개인 허용</option><option value="false">개인 제한</option>
              </select></td>
            </tr>)}</tbody>
          </table></div>
          {team !== person.team && <p className="desk-sub">팀 신속답변 조회 범위 {person.team} → {team}</p>}
        </fieldset>}
        {kind === 'reset' && <>
          <p className="desk-sub">아래 계정의 비밀번호를 초기화할까요?</p>
          <div className="staff-callout"><strong>{person.name}</strong><p>{person.email}</p></div>
          <p className="desk-sub">직원은 임시 비밀번호로 로그인한 뒤 새 비밀번호를 설정합니다.</p>
          <p className="desk-sub">시안에서는 초기화 요청을 변경 이력에 남깁니다.</p>
        </>}
        {kind === 'deactivate' && <>
          <p className="desk-sub staff-gap">비활성화하면 이 계정의 로그인이 제한됩니다.</p>
          <div className="staff-callout">
            <strong>남은 업무</strong>
            <dl><div><dt>미처리 티켓</dt><dd>{person.tickets}건</dd></div><div><dt>미회신 검토 요청</dt><dd>{person.reviews}건</dd></div></dl>
          </div>
          {hasWork && <div className="staff-pending-text"><Badge variant="pending">인수인계 필요</Badge> 남은 업무를 확인해 주세요.</div>}
          {!hasWork && <p className="desk-sub">인수인계할 업무가 없습니다.</p>}
        </>}
        {duplicate && <button className="desk-button" type="button" onClick={() => openPerson(duplicate.id)}>기존 사원 보기</button>}
        {error && <p className="staff-error" role="alert">{error}</p>}
      </div>
      <div className="staff-dialog-actions"><button type="button" className="desk-button" onClick={close}>취소</button>
        <button type="submit" className={'desk-button ' + (kind === 'deactivate' ? 'staff-danger' : 'desk-button-solid')} disabled={hasWork}>{action}</button>
      </div>
    </form>
  </dialog>;
}

export function StaffManagement() {
  const [staff, setStaff] = useState(loadStaff);
  const [selected, setSelected] = useState(() => new URLSearchParams(location.search).get('staff'));
  const [operations] = useState(loadOperations);
  const [filters, setFilters] = useState({ query: '', team: '', account: '' });
  const [modal, setModal] = useState(null);
  const [notice, setNotice] = useState('');
  const staffWithWork = staff.map(entry => ({
    ...entry, tickets: entry.tickets + operations.tickets.filter(ticket => ticket.assignee === entry.id).length
  }));
  const person = staffWithWork.find(entry => entry.id === selected);
  useEffect(() => {
    function onBack() {
      setSelected(new URLSearchParams(location.search).get('staff'));
      setModal(null);
      setNotice('');
    }
    window.addEventListener('popstate', onBack);
    return () => window.removeEventListener('popstate', onBack);
  }, []);
  function select(id) {
    const url = new URL(location.href);
    if (id) url.searchParams.set('staff', id);
    else url.searchParams.delete('staff');
    history.pushState(null, '', url);
    setSelected(id);
    setNotice('');
  }
  function save(fields) {
    const notices = { issue: '사원 계정을 발급했습니다.', permissions: '소속·권한을 저장했습니다.', reset: '초기화 요청을 변경 이력에 남겼습니다.', deactivate: '계정을 비활성화했습니다.' };
    const current = loadStaff();
    let next;
    if (modal === 'issue') {
      next = [...current, { ...fields, id: crypto.randomUUID(), account: 'pending', overrides: {}, tickets: 0, reviews: 0, history: [logChange('사원 계정 발급', null, accountLabels.pending)] }];
      setFilters({ query: '', team: '', account: '' });
    } else {
      const updates = modal === 'permissions' ? fields : modal === 'deactivate' ? { account: 'inactive' } : {};
      next = current.map(entry => {
        if (entry.id !== person.id) return entry;
        const history = modal === 'permissions' ? permissionChanges(entry, fields) :
          modal === 'reset' ? [logChange('비밀번호 초기화 요청')] :
            [logChange('계정 상태', accountLabels[entry.account], accountLabels.inactive)];
        return { ...entry, ...updates, history: [...history, ...entry.history] };
      });
    }
    if (!saveStaff(next)) {
      setNotice('브라우저에 사원 정보를 저장할 수 없습니다.');
      return;
    }
    setStaff(loadStaff());
    setNotice(notices[modal]);
    setModal(null);
  }
  return <>
    {notice && <p className="staff-notice" role="status">{notice}</p>}
    {person ? <StaffDetail person={person} back={() => select(null)} open={setModal} /> :
      <StaffList staff={staffWithWork} filters={filters} setFilters={setFilters} openPerson={select} onIssue={() => setModal('issue')} />}
    {modal && <StaffDialog kind={modal} person={person} staff={staff} close={() => setModal(null)} save={save} openPerson={id => { setModal(null); select(id); }} />}
  </>;
}

export function launchLegacyStaffPage() {
  const params = new URLSearchParams(location.search);
  const view = params.get('view');
  if (view !== 'intake' && view !== 'opinions') {
    const target = new URL(settingsUrl, location.href);
    const replacement = { list: 'staff', teams: 'assignment' }[view];
    if (replacement) target.searchParams.set('view', replacement);
    for (const key of ['staff', 'preview', 'as']) {
      if (params.has(key)) target.searchParams.set(key, params.get(key));
    }
    location.replace(target);
    return;
  }
  const intake = view === 'intake';
  const allowed = hasStaffRight(currentStaff(params), 'opinions');
  const title = intake ? '직원 의견 등록' : '직원 의견';
  document.title = title + ' | 섭명뭐';
  function OpinionsPage() {
    const [data, setData] = useState(loadOperations);
    function update(next) { saveOperations(next); setData(next); }
    return <DeskLayout active={intake ? 'home' : undefined}>
      <section className="staff-page">
        <header className="staff-header"><h1>{title}</h1><p className="desk-sub">가상 고객사 · 리테일 A</p></header>
        {intake ? <OpinionIntake onChange={update} /> : allowed ?
          <StaffOperations view="opinions" data={data} staff={[]} teams={teams} onChange={update} route={withStaffPreview("?view=opinions")} selectedOpinion={params.get('opinion')} canEdit={currentStaff(params)?.role === '관리자'} /> :
          <p role="status">직원 의견을 열람할 권한이 필요합니다.</p>}
      </section>
    </DeskLayout>;
  }
  createRoot(document.getElementById('app')).render(<OpinionsPage />);
}
