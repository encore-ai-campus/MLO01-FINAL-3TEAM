import React, { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { DeskLayout } from '../00-Shared/00-DeskLayout.jsx';

const agent = '한서윤';
const categories = ['결제', '예약', '배송', '환불', '주문', '교환', '계정 관리', '이용 문의'];
const statuses = ['미할당', '할당', '답변완료'];
const emotions = ['긍정', '중립', '부정', '매우 부정'];
const periods = [1, 3, 7, 30];
const sortOptions = {
  default: { label: '기본', key: 'received', descending: false },
  'updated-desc': { label: '업데이트 최신순', key: 'updated', descending: true },
  'updated-asc': { label: '업데이트 오래된순', key: 'updated', descending: false },
  'received-desc': { label: '접수일자 최신순', key: 'received', descending: true },
  'received-asc': { label: '접수일자 오래된순', key: 'received', descending: false }
};
const sortOrder = Object.keys(sortOptions);
const day = 86_400_000;
const now = new Date();
const pad = value => String(value).padStart(2, '0');
const dateKey = date => [date.getFullYear(), pad(date.getMonth() + 1), pad(date.getDate())].join('-');
const dayNumber = key => Date.parse(key + 'T00:00:00Z') / day;
const today = dateKey(now);
const previousDays = days => {
  const date = new Date(now);
  date.setDate(date.getDate() - days);
  return dateKey(date);
};
const initialFilters = () => ({ status: [], category: [], emotion: [], period: 'all', start: '', end: '' });
const sampleSubjects = [
  '카드 결제가 중복됐어요', '예약 확인서를 다시 보내 주세요', '배송 일정을 확인해 주세요',
  '환불 진행 상황을 알고 싶어요', '상품 옵션을 변경하고 싶어요', '교환 신청을 확인해 주세요',
  '회사 이메일 주소를 변경하고 싶어요', '이용 방법을 알려주세요'
];

const homeTickets = {
  '1031': { title: '결제 취소 내역을 확인해 주세요', category: '결제' },
  '1046': { title: '예약 확인서를 다시 보내 주세요', category: '예약' },
  '1048': { title: '환불 진행 상황을 알고 싶어요', category: '환불' }
};

const demoTickets = Array.from({ length: 1000 }, (_, index) => {
  const id = String(1031 + index);
  const homeTicket = homeTickets[id];
  const status = homeTicket ? '할당' : statuses[Math.floor(index / 3) % statuses.length];
  const received = new Date(now.getTime() - (999 - index) * 86 * 60_000);
  const updateDelay = (index * 73 % 1100) * 60_000;
  return {
    id,
    customer: 'C-' + String(821 + index % 113).padStart(4, '0'),
    title: sampleSubjects[index % sampleSubjects.length],
    status,
    assignee: homeTicket ? agent : status === '미할당' ? '' : [agent, '박유진', '이도윤'][index % 3],
    category: index % 37 === 0 ? '분석 중' : index % 53 === 0 ? '분석 실패' : categories[index % categories.length],
    emotion: index % 29 === 0 ? '분석 중' : index % 47 === 0 ? '분석 실패' : emotions[Math.floor(index / 2) % emotions.length],
    received,
    updated: new Date(Math.min(received.getTime() + updateDelay, now.getTime())),
    ...homeTicket
  };
});

function receivedLabel(date) {
  if (dateKey(date) === today) return pad(date.getHours()) + ':' + pad(date.getMinutes());
  const shortDate = pad(date.getMonth() + 1) + '/' + pad(date.getDate());
  return date.getFullYear() === now.getFullYear() ? shortDate : date.getFullYear() + '/' + shortDate;
}
function updatedLabel(date) {
  const minutes = Math.floor((now - date) / 60_000);
  if (minutes < 1) return '1분 미만';
  if (minutes < 60) return minutes + '분 전';
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return hours + '시간 전';
  const days = Math.floor(hours / 24);
  if (days <= 7) return days + '일 전';
  if (date.getFullYear() !== now.getFullYear()) return '지난해';
  if (date.getMonth() !== now.getMonth()) return '지난달';
  return '지난주';
}

const badgeClass = { 미할당: 'neutral', 할당: 'amber', 답변완료: 'green', 긍정: 'green', 중립: 'amber', 부정: 'red', '매우 부정': 'strong-red', '분석 중': 'neutral', '분석 실패': 'red' };
function Badge({ value }) {
  return <span className={'ticket-badge badge-' + (badgeClass[value] || 'neutral')}>{value}</span>;
}

function Checkboxes({ values, selected, onToggle }) {
  return <div className="ticket-checks">{values.map(value =>
    <label key={value}><input type="checkbox" checked={selected.includes(value)} onChange={() => onToggle(value)} />{value}</label>
  )}</div>;
}

function Calendar({ name, value, onPick }) {
  const [month, setMonth] = useState(() => {
    const ref = value ? new Date(value + 'T12:00:00') : now;
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
function FilterDialog({ current, onApply }) {
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
    setDates({ start: draft.start || previousDays(6), end: draft.end || today });
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
        <Calendar name="시작일" value={dates.start} onPick={start => setDates(old => ({ ...old, start }))} />
        <Calendar name="종료일" value={dates.end} onPick={end => setDates(old => ({ ...old, end }))} />
      </div>
      {dateError && <p className="ticket-date-error" role="alert">{dateError}</p>}
      <footer><button className="desk-button" type="button" onClick={() => setView('filters')}>취소</button><button className="desk-button desk-button-solid" type="button" disabled={!!dateError} onClick={() => { setDraft({ ...draft, ...dates, period: 'custom' }); setView('filters'); }}>기간 적용</button></footer>
    </section> : <div className="ticket-filter-layout">
      <section className="ticket-panel">
        <header><h2 id="ticket-filter-title">필터 설정</h2><p>조회할 티켓의 조건을 선택하세요.</p></header>
        <div className="ticket-filter-body">
          <fieldset><legend>처리 상태</legend><Checkboxes values={statuses} selected={draft.status} onToggle={value => set('status', toggle(draft.status, value))} /></fieldset>
          <fieldset className="ticket-category-field"><legend>카테고리</legend>
            <button type="button" className="desk-button ticket-all-categories" onClick={startCategories}>전체 카테고리 보기</button>
            <Checkboxes values={[...new Set([...categories.slice(0, 4), ...draft.category])]} selected={draft.category} onToggle={value => set('category', toggle(draft.category, value))} />
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
        <div className="ticket-side-options"><Checkboxes values={categories} selected={sideSelection} onToggle={value => setSideSelection(toggle(sideSelection, value))} /></div>
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

function TicketListPage() {
  const [mine, setMine] = useState(location.hash === '#03-02');
  const [searchType, setSearchType] = useState('title');
  const [input, setInput] = useState('');
  const [search, setSearch] = useState({ key: 'title', query: '' });
  const [filters, setFilters] = useState(initialFilters);
  const [modal, setModal] = useState(false);
  const [order, setOrder] = useState('default');
  const [pageSize, setPageSize] = useState(10);
  const [page, setPage] = useState(1);

  useEffect(() => {
    function syncTab() {
      setMine(location.hash === '#03-02');
      setPage(1);
    }
    window.addEventListener('hashchange', syncTab);
    return () => window.removeEventListener('hashchange', syncTab);
  }, []);

  const periodLabel = filters.period === 'custom' ? filters.start + ' ~ ' + filters.end
    : filters.period !== 'all' ? '최근 ' + filters.period + '일' : '';
  const filterLabels = [...filters.status, ...filters.category, ...filters.emotion, ...(periodLabel ? [periodLabel] : [])];

  const matches = demoTickets.filter(ticket => {
    const value = dateKey(ticket.received);
    const searchValue = search.key === 'id' ? '#' + ticket.id : ticket[search.key];
    return (!mine || ticket.assignee === agent) &&
      (!search.query || searchValue.toLocaleLowerCase().includes(search.query)) &&
      ['status', 'category', 'emotion'].every(key => !filters[key].length || filters[key].includes(ticket[key])) &&
      (filters.period === 'all' || (filters.period === 'custom'
        ? value >= filters.start && value <= filters.end
        : value >= previousDays(filters.period - 1) && value <= today));
  });
  const { key: sortKey, descending, label: sortLabel } = sortOptions[order];
  const showReceived = sortKey === 'received' && order !== 'default';
  matches.sort((a, b) => (descending ? b[sortKey] - a[sortKey] : a[sortKey] - b[sortKey]) || Number(a.id) - Number(b.id));
  const totalPages = Math.ceil(matches.length / pageSize);
  const currentPage = Math.min(page, Math.max(totalPages, 1));
  const offset = (currentPage - 1) * pageSize;
  const visible = matches.slice(offset, offset + pageSize);
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
  return <DeskLayout active="tickets"><section className="ticket-list" aria-labelledby="ticket-list-title">
    <h1 id="ticket-list-title">티켓 목록</h1>
    <nav className="ticket-tabs" aria-label="티켓 범위">
      <a href="#03-01" className="desk-button" aria-current={!mine ? 'page' : undefined}>전체 티켓</a>
      <a href="#03-02" className="desk-button" aria-current={mine ? 'page' : undefined}>내게 할당된 티켓</a>
    </nav>
    <form className="ticket-search" onSubmit={event => {
      event.preventDefault(); setSearch({ key: searchType, query: input.trim().toLocaleLowerCase() }); setPage(1);
    }}>
      <select aria-label="검색 대상" value={searchType} onChange={event => setSearchType(event.target.value)}>
        <option value="title">제목</option><option value="customer">고객 ID</option><option value="id">티켓 ID</option>
      </select>
      <input type="search" aria-label="검색어" value={input} maxLength={50} onChange={event => setInput(event.target.value)} placeholder="검색어를 입력해 주세요" />
      <button type="submit" className="desk-button desk-button-solid">검색</button>
    </form>
    <div className="ticket-toolbar">
      <div className="ticket-results" aria-live="polite"><strong>조회 결과 {matches.length.toLocaleString('ko-KR')}건</strong>
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
      <table className="ticket-table"><colgroup>{['5%', '10%', '27%', '11%', '10%', '11%', '11%', '15%'].map((width, index) => <col key={index} style={{ width }} />)}</colgroup>
        <thead><tr>{['행 번호', '티켓번호', '제목', '카테고리', '담당자', '처리 상태', '감성'].map(title => <th scope="col" key={title}>{title}</th>)}
          <th scope="col" className="ticket-sort-column" aria-sort={order === 'default' ? 'none' : descending ? 'descending' : 'ascending'}>
            <button id="ticket-sort" type="button" onClick={cycleSort}>
              <span className="ticket-sort-title">최근 업데이트 <span aria-hidden="true">↕</span></span>
              <span className="ticket-sort-label">{sortLabel}</span>
            </button>
          </th>
        </tr></thead>
        <tbody>{visible.map((ticket, index) => <tr key={ticket.id} data-ticket={ticket.id}>
          <td className="ticket-row-number">{matches.length - offset - index}</td><td>#{ticket.id}</td><th scope="row">{ticket.title}</th>
          <td>{badgeClass[ticket.category] ? <Badge value={ticket.category}/> : ticket.category}</td>
          <td>{ticket.assignee || '-'}</td><td><Badge value={ticket.status}/></td><td><Badge value={ticket.emotion}/></td>
          <td className="ticket-updated-cell">
            <time dateTime={ticket.updated.toISOString()}>{updatedLabel(ticket.updated)}</time>
            {showReceived && <time className="ticket-received-date" dateTime={ticket.received.toISOString()}>접수 {receivedLabel(ticket.received)}</time>}
          </td>
        </tr>)}</tbody>
      </table>
      {!visible.length && <div className="ticket-empty"><strong>검색 조건에 맞는 티켓이 없습니다.</strong><p>검색어를 바꾸거나 필터를 초기화해 보세요.</p>
        <button type="button" className="desk-button" onClick={reset}>조건 초기화</button></div>}
    </div>
    <Pagination page={currentPage} total={totalPages} onPage={setPage} />
    {modal && <FilterDialog current={filters} onApply={next => { if (next) { setFilters(next); setPage(1); } setModal(false); }} />}
  </section></DeskLayout>;
}

createRoot(document.getElementById('app')).render(<TicketListPage />);
