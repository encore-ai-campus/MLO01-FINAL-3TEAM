// Previously shared bookmarks used hash routes or standalone HTML files.
(() => {
  const launcher = document.querySelector('[data-desk-launcher]');
  const [screen, query = ''] = location.hash.slice(1).split('?');
  const params = new URLSearchParams(location.search);
  for (const [key, value] of new URLSearchParams(query)) params.set(key, value);

  const paths = {
    login: '/login/', home: '/home/', tickets: '/tickets/',
    detail: '/tickets/', inbox: '/inbox/', analytics: '/analytics/',
  };
  const screens = {
    '03-01': 'tickets', '03-02': 'tickets', '04-01': 'detail',
  };
  const page = launcher ? launcher.dataset.deskLauncher : screens[screen];
  if (!Object.hasOwn(paths, page)) return;

  const target = new URL(paths[page], location.protocol === 'file:'
    ? 'http://127.0.0.1:18082' : location.origin);
  if (page === 'tickets' && screen === '03-02') target.searchParams.set('scope', 'mine');
  if (page === 'detail') {
    const id = params.get('ticket');
    if (/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(id || '')) {
      target.pathname += id + '/';
    }
    if (params.get('tab') === 'assigned') target.searchParams.set('scope', 'mine');
  }
  if (page === 'home' && screen === '02-02') target.hash = '02-02';
  if (page === 'inbox' || page === 'analytics') {
    for (const key of ['tab', 'request', 'preview', 'empty', 'scope', 'q']) {
      if (params.has(key)) target.searchParams.set(key, params.get(key));
    }
  }
  if (!launcher && location.pathname === '/login/') {
    const login = new URL('/login/', location.origin);
    login.searchParams.set('next', target.pathname + target.search + target.hash);
    location.replace(login);
    return;
  }
  if (launcher) launcher.href = target.href;
  location.replace(target);
})();
