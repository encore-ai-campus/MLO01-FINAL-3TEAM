import { useCallback, useEffect, useState } from 'react';
import LoginPage from './pages/01-LoginPage/01-LoginPage.jsx';
import { DeskLayout } from './pages/00-Shared/00-DeskLayout.jsx';
import './pages/00-Shared/00-DeskLayout.css';
import TicketListPage from './pages/03-TicketListPage/03-TicketListPage.jsx';
import TicketDetailPage from './pages/04-TicketDetailPage/04-TicketDetailPage.jsx';

function readRoute() {
  const [screen, query] = location.hash.slice(1).split('?');
  const params = new URLSearchParams(query);
  return {
    mine: screen === '03-02' || (screen === '04-01' && params.get('tab') === 'assigned'),
    ticket: screen === '04-01' ? params.get('ticket') : null,
  };
}

export default function App() {
  const [route, setRoute] = useState(readRoute);
  const [account, setAccount] = useState(null);
  const [loading, setLoading] = useState(true);
  const [attempt, setAttempt] = useState(0);
  const [error, setError] = useState('');
  const [logoutError, setLogoutError] = useState('');
  const expire = useCallback(() => { setAccount(null); setLogoutError(''); }, []);
  useEffect(() => {
    function navigate() { setRoute(readRoute()); window.scrollTo(0, 0); }
    window.addEventListener('hashchange', navigate);
    return () => window.removeEventListener('hashchange', navigate);
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError('');
    async function checkSession() {
      try {
        const response = await fetch('/api/auth/me', { signal: controller.signal, cache: 'no-store' });
        if (response.status === 401) { expire(); return; }
        if (!response.ok) throw new Error();
        const identity = await response.json();
        if (!controller.signal.aborted) setAccount(identity);
      } catch {
        if (!controller.signal.aborted) setError('로그인 상태를 확인하지 못했습니다.');
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    }
    checkSession();
    return () => controller.abort();
  }, [attempt, expire]);
  useEffect(() => {
    document.title = (account ? (route.ticket ? '티켓 상세' : '티켓 목록') : '로그인') + ' | 섭명뭐';
  }, [account, route.ticket]);

  async function logout() {
    try {
      const response = await fetch('/api/auth/logout', { method: 'POST' });
      if (!response.ok && response.status !== 401) throw new Error();
      expire();
      location.hash = '03-01';
    } catch {
      setLogoutError('로그아웃하지 못했습니다. 다시 시도해 주세요.');
    }
  }
  if (loading || error) return <main className="screen"><div role="status">{error || '로그인 상태를 확인하고 있습니다.'}
    {error && <button className="desk-button" onClick={() => setAttempt(value => value + 1)}>다시 시도</button>}</div></main>;
  if (!account) return <LoginPage onLoginSuccess={() => {
    if (!location.hash) location.hash = '03-01';
    setAttempt(value => value + 1);
  }} />;
  return <DeskLayout active="tickets" accountName={account.name || account.email} onLogout={logout}>
    {logoutError && <p role="alert">{logoutError}</p>}
    <div hidden={Boolean(route.ticket)}>
      <TicketListPage mine={route.mine} active={!route.ticket} onExpired={expire} />
    </div>
    {route.ticket && <TicketDetailPage id={route.ticket} mine={route.mine} onExpired={expire} />}
  </DeskLayout>;
}
