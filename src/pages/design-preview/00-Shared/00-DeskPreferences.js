export const cardDefinitions = {
  tickets: ['내게 할당된 티켓', '오늘 배정된 티켓과 처리가 남은 티켓'],
  requests: ['받은 요청', '나에게 들어온 검토 요청'],
  performance: ['내 실적', '오늘과 전날의 처리 건수']
};
export const sidebarLabels = {
  home: '업무 홈', tickets: '티켓 목록', inbox: '받은 요청',
  followup: '팔로업', analytics: '분석 대시보드'
};

const cardKey = 'voc.work-home.han-seoyun.v1';
const sidebarKey = 'voc.desk.sidebar.han-seoyun.v1';
const preferencesKey = 'voc.desk.preferences.han-seoyun.v1';

export function readPreview(key, fallback) {
  try { return JSON.parse(localStorage.getItem(key)) ?? fallback; }
  catch { return fallback; }
}

export function writePreview(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); return true; }
  catch { return false; }
}

export const defaultCards = () => Object.keys(cardDefinitions).map(id => ({ id, visible: true }));

export function loadCards() {
  const cards = readPreview(preferencesKey, {}).cards ?? readPreview(cardKey, null);
  const ids = Object.keys(cardDefinitions);
  if (Array.isArray(cards) && cards.length === ids.length &&
      new Set(cards.map(card => card?.id)).size === ids.length &&
      cards.every(card => ids.includes(card?.id) && typeof card.visible === 'boolean')) return cards;
  return defaultCards();
}

export function loadSidebar() {
  const saved = readPreview(preferencesKey, {}).sidebar ?? readPreview(sidebarKey, {});
  return Object.fromEntries(Object.keys(sidebarLabels).map(id => [id, saved[id] !== false]));
}

export function saveDeskPreferences(cards, sidebar) {
  return writePreview(preferencesKey, { cards, sidebar });
}
