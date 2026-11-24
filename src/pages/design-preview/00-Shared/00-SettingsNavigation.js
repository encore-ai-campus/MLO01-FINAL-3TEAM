export const settingsGroups = [
  { id: 'personal', label: '개인 설정', views: ['replies', 'display'] },
  { id: 'work', label: '업무 관리', views: ['messenger', 'assignment'] },
  { id: 'hr', label: '인사 관리', views: ['staff', 'permissions'] }
];

export function availableSettingsGroups(preview) {
  return settingsGroups.filter(({ id }) => {
    if (['employee', 'team-lead', 'overview-only'].includes(preview)) return id === 'personal';
    if (preview === 'hr' || preview === 'account-admin') return id !== 'work';
    if (preview === 'operations') return id !== 'hr';
    return true;
  });
}
