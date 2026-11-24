import React, { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { DeskLayout } from '../00-Shared/00-DeskLayout.jsx';
import { availableSettingsGroups, settingsGroups } from '../00-Shared/00-SettingsNavigation.js';
import { cardDefinitions, defaultCards, loadCards, loadSidebar, readPreview, saveDeskPreferences, sidebarLabels, writePreview } from '../00-Shared/00-DeskPreferences.js';
import { StaffManagement } from '../07-StaffManagementPage/07-StaffManagementPage.jsx';
import { currentStaff, loadStaff, withStaffPreview } from '../00-Shared/00-StaffAccess.js';
import { RolePermissions } from './07-RolePermissions.jsx';
import { replyKey, replyCategories, scopeLabel, loadReplies, replyAccess, mayUseReply, loadFavorites, setFavorites } from '../00-Shared/00-QuickReplies.js';
import { AssignmentSettings } from './07-AssignmentSettings.jsx';

const params = new URLSearchParams(location.search);
const preview = params.has('as')
  ? (currentStaff()?.role === '관리자' && currentStaff()?.account === 'active' ? 'administrator' : 'employee')
  : params.get('preview');
const canManageRoles = preview === null || preview === 'administrator';
const settingsTiles = {
  replies: ['신속답변', '내 답변과 팀 답변을 찾아 관리합니다.'],
  display: ['화면 설정', '업무 홈 카드와 좌측 메뉴를 설정합니다.'],
  staff: ['사원 관리', '사원 계정, 소속과 권한을 관리합니다.'],
  permissions: ['권한 관리', '역할별 공통 권한과 추가 권한을 관리합니다.'],
  messenger: ['메신저 연동', '이관·수신 채널을 지정합니다.'],
  assignment: ['자동 배정', '배정 건수, 시점과 대상을 설정합니다.']
};
const permitted = availableSettingsGroups(preview);
const currentView = params.get('view') ?? 'home';
const group = settingsGroups.find(({ id, views }) => id === currentView || views.includes(currentView));
const allowed = (!group || permitted.includes(group)) && (currentView !== 'permissions' || canManageRoles);

function href(view, extra = {}) {
  const query = new URLSearchParams(extra);
  if (view !== 'home') query.set('view', view);
  if (params.has('preview')) query.set('preview', params.get('preview'));
  return withStaffPreview('07-SettingsPage.html' + (query.size ? '?' + query : ''));
}
const workHome = withStaffPreview('../02-WorkHomePage/02-WorkHomePage.html#02-01');
const date = () => new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Seoul', dateStyle: 'short' }).format(new Date());

function Header({ title, description }) {
  return <header className="settings-header">
    <div className="settings-path"><a className="desk-button" href={href(group?.id ?? 'home')}>‹ {group?.label ?? '설정 홈'}</a></div>
    <h1 id="settings-title">{title}</h1>
    {description && <p className="desk-sub">{description}</p>}
  </header>;
}
function Actions({ cancel = href(group?.id ?? 'home'), label = '저장', children }) {
  return <div className="settings-actions">{children}<a className="desk-button" href={cancel}>취소</a><button className="desk-button desk-button-solid" type="submit">{label}</button></div>;
}
function Switch({ label, value, onChange }) {
  return <button className="desk-switch" type="button" role="switch" aria-label={label} aria-checked={value} onClick={() => onChange(!value)} />;
}

function SettingsHome({ category }) {
  const groups = category ? [category] : permitted;
  return <>
    <header className="settings-header">
      <h1 id="settings-title">{category?.label ?? '설정'}</h1>
      {!category && <p className="desk-sub">내 화면과 권한이 있는 관리 기능을 설정합니다.</p>}
    </header>
    {groups.map(({ id, label, views }) => <section className="settings-link-group" key={id} aria-label={label}>
      {!category && <h2>{label}</h2>}
      <div className="settings-link-grid">
        {views.filter(view => view !== 'permissions' || canManageRoles).map(view => {
          const [title, description] = settingsTiles[view];
          return <a className="staff-card settings-tile" href={href(view)} key={view} aria-label={title}>
            <strong>{title}</strong><span aria-hidden="true">›</span><p>{description}</p>
          </a>;
        })}
      </div>
    </section>)}
  </>;
}

function ReplyDialog({ reply, onCancel, onConfirm }) {
  const ref = useRef(null);
  useEffect(() => {
    const node = ref.current;
    node.showModal();
    return () => node.close();
  }, []);
  return <dialog className="staff-dialog" ref={ref} aria-labelledby="reply-delete-title" onCancel={event => { event.preventDefault(); onCancel(); }}>
    <div className="staff-dialog-main"><h2 id="reply-delete-title">{onConfirm ? '신속답변을 삭제할까요?' : '본문 미리보기'}</h2><strong>{reply.name}</strong>
      <p className="desk-sub">{reply.team ?? scopeLabel[reply.scope]} · {reply.category}</p>
      <p className="reply-body">{onConfirm ? '삭제하면 이 답변이 목록에서 사라집니다.' : reply.body}</p></div>
    <div className="staff-dialog-actions"><button type="button" className="desk-button" onClick={onCancel}>{onConfirm ? '취소' : '닫기'}</button>
      {onConfirm && <button type="button" className="desk-button staff-danger" onClick={onConfirm}>삭제</button>}</div>
  </dialog>;
}

function ReplySettings() {
  const access = replyAccess();
  const { canEditTeam } = access;
  const [replies, setReplies] = useState(loadReplies);
  const [favorites, updateFavorites] = useState(() => loadFavorites(access));
  const [favoritesOnly, filterFavorites] = useState(false);
  const [teamFilter, setTeamFilter] = useState('');
  const [previewReply, setPreviewReply] = useState(null);
  const [scope, setScope] = useState(params.get('scope') === 'team' ? 'team' : 'mine');
  const [query, setQuery] = useState('');
  const [category, setCategory] = useState('');
  const [form, setForm] = useState(null);
  const [deleting, setDeleting] = useState(false);
  const [notice, setNotice] = useState('');
  const [error, setError] = useState('');
  function editable(reply, identity = access) {
    return !!identity.id && mayUseReply(reply, identity) && (reply.scope === 'mine' || identity.canEditTeam);
  }
  function checkWriteAccess(reply) {
    if (editable(reply, replyAccess())) return true;
    setDeleting(false);
    setError('이 신속답변을 편집할 권한이 필요합니다.');
    return false;
  }
  const visible = replies.filter(reply => reply.scope === scope && mayUseReply(reply, access) && (!teamFilter || scope === 'mine' || reply.team === teamFilter) && (!favoritesOnly || favorites.includes(reply.id)) && (!category || reply.category === category) && reply.name.toLowerCase().includes(query.trim().toLowerCase()));
  function start(reply) {
    setError('');
    setNotice('');
    if (reply && !editable(reply)) return;
    setForm(reply ?? { name: '', category: '', scope: scope === 'team' && canEditTeam ? 'team' : 'mine', body: '', owner: access.id, team: teamFilter || access.memberships[0] });
  }
  function toggleFavorite(id) {
    const next = favorites.includes(id) ? favorites.filter(item => item !== id) : [...favorites, id];
    if (setFavorites(access, next)) updateFavorites(next);
    else setNotice('브라우저에 즐겨찾기를 저장할 수 없습니다.');
  }
  function commit(next, message) {
    const stored = writePreview(replyKey, next);
    setReplies(next);
    setForm(null);
    setDeleting(false);
    setNotice(stored ? message : '브라우저에 저장할 수 없어 현재 화면에만 적용했습니다.');
  }
  function save(event) {
    event.preventDefault();
    if (!checkWriteAccess(form)) return;
    const name = form.name.trim();
    const body = form.body.trim();
    if (!name || !body) { setError('이름과 답변 내용을 입력해 주세요.'); return; }
    const record = { ...form, id: form.id ?? crypto.randomUUID(), team: form.scope === 'team' ? form.team : null, name, body, date: date() };
    const current = loadReplies();
    const next = form.id ? current.map(reply => reply.id === form.id ? record : reply) : [record, ...current];
    setScope(record.scope); setQuery(''); setCategory(''); setTeamFilter(''); filterFavorites(false);
    commit(next, '신속답변을 저장했습니다.');
  }
  if (form) return <>
    <Header title={form.id ? '신속답변 편집' : '신속답변 등록'} description="이름, 사용 범위와 답변 내용을 설정합니다." />
    <form className="staff-card settings-form settings-reply-form" onSubmit={save}>
      <label>이름<input required maxLength={80} value={form.name} onChange={event => setForm({ ...form, name: event.target.value })} /></label>
      <div className="settings-pair">
        <label>카테고리<select required value={form.category} onChange={event => setForm({ ...form, category: event.target.value })}><option value="">카테고리 선택</option>{replyCategories.map(item => <option key={item}>{item}</option>)}</select></label>
        <label>사용 범위<select value={form.scope} disabled={!!form.id} onChange={event => setForm({ ...form, scope: event.target.value })}><option value="mine">내 신속답변</option>{canEditTeam && access.memberships.length > 0 && <option value="team">팀 신속답변</option>}</select></label>
      </div>
      {form.scope === 'team' && <label>공유할 팀<select required disabled={!!form.id} value={form.team ?? ''} onChange={event => setForm({ ...form, team: event.target.value })}><option value="">팀 선택</option>{access.memberships.map(team => <option key={team}>{team}</option>)}</select></label>}
      <label>답변 내용<textarea required rows={6} maxLength={2000} value={form.body} onChange={event => setForm({ ...form, body: event.target.value })} /></label>
      {error && <p className="staff-error" role="alert">{error}</p>}
      <div className="settings-actions">
        {form.id && <button type="button" className="desk-button settings-delete" onClick={() => setDeleting(true)}>삭제</button>}
        <button type="button" className="desk-button" onClick={() => setForm(null)}>취소</button><button type="submit" className="desk-button desk-button-solid">저장</button>
      </div>
    </form>
    {deleting && <ReplyDialog reply={form} onCancel={() => setDeleting(false)}
      onConfirm={() => { if (checkWriteAccess(form)) commit(loadReplies().filter(reply => reply.id !== form.id), '신속답변을 삭제했습니다.'); }} />}
  </>;
  return <>
    <Header title="신속답변" description="자주 쓰는 답변을 찾아 등록하거나 수정합니다." />
    {notice && <p className="staff-notice" role="status">{notice}</p>}
    <div className="settings-toolbar">
      {Object.entries(scopeLabel).map(([key, label]) => <button className={'desk-button ' + (scope === key ? 'desk-button-solid' : '')} type="button" aria-pressed={scope === key} onClick={() => setScope(key)} key={key}>{label}</button>)}
      {access.id && (scope === 'mine' || (canEditTeam && access.memberships.length > 0)) && <button className="desk-button desk-button-solid settings-end" type="button" onClick={() => start()}>신속답변 등록</button>}
    </div>
    <div className="staff-filters settings-reply-filters">
      <input type="search" aria-label="신속답변 이름 검색" placeholder="신속답변 이름 검색" value={query} onChange={event => setQuery(event.target.value)} />
      <select aria-label="카테고리" value={category} onChange={event => setCategory(event.target.value)}><option value="">카테고리 전체</option>{replyCategories.map(item => <option key={item}>{item}</option>)}</select>
      {scope === 'team' && <select aria-label="소속 팀" value={teamFilter} onChange={event => setTeamFilter(event.target.value)}><option value="">소속 팀 전체</option>{access.memberships.map(team => <option key={team}>{team}</option>)}</select>}
      <button className={'desk-button ' + (favoritesOnly ? 'desk-button-solid' : '')} type="button" aria-pressed={favoritesOnly} onClick={() => filterFavorites(!favoritesOnly)}>즐겨찾기만</button>
    </div>
    <div className="staff-table-wrap"><table className="staff-table settings-reply-table" aria-label="신속답변 목록">
      <thead><tr>{['이름', '카테고리', '사용 범위', '최근 수정', '관리'].map(label => <th scope="col" key={label}>{label}</th>)}</tr></thead>
      <tbody>
        {visible.map(reply => <tr key={reply.id}>
          <th scope="row"><button className="settings-star" type="button" aria-label={reply.name + ' 즐겨찾기'} aria-pressed={favorites.includes(reply.id)} onClick={() => toggleFavorite(reply.id)}>{favorites.includes(reply.id) ? '★' : '☆'}</button>{reply.name}</th>
          <td>{reply.category}</td><td>{reply.team ?? scopeLabel[reply.scope]}</td><td>{reply.date}</td>
          <td className="settings-reply-actions"><button className="desk-button" type="button" onClick={() => setPreviewReply(reply)}>미리보기</button>
          {editable(reply) && <button className="desk-button" type="button" onClick={() => start(reply)}>편집</button>}</td>
        </tr>)}
        {!visible.length && <tr><td colSpan={5} className="staff-no-results">조건에 맞는 신속답변이 없습니다.</td></tr>}
      </tbody>
    </table></div>
    {previewReply && <ReplyDialog reply={previewReply} onCancel={() => setPreviewReply(null)} />}
  </>;
}

function DisplaySettings() {
  const [cards, setCards] = useState(loadCards);
  const [sidebar, setSidebar] = useState(loadSidebar);
  const [notice, setNotice] = useState('');
  const drag = useRef(null);
  function move(id, position) {
    setCards(current => {
      const from = current.findIndex(card => card.id === id);
      if (from < 0 || from === position || position < 0 || position >= current.length) return current;
      const next = [...current];
      const [card] = next.splice(from, 1);
      next.splice(position, 0, card);
      return next;
    });
  }
  function save(event) {
    event.preventDefault();
    if (saveDeskPreferences(cards, sidebar)) location.assign(params.get('from') === 'home' ? workHome : href('personal'));
    else setNotice('브라우저에서 화면 설정을 저장할 수 없습니다.');
  }
  const sidebarEntries = Object.entries(sidebarLabels).filter(([id]) => id !== 'analytics' || preview !== 'employee');
  return <>
    <Header title="화면 설정" description="업무 홈에 표시할 카드와 좌측 패널 항목을 선택합니다." />
    <form className="settings-display" onSubmit={save}>
      <section className="staff-card"><h2>카드 표시 및 순서</h2><p className="desk-sub">위아래 버튼이나 드래그로 카드 순서를 변경할 수 있습니다.</p>
        <div className="desk-setting-head" aria-hidden="true"><span>카드</span><span>표시</span><span>순서</span></div>
        {cards.map(({ id, visible }, index) => {
          const [title, description] = cardDefinitions[id];
          return <div className="desk-setting-row" key={id} onDragOver={event => { if (drag.current) event.preventDefault(); }}
            onDrop={event => { event.preventDefault(); if (drag.current) move(drag.current, index); drag.current = null; }}>
            <div className="desk-setting-title"><span className="desk-grip" draggable
              onDragStart={event => { drag.current = id; event.dataTransfer.setData('text/plain', id); }}
              onDragEnd={() => { drag.current = null; }}><svg aria-hidden="true"><use href="#desk-grip" /></svg></span>
              <span><strong>{title}</strong><small>{description}</small></span></div>
            <Switch label={title + ' 표시'} value={visible} onChange={next => setCards(cards.map(card => card.id === id ? { ...card, visible: next } : card))} />
            <div className="desk-arrows">
              <button type="button" className="desk-arrow" aria-label={title + ' 위로 이동'} disabled={index === 0} onClick={() => move(id, index - 1)}><svg aria-hidden="true"><use href="#desk-up" /></svg></button>
              <button type="button" className="desk-arrow" aria-label={title + ' 아래로 이동'} disabled={index === cards.length - 1} onClick={() => move(id, index + 1)}><svg aria-hidden="true"><use href="#desk-down" /></svg></button>
            </div>
          </div>;
        })}
        <button className="desk-button settings-reset" type="button" onClick={() => setCards(defaultCards())}>기본 배치로 되돌리기</button>
      </section>
      <section className="staff-card"><h2>좌측 패널 표시 설정</h2><p className="desk-sub">사용할 수 있는 메뉴 중 화면에 표시할 항목을 고릅니다.</p>
        <div className="settings-sidebar-options">{sidebarEntries.map(([id, label]) => <div className="settings-sidebar-option" key={id}>
          <strong>{label}</strong><Switch label={label + ' 메뉴 표시'} value={sidebar[id]} onChange={next => setSidebar({ ...sidebar, [id]: next })} />
        </div>)}</div>
      </section>
      {notice && <p className="staff-error" role="alert">{notice}</p>}
      <Actions cancel={params.get('from') === 'home' ? workHome : href('personal')} />
    </form>
  </>;
}

const messengerKey = 'voc.settings.messenger.v1';
function MessengerSettings() {
  const [config, setConfig] = useState(() => {
    const saved = readPreview(messengerKey, null)?.configs?.Discord;
    return { transfer: '', teams: {}, people: {}, ...saved };
  });
  const [team, setTeam] = useState(teams[0]);
  const [person, setPerson] = useState(() => loadStaff().find(item => item.account !== 'inactive')?.id ?? '');
  const [notice, setNotice] = useState('');
  const people = loadStaff().filter(item => item.account !== 'inactive');
  function update(next) {
    setConfig(current => ({ ...current, ...next }));
    setNotice('');
  }
  function save(event) {
    event.preventDefault();
    const previous = readPreview(messengerKey, null);
    const stored = writePreview(messengerKey, {
      provider: 'Discord',
      configs: { ...previous?.configs, Discord: config }
    });
    setNotice(stored ? '메신저 연동 설정을 저장했습니다.' : '브라우저에서 메신저 연동 설정을 저장할 수 없습니다.');
  }
  return <>
    <Header title="메신저 연동" description="이관 알림과 팀·사원별 수신 채널을 설정합니다." />
    <form className="settings-display" onSubmit={save}>
      <section className="staff-card settings-form">
        <div className="settings-pair">
          <label>메신저<input value="Discord" readOnly /></label>
          <label>기본 이관 채널<input required maxLength={100} placeholder="#voc-transfer" value={config.transfer} onChange={event => update({ transfer: event.target.value })} /></label>
        </div>
        <p className="desk-sub">팀·사원별 수신 채널은 아래에서 지정합니다.</p>
      </section>
      <section className="staff-card settings-form">
        <h2>팀·사원별 수신 채널</h2>
        <div className="settings-pair settings-target-pair">
          <label>팀<select value={team} onChange={event => setTeam(event.target.value)}>{teams.map(name => <option key={name}>{name}</option>)}</select></label>
          <label>팀 수신 채널<input maxLength={100} value={config.teams[team] ?? ''} onChange={event => update({ teams: { ...config.teams, [team]: event.target.value } })} /></label>
          <label>사원<select value={person} onChange={event => setPerson(event.target.value)}>{!people.length && <option value="">사원이 없습니다.</option>}{people.map(item => <option value={item.id} key={item.id}>{item.name}</option>)}</select></label>
          <label>개인 수신 채널<input disabled={!person} maxLength={100} value={config.people[person] ?? ''} onChange={event => update({ people: { ...config.people, [person]: event.target.value } })} /></label>
        </div>
      </section>
      {notice && <p className="staff-notice" role="status">{notice}</p>}
      <Actions />
    </form>
  </>;
}

const content = {
  home: SettingsHome,
  replies: ReplySettings,
  display: DisplaySettings,
  staff: () => <><Header title="사원 관리" /><StaffManagement /></>,
  permissions: () => <RolePermissions backHref={href('hr')} />,
  messenger: MessengerSettings,
  assignment: () => <AssignmentSettings Header={Header} Actions={Actions} Switch={Switch} />
};
const Page = Object.hasOwn(content, currentView) ? content[currentView] : SettingsHome;

function SettingsPage() {
  return <DeskLayout active="settings" settingsPreview={preview} settingsCategory={allowed ? group?.id : undefined}>
    <section className="staff-page settings-page" aria-labelledby="settings-title">
      {allowed ? <Page category={group?.id === currentView ? group : undefined} /> : <>
        <Header title="설정" />
        <p role="status">이 설정을 관리할 권한이 필요합니다.</p>
      </>}
    </section>
  </DeskLayout>;
}
createRoot(document.getElementById('app')).render(<SettingsPage />);
