import React, { useEffect, useRef, useState } from 'react';
import { analysisLabel, kstDay, receivedLabel, updatedLabel, useTicketData } from './ticketData.js';
import './03-TicketListPage.css';

const statuses = ['미처리', '보류', '답변완료'];
const emotions = ['긍정', '중립', '부정', '매우 부정'];
const periods = [1, 3, 7, 30];
const sortOptions = {
  default: { label: '접수 오래된순', descending: false },
  'updated-desc': { label: '업데이트 최신순', descending: true },
  'updated-asc': { label: '업데이트 오래된순', descending: false },
  'received-desc': { label: '접수일자 최신순', descending: true },
  'received-asc': { label: '접수일자 오래된순', descending: false }
};
const sortOrder = Object.keys(sortOptions);
const day = 86_400_000;
const pad = value => String(value).padStart(2, '0');
const dateKey = date => [date.getFullYear(), pad(date.getMonth() + 1), pad(date.getDate())].join('-');
const dayNumber = key => Date.parse(key + 'T00:00:00Z') / day;
const initialFilters = () => ({ team: [], assignee: [], status: [], category: [], emotion: [], period: 'all', start: '', end: '' });

const badgeClass = { 미배정: 'neutral', 미처리: 'amber', 보류: 'blue', 답변완료: 'green', 긍정: 'green', 중립: 'amber', 부정: 'red', '매우 부정': 'strong-red', '분류 대기': 'neutral', '분석 중': 'neutral', '분석 실패': 'red' };
function Badge({ value }) {
  return <span className={'ticket-badge badge-' + (badgeClass[value] || 'neutral')}>{value}</span>;
}

function Checkboxes({ values, selected, onToggle }) {
  return <div className="ticket-checks">{values.map(option => {
    const id = typeof option === 'string' ? option : option.id;
    const label = typeof option === 'string' ? option : option.name;
    return <label key={id}><input type="checkbox" checked={selected.includes(id)} onChange={() => onToggle(id)} />{label}</label>;
  })}</div>;
}

function Calendar({ name, value, onPick, today }) {
  const [month, setMonth] = useState(() => {
    const ref = new Date((value || today) + 'T12:00:00');
    return new Date(ref.getFullYear(), ref.getMonth(), 1);
  });
  const year = month.getFullYear();
  const monthIndex = month.getMonth();
  const days = new Date(year, monthIndex + 1, 0).getDate();
  const changeMonth = offset => setMonth(new Date(year, monthIndex + offset, 1));
  return <section className="ticket-calendar" aria-label={name + ' 달력'}>
    <h3>{name}</h3>
    <output aria-label={name} className="ticket-date">{value || '날짜를 선택해 주세요'}</output>
    <div className="ticket-month">
      <button type="button" aria-label={name + ' 이전 달'} onClick={() => changeMonth(-1)}>‹</button>
      <strong>{year}년 {monthIndex + 1}월</strong>
      <button type="button" aria-label={name + ' 다음 달'} onClick={() => changeMonth(1)}>›</button>
    </div>
    <div className="ticket-calendar-grid">
      {['일','월','화','수','목','금','토'].map(title => <span key={title}>{title}</span>)}
      {Array.from({ length: month.getDay() }, (_, index) => <span key={'blank-' + index} />)}
      {Array.from({ length: days }, (_, index) => {
        const date = dateKey(new Date(year, monthIndex, index + 1));
        return <button key={date} type="button" aria-label={name + ' ' + date} aria-pressed={date === value}
          disabled={date > today} onClick={() => onPick(date)}>{index + 1}</button>;
      })}
    </div>
  </section>;
}
function FilterDialog({ current, onApply, options }) {
  const today = kstDay(Date.now());
  const categories = options.data?.categories || [];
  const teams = [{ id: 'unassigned', name: '미배정' }, ...(options.data?.teams || [])];
  const assignees = [{ id: 'unassigned', name: '미지정' }, ...(options.data?.assignees || [])];
  const dialog = useRef(null);
  const [draft, setDraft] = useState(current);
  const [view, setView] = useState('filters');
  const [dates, setDates] = useState({ start: '', end: '' });
  const [sideSelection, setSideSelection] = useState([]);
  useEffect(() => {
    dialog.current.showModal();
  }, []);
  const set = (field, value) => setDraft(old => ({ ...old, [field]: value }));
  const toggle = (list, value) => list.includes(value) ? list.filter(item => item !== value) : [...list, value];
  const close = () => dialog.current.close();
  const startCalendar = () => {
    setDates({ start: draft.start || kstDay(Date.now() - 6 * day), end: draft.end || today });
    setView('dates');
  };
  const startCategories = () => {
    setSideSelection(draft.category);
    setView('categories');
  };
  const dateError = dates.start > dates.end ? '종료일은 시작일보다 빠를 수 없습니다.'
    : dates.start && dates.end && dayNumber(dates.end) - dayNumber(dates.start) + 1 > 365 ? '최대 365일까지 조회할 수 있습니다.' : '';
  return <dialog ref={dialog} className={'ticket-dialog ticket-dialog-' + view} aria-labelledby="ticket-filter-title"
    onClose={() => onApply(null)} onCancel={event => { if (view !== 'filters') { event.preventDefault(); setView('filters'); } }}>
    {view === 'dates' ? <section className="ticket-panel">
      <header><h2 id="ticket-filter-title">접수 기간 직접 지정</h2><p>시작일과 종료일을 선택하세요. 최대 365일까지 조회할 수 있습니다.</p></header>
      <div className="ticket-calendars">
        <Calendar today={today} name="시작일" value={dates.start} onPick={start => setDates(old => ({ ...old, start }))} />
        <Calendar today={today} name="종료일" value={dates.end} onPick={end => setDates(old => ({ ...old, end }))} />
      </div>
      {dateError && <p className="ticket-date-error" role="alert">{dateError}</p>}
      <footer><button className="desk-button" type="button" onClick={() => setView('filters')}>취소</button><button className="desk-button desk-button-solid" type="button" disabled={!!dateError} onClick={() => { setDraft({ ...draft, ...dates, period: 'custom' }); setView('filters'); }}>기간 적용</button></footer>
    </section> : <div className="ticket-filter-layout">
      <section className="ticket-panel">
        <header><h2 id="ticket-filter-title">필터 설정</h2><p>조회할 티켓의 조건을 선택하세요.</p></header>
        <div className="ticket-filter-body">
          {options.loading && <p role="status">등록된 필터 항목을 불러오는 중입니다.</p>}
          {options.error && <p role="alert">{options.error} <button className="desk-button" type="button" onClick={options.retry}>다시 시도</button></p>}
          <fieldset><legend>담당 부서</legend><Checkboxes values={teams} selected={draft.team} onToggle={value => set('team', toggle(draft.team, value))} /></fieldset>
          <fieldset><legend>담당자</legend><Checkboxes values={assignees} selected={draft.assignee} onToggle={value => set('assignee', toggle(draft.assignee, value))} /></fieldset>
          <fieldset><legend>처리 상태</legend><Checkboxes values={statuses} selected={draft.status} onToggle={value => set('status', toggle(draft.status, value))} /></fieldset>
          <fieldset className="ticket-category-field"><legend>카테고리</legend>
            <button type="button" className="desk-button ticket-all-categories" onClick={startCategories}>전체 카테고리 보기</button>
            <Checkboxes values={categories.filter((item, i) => i < 4 || draft.category.includes(item.id))} selected={draft.category} onToggle={value => set('category', toggle(draft.category, value))} />
          </fieldset>
          <fieldset><legend>감성</legend><Checkboxes values={emotions} selected={draft.emotion} onToggle={value => set('emotion', toggle(draft.emotion, value))} /></fieldset>
          <fieldset><legend>접수 기간</legend>
            <div className="ticket-periods">{periods.map(value => <button key={value} className="desk-button" type="button" aria-pressed={draft.period === value} onClick={() => set('period', value)}>최근 {value}일</button>)}
              <button className="desk-button" type="button" aria-pressed={draft.period === 'custom'} onClick={startCalendar}>직접 지정</button>
            </div>
            <p>{draft.period === 'custom' ? draft.start + ' ~ ' + draft.end : '직접 지정 시 최대 365일'}</p>
          </fieldset>
        </div>
        <footer><button type="button" className="desk-button ticket-reset" onClick={() => setDraft(initialFilters())}>초기화</button>
          <button type="button" className="desk-button" onClick={close}>취소</button>
          <button type="button" className="desk-button desk-button-solid" onClick={() => onApply(draft)}>적용</button></footer>
      </section>
      {view === 'categories' && <aside className="ticket-panel ticket-category-panel" aria-labelledby="ticket-categories-title">
        <header><div className="ticket-side-heading"><h2 id="ticket-categories-title">전체 카테고리</h2><button type="button" className="desk-button" onClick={() => setView('filters')}>닫기</button></div>
          <p>고객사에 등록된 카테고리를 선택하세요.</p></header>
        <div className="ticket-side-options">{!options.loading && !options.error && !categories.length && <p>등록된 카테고리가 없습니다.</p>}<Checkboxes values={categories} selected={sideSelection} onToggle={value => setSideSelection(toggle(sideSelection, value))} /></div>
        <footer><button type="button" className="desk-button desk-button-solid" onClick={() => { set('category', sideSelection); setView('filters'); }}>선택 완료</button></footer>
      </aside>}
    </div>}
  </dialog>;
}

function Pagination({ page, total, onPage }) {
  if (total === 0) return null;
  const first = Math.floor((page - 1) / 10) * 10 + 1;
  const last = Math.min(first + 9, total);
  const button = (caption, value, name) => <button type="button" className="desk-button" onClick={() => onPage(value)} aria-label={name}>{caption}</button>;
  return <nav className="ticket-pagination" aria-label="페이지 이동">
    <div className="ticket-page-controls">
      {first > 1 && <>{button('«', 1, '첫 페이지')}{button('‹', first - 10, '이전 10페이지')}</>}
      {Array.from({ length: last - first + 1 }, (_, index) => first + index).map(value =>
        <button type="button" className="desk-button" key={value} aria-label={value + '페이지'} aria-current={value === page ? 'page' : undefined} onClick={() => onPage(value)}>{value}</button>)}
      {last < total && <>{button('›', last + 1, '다음 10페이지')}{button('»', total, '마지막 페이지')}</>}
    </div>
    <span className="ticket-page-count">{page} / {total} 페이지</span>
  </nav>;
}

export default function TicketListPage({ mine, active, onExpired }) {
  const [searchType, setSearchType] = useState('title');
  const [input, setInput] = useState('');
  const [search, setSearch] = useState({ key: 'title', query: '' });
  const [filters, setFilters] = useState(initialFilters);
  const [modal, setModal] = useState(false);
  const [order, setOrder] = useState('default');
  const [pageSize, setPageSize] = useState(10);
  const [page, setPage] = useState(1);
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(timer);
  }, [active]);

  const params = new URLSearchParams({ scope: mine ? 'mine' : 'all', search_by: search.key,
    sort: order, page_size: pageSize, page });
  if (search.query) params.set('q', search.query);
  for (const key of ['team', 'assignee', 'category', 'status', 'emotion']) {
    for (const value of filters[key]) params.append(key === 'emotion' ? 'sentiment' : key, value);
  }
  if (filters.period === 'custom') {
    params.set('start', filters.start);
    params.set('end', filters.end);
  } else if (filters.period !== 'all') params.set('days', filters.period);
  const list = useTicketData(active ? '/api/tickets?' + params : null, onExpired);
  const options = useTicketData(active ? '/api/tickets/options' : null, onExpired);
  const total = list.data?.total ?? 0;
  const totalPages = Math.ceil(total / pageSize);
  const currentPage = list.data?.page ?? page;
  const visible = list.data?.items || [];
  const offset = (currentPage - 1) * pageSize;
  const periodLabel = filters.period === 'custom' ? filters.start + ' ~ ' + filters.end
    : filters.period !== 'all' ? '최근 ' + filters.period + '일' : '';
  const names = (ids, choices, missing) => ids.map(id =>
    id === 'unassigned' ? missing : choices?.find(option => option.id === id)?.name || '선택됨');
  const filterLabels = [...filters.status, ...names(filters.category, options.data?.categories),
    ...names(filters.team, options.data?.teams, '미배정'), ...names(filters.assignee, options.data?.assignees, '미지정'),
    ...filters.emotion, ...(periodLabel ? [periodLabel] : [])];
  const { descending, label: sortLabel } = sortOptions[order];
  function cycleSort() {
    setOrder(current => sortOrder[(sortOrder.indexOf(current) + 1) % sortOrder.length]);
    setPage(1);
  }
  const reset = () => {
    setFilters(initialFilters());
    setInput('');
    setSearchType('title');
    setSearch({ key: 'title', query: '' });
    setPage(1);
  };
  return <section className="ticket-list" aria-labelledby="ticket-list-title">
    <h1 id="ticket-list-title">티켓 목록</h1>
    <nav className="ticket-tabs" aria-label="티켓 범위">
      <a onClick={() => setPage(1)} href="#03-01" className="desk-button" aria-current={!mine ? 'page' : undefined}>전체 티켓</a>
      <a onClick={() => setPage(1)} href="#03-02" className="desk-button" aria-current={mine ? 'page' : undefined}>내게 할당된 티켓</a>
    </nav>
    <form className="ticket-search" onSubmit={event => {
      event.preventDefault(); setSearch({ key: searchType, query: input.trim() }); setPage(1);
    }}>
      <select aria-label="검색 대상" value={searchType} onChange={event => setSearchType(event.target.value)}>
        <option value="title">제목</option><option value="customer">고객 ID</option><option value="id">티켓 ID</option>
      </select>
      <input type="search" aria-label="검색어" value={input}
        onChange={event => setInput(Array.from(event.target.value).slice(0, 50).join(''))} placeholder="검색어를 입력해 주세요" />
      <button type="submit" className="desk-button desk-button-solid">검색</button>
    </form>
    <div className="ticket-toolbar">
      <div className="ticket-results" aria-live="polite"><strong>{list.loading ? '조회 중…' : list.error ? '조회 실패' : '조회 결과 ' + total.toLocaleString('ko-KR') + '건'}</strong>
        {filterLabels.map((value, index) => <span key={index}>{value}</span>)}
        {(filterLabels.length > 0 || search.query) && <button type="button" className="desk-button" onClick={reset}>초기화</button>}
      </div>
      <div className="ticket-tools">
        <label htmlFor="ticket-page-size">페이지당</label>
        <select id="ticket-page-size" value={pageSize} onChange={event => { setPageSize(Number(event.target.value)); setPage(1); }}>
          {[10, 20, 30, 50].map(value => <option key={value} value={value}>{value}건</option>)}
        </select>
        <button type="button" className="desk-button" onClick={() => setModal(true)}>필터 설정</button>
      </div>
    </div>
    <div className="ticket-table-wrap">
      <table className="ticket-table"><colgroup>{['5%', '10%', '23%', '10%', '10%', '9%', '10%', '9%', '14%'].map((width, index) => <col key={index} style={{ width }} />)}</colgroup>
        <thead><tr>{['행 번호', '티켓번호', '제목', '카테고리', '담당 부서', '담당자', '처리 상태', '감성'].map(title => <th scope="col" key={title}>{title}</th>)}
          <th scope="col" className="ticket-sort-column" aria-sort={descending ? 'descending' : 'ascending'}>
            <button id="ticket-sort" type="button" onClick={cycleSort}>
              <span className="ticket-sort-title">업데이트 / 접수 <span aria-hidden="true">↕</span></span>
              <span className="ticket-sort-label">{sortLabel}</span>
            </button>
          </th>
        </tr></thead>
        <tbody>{visible.map((ticket, index) => {
          const detailHref = '#04-01?ticket=' + encodeURIComponent(ticket.ticket_uuid) + (mine ? '&tab=assigned' : '');
          const category = analysisLabel(ticket, 'category');
          return <tr key={ticket.ticket_uuid} data-ticket={ticket.ticket_number}>
            <td className="ticket-row-number">{offset + index + 1}</td><td>#{ticket.ticket_number}</td><th scope="row">
              <a className="ticket-detail-link" href={detailHref}>{ticket.title}</a>
            </th>
            <td>{badgeClass[category] ? <Badge value={category} /> : category}</td>
            <td>{ticket.team_name || <Badge value="미배정" />}</td><td>{ticket.assignee_name || '미지정'}</td>
            <td><Badge value={ticket.status} /></td><td><Badge value={analysisLabel(ticket, 'sentiment')} /></td>
            <td className="ticket-updated-cell">
              <time dateTime={ticket.updated_at}>{updatedLabel(ticket.updated_at, now)}</time>
              <time className="ticket-received-date" dateTime={ticket.created_at}>접수 {receivedLabel(ticket.created_at, now)}</time>
            </td>
          </tr>;
        })}</tbody>
      </table>
      {list.loading && <div className="ticket-empty" role="status">티켓을 불러오는 중입니다.</div>}
      {list.error && <div className="ticket-empty" role="alert"><strong>{list.error}</strong>
        <button type="button" className="desk-button" onClick={list.retry}>다시 시도</button></div>}
      {list.data && !visible.length && <div className="ticket-empty"><strong>{search.query || filterLabels.length
        ? '검색 조건에 맞는 티켓이 없습니다.' : mine ? '내게 할당된 티켓이 없습니다.' : '접수된 티켓이 없습니다.'}</strong>
        {(search.query || filterLabels.length > 0) && <button type="button" className="desk-button" onClick={reset}>조건 초기화</button>}</div>}
    </div>
    <Pagination page={currentPage} total={totalPages} onPage={setPage} />
    {modal && <FilterDialog current={filters} options={options} onApply={next => { if (next) { setFilters(next); setPage(1); } setModal(false); }} />}
  </section>;
}
