import React from 'react';
import { createRoot } from 'react-dom/client';
import { flushSync } from 'react-dom';
import { DeskLayout, inboxUrl } from '../00-Shared/00-DeskLayout.jsx';

function WorkHomePage() {
  return (
    <DeskLayout active="home">

      <section id="desk-home-view" aria-labelledby="desk-home-title">
        <div className="desk-heading">
          <div><h1 id="desk-home-title">업무 홈</h1><p className="desk-sub">한서윤님의 오늘 업무를 확인하세요.</p></div>
          <a className="desk-button" href="#02-02"><svg aria-hidden="true"><use href="#desk-settings"/></svg>화면 설정</a>
        </div>
        <div id="desk-cards" className="desk-stack">
          <section className="desk-card" data-card="tickets">
            <h2>내게 할당된 티켓 <span className="desk-count">3건</span></h2>
            <p>오늘 배정되었거나 아직 처리가 남은 티켓</p>
            <a className="desk-ticket-list-link" href="../03-TicketListPage/03-TicketListPage.html#03-02">전체 보기</a>
            <table className="desk-table">
              <thead><tr><th scope="col">티켓</th><th scope="col">상태</th><th scope="col">배정</th></tr></thead>
              <tbody>
                <tr><td><span className="desk-ticket-id">#1048</span><strong>환불 진행 상황을 알고 싶어요</strong></td><td><span className="desk-badge desk-badge-needed">답변 필요</span></td><td className="desk-time">오늘 10:42 <svg aria-hidden="true"><use href="#desk-right"/></svg></td></tr>
                <tr><td><span className="desk-ticket-id">#1046</span><strong>예약 확인서를 다시 보내 주세요</strong></td><td><span className="desk-badge desk-badge-active">처리 중</span></td><td className="desk-time">오늘 09:20 <svg aria-hidden="true"><use href="#desk-right"/></svg></td></tr>
                <tr><td><span className="desk-ticket-id">#1031</span><strong>결제 취소 내역을 확인해 주세요</strong></td><td><span className="desk-badge desk-badge-needed">답변 필요</span></td><td className="desk-time">전날 16:35 <svg aria-hidden="true"><use href="#desk-right"/></svg></td></tr>
              </tbody>
            </table>
          </section>
          <section className="desk-card" data-card="requests">
            <h2>받은 요청 <span className="desk-count">2건</span></h2>
            <p>나에게 들어온 검토 요청</p>
            <a className="desk-ticket-list-link" href={inboxUrl + '?tab=review'}>전체 보기</a>
            <a className="desk-request" href={inboxUrl + '?tab=review&request=review-1048'}><strong>환불 안내 답변 검토</strong><span>박유진 · #1048</span><span className="desk-time">오늘 10:08 <svg aria-hidden="true"><use href="#desk-right"/></svg></span></a>
            <a className="desk-request" href={inboxUrl + '?tab=review&request=review-1042'}><strong>배송 지연 안내 검토</strong><span>이도윤 · #1042</span><span className="desk-time">오늘 09:36 <svg aria-hidden="true"><use href="#desk-right"/></svg></span></a>
          </section>
          <section className="desk-card" data-card="performance">
            <h2>내 실적</h2><p>처리 건수</p>
            <div className="desk-metrics"><div className="desk-metric">오늘<strong>12건</strong></div><div className="desk-metric">전날<strong>15건</strong></div></div>
          </section>
        </div>
        <p id="desk-empty" className="desk-card" hidden>표시할 카드가 없습니다. 화면 설정에서 카드를 추가할 수 있습니다.</p>
      </section>
      <section id="desk-settings-view" aria-labelledby="desk-settings-title" hidden>
        <a className="desk-back" href="#02-01">‹ 업무 홈</a>
        <h1 id="desk-settings-title">화면 설정</h1>
        <p className="desk-sub">업무 홈에 표시할 카드와 순서를 설정하세요.</p>
        <section className="desk-card desk-settings-card" aria-labelledby="desk-card-settings-title">
          <h2 id="desk-card-settings-title">카드 표시 및 순서</h2>
          <p>위아래 버튼이나 드래그로 카드 순서를 변경할 수 있습니다.</p>
          <div className="desk-setting-head" aria-hidden="true"><span>카드</span><span>표시</span><span>순서</span></div>
          <div id="desk-settings-rows"></div>
          <div className="desk-actions">
            <button type="button" className="desk-button" id="desk-reset">기본 배치로 되돌리기</button>
            <a href="#02-01" className="desk-button">취소</a>
            <button type="button" className="desk-button desk-button-solid" id="desk-save">저장</button>
          </div>
        </section>
      </section>

    </DeskLayout>
  );
}

const root = createRoot(document.getElementById('app'));
flushSync(() => root.render(<WorkHomePage />));

    const deskHome = document.querySelector('#desk-home-view');
    const deskSettings = document.querySelector('#desk-settings-view');
    const deskCards = document.querySelector('#desk-cards');
    const deskRows = document.querySelector('#desk-settings-rows');
    const cardDefinitions = {
      tickets: ['내게 할당된 티켓', '오늘 배정된 티켓과 처리가 남은 티켓'],
      requests: ['받은 요청', '나에게 들어온 검토 요청'],
      performance: ['내 실적', '오늘과 전날의 처리 건수']
    };
    const defaultCards = () => Object.keys(cardDefinitions).map(id => ({ id, visible: true }));
    const cardElements = new Map([...deskCards.children].map(card => [card.dataset.card, card]));
    const storageKey = 'voc.work-home.han-seoyun.v1';

    function loadDeskCards() {
      try {
        const cards = JSON.parse(localStorage.getItem(storageKey));
        const ids = Object.keys(cardDefinitions);
        if (Array.isArray(cards) && cards.length === ids.length &&
            new Set(cards.map(card => card?.id)).size === ids.length &&
            cards.every(card => ids.includes(card?.id) && typeof card.visible === 'boolean')) return cards;
      } catch {
        // A local file preview can run with browser storage disabled.
      }
      return defaultCards();
    }

    let savedCards = loadDeskCards();
    let editingCards = defaultCards();
    let draggedCard = null;

    function renderDeskHome() {
      for (const { id, visible } of savedCards) {
        const element = cardElements.get(id);
        element.hidden = !visible;
        deskCards.append(element);
      }
      document.querySelector('#desk-empty').hidden = savedCards.some(card => card.visible);
    }

    function deskIcon(id) {
      return '<svg aria-hidden="true"><use href="#desk-' + id + '"/></svg>';
    }

    function renderDeskSettings() {
      deskRows.innerHTML = editingCards.map(({ id, visible }, index) => {
        const [title, description] = cardDefinitions[id];
        const disabledUp = index === 0 ? ' disabled' : '';
        const disabledDown = index === editingCards.length - 1 ? ' disabled' : '';
        return '<div class="desk-setting-row" data-id="' + id + '">' +
          '<div class="desk-setting-title"><span class="desk-grip" draggable="true">' + deskIcon('grip') + '</span>' +
          '<span><strong>' + title + '</strong><small>' + description + '</small></span></div>' +
          '<button type="button" class="desk-switch" role="switch" aria-label="' + title + ' 표시" aria-checked="' + visible + '"></button>' +
          '<div class="desk-arrows"><button type="button" class="desk-arrow" data-direction="up" aria-label="' + title + ' 위로 이동"' + disabledUp + '>' + deskIcon('up') + '</button>' +
          '<button type="button" class="desk-arrow" data-direction="down" aria-label="' + title + ' 아래로 이동"' + disabledDown + '>' + deskIcon('down') + '</button></div></div>';
      }).join('');
    }

    function moveDeskCard(id, destination) {
      const index = editingCards.findIndex(card => card.id === id);
      if (index < 0 || destination < 0 || destination >= editingCards.length || index === destination) return;
      const [card] = editingCards.splice(index, 1);
      editingCards.splice(destination, 0, card);
      renderDeskSettings();
    }

    deskRows.addEventListener('click', event => {
      const button = event.target.closest('button');
      if (!button) return;
      const { id } = button.closest('[data-id]').dataset;
      const index = editingCards.findIndex(card => card.id === id);
      if (button.getAttribute('role') === 'switch') {
        editingCards[index].visible = !editingCards[index].visible;
        button.setAttribute('aria-checked', String(editingCards[index].visible));
        return;
      }
      const direction = button.dataset.direction;
      moveDeskCard(id, index + (direction === 'up' ? -1 : 1));
      const row = deskRows.querySelector('[data-id="' + id + '"]');
      (row.querySelector('[data-direction="' + direction + '"]:not(:disabled)') ||
        row.querySelector('.desk-arrow:not(:disabled)'))?.focus();
    });

    deskRows.addEventListener('dragstart', event => {
      const handle = event.target.closest('.desk-grip');
      if (!handle) return;
      draggedCard = handle.closest('[data-id]').dataset.id;
      event.dataTransfer.setData('text/plain', draggedCard);
      event.dataTransfer.effectAllowed = 'move';
    });
    deskRows.addEventListener('dragover', event => {
      if (draggedCard && event.target.closest('[data-id]')) event.preventDefault();
    });
    deskRows.addEventListener('drop', event => {
      const row = event.target.closest('[data-id]');
      if (!draggedCard || !row) return;
      event.preventDefault();
      moveDeskCard(draggedCard, editingCards.findIndex(card => card.id === row.dataset.id));
      draggedCard = null;
    });
    deskRows.addEventListener('dragend', () => { draggedCard = null; });

    document.querySelector('#desk-reset').addEventListener('click', () => {
      editingCards = defaultCards();
      renderDeskSettings();
    });
    document.querySelector('#desk-save').addEventListener('click', () => {
      savedCards = editingCards.map(card => ({ ...card }));
      try {
        localStorage.setItem(storageKey, JSON.stringify(savedCards));
      } catch {
        // The current preview keeps its preferences in memory.
      }
      location.hash = '02-01';
    });

    function showScreenFromHash() {
      const settings = location.hash === '#02-02';
      deskHome.hidden = settings;
      deskSettings.hidden = !settings;
      document.title = (settings ? '화면 설정' : '업무 홈') + ' | 섭명뭐';
      if (settings) {
        editingCards = savedCards.map(card => ({ ...card }));
        renderDeskSettings();
      } else {
        renderDeskHome();
      }
      window.scrollTo(0, 0);
    }
    window.addEventListener('hashchange', showScreenFromHash);
    showScreenFromHash();
