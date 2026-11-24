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

export function demoTicketSubject(id) {
  const index = Number(id) - 1031;
  if (!/^\d+$/.test(id ?? '') || index < 0 || index >= 1000) return null;
  return homeTickets[id]?.title ?? sampleSubjects[index % sampleSubjects.length];
}

export { homeTickets };
