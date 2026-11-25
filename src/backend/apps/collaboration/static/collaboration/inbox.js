const drawer = document.getElementById('request-drawer');

async function submit(form) {
  const response = await fetch(form.action, {method: 'POST', body: new FormData(form), keepalive: true});
  if (response.status === 401) {
    location.reload();
    return false;
  }
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail?.message);
  }
  return true;
}

if (drawer) {
  drawer.close();
  drawer.showModal();
  const heading = drawer.querySelector('h2');
  const detailTitle = heading.textContent;
  const status = document.getElementById('inbox-status');
  const close = document.getElementById('drawer-close');
  const accept = document.getElementById('request-accept');
  heading.focus();

  drawer.addEventListener('click', event => {
    if (event.target === drawer) close.click();
    const view = event.target.closest('[data-view]')?.dataset.view;
    if (!view) return;
    for (const name of ['detail', 'write', 'history']) {
      const panel = document.getElementById('request-' + name);
      if (panel) panel.hidden = view !== name;
    }
    document.getElementById('detail-actions').hidden = view === 'write';
    if (accept) accept.hidden = view !== 'detail';
    const writeActions = document.getElementById('write-actions');
    if (writeActions) writeActions.hidden = view !== 'write';
    heading.textContent = {detail: detailTitle, write: '검토 답변 작성', history: '이관 이력'}[view];
    (view === 'write' ? document.getElementById('inbox-reply-text') : heading).focus();
  });
  drawer.addEventListener('cancel', event => {
    event.preventDefault();
    close.click();
  });

  const read = document.getElementById('request-read');
  if (read) {
    submit(read).then(ok => {
      if (!ok) return;
      document.getElementById('request-read-label').textContent = '확인';
      const {tab, selected} = drawer.dataset;
      if (!drawer.hasAttribute('data-needs-action')) {
        const link = document.querySelector(`.inbox-tabs [data-tab="${tab}"]`);
        link.dataset.attentionCount = Math.max(0, Number(link.dataset.attentionCount) - 1);
      }
      const row = document.querySelector(`tr[data-id="${selected}"]`);
      if (!row) return;
      row.removeAttribute('data-unread');
      row.querySelector('.inbox-unread-label')?.remove();
      if (document.getElementById('inbox').dataset.scope === 'unread') {
        row.remove();
        document.getElementById('inbox-empty').hidden = Boolean(document.querySelector('#inbox-rows tr'));
      }
    }).catch(() => {
      status.textContent = '확인 상태를 저장하지 못했습니다. 요청을 다시 열어 주세요.';
    });
  }

  if (accept) {
    const button = accept.querySelector('button');
    accept.addEventListener('submit', async event => {
      event.preventDefault();
      if (button.disabled) return;
      button.disabled = true;
      button.textContent = '수락 중…';
      status.textContent = '';
      try {
        if (await submit(accept)) location.reload();
      } catch (error) {
        status.textContent = error.message || '이관을 수락하지 못했습니다. 다시 시도해 주세요.';
        button.textContent = '수락';
        button.disabled = false;
      }
    });
  }

  const form = document.getElementById('request-write');
  if (form) {
    const textarea = form.querySelector('textarea');
    const send = drawer.querySelector('button[form="request-write"]');
    const sync = () => { send.disabled = !textarea.value.trim(); };
    sync();
    textarea.addEventListener('input', sync);
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (send.disabled || !textarea.value.trim()) return;
      send.disabled = true;
      textarea.readOnly = true;
      status.textContent = '';
      try {
        if (await submit(form)) location.reload();
      } catch (error) {
        status.textContent = error.message || '답변을 보내지 못했습니다. 다시 시도해 주세요.';
        textarea.readOnly = false;
        sync();
      }
    });
  }
}
