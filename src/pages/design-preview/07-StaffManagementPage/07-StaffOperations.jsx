import React, { useEffect, useRef, useState } from 'react';

const baseCategories = [
  { id: 'refund', group: '결제', name: '환불 지연', active: true, team: '결제지원팀', date: '2026-11-24' },
  { id: 'cancel', group: '결제', name: '결제 취소', active: true, team: '', date: '2026-11-21' },
  { id: 'account', group: '계정', name: '로그인 오류', active: true, team: '고객지원팀', date: '2026-11-20' },
  { id: 'delivery', group: '배송', name: '배송 지연', active: true, team: '배송지원팀', date: '2026-11-20' },
  { id: 'courier', group: '배송', name: '배송 기사 연락처', active: false, team: '', date: '2026-11-18' }
];
const baseTickets = [
  { id: '#1048', category: 'refund', title: '환불 진행 상황을 알고 싶어요' },
  { id: '#1045', category: 'refund', title: '환불 예정일을 확인해 주세요' },
  { id: '#1042', category: 'delivery', title: '상품이 아직 배송되지 않았어요' },
  { id: '#1039', category: 'delivery', title: '배송이 지연되는 이유가 궁금해요' }
];
const baseOpinions = [
  { id: 'VOE-021', title: '야간 티켓 이관 기준을 정해주세요', message: '야간에 들어온 문의를 어느 팀에 넘겨야 하는지 기준이 있으면 좋겠어요.', type: '개선 제안', status: '접수', handler: '', date: '2026-11-24' },
  { id: 'VOE-020', title: '상담 도구 접근 권한 개선 요청', message: '팀별로 필요한 도구를 확인해 주세요.', type: '개선 제안', status: '접수', handler: '', date: '2026-11-24' },
  { id: 'VOE-018', title: '배송 문의 신속답변 보완 요청', message: '배송이 늦을 때 사용할 답변 문구를 보완해 주세요.', type: '개선 제안', status: '처리 완료', handler: '고객지원팀', date: '2026-11-20' }
];
const key = 'voc-staff-operations-v1';
const statusOptions = ['접수', '검토 중', '처리 완료'];
const typeOptions = ['개선 제안', '고충'];
const handlers = ['고객지원팀', '운영 담당자', '인사 담당자'];
const date = () => new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Seoul', dateStyle: 'short' }).format(new Date());
const canReceive = person => person.account === 'active' && person.permissions[0] && person.availability === '상담 가능';

export function loadOperations() {
  try {
    const saved = JSON.parse(localStorage.getItem(key));
    if (saved) return saved;
  } catch { /* Preview still works where storage is unavailable. */ }
  return {
    categories: baseCategories,
    tickets: baseTickets,
    opinions: baseOpinions,
    rule: { enabled: true, size: 5, timing: 'after-all', targets: ['refund', 'cancel', 'account', 'delivery'] }
  };
}

export function saveOperations(data) {
  try { localStorage.setItem(key, JSON.stringify(data)); return true; }
  catch { return false; }
}

function Table({ label, columns, children }) {
  return <div className="staff-table-wrap">
    <table className="staff-table staff-operation-table" aria-label={label}>
      <thead><tr>{columns.map(column => <th scope="col" key={column}>{column}</th>)}</tr></thead>
      <tbody>{children}</tbody>
    </table>
  </div>;
}

function TeamAssignment({ data, staff, open }) {
  const { rule, categories, tickets } = data;
  const queue = tickets.filter(ticket => !ticket.assignee);
  const rules = categories.filter(category => category.team);
  return <section className="staff-operations">
    <h2>팀·업무 배정</h2>
    <div className="staff-card">
      <div className="staff-card-head"><h3>자동 배정 설정 <span className={'staff-badge ' + (rule.enabled ? 'active' : '')}>{rule.enabled ? '사용 중' : '사용 안 함'}</span></h3>
        <button type="button" className="desk-button" onClick={() => open({ kind: 'rule' })}>설정 편집</button>
      </div>
      <p className="desk-sub">{rule.size}건씩 배정 · {rule.timing === 'after-all' ? '기존 배정분을 모두 처리하면 재배정' : '소진되는 즉시 재배정'} · 대상 {rule.targets.filter(id => categories.some(category => category.active && category.id === id)).length}개</p>
    </div>
    <h3>상담 인력 현황</h3>
    <Table label="상담 인력 현황" columns={['사원', '소속 팀', '상담 가능 상태', '미처리 티켓']}>
      {staff.filter(person => person.account === 'active' && person.permissions[0]).map(person => <tr key={person.id}>
        <th scope="row">{person.name}</th><td>{person.team}</td><td>{person.availability || '미설정'}</td><td>{person.tickets}건</td>
      </tr>)}
    </Table>
    <h3>카테고리별 담당 팀</h3>
    <Table label="카테고리별 담당 팀" columns={['카테고리', '담당 팀', '배정 가능한 담당자', '미배정', '관리']}>
      {rules.map(category => <tr key={category.id}>
        <th scope="row">{category.group} › {category.name}</th><td>{category.team}</td>
        <td>{staff.filter(person => person.team === category.team && canReceive(person)).length}명</td>
        <td>{queue.filter(ticket => ticket.category === category.id).length}건</td>
        <td><button className="staff-name" type="button" onClick={() => open({ kind: 'route', id: category.id })}>담당 팀 변경</button></td>
      </tr>)}
      {rules.length === 0 && <tr><td colSpan="5">설정된 담당 팀이 없습니다.</td></tr>}
    </Table>
    <h3>미배정 티켓 {queue.length}건</h3>
    <Table label="미배정 티켓" columns={['티켓', '제목', '사유', '담당자']}>
      {queue.map(ticket => {
        const team = categories.find(category => category.id === ticket.category)?.team;
        const reason = team ? (staff.some(person => canReceive(person) && person.team === team) ? '담당자 미지정' : '담당 팀에 배정 가능한 사원 없음') : '담당 팀 미설정';
        return <tr key={ticket.id}><th scope="row">{ticket.id}</th><td>{ticket.title}</td><td>{reason}</td>
          <td><button className="staff-name" type="button" onClick={() => open({ kind: 'assign', id: ticket.id })}>배정하기 ›</button></td></tr>;
      })}
      {queue.length === 0 && <tr><td colSpan="4">미배정 티켓이 없습니다.</td></tr>}
    </Table>
  </section>;
}

function CategoryManagement({ data, open }) {
  return <section className="staff-operations">
    <div className="staff-section-head"><h2>카테고리 관리</h2><button className="desk-button desk-button-solid" type="button" onClick={() => open({ kind: 'category' })}>카테고리 추가</button></div>
    <p className="desk-sub">가상 고객사 · 리테일 A에서 사용하는 문의 분류</p>
    <Table label="카테고리 목록" columns={['대분류', '세부 카테고리', '사용 상태', '자동 배정 대상', '최근 수정', '관리']}>
      {data.categories.map(category => <tr key={category.id}>
        <th scope="row">{category.group}</th><td>{category.name}</td><td><span className={'staff-badge ' + (category.active ? 'active' : '')}>{category.active ? '사용 중' : '사용 중지'}</span></td>
        <td>{category.active && data.rule.targets.includes(category.id) ? '대상' : '—'}</td><td>{category.date}</td>
        <td><button type="button" className="staff-name" aria-label={category.name + ' 편집'} onClick={() => open({ kind: 'category', id: category.id })}>편집</button></td>
      </tr>)}
    </Table>
    <p className="desk-sub">사용 중지된 분류는 자동 배정 설정의 선택지에서 제외합니다.</p>
  </section>;
}

function Opinions({ data, open, selected, onSelect, canEdit }) {
  const [type, setType] = useState('');
  const [status, setStatus] = useState('');
  const visible = data.opinions.filter(opinion => (opinion.type !== '고충' || (canEdit && opinion.handler === '인사 담당자')) && (!type || opinion.type === type) && (!status || opinion.status === status));
  const opinion = visible.find(item => item.id === selected) ?? visible[0];
  return <section className="staff-operations">
    <h2>직원 의견</h2>
    <div className="staff-filters staff-opinion-filters">
      <select aria-label="의견 유형" value={type} onChange={event => setType(event.target.value)}><option value="">유형 전체</option>{typeOptions.map(option => <option key={option}>{option}</option>)}</select>
      <select aria-label="처리 상태" value={status} onChange={event => setStatus(event.target.value)}><option value="">상태 전체</option>{statusOptions.map(option => <option key={option}>{option}</option>)}</select>
    </div>
    <Table label="직원 의견 목록" columns={['접수 번호', '제목', '유형', '처리 상태', '담당자', '접수일']}>
      {visible.map(item => <tr key={item.id} className={item.id === opinion?.id ? 'staff-selected' : ''}>
        <td>{item.id}</td><th scope="row"><button type="button" className="staff-name staff-text-link" onClick={() => onSelect(item.id)} aria-pressed={item.id === opinion?.id}>{item.type === '고충' ? '비공개 고충' : item.title}</button></th>
        <td>{item.type}</td><td><span className={'staff-badge ' + (item.status === '접수' ? 'pending' : item.status === '처리 완료' ? 'active' : '')}>{item.status}</span></td>
        <td>{item.handler || '미지정'}</td><td>{item.date}</td>
      </tr>)}
      {visible.length === 0 && <tr><td colSpan="6">조건에 맞는 직원 의견이 없습니다.</td></tr>}
    </Table>
    {opinion && <section className="staff-card">
      <h3>{opinion.type === '고충' ? '비공개 고충' : opinion.title}</h3>
      <p className="desk-sub">{opinion.id} · {opinion.type} · {opinion.date}</p>
      <p className="staff-opinion-message">{opinion.type === '고충' ? '시안에서는 고충의 접수 상태와 담당자를 확인할 수 있습니다.' : opinion.message}</p>
      {opinion.updated && <p className="desk-sub">최근 변경 {opinion.updated}</p>}
      {canEdit && <div className="staff-actions staff-operation-actions">
        <button className="desk-button" type="button" onClick={() => open({ kind: 'handler', id: opinion.id })}>담당자 지정</button>
        <button className="desk-button" type="button" onClick={() => open({ kind: 'status', id: opinion.id })}>처리 상태 변경</button>
      </div>}
    </section>}
  </section>;
}

const dialogTitles = { rule: '자동 배정 설정', route: '담당 팀 변경', assign: '티켓 담당자 배정', category: '카테고리 관리', handler: '담당자 지정', status: '처리 상태 변경' };

function OperationsDialog({ dialog, data, staff, teams, onClose, onSave }) {
  const ref = useRef(null);
  const [error, setError] = useState('');
  const [amount, setAmount] = useState([1, 5, 10].includes(data.rule.size) ? String(data.rule.size) : 'custom');
  const { kind, id } = dialog;
  const category = data.categories.find(item => item.id === id);
  const ticket = data.tickets.find(item => item.id === id);
  const opinion = data.opinions.find(item => item.id === id);
  const opinionTitle = opinion?.type === '고충' ? '비공개 고충' : opinion?.title;
  const available = staff.filter(canReceive);
  useEffect(() => {
    const node = ref.current;
    node.showModal();
    return () => node.close();
  }, []);

  function submit(event) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const value = key => String(form.get(key) ?? '').trim();
    let next;
    if (kind === 'rule') {
      const size = Number(amount === 'custom' ? value('size') : amount);
      if (!Number.isSafeInteger(size) || size < 1) return setError('한 번에 배정할 건수를 입력해 주세요.');
      next = { ...data, rule: { enabled: form.has('enabled'), size, timing: value('timing'), targets: form.getAll('targets') } };
    } else if (kind === 'category' || kind === 'route') {
      const changed = kind === 'route' ? { ...category, team: value('team'), date: date() } :
        { id: id ?? crypto.randomUUID(), group: value('group'), name: value('name'), active: value('active') === 'yes', team: value('team'), date: date() };
      if (!changed.group || !changed.name) return setError('대분류와 세부 카테고리를 입력해 주세요.');
      if (data.categories.some(item => item.id !== id && item.group === changed.group && item.name === changed.name)) return setError('같은 대분류에 이미 등록된 카테고리입니다.');
      next = { ...data, categories: id ? data.categories.map(item => item.id === id ? changed : item) : [...data.categories, changed] };
    } else if (kind === 'assign') {
      if (!available.some(person => person.id === value('staff'))) return setError('배정 가능한 사원을 선택해 주세요.');
      next = { ...data, tickets: data.tickets.map(item => item.id === id ? { ...item, assignee: value('staff') } : item) };
    } else {
      const field = kind === 'handler' ? 'handler' : 'status';
      next = { ...data, opinions: data.opinions.map(item => item.id === id ? { ...item, [field]: value(field), updated: date() + ' · 최지민' } : item) };
    }
    onSave(next);
  }
  return <dialog ref={ref} className="staff-dialog" aria-labelledby="staff-operation-title" onCancel={event => { event.preventDefault(); onClose(); }}>
    <form onSubmit={submit}>
      <div className="staff-dialog-main">
        <h2 id="staff-operation-title">{kind === 'category' ? (id ? '카테고리 편집' : '카테고리 추가') : dialogTitles[kind]}</h2>
        {kind === 'rule' && <>
          <div className="staff-fields">
            <label className="staff-inline"><input type="checkbox" name="enabled" defaultChecked={data.rule.enabled} />자동 배정 사용</label>
            <label>배정 건수<select value={amount} onChange={event => setAmount(event.target.value)}>{[1, 5, 10].map(n => <option key={n} value={n}>{n}건</option>)}<option value="custom">직접 입력</option></select></label>
            {amount === 'custom' && <label>직접 입력할 건수<input name="size" type="number" min="1" step="1" defaultValue={data.rule.size} required /></label>}
            <label>재배정 시점<select name="timing" defaultValue={data.rule.timing}><option value="after-all">기존 배정분을 모두 처리한 뒤</option><option value="top-up">소진되는 즉시</option></select></label>
          </div>
          <fieldset className="staff-permissions"><legend>대상 카테고리</legend>{data.categories.filter(item => item.active).map(item => <label key={item.id}><input type="checkbox" name="targets" value={item.id} defaultChecked={data.rule.targets.includes(item.id)} />{item.group} › {item.name}</label>)}</fieldset>
        </>}
        {(kind === 'category' || kind === 'route') && <div className="staff-fields">
          {kind === 'category' ? <>
            <label>대분류<input name="group" maxLength={40} defaultValue={category?.group} required /></label>
            <label>세부 카테고리<input name="name" maxLength={60} defaultValue={category?.name} required /></label>
            <label>사용 상태<select name="active" defaultValue={category?.active === false ? 'no' : 'yes'}><option value="yes">사용 중</option><option value="no">사용 중지</option></select></label>
          </> : <p>{category.group} › {category.name}</p>}
          <label>담당 팀<select name="team" defaultValue={category?.team ?? ''}><option value="">미지정</option>{teams.map(team => <option key={team}>{team}</option>)}</select></label>
        </div>}
        {kind === 'assign' && <div className="staff-fields">
          <p>{ticket.id} · {ticket.title}</p>
          <label>담당자<select name="staff" defaultValue="" required><option value="">배정 가능한 사원 선택</option>{available.map(person => <option value={person.id} key={person.id}>{person.name} · {person.team}</option>)}</select></label>
          {available.length === 0 && <p className="desk-sub">현재 배정 가능한 사원이 없습니다.</p>}
        </div>}
        {(kind === 'handler' || kind === 'status') && <div className="staff-fields">
          <p>{opinion.id} · {opinionTitle}</p>
          {kind === 'handler' ?
            <label>담당자<select name="handler" defaultValue={opinion.handler}>{opinion.type !== "고충" && <option value="">미지정</option>}{(opinion.type === "고충" ? ["인사 담당자"] : handlers).map(option => <option key={option}>{option}</option>)}</select></label> :
            <label>처리 상태<select name="status" defaultValue={opinion.status}>{statusOptions.map(option => <option key={option}>{option}</option>)}</select></label>}
        </div>}
        {error && <p className="staff-error" role="alert">{error}</p>}
      </div>
      <div className="staff-dialog-actions">
        <button type="button" className="desk-button" onClick={onClose}>취소</button>
        <button type="submit" className="desk-button desk-button-solid" disabled={kind === 'assign' && !available.length}>{kind === 'assign' ? '배정' : '저장'}</button>
      </div>
    </form>
  </dialog>;
}

export function StaffOperations({ view, data, staff, teams, onChange, route, selectedOpinion, canEdit = true }) {
  const [dialog, setDialog] = useState(null);
  const [notice, setNotice] = useState('');
  const [opinion, setOpinion] = useState(selectedOpinion);
  const panel = view === 'teams' ? <TeamAssignment data={data} staff={staff} open={setDialog} /> :
    view === 'categories' ? <CategoryManagement data={data} open={setDialog} /> :
      <Opinions data={data} open={setDialog} selected={opinion} onSelect={selectOpinion} canEdit={canEdit} />;
  function selectOpinion(id) {
    const url = new URL(route, location.href);
    url.searchParams.set('opinion', id);
    history.replaceState(null, '', url);
    setOpinion(id);
  }
  function save(next) {
    onChange(next);
    setNotice(dialog.kind === 'assign' ? '티켓 담당자를 배정했습니다.' : '변경 사항을 저장했습니다.');
    setDialog(null);
  }
  return <>
    {notice && <p className="staff-notice" role="status">{notice}</p>}
    {panel}
    {dialog && <OperationsDialog dialog={dialog} data={data} staff={staff} teams={teams} onClose={() => setDialog(null)} onSave={save} />}
  </>;
}

export function OpinionIntake({ onChange }) {
  const [type, setType] = useState(typeOptions[0]);
  const [receipt, setReceipt] = useState('');
  const [error, setError] = useState('');
  function submit(event) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const confidential = type === '고충';
    const title = confidential ? '비공개 고충' : form.get('title').trim();
    const message = confidential ? '' : form.get('message').trim();
    if (!title || (!confidential && !message)) {
      setError('제목과 의견을 입력해 주세요.');
      return;
    }
    const current = loadOperations();
    const number = Math.max(0, ...current.opinions.map(item => Number(item.id.slice(4)))) + 1;
    const id = 'VOE-' + String(number).padStart(3, '0');
    onChange({ ...current, opinions: [
      { id, title, message, type, status: '접수', handler: confidential ? '인사 담당자' : '', date: date() },
      ...current.opinions
    ] });
    setReceipt(id);
  }
  return <section className="staff-card staff-intake">
    {receipt ? <div role="status"><h2>의견을 접수했습니다.</h2><p className="desk-sub">접수 번호 {receipt}</p></div> :
      <form onSubmit={submit}>
        <p className="desk-sub">개선 제안이나 고충을 사내 담당자에게 접수합니다.</p>
        <div className="staff-fields">
          <label>유형<select name="type" value={type} onChange={event => { setType(event.target.value); setError(''); }}>{typeOptions.map(option => <option key={option}>{option}</option>)}</select></label>
          {type === '고충' ? <p className="desk-sub">시안에서는 고충 접수만 시연합니다. 세부 내용 입력은 실제 권한 연동 후 제공됩니다.</p> : <>
            <label>제목<input name="title" maxLength={100} required /></label>
            <label>의견<textarea name="message" rows="4" maxLength={1000} required /></label>
          </>}
        </div>
        {error && <p className="staff-error" role="alert">{error}</p>}
        <button className="desk-button desk-button-solid" type="submit">등록</button>
      </form>}
    <a className="desk-button" href="../02-WorkHomePage/02-WorkHomePage.html#02-01">업무 홈으로</a>
  </section>;
}
