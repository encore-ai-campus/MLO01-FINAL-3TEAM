import { readPreview, writePreview } from './00-DeskPreferences.js';
import { currentStaff, hasStaffRight } from './00-StaffAccess.js';

export const replyKey = 'voc.settings.replies.v1';
export const replyCategories = ['결제', '배송', '계정', '기타'];
const seedReplies = [
  { id: 'r1', name: '환불 안내', category: '결제', scope: 'mine', body: '안녕하세요. 환불 신청 내역을 확인한 뒤 예정일을 안내드릴게요.', date: '2026-11-24' },
  { id: 'r2', name: '배송 일정 안내', category: '배송', scope: 'mine', body: '배송 일정을 확인해 안내드리겠습니다.', date: '2026-11-21' },
  { id: 'r3', name: '계정 확인 요청', category: '계정', scope: 'mine', body: '계정 확인에 필요한 정보를 안내드리겠습니다.', date: '2026-11-20' },
  { id: 't1', name: '환불 지연 안내', category: '결제', scope: 'team', body: '환불 지연 사유와 접수 상태를 확인하겠습니다.', date: '2026-11-24' },
  { id: 't2', name: '배송 상태 안내', category: '배송', scope: 'team', body: '상품의 현재 배송 상태를 안내드립니다.', date: '2026-11-21' },
  { id: 't3', name: '로그인 오류 안내', category: '계정', scope: 'team', body: '발생한 오류 메시지를 알려 주시면 확인하겠습니다.', date: '2026-11-20' }
];
export const scopeLabel = { mine: '내 신속답변', team: '팀 신속답변' };


// The preview signs in as the sample agent. Old records predate owner/team fields.
const sampleOwner = 'sy';
const sampleTeam = '고객지원팀';
const favoriteKey = 'voc.settings.reply-favorites.v1';
export function loadReplies() {
  return readPreview(replyKey, seedReplies).map(reply => ({
    ...reply,
    owner: reply.owner ?? sampleOwner,
    team: reply.scope === 'team' ? (reply.team ?? sampleTeam) : null
  }));
}
export function replyAccess() {
  const agent = currentStaff();
  const active = agent?.account === 'active';
  const memberships = active ? [...new Set([agent.team, ...(agent.additionalTeams ?? [])].filter(Boolean))] : [];
  return { id: active ? agent.id : null, memberships, canEditTeam: hasStaffRight(agent, 'replies') };
}
export function mayUseReply(reply, access) {
  return reply.scope === 'team' ? access.memberships.includes(reply.team) : reply.owner === access.id;
}
export function loadFavorites(access) {
  return readPreview(favoriteKey, {})[access.id] ?? [];
}
export function setFavorites(access, ids) {
  return writePreview(favoriteKey, { ...readPreview(favoriteKey, {}), [access.id]: ids });
}
