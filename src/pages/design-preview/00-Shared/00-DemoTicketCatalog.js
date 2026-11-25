import { demoTicketSubject, homeTickets } from './00-DemoTicketSubjects.js';

export const categories = ['결제', '예약', '배송', '환불', '주문', '교환', '계정 관리', '이용 문의'];
export const statuses = ['미할당', '할당', '답변완료', '문의 종료'];
export const emotions = ['긍정', '중립', '부정', '매우 부정'];
const agent = '한서윤';
const count = 1000;
const first = 1031;

// Request fixtures also contain tickets that precede the generated list.
const extra = {
  '1029': { title: '결제 영수증 재발급 요청', category: '결제', emotion: '분석 중', assignee: agent },
  '1026': { title: '쿠폰 재발급 안내 검토', category: '쿠폰', emotion: '중립', assignee: agent }
};
const inboxExamples = {
  '1037': { title: '교환 상품 수거 일정 문의', category: '교환', emotion: '중립' },
  '1042': { title: '배송 지연 안내 문의', category: '배송', emotion: '분석 중' },
  '1048': { emotion: '부정', received: new Date('2026-11-25T10:42:00+09:00') }
};

function ticketAt(index, now) {
  const id = String(first + index);
  const homeTicket = homeTickets[id];
  const status = homeTicket ? '할당' : statuses[Math.floor(index / 3) % 3];
  const received = inboxExamples[id]?.received ?? new Date(now.getTime() - (count - 1 - index) * 86 * 60_000);
  const updateDelay = (index * 73 % 1100) * 60_000;
  return {
    id,
    customer: 'C-' + String(821 + index % 113).padStart(4, '0'),
    title: demoTicketSubject(id),
    status,
    assignee: homeTicket ? agent : status === '미할당' ? '' : [agent, '박유진', '이도윤'][index % 3],
    category: index % 37 === 0 ? '분석 중' : index % 53 === 0 ? '분석 실패' : categories[index % categories.length],
    emotion: index % 29 === 0 ? '분석 중' : index % 47 === 0 ? '분석 실패' : emotions[Math.floor(index / 2) % emotions.length],
    received,
    updated: new Date(Math.max(received.getTime(), Math.min(received.getTime() + updateDelay, now.getTime()))),
    ...homeTicket,
    ...inboxExamples[id]
  };
}

export function demoTicketCatalog(now = new Date()) {
  return Array.from({ length: count }, (_, index) => ticketAt(index, now));
}

export function findDemoTicket(id, now = new Date()) {
  const index = Number(id) - first;
  if (!/^\d{4}$/.test(id ?? '')) return null;
  if (index >= 0 && index < count) return ticketAt(index, now);
  if (!extra[id]) return null;
  const received = new Date('2026-11-23T13:06:00+09:00');
  return { ...extra[id], id, status: '할당', received, updated: received };
}
