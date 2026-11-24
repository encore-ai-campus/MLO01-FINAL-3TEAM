import { useEffect, useState } from 'react';

export function useTicketData(url, onExpired) {
  const [attempt, setAttempt] = useState(0);
  const [result, setResult] = useState(null);
  useEffect(() => {
    if (!url) return;
    const controller = new AbortController();
    async function load() {
      try {
        const response = await fetch(url, { signal: controller.signal, cache: 'no-store' });
        if (response.status === 401) {
          if (!controller.signal.aborted) onExpired();
          return;
        }
        if (!response.ok) {
          const body = await response.json().catch(() => ({}));
          const fallback = response.status === 403 ? '티켓 조회 권한이 없습니다.'
            : response.status === 404 ? '티켓을 찾을 수 없습니다.' : '조회하지 못했습니다. 다시 시도해 주세요.';
          throw new Error(body.detail?.message || fallback);
        }
        const data = await response.json();
        if (!controller.signal.aborted) setResult({ url, attempt, data });
      } catch (error) {
        if (!controller.signal.aborted) setResult({ url, attempt, error: error.message === 'Failed to fetch'
          ? '서버에 연결할 수 없습니다.' : error.message });
      }
    }
    load();
    return () => controller.abort();
  }, [url, attempt, onExpired]);
  const current = result?.url === url && result?.attempt === attempt ? result : null;
  return { ...current, loading: Boolean(url && !current), retry: () => setAttempt(value => value + 1) };
}

const kstFormatter = new Intl.DateTimeFormat('sv-SE', {
  timeZone: 'Asia/Seoul', year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
});
export const kstTime = value => kstFormatter.format(new Date(value));
export const kstDay = value => kstTime(value).slice(0, 10);
export const detailTime = value => kstTime(value).replaceAll('-', '/');

export function receivedLabel(value, now) {
  const date = kstTime(value);
  const today = kstDay(now);
  if (date.startsWith(today)) return date.slice(11);
  return date.slice(date.startsWith(today.slice(0, 4)) ? 5 : 0, 10).replaceAll('-', '/');
}

export function updatedLabel(value, now) {
  const minutes = Math.floor((now - Date.parse(value)) / 60_000);
  if (minutes < 1) return '1분 미만';
  if (minutes < 60) return minutes + '분 전';
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return hours + '시간 전';
  const days = Math.floor(hours / 24);
  if (days <= 7) return days + '일 전';
  const date = kstDay(value), today = kstDay(now);
  if (date.slice(0, 4) !== today.slice(0, 4)) return '지난해';
  if (date.slice(0, 7) !== today.slice(0, 7)) return '지난달';
  return '지난주';
}

export function analysisLabel(ticket, field) {
  const state = ticket[field + '_status'];
  if (state === 'succeeded') return ticket[field] || (field === 'category' ? '미분류' : '결과 없음');
  return { pending: '분류 대기', running: '분석 중', failed: '분석 실패' }[state] || '분류 대기';
}
