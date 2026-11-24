import React, { useState } from 'react';
import { loadStaff, teams } from '../00-Shared/00-StaffAccess.js';
import { loadOperations, saveOperations } from '../07-StaffManagementPage/07-StaffOperations.jsx';

const weekdays = ['월', '화', '수', '목', '금', '토', '일'];
const defaultRule = {
  channels: [], days: weekdays.slice(0, 5), timezone: 'Asia/Seoul', start: '09:00', end: '18:00',
  excludeHolidays: false, includeTeams: [], includeStaff: [], excludeTeams: [], excludeStaff: [],
  mode: 'teams', limit: 10
};
const zones = ['Asia/Seoul', 'Asia/Tokyo', 'UTC', 'America/Los_Angeles'];
const timingLabels = { 'top-up': '1건 처리 시 1건 보충', 'after-all': '배정분을 모두 처리한 뒤 일괄 배정' };
const tally = (person, tickets) => person.tickets + tickets.filter(ticket => ticket.assignee === person.id).length;

function choices(values) {
  return values.map(value => ({ id: value, label: value }));
}

function CheckGroup({ label, options, selected, onChange }) {
  return <fieldset className="settings-fieldset">
    <legend>{label}</legend>
    <div className="settings-checks">{options.map(({ id, label: name }) => <label key={id}>
      <input type="checkbox" checked={selected.includes(id)} onChange={event => onChange(
        event.target.checked ? [...selected, id] : selected.filter(value => value !== id)
      )} />{name}</label>)}</div>
  </fieldset>;
}

function findCandidate(ticket, category, rule, clock, staff, tickets, counts, batchSize) {
  if (!rule.enabled) return { result: '대기', reason: '자동 배정 사용 안 함' };
  if (!ticket.category) return { result: '대기', reason: '카테고리 분석 결과 대기' };
  if (!category?.active || !rule.targets.includes(category.id)) return { result: '대기', reason: '대상 카테고리 아님' };
  if (rule.channels.length && !rule.channels.includes(ticket.channel)) return { result: '대기', reason: '대상 채널 아님' };
  if (!rule.days.includes(clock.day) || clock.time < rule.start || clock.time >= rule.end ||
      (rule.excludeHolidays && clock.holiday)) return { result: '대기', reason: '운영 시간 외' };
  const eligible = staff.filter(person => person.account === 'active' && person.availability === '상담 가능' &&
    person.permissions.some((value, index) => index < 2 && value) &&
    (!category.team || person.team === category.team) &&
    (!rule.includeTeams.length || rule.includeTeams.includes(person.team)) &&
    !rule.excludeTeams.includes(person.team) && !rule.excludeStaff.includes(person.id) &&
    (rule.mode === 'teams' || rule.includeStaff.includes(person.id)));
  if (!eligible.length) return { result: '대기', reason: '배정 가능한 담당자 없음' };
  const triggered = staff.find(person => person.id === clock.trigger);
  if (triggered && !eligible.includes(triggered)) return { result: '대기', reason: '해당 담당자에게 배정할 수 없음' };
  const open = eligible.filter(person => (!triggered || person === triggered) && tally(person, tickets) < rule.limit);
  if (!open.length) return { result: '대기', reason: '동시 처리 한도 도달' };
  const [agent] = open.filter(person => (counts.get(person.id) ?? 0) < batchSize).sort((a, b) =>
    tally(a, tickets) - tally(b, tickets) ||
    (a.lastAssignment ?? '').localeCompare(b.lastAssignment ?? '') || a.id.localeCompare(b.id));
  return agent ? { result: '배정 가능', reason: agent.name, agentId: agent.id } : { result: '대기', reason: '이번 배정 건수 도달' };
}

export function AssignmentSettings({ Header, Actions, Switch }) {
  const [data] = useState(loadOperations);
  const [staff] = useState(loadStaff);
  const [rule, setRule] = useState(() => ({ ...defaultRule, ...data.rule }));
  const [amount, setAmount] = useState([1, 5, 10].includes(data.rule.size) ? String(data.rule.size) : 'custom');
  const [notice, setNotice] = useState('');
  const [error, setError] = useState('');
  const [preview, setPreview] = useState(null);
  const [clock, setClock] = useState({ day: '월', time: '10:00', holiday: false, trigger: 'initial' });
  const categories = data.categories.filter(item => item.active);
  const people = staff.map(person => ({ id: person.id, label: person.name + ' · ' + person.team }));
  const set = (key, value) => {
    setRule(current => ({ ...current, [key]: value }));
    setError(''); setPreview(null); setNotice('');
  };
  const group = (key, label, options) => <CheckGroup label={label} options={options} selected={rule[key]} onChange={value => set(key, value)} />;
  function configuredRule() {
    const size = Number(amount === 'custom' ? rule.size : amount);
    const limit = Number(rule.limit);
    let message;
    if (!Number.isSafeInteger(size) || size < 1 || !Number.isSafeInteger(limit) || limit < 1) message = '배정 건수와 동시 처리 한도는 1 이상의 정수로 입력해 주세요.';
    else if (rule.enabled && (!rule.days.length || !rule.start || !rule.end || rule.start >= rule.end)) message = '운영 요일을 선택하고 종료 시각을 시작 시각보다 늦게 설정해 주세요.';
    else if (rule.enabled && !rule.targets.length) message = '자동 배정 대상 카테고리를 한 개 이상 선택해 주세요.';
    else if (rule.enabled && rule.mode === 'selected' && !rule.includeStaff.length) message = '포함할 담당자를 한 명 이상 선택해 주세요.';
    setError(message ?? '');
    return message ? null : { ...rule, size, limit };
  }
  function save(event) {
    event.preventDefault();
    const next = configuredRule();
    if (!next) return;
    const current = loadOperations();
    next.targets = next.targets.filter(id => current.categories.some(category => category.id === id && category.active));
    if (next.enabled && !next.targets.length) {
      setError('사용 가능한 대상 카테고리를 다시 선택해 주세요.');
      return;
    }
    setNotice(saveOperations({ ...current, rule: next }) ? '자동 배정 설정을 저장했습니다.' : '브라우저에 자동 배정 설정을 저장할 수 없습니다.');
  }
  function runPreview() {
    const next = configuredRule();
    if (!next) return;
    const tickets = [...data.tickets];
    const counts = new Map();
    const batchSize = clock.trigger !== 'initial' && next.timing === 'top-up' ? 1 : next.size;
    const candidates = data.tickets.filter(ticket => !ticket.assignee).sort((a, b) =>
      (a.receivedAt ?? '').localeCompare(b.receivedAt ?? '') || a.id.localeCompare(b.id));
    setPreview(candidates.map(ticket => {
      const candidate = findCandidate({ ...ticket, channel: ticket.channel ?? 'Gmail' },
        categories.find(category => category.id === ticket.category), next, clock, staff, tickets, counts, batchSize);
      if (!candidate.agentId) return { ...ticket, ...candidate };
      const count = counts.get(candidate.agentId) ?? 0;
      counts.set(candidate.agentId, count + 1);
      tickets.push({ assignee: candidate.agentId });
      return { ...ticket, ...candidate };
    }));
  }
  function updateClock(key, value) {
    setClock(current => ({ ...current, [key]: value }));
    setPreview(null);
  }
  return <>
    <Header title="자동 배정" description="티켓이 자동 배정되는 조건과 담당자 범위를 설정합니다." />
    <form className="settings-display" onSubmit={save}>
      <section className="staff-card settings-form">
        <div className="staff-card-head"><h2>자동 배정 사용</h2><Switch label="자동 배정 사용" value={rule.enabled} onChange={value => set('enabled', value)} /></div>
        <p className="desk-sub">표준화와 저장을 마친 신규 미배정 티켓에 적용합니다. 카테고리 분석 중인 티켓은 목록에서 확인할 수 있고, 배정은 분석 결과가 나온 뒤에 판단합니다.</p>
        {group('targets', '자동 배정 대상 카테고리', categories.map(({ id, group, name }) => ({ id, label: group + ' › ' + name })))}
        {group('channels', '대상 채널', choices(['Gmail', 'CSV', 'Discord']))}
        <p className="desk-sub">채널을 선택하지 않으면 모든 채널을 대상으로 삼습니다.</p>
      </section>
      <div className="assignment-grid">
        <section className="staff-card settings-form">
          <h2>운영 시간</h2>
          <label>시간대<select value={rule.timezone} onChange={event => set('timezone', event.target.value)}>{zones.map(zone => <option key={zone}>{zone}</option>)}</select></label>
          {group('days', '운영 요일', choices(weekdays))}
          <div className="settings-pair">
            <label>시작 시각<input type="time" value={rule.start} onChange={event => set('start', event.target.value)} /></label>
            <label>종료 시각<input type="time" value={rule.end} onChange={event => set('end', event.target.value)} /></label>
          </div>
          <div className="settings-checks"><label><input type="checkbox" checked={rule.excludeHolidays} onChange={event => set('excludeHolidays', event.target.checked)} />공휴일 제외</label></div>
        </section>
        <section className="staff-card settings-form">
          <h2>배정 건수 및 시점</h2>
          <fieldset className="settings-fieldset"><legend>한 번에 배정할 건수</legend>
            <div className="settings-toolbar">{['1', '5', '10', 'custom'].map(value => <button type="button" key={value}
              className={'desk-button ' + (amount === value ? 'desk-button-solid' : '')} aria-pressed={amount === value}
              onClick={() => { setAmount(value); setPreview(null); setNotice(''); setError(''); }}>{value === 'custom' ? '직접 입력' : value + '건'}</button>)}</div>
            {amount === 'custom' && <input aria-label="직접 입력할 배정 건수" type="number" min="1" required value={rule.size} onChange={event => set('size', event.target.value)} />}
          </fieldset>
          <label>담당자당 동시 처리 한도<input type="number" min="1" required value={rule.limit} onChange={event => set('limit', event.target.value)} /></label>
          <fieldset className="settings-fieldset"><legend>재배정 시점</legend><div className="settings-checks">
            {Object.entries(timingLabels).map(([value, label]) => <label key={value}><input type="radio" checked={rule.timing === value} name="timing" value={value} onChange={() => set('timing', value)} />{label}</label>)}
          </div></fieldset>
        </section>
        <section className="staff-card settings-form">
          <h2>포함 대상</h2>
          {group('includeTeams', '포함 팀', choices(teams))}
          <p className="desk-sub">포함 팀을 선택하지 않으면 카테고리 담당 팀 전체를 대상으로 삼습니다. 담당 팀이 없으면 전 팀이 대상입니다.</p>
          <label>담당자 범위<select value={rule.mode} onChange={event => set('mode', event.target.value)}>
            <option value="teams">포함 팀의 상담 가능 담당자 전체</option><option value="selected">선택한 담당자만</option>
          </select></label>
          {rule.mode === 'selected' && group('includeStaff', '포함 담당자', people)}
        </section>
        <section className="staff-card settings-form">
          <h2>제외 대상</h2><p className="desk-sub">제외 조건을 포함 조건보다 먼저 적용합니다.</p>
          {group('excludeTeams', '제외 팀', choices(teams))}
          {group('excludeStaff', '제외 담당자', people)}
        </section>
      </div>
      <section className="staff-card settings-form">
        <h2>배정 미리보기</h2>
        <p className="desk-sub">미배정 예시 티켓의 우선 후보와 대기 사유를 확인합니다. 입력한 운영 시간대 기준으로 시험합니다.</p>
        <div className="settings-pair">
          <label>예시 요일<select value={clock.day} onChange={event => updateClock('day', event.target.value)}>{weekdays.map(day => <option key={day}>{day}</option>)}</select></label>
          <label>예시 시각<input type="time" value={clock.time} onChange={event => updateClock('time', event.target.value)} /></label>
        </div>
        <label>시험할 시점<select value={clock.trigger} onChange={event => updateClock('trigger', event.target.value)}>
          <option value="initial">신규 배정</option>
          {staff.filter(person => person.account === 'active' && person.availability === '상담 가능').map(person =>
            <option key={person.id} value={person.id}>{person.name} · {timingLabels[rule.timing]} 조건 충족</option>)}
        </select></label>
        <div className="settings-checks"><label><input type="checkbox" checked={clock.holiday} onChange={event => updateClock('holiday', event.target.checked)} />공휴일로 시험</label></div>
        <div><button type="button" className="desk-button" disabled={!clock.time} onClick={runPreview}>배정 미리보기</button></div>
        {preview && <div className="staff-table-wrap">
          <table className="staff-table" aria-label="자동 배정 미리보기 결과">
            <thead><tr>{['티켓', '판정', '담당자 또는 대기 사유'].map(label => <th scope="col" key={label}>{label}</th>)}</tr></thead>
            <tbody>{preview.map(item => <tr key={item.id}><th scope="row">{item.id}</th><td>{item.result}</td><td>{item.reason}</td></tr>)}
              {!preview.length && <tr><td colSpan={3}>미배정 예시 티켓이 없습니다.</td></tr>}</tbody>
          </table>
        </div>}
      </section>
      {error && <p className="staff-error" role="alert">{error}</p>}
      {notice && <p className="staff-notice" role="status">{notice}</p>}
      <Actions />
    </form>
  </>;
}
