import React from 'react';
import { createRoot } from 'react-dom/client';
import { DeskLayout } from '../00-Shared/00-DeskLayout.jsx';

const listUrl = new URL('../03-TicketListPage/03-TicketListPage.html', location.href);
const params = new URLSearchParams(location.search);
const fromAssignedTab = params.get('tab') === 'assigned';
const listHref = listUrl.href + (fromAssignedTab ? '#03-02' : '');

function returnToList(event) {
  if (!params.has('ticket')) return;
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  const currentIndex = window.navigation?.currentEntry?.index;
  const previousUrl = window.navigation?.entries().find(
    entry => entry.index === currentIndex - 1
  )?.url || document.referrer;
  if (!previousUrl) return;
  const previous = new URL(previousUrl);
  if (previous.origin === listUrl.origin && previous.pathname === listUrl.pathname && history.length > 1) {
    event.preventDefault();
    history.back();
  }
}

function TicketDetailPage() {
  return (
    <DeskLayout active="tickets">
      <section className="ticket-detail" aria-labelledby="ticket-detail-title">
        <a className="desk-button ticket-detail-back" href={listHref} onClick={returnToList}>
          <span aria-hidden="true">‹</span> 티켓 목록
        </a>
        <h1 id="ticket-detail-title">티켓 상세</h1>
        <p className="ticket-detail-placeholder">준비중입니다.</p>
      </section>
    </DeskLayout>
  );
}

createRoot(document.getElementById('app')).render(<TicketDetailPage />);
