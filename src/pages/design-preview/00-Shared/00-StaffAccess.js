import { readPreview, writePreview } from './00-DeskPreferences.js';

export const staffKey = 'voc-staff-preview-v1';
export const rolePolicyKey = 'voc-role-policy-v1';
export const teams = ['고객지원팀', '결제지원팀', '배송지원팀'];
export const staffRoles = ['상담사', '팀 리더', '관리자'];
export const editableRoles = staffRoles.slice(0, 2);
export const rights = {
  ticket: '티켓 상담',
  chat: '채팅 상담',
  replies: '팀 신속답변 편집',
  dashboard: '분석 대시보드 열람',
  opinions: '직원 의견(VoE) 열람'
};
export const roleRights = ['replies', 'dashboard', 'opinions'];
const legacyRights = ['ticket', 'chat', 'replies', 'dashboard'];
const legacyDefaults = { 상담사: [true, false, false, false], '팀 리더': [true, true, true, true], 관리자: [true, true, true, true] };
const previewAccounts = { administrator: 'admin', 'overview-only': 'admin', 'team-lead': 'hn', employee: 'sy' };
const sample = [
  { id: 'sy', name: '한서윤', email: 'seoyun@example.com', team: teams[0], role: '상담사', account: 'active', availability: '상담 가능', tickets: 7, reviews: 2,
    history: [
      { time: '2026-11-24 10:17', actor: '최지민', message: '소속 팀을 고객지원팀으로 변경' },
      { time: '2026-11-18 09:20', actor: '최지민', message: '상담사 계정 발급' }
    ] },
  { id: 'hn', name: '김하늘', email: 'haneul@example.com', team: teams[1], role: '팀 리더', account: 'active', availability: '상담 가능', tickets: 3, reviews: 1 },
  { id: 'sb', name: '정수빈', email: 'subin@example.com', team: teams[1], role: '상담사', account: 'active', availability: '부재 중', tickets: 5, reviews: 0 },
  { id: 'jw', name: '박지우', email: 'jiwoo@example.com', team: teams[0], role: '상담사', account: 'pending', tickets: 0, reviews: 0 },
  { id: 'dy', name: '이도윤', email: 'doyun@example.com', team: teams[0], role: '상담사', account: 'inactive', tickets: 0, reviews: 0 },
  { id: 'admin', name: '최지민', email: 'admin@example.com', team: teams[0], role: '관리자', account: 'active', availability: '상담 가능', tickets: 0, reviews: 0 }
].map(person => ({ history: [], overrides: {}, ...person }));

export function loadRolePolicy() {
  return readPreview(rolePolicyKey, {}) ?? {};
}

export function saveRolePolicy(policy) {
  return writePreview(rolePolicyKey, policy);
}

export function roleDefaults(role, policy = loadRolePolicy()) {
  const lead = role === '팀 리더' || role === '관리자';
  return { ticket: true, chat: true, replies: lead, dashboard: lead, opinions: lead, ...policy[role] };
}

function legacyOverrides(person) {
  if (!Array.isArray(person.permissions)) return {};
  const recorded = person.permissions.length === 3 ? ['ticket', 'replies', 'dashboard'] : legacyRights;
  const defaults = legacyDefaults[person.role] ?? legacyDefaults.상담사;
  return Object.fromEntries(recorded.flatMap((key, i) => {
    const value = person.permissions[i];
    // A historical change can be explicit even if it matches the former default.
    const item = key === 'dashboard' ? 'VoC 대시보드 접근' : rights[key];
    const changed = person.history?.some(row => row.item === item || row.item === rights[key]);
    return typeof value === 'boolean' && (value !== defaults[legacyRights.indexOf(key)] || changed) ? [[key, value]] : [];
  }));
}

export function loadStaff(policy = loadRolePolicy()) {
  const stored = readPreview(staffKey, sample);
  const entries = Array.isArray(stored) ? stored : sample;
  // Existing browser records predate the admin preview account.
  const people = entries.some(person => person.id === 'admin') ? entries : [...entries, sample.find(person => person.id === 'admin')];
  return people.map(({ permissions, ...person }) => {
    const overrides = person.overrides ?? legacyOverrides({ ...person, permissions });
    const effective = { ...roleDefaults(person.role, policy), ...overrides };
    return { ...person, overrides, effective, permissions: legacyRights.map(key => effective[key]) };
  });
}

export function saveStaff(people) {
  return writePreview(staffKey, people.map(({ permissions, effective, ...person }) => person));
}

export function currentStaff(params = new URLSearchParams(location.search), staff = loadStaff()) {
  const preset = params.get('preview');
  const id = params.get('as') ?? (previewAccounts[preset] ?? 'sy');
  return staff.find(person => person.id === id);
}

export function hasStaffRight(person, key) {
  return person?.account === 'active' && person.effective[key] === true;
}

export function withStaffPreview(path) {
  const current = new URLSearchParams(location.search);
  const id = current.get('as') ?? previewAccounts[current.get('preview')];
  if (!id) return path;
  const url = new URL(path, location.href);
  url.searchParams.set('as', id);
  return url.href;
}
