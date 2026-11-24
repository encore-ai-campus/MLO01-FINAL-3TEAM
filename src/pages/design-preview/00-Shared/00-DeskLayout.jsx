import React, { useEffect, useState } from 'react';
import { loadSidebar } from './00-DeskPreferences.js';
import { currentStaff, rolePolicyKey, staffKey, withStaffPreview } from './00-StaffAccess.js';
import { availableSettingsGroups } from './00-SettingsNavigation.js';

const workHome = '../02-WorkHomePage/02-WorkHomePage.html#02-01';
const ticketList = '../03-TicketListPage/03-TicketListPage.html';
const analytics = '../06-AnalyticsDashboardPage/06-AnalyticsDashboardPage.html';
export const settingsUrl = '../07-SettingsPage/07-SettingsPage.html';
export const settingsDisplayUrl = settingsUrl + '?view=display&from=home';
export const opinionIntakeUrl = '../07-StaffManagementPage/07-StaffManagementPage.html?view=intake';

export const inboxUrl = '../05-InboxPage/05-InboxPage.html';

export const defaultAccountName = '한서윤';

export function DeskLayout({active, children, accountName, settingsPreview, settingsCategory}) {
  const previewId = new URLSearchParams(location.search).get('as');
  const name = accountName ?? (previewId ? currentStaff()?.name : null) ?? defaultAccountName;
  const sidebar = loadSidebar();
  const [settingsOpen, setSettingsOpen] = useState(active === "settings");
  useEffect(() => {
    const keys = [rolePolicyKey, staffKey];
    let saved;
    try { saved = keys.map(key => localStorage.getItem(key)); }
    catch { return; }
    function refreshRestoredPage(event) {
      if (event.persisted && keys.some((key, index) => localStorage.getItem(key) !== saved[index])) location.reload();
    }
    window.addEventListener('pageshow', refreshRestoredPage);
    return () => window.removeEventListener('pageshow', refreshRestoredPage);
  }, []);
  const settingGroups = availableSettingsGroups(previewId ? (currentStaff()?.role === '관리자' && currentStaff()?.account === 'active' ? 'administrator' : 'employee') : settingsPreview);
  return (
    <>
  <svg aria-hidden="true" width="0" height="0" style={{ position: "absolute" }}>
    <symbol id="desk-home" viewBox="0 0 24 24"><path d="m3 10 9-7 9 7v10H3zM9 20v-6h6v6"/></symbol>
    <symbol id="desk-ticket" viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="14" rx="3"/><path d="M8 10h8m-8 4h5"/></symbol>
    <symbol id="desk-inbox" viewBox="0 0 24 24"><path d="M6 4h12l3 8v7a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1v-7zM3 12h5l2 3h4l2-3h5"/></symbol>
    <symbol id="desk-analytics" viewBox="0 0 24 24"><path d="M4 4v16h16M8 16v-4m5 4V8m5 8V5"/></symbol>
    <symbol id="desk-staff" viewBox="0 0 24 24"><rect x="3" y="7" width="18" height="13" rx="2"/><path d="M8 7V5a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2M3 12h18M10 12v2h4v-2"/></symbol>
    <symbol id="desk-clock" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></symbol>
    <symbol id="desk-settings" viewBox="0 0 24 24"><path d="M4 7h16M4 12h16M4 17h16M8 5v4m8 1v4m-6 1v4"/></symbol>
    <symbol id="desk-bell" viewBox="0 0 24 24"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4"/></symbol>
    <symbol id="desk-up" viewBox="0 0 24 24"><path d="m6 14 6-6 6 6"/></symbol>
    <symbol id="desk-down" viewBox="0 0 24 24"><path d="m6 10 6 6 6-6"/></symbol>
    <symbol id="desk-right" viewBox="0 0 24 24"><path d="m9 6 6 6-6 6"/></symbol>
    <symbol id="desk-grip" viewBox="0 0 24 24"><path d="M9 5h.01M15 5h.01M9 12h.01M15 12h.01M9 19h.01M15 19h.01" strokeWidth="3"/></symbol>
  </svg>
<div id="desk" className="desk">
    <header className="desk-bar">
      <a className="brand" href={withStaffPreview(workHome)}><span className="logo" aria-hidden="true">?</span><span>섭명뭐</span></a>
      <span title="알림"><svg aria-label="알림" role="img"><use href="#desk-bell"/></svg></span>
      <span className="desk-account" aria-label={'내 계정 ' + name}><span className="desk-avatar" aria-hidden="true">{name[0]}</span>{name}<svg aria-hidden="true"><use href="#desk-down"/></svg></span>
      <a id="desk-logout" className="desk-button desk-logout" href="../01-LoginPage/01-LoginPage.html">로그아웃</a>
    </header>
    <nav className="desk-nav" aria-label="업무 메뉴">
      {sidebar.home && <a className="desk-nav-item" href={withStaffPreview(workHome)} aria-current={active === "home" ? "page" : undefined}><svg aria-hidden="true"><use href="#desk-home"/></svg>업무 홈</a>}
      {sidebar.tickets && <a className="desk-nav-item" href={withStaffPreview(ticketList)} aria-current={active === "tickets" ? "page" : undefined}><svg aria-hidden="true"><use href="#desk-ticket"/></svg>티켓 목록</a>}
      {sidebar.inbox && <a className="desk-nav-item" href={withStaffPreview(inboxUrl)} aria-current={active === "inbox" ? "page" : undefined}><svg aria-hidden="true"><use href="#desk-inbox"/></svg>받은 요청</a>}
      {sidebar.followup && <span className="desk-nav-item" aria-disabled="true"><svg aria-hidden="true"><use href="#desk-clock"/></svg>팔로업</span>}
      {sidebar.analytics && <a className="desk-nav-item" href={active === "analytics" ? location.href : withStaffPreview(analytics)} aria-current={active === "analytics" ? "page" : undefined}><svg aria-hidden="true"><use href="#desk-analytics"/></svg>분석 대시보드</a>}
      <button className="desk-nav-item desk-nav-settings" type="button" aria-expanded={settingsOpen}
        aria-controls="desk-settings-links" data-active={active === "settings" || undefined}
        onClick={() => setSettingsOpen(open => !open)}>
        <svg aria-hidden="true"><use href="#desk-settings"/></svg>설정
        <svg className="desk-settings-chevron" aria-hidden="true"><use href="#desk-down"/></svg>
      </button>
      <div id="desk-settings-links" className="desk-settings-links" hidden={!settingsOpen}>
        {settingGroups.map(({ id, label }) => {
          const query = new URLSearchParams({ view: id });
          if (settingsPreview) query.set("preview", settingsPreview);
          return <a className="desk-nav-item desk-nav-subitem" key={id} href={withStaffPreview(settingsUrl + "?" + query)}
            aria-current={settingsCategory === id ? "page" : undefined}>{label}</a>;
        })}
      </div>
    </nav>
<main className="desk-content">{children}</main>
</div>
    </>
  );
}
