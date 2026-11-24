export const requestGroups = {
  "transfer": [
    {
      "id": "1048",
      "requestId": "transfer-1048",
      "category": "환불",
      "sentiment": "매우 부정",
      "unread": true,
      "title": "환불 진행 상황을 알고 싶어요",
      "sender": "김하늘",
      "time": "11/24 10:21",
      "status": "미처리",
      "summary": "결제 취소는 완료됐고 입금 예정일 확인이 필요합니다.",
      "memo": "결제 취소 처리는 완료됐습니다. 카드사 반영 일정을 확인한 뒤 고객에게 환불 예정일을 안내해 주세요. 고객에게는 확인 후 다시 연락드리겠다고 말씀드렸습니다.",
      "extra": "안녕하세요. 카드사에 환불 반영 일정을 확인한 뒤 다시 안내드리겠습니다."
    },
    {
      "id": "1037",
      "requestId": "transfer-1037",
      "category": "교환",
      "sentiment": "중립",
      "unread": true,
      "title": "교환 상품 수거 일정 문의",
      "sender": "정수빈",
      "time": "11/24 09:48",
      "status": "미처리",
      "summary": "택배사 수거 일정을 확인해 주세요.",
      "memo": "고객이 이번 주 안으로 수거를 원합니다. 택배사에 접수된 수거 요청을 확인하고 방문 가능 일정을 알려 주세요."
    },
    {
      "id": "1029",
      "requestId": "transfer-1029",
      "category": "결제",
      "sentiment": "분석 중",
      "unread": false,
      "title": "결제 영수증 재발급 요청",
      "sender": "이도윤",
      "time": "11/23 16:12",
      "status": "처리",
      "summary": "영수증을 재발급해 전달했습니다.",
      "memo": "결제 영수증을 재발급해 고객에게 전달했습니다. 재문의 시 발급 내역을 확인해 주세요."
    }
  ],
  "review": [
    {
      "id": "1048",
      "requestId": "review-1048",
      "category": "환불",
      "sentiment": "매우 부정",
      "unread": true,
      "title": "환불 안내 답변 검토",
      "sender": "박유진",
      "time": "11/24 10:08",
      "status": "미처리",
      "summary": "환불 예정일과 안내 문구를 확인해 주세요.",
      "memo": "고객에게 발송하기 전에 환불 예정일이 맞는지 검토해 주세요. 첨부한 환불 처리 내역과 대조하고, 카드사 반영 기간 안내 문구도 확인 부탁드립니다.",
      "extra": "환불 처리 내역.png"
    },
    {
      "id": "1042",
      "requestId": "review-1042",
      "category": "배송",
      "sentiment": "분석 중",
      "unread": true,
      "title": "배송 지연 안내 검토",
      "sender": "이도윤",
      "time": "11/24 09:36",
      "status": "보류",
      "summary": "출고 일정과 보상 안내 범위를 확인해 주세요.",
      "memo": "예상 출고일과 고객에게 안내할 보상 범위를 확인 부탁드립니다. 출고 일정은 물류팀 회신을 기다리고 있습니다."
    },
    {
      "id": "1026",
      "requestId": "review-1026",
      "category": "쿠폰",
      "sentiment": "중립",
      "unread": false,
      "title": "쿠폰 재발급 안내 검토",
      "sender": "정수빈",
      "time": "11/23 14:30",
      "status": "처리",
      "summary": "사용 기한 연장 안내 문구를 확인해 주세요.",
      "memo": "재발급 쿠폰의 사용 기한과 적용 상품을 안내 문구에서 확인해 주세요."
    }
  ]
};

export const ticketProfiles = {
  '1048': {
    receivedAt: '2026-11-23 10:42',
    transferredAt: '2026-11-24 10:21',
    priorTransfers: [
      ['김하늘', '2026-11-24 09:15', '결제 취소 승인 여부를 확인하고 환불 진행 상황을 알려 주세요.'],
      ['박유진', '2026-11-23 11:05', '고객 요청을 확인했습니다. 주문과 결제 취소 내역을 확인해 주세요.']
    ]
  },
  '1037': {
    receivedAt: '2026-11-23 14:22',
    transferredAt: '2026-11-24 09:48',
    priorTransfers: [
      ['정수빈', '2026-11-23 15:10', '교환이 접수되어 수거 가능 일정을 먼저 확인해 주세요.']
    ]
  },
  '1029': { receivedAt: '2026-11-23 13:06', transferredAt: '2026-11-23 16:12', priorTransfers: [] },
  '1042': { receivedAt: '2026-11-24 09:02' },
  '1026': { receivedAt: '2026-11-23 10:18' }
};
