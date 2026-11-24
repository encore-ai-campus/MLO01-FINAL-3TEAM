import React, { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { DeskLayout } from '../00-Shared/00-DeskLayout.jsx';
import { periods, scopeNames, sample, reports, pendingWork, individualTrends, sum } from './06-AnalyticsDashboardData.js';

const previewRoles = {
  administrator: { name: '최지민', scopes: ['org', 'support', 'tech'], individual: true },
  'team-lead': { name: '최지민', scopes: ['support'], individual: true },
  'overview-only': { name: '최지민', scopes: ['org', 'support', 'tech'], individual: false }
};
const params = new URLSearchParams(location.search);
const preset = params.get('preview');
const role = Object.hasOwn(previewRoles, preset) ? previewRoles[preset] : null;

const percent = ([numerator, denominator]) => denominator ? Math.round(100 * numerator / denominator) : null;
const delta = (value, unit) => value ? (value > 0 ? '+' : '−') + Math.abs(value) + unit : '변동 없음';
const duration = minutes => minutes < 60 ? minutes + '분' : Math.floor(minutes / 60) + '시간' + (minutes % 60 ? ' ' + minutes % 60 + '분' : '');
const storeTotal = value => Array.isArray(value) ? sum(value) : value;

function Metric({ label, value, unit, basis, comparison, detail }) {
  return <section className="analytics-card analytics-metric">
    <h2>{label}</h2>
    <p className="analytics-metric-value">{value}<small>{unit}</small></p>
    <p>{basis}</p>
    {comparison && <strong>{comparison}</strong>}
    {detail && <p>{detail}</p>}
  </section>;
}

function TrendChart({ dates, opened, done, openingLabel = '신규 접수' }) {
  const x = index => 62 + 520 * index / (dates.length - 1);
  const max = Math.max(4, Math.ceil(Math.max(...opened, ...done) / 4) * 4);
  const y = value => 208 - 168 * value / max;
  const lines = [{ name: openingLabel, data: opened, color: '#326caa' }, { name: '처리 완료', data: done, color: '#287e70' }];
  return <section className="analytics-card analytics-trend">
    <div className="analytics-card-title">
      <h2>{openingLabel}·처리 완료 추이</h2>
      <ul className="analytics-legend">{lines.map(line => <li key={line.name}>
        <span style={{ background: line.color }} />{line.name}
      </li>)}</ul>
    </div>
    <svg className="analytics-trend-svg" viewBox="0 0 600 253" role="img" aria-labelledby="analytics-trend-title analytics-trend-description">
      <title id="analytics-trend-title">날짜별 {openingLabel} 및 처리 완료 건수</title>
      <desc id="analytics-trend-description">{dates.map((date, i) => `${date} ${openingLabel} ${opened[i]}건, 처리 완료 ${done[i]}건`).join('. ')}</desc>
      <text x="17" y="20">건수</text>
      {Array.from({ length: 5 }, (_, i) => i * max / 4).map(tick => <g key={tick}>
        <line x1="47" x2="590" y1={y(tick)} y2={y(tick)} stroke="#e4e4e4" />
        <text x="38" y={y(tick) + 4} textAnchor="end">{tick}</text>
      </g>)}
      {dates.map((date, i) => <text key={date} x={x(i)} y={231} textAnchor="middle">{date}</text>)}
      {lines.map(line => <g key={line.name}>
        <polyline points={line.data.map((value, i) => x(i) + ',' + y(value)).join(' ')}
          fill="none" stroke={line.color} strokeWidth="2.5" strokeLinejoin="round" />
        {line.data.map((value, i) => <circle key={dates[i]} cx={x(i)} cy={y(value)} r="3.5" fill={line.color}>
          <title>{dates[i]} {line.name} {value}건</title>
        </circle>)}
      </g>)}
    </svg>
  </section>;
}

const teamBacklog = { title: '현재 미처리 상태별 구성', names: ['미할당', '담당자 배정', '보류'], colors: ['#9baec1', '#326caa', '#c28935'] };
const individualBacklog = { title: '현재 미처리 티켓 구성', names: ['할당 티켓', '이관'], colors: ['#326caa', '#9baec1'] };

function BacklogChart({ counts, layout = teamBacklog }) {
  const total = sum(counts);
  const { title, names, colors } = layout;
  let angle = 0;
  const bands = counts.flatMap((count, i) => {
    const span = total ? 360 * count / total : 0;
    if (!span) return [];
    const start = angle;
    angle += span;
    const gap = Math.min(span / 2, 1.2);
    return [`white ${start}deg ${start + gap}deg`, `${colors[i]} ${start + gap}deg ${angle - gap}deg`, `white ${angle - gap}deg ${angle}deg`];
  });
  const percentages = counts.map(count => total ? percent([count, total]) : 0);
  if (total) percentages[counts.indexOf(Math.max(...counts))] += 100 - sum(percentages);
  return <section className="analytics-card analytics-backlog">
    <h2>{title}</h2>
    <p className="analytics-muted">조회 시점 기준</p>
    <div className="analytics-donut" style={{ background: bands.length ? `conic-gradient(${bands.join(',')})` : '#f3f3f3' }}>
      <div><strong>{total}건</strong><span>현재 미처리</span></div>
    </div>
    <ul className="analytics-backlog-items">{counts.map((count, i) => <li key={names[i]}>
      <span className="analytics-dot" style={{ background: colors[i] }} />
      <span>{names[i]}</span><strong>{count}건 ({percentages[i]}%)</strong>
    </li>)}</ul>
  </section>;
}

function CategoryChanges({ current, previous, comparison }) {
  const rows = ['환불', '계정', '배송'].map((label, i) => ({
    label, before: previous[i], after: current[i], increase: current[i] - previous[i]
  })).filter(row => row.increase > 0).sort((a, b) => b.increase - a.increase).slice(0, 3);
  return <section className="analytics-card analytics-categories">
    <h2>증가 카테고리 상위 3개</h2>
    <p className="analytics-muted">{comparison}</p>
    <table className="analytics-table">
      <thead><tr>{['카테고리', '비교 기간', '선택 기간', '증감'].map(label => <th scope="col" key={label}>{label}</th>)}</tr></thead>
      <tbody>{rows.map(({ label, before, after, increase }) => <tr key={label}>
        <th scope="row">{label}</th><td>{before}건</td><td>{after}건</td><td className="analytics-increase">{delta(increase, '건')}</td>
      </tr>)}</tbody>
    </table>
    {!rows.length && <p className="analytics-muted analytics-empty">증가한 카테고리가 없습니다.</p>}
  </section>;
}

function Latency({ first, completed }) {
  return <section className="analytics-card analytics-latency">
    <h2>고객 대기 시간</h2>
    <p className="analytics-muted">선택 기간 중앙값</p>
    <dl>
      <div><dt>첫 답변 시간<span>접수부터 첫 고객 답변까지</span></dt><dd>{duration(first)}</dd></div>
      <div><dt>처리 완료 시간<span>접수부터 처리 완료까지</span></dt><dd>{duration(completed)}</dd></div>
    </dl>
  </section>;
}

function Summary({ scope, period }) {
  const data = sample[scope];
  const current = data[period];
  const previous = period === 'current' ? data.previous : data.earlier;
  const comparison = periods[period].compare;
  const counts = sum(data.pending);
  const opened = sum(current.opened);
  const closed = sum(current.done);
  const failed = scope === 'tech' ? 0 : 1;
  const complaint = percent(current.negative);
  const satisfaction = percent(current.csat);
  return <>
    <div className="analytics-metrics">
      <Metric label="신규 접수" value={opened} unit="건" basis={comparison} comparison={delta(opened - storeTotal(previous.opened), '건')} />
      <Metric label="처리 완료" value={closed} unit="건" basis={comparison} comparison={delta(closed - storeTotal(previous.done), '건')} />
      <Metric label="24시간 이상 미처리" value={data.aged} unit="건"
        basis="조회 시점 기준" comparison={`전체 미처리 ${counts}건 중 ${percent([data.aged, counts])}%`} />
      <Metric label="부정·매우 부정 문의 비율" value={complaint} unit="%"
        basis={`분석 완료 ${current.negative[1]}건 중 ${current.negative[0]}건`}
        comparison={comparison + ' ' + delta(complaint - percent(previous.negative), '%p')}
        detail={`분석 중 ${opened - current.negative[1] - failed}건 · 실패 ${failed}건`} />
      <Metric label="고객 만족도 (CSAT)" value={satisfaction} unit="%"
        basis={`설문 응답 ${current.csat[1]}건 중 만족 ${current.csat[0]}건`}
        comparison={comparison + ' ' + delta(satisfaction - percent(previous.csat), '%p')}
        detail="5점 설문 중 4·5점 응답" />
    </div>
    <div className="analytics-detail-grid">
      <TrendChart dates={periods[period].dates} opened={current.opened} done={current.done} />
      <BacklogChart counts={data.pending} />
      <CategoryChanges current={current.categories} previous={previous.categories} comparison={comparison} />
      <Latency first={current.reply} completed={current.resolve} />
    </div>
  </>;
}

function IndividualReport({ scope, period, onSelect }) {
  const keys = scope === 'org' ? ['support', 'tech'] : [scope];
  return <section className="analytics-card analytics-individual">
    <h2>상담사별 실적</h2>
    <div className="analytics-table-scroll">
      <table className="analytics-table">
        <colgroup span="4" /><colgroup span="3" /><colgroup span="1" />
        <thead>
          <tr>
            {['상담사', '소속 팀', '기간 내 배정', '기간 내 처리 완료'].map(label => <th key={label} scope="col" rowSpan={2}>{label}</th>)}
            <th className="analytics-pending-heading" scope="colgroup" colSpan={3}>현재 미처리</th>
            <th scope="col" rowSpan={2} aria-describedby="analytics-first-response-basis">첫 답변 중앙값</th>
          </tr>
          <tr>{['할당 티켓', '이관', '검토 요청'].map(label => <th className="analytics-pending-column" key={label} scope="col">{label}</th>)}</tr>
        </thead>
        <tbody>{keys.flatMap(key => reports[key][period].map(([name, assigned, closed, response]) => {
          const { assigned: pendingAssigned, transfer, review } = pendingWork[name];
          return <tr key={key + name}>
            <th scope="row"><button type="button" className="analytics-agent-link" onClick={() => onSelect(key, name)}
              aria-label={name + ' 상담사 상세'}>{name}</button></th>
            <td>{scopeNames[key]}</td><td>{assigned}건</td><td>{closed}건</td>
            <td>{pendingAssigned}건</td><td>{transfer}건</td><td>{review}건</td><td>{response}분</td>
          </tr>;
        }))}</tbody>
      </table>
    </div>
    <div className="analytics-individual-notes analytics-muted">
      <p>처리 완료는 완료를 기록한 상담사 기준입니다.</p>
      <p>할당 티켓·이관은 현재 담당 티켓, 검토 요청은 받은 요청 중 미회신 건입니다.</p>
      <p id="analytics-first-response-basis">선택 기간에 해당 상담사가 보낸 첫 고객 답변을 기준으로, 티켓 접수부터 걸린 시간의 중앙값을 표시합니다.</p>
    </div>
  </section>;
}

function getDetail(selection, permission) {
  if (!permission?.individual || !selection || !permission.scopes.includes(selection.scope)) return null;
  const { scope, period, team, name } = selection;
  if ((scope !== 'org' && team !== scope) || !Object.hasOwn(reports, team) || !Object.hasOwn(periods, period)) return null;
  const metrics = reports[team][period].find(row => row[0] === name);
  return metrics ? selection : null;
}

function AgentDetail({ selection, onBack }) {
  const { team, name, period } = selection;
  const [, assigned, closed, response] = reports[team][period].find(row => row[0] === name);
  const work = pendingWork[name];
  const series = individualTrends[team][period][name];
  const dateRange = periods[period];
  const title = useRef(null);
  useEffect(() => { title.current.focus({ preventScroll: true }); }, [name]);

  return <>
    <button type="button" className="desk-button analytics-detail-back" onClick={onBack}>
      <span aria-hidden="true">‹</span> 상담사별 실적
    </button>
    <div className="analytics-agent-heading">
      <div>
        <h1 id="analytics-title" ref={title} tabIndex={-1}>{name} <span>상담사 상세</span></h1>
        <p className="analytics-muted">{scopeNames[team]}</p>
      </div>
      <div className="analytics-agent-period">
        <p>목록에서 선택한 기간</p>
        <strong>{dateRange.label} · {dateRange.dates[0]}–{dateRange.dates.at(-1)}</strong>
      </div>
    </div>
    <div className="analytics-metrics">
      <Metric label="기간 내 배정" value={assigned} unit="건" basis="선택 기간 기준" />
      <Metric label="기간 내 처리 완료" value={closed} unit="건" basis="선택 기간 기준" />
      <Metric label="현재 미처리 티켓" value={work.assigned + work.transfer} unit="건" basis="조회 시점 기준" />
      <Metric label="첫 답변 중앙값" value={response} unit="분" basis="선택 기간 기준" />
      <Metric label="미회신 검토 요청" value={work.review} unit="건" basis="조회 시점 기준 · 티켓과 별도" />
    </div>
    <div className="analytics-detail-grid">
      <TrendChart dates={dateRange.dates} opened={series.assigned} done={series.done} openingLabel="배정" />
      <BacklogChart counts={[work.assigned, work.transfer]} layout={individualBacklog} />
    </div>
    <p className="analytics-muted analytics-agent-basis">첫 답변 중앙값은 티켓 접수부터 해당 상담사가 보낸 첫 고객 답변까지 걸린 시간의 중앙값입니다.</p>
  </>;
}

function Dashboard({ permission }) {
  const [detail, setDetail] = useState(() => getDetail(history.state?.agentDetail, permission));
  const [listState] = useState(() => {
    const state = history.state?.agentList;
    return permission?.individual && state && permission.scopes.includes(state.scope) && Object.hasOwn(periods, state.period) ? state : null;
  });
  const [scope, setScope] = useState(detail?.scope ?? listState?.scope ?? permission?.scopes[0]);
  const [period, setPeriod] = useState(detail?.period ?? listState?.period ?? 'current');
  const [tab, setTab] = useState(detail || listState || (permission?.individual && params.get('tab') === 'agents') ? 'agents' : 'summary');

  useEffect(() => {
    function syncDetail(event) {
      const next = getDetail(event.state?.agentDetail, permission);
      const filters = next ?? event.state?.agentList;
      setDetail(next);
      if (filters && permission?.individual && permission.scopes.includes(filters.scope) && Object.hasOwn(periods, filters.period)) {
        setScope(filters.scope);
        setPeriod(filters.period);
        setTab('agents');
      }
    }
    window.addEventListener('popstate', syncDetail);
    return () => window.removeEventListener('popstate', syncDetail);
  }, [permission]);

  function openDetail(team, name) {
    const next = getDetail({ team, name, scope, period }, permission);
    if (!next) return;
    history.replaceState({ ...history.state, agentList: { scope, period } }, '');
    history.pushState({ agentDetail: next }, '', '#06-04');
    setDetail(next);
    window.scrollTo(0, 0);
  }

  return <DeskLayout active="analytics" accountName={permission?.name}>
    <section className="analytics" aria-labelledby="analytics-title">
      {detail ? <AgentDetail selection={detail} onBack={() => history.back()} /> : <>
        <h1 id="analytics-title">분석 대시보드</h1>
        {permission ? <>
          <div className="analytics-toolbar">
            <nav aria-label="분석 보기">
              <button className="desk-button" type="button" aria-pressed={tab === 'summary'} onClick={() => setTab('summary')}>조직·팀 실적</button>
              {permission.individual && <button className="desk-button" type="button" aria-pressed={tab === 'agents'} onClick={() => setTab('agents')}>상담사별 실적</button>}
            </nav>
            <div className="analytics-filters">
              <label>기간<select value={period} onChange={event => { if (Object.hasOwn(periods, event.target.value)) setPeriod(event.target.value); }}>
                {Object.entries(periods).map(([key, value]) => <option key={key} value={key}>{value.label}</option>)}
              </select></label>
              <label>조직 범위<select value={scope} onChange={event => { if (permission.scopes.includes(event.target.value)) setScope(event.target.value); }}>
                {permission.scopes.map(key => <option key={key} value={key}>{scopeNames[key]}</option>)}
              </select></label>
            </div>
          </div>
          {tab === 'agents' && permission.individual ? <IndividualReport scope={scope} period={period} onSelect={openDetail} /> : <Summary scope={scope} period={period} />}
        </> : <div className="analytics-denied">
          <p>접근 권한이 필요합니다.</p>
          <a className="desk-button" href="?preview=administrator">임시 접근</a>
        </div>}
      </>}
    </section>
  </DeskLayout>;
}

createRoot(document.getElementById('app')).render(<Dashboard permission={role} />);
