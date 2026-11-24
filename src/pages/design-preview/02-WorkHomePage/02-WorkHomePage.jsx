import React from 'react';
import { createRoot } from 'react-dom/client';
import { flushSync } from 'react-dom';
import { DeskLayout, inboxUrl, opinionIntakeUrl, settingsDisplayUrl } from '../00-Shared/00-DeskLayout.jsx';

import { currentStaff, hasStaffRight, withStaffPreview } from '../00-Shared/00-StaffAccess.js';

import { loadCards } from '../00-Shared/00-DeskPreferences.js';

function WorkHomePage() {
  const person = currentStaff();
  return (
    <DeskLayout active="home">

      <section id="desk-home-view" aria-labelledby="desk-home-title">
        <div className="desk-heading">
          <div><h1 id="desk-home-title">업무 홈</h1><p className="desk-sub">{person?.name ?? '상담사'}님의 오늘 업무를 확인하세요.</p></div>
          <div className="desk-home-actions">
            <a className="desk-button" href={withStaffPreview(opinionIntakeUrl)}>직원 의견 등록</a>
            {hasStaffRight(person, 'opinions') &&
              <a className="desk-button" href={withStaffPreview(opinionIntakeUrl.replace('view=intake', 'view=opinions'))}>직원 의견 보기</a>}
            <a className="desk-button" href={withStaffPreview(settingsDisplayUrl)}><svg aria-hidden="true"><use href="#desk-settings"/></svg>화면 설정</a>
          </div>
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
    </DeskLayout>
  );
}

if (location.hash === '#02-02') {
  location.replace(withStaffPreview(settingsDisplayUrl));
} else {
  const root = createRoot(document.getElementById('app'));
  flushSync(() => root.render(<WorkHomePage />));
  const cards = document.querySelector('#desk-cards');
  const elements = new Map([...cards.children].map(card => [card.dataset.card, card]));
  const preferences = loadCards();
  for (const { id, visible } of preferences) {
    const card = elements.get(id);
    card.hidden = !visible;
    cards.append(card);
  }
  document.querySelector('#desk-empty').hidden = preferences.some(card => card.visible);
}
