import React, { useState } from 'react';
import { editableRoles, loadRolePolicy, loadStaff, rights, roleDefaults, roleRights, saveRolePolicy } from '../00-Shared/00-StaffAccess.js';

const label = value => value ? '허용' : '제한';
const snapshot = policy => Object.fromEntries(editableRoles.map(role => [role, roleDefaults(role, policy)]));
const common = ['티켓 조회·처리·배정·이관', '검토 요청의 문의 본문·첨부파일·이전 대화 열람'];

function RoleTable({ values, onToggle }) {
  return <div className="staff-table-wrap permission-table">
    <table className="staff-table" aria-label={onToggle ? '역할별 추가 권한 편집' : '역할별 권한 비교'}>
      <thead><tr><th scope="col">권한</th>{editableRoles.map(role => <th scope="col" key={role}>{role}</th>)}</tr></thead>
      <tbody>
        {!onToggle && <tr>
          <th scope="row"><span>공통 권한</span><p className="desk-sub">{common.join(' · ')}</p></th>
          {editableRoles.map(role => <td key={role}><span className="staff-badge active">허용</span></td>)}
        </tr>}
        {roleRights.map(key => <tr key={key}>
          <th scope="row">{rights[key]}</th>
          {editableRoles.map(role => <td key={role}>
            {onToggle ? <label className="permission-check">
              <input type="checkbox" aria-label={role + ' ' + rights[key]} checked={values[role][key]} onChange={event => onToggle(role, key, event.target.checked)} />
              {label(values[role][key])}
            </label> : <span className={'staff-badge ' + (values[role][key] ? 'active' : '')}>{label(values[role][key])}</span>}
          </td>)}
        </tr>)}
      </tbody>
    </table>
  </div>;
}

export function RolePermissions({ backHref }) {
  const [base, setBase] = useState(() => snapshot(loadRolePolicy()));
  const [draft, setDraft] = useState(base);
  const [step, setStep] = useState('list');
  const [notice, setNotice] = useState('');
  const people = loadStaff();
  const changes = editableRoles.flatMap(role => roleRights.filter(key => base[role][key] !== draft[role][key])
    .map(key => ({ role, key, before: base[role][key], after: draft[role][key] })));
  const affected = people.filter(({ role, overrides }) => changes.some(change => change.role === role && !Object.hasOwn(overrides, change.key)));
  const titles = { list: '권한 관리', edit: '역할 권한 편집', confirm: '권한 변경 확인' };

  function start() {
    const current = snapshot(loadRolePolicy());
    setBase(current);
    setDraft(current);
    setNotice('');
    setStep('edit');
  }
  function save() {
    const policy = loadRolePolicy();
    if (!changes.length) return;
    if (changes.some(({ role, key, before }) => roleDefaults(role, policy)[key] !== before)) {
      const current = snapshot(policy);
      setBase(current);
      setDraft(current);
      setStep('edit');
      setNotice('다른 화면에서 역할 권한이 바뀌었습니다. 최신 기본값을 확인하고 다시 수정해 주세요.');
      return;
    }
    for (const { role, key, after } of changes) policy[role] = { ...policy[role], [key]: after };
    if (!saveRolePolicy(policy)) {
      setNotice('브라우저에 권한을 저장할 수 없습니다.');
      return;
    }
    setBase(snapshot(policy));
    setNotice('역할 권한을 저장했습니다.');
    setStep('list');
  }

  return <>
    <header className="settings-header">
      <div className="settings-path">{step === 'list' ? <a className="desk-button" href={backHref}>‹ 인사 관리</a> :
        <button className="desk-button" type="button" onClick={() => setStep(step === 'confirm' ? 'edit' : 'list')}>‹ {step === 'confirm' ? '역할 권한 편집' : '권한 관리'}</button>}</div>
      <h1 id="settings-title">{titles[step]}</h1>
      <p className="desk-sub">{step === 'list' ? '상담사와 팀 리더의 공통 권한과 추가 권한을 확인합니다.' :
        step === 'edit' ? '역할 기본값을 변경합니다. 사원별 예외는 사원 상세에서 설정합니다.' :
          '저장하기 전에 권한의 변경 전후와 적용 대상을 확인합니다.'}</p>
    </header>
    <div className="settings-display">
      {notice && <p className="staff-notice" role="status">{notice}</p>}
      {step === 'list' && <>
        <RoleTable values={base} />
        <div className="settings-actions"><button type="button" className="desk-button desk-button-solid" onClick={start}>역할 권한 편집</button></div>
      </>}
      {step === 'edit' && <>
        <section className="staff-card"><h2>상담사·팀 리더 공통</h2>{common.map(item => <p key={item} className="desk-sub">{item}</p>)}</section>
        <RoleTable values={draft} onToggle={(role, key, value) => setDraft(current => ({ ...current, [role]: { ...current[role], [key]: value } }))} />
        <div className="settings-actions">
          <button type="button" className="desk-button" onClick={() => { setNotice(''); setStep('list'); }}>취소</button>
          <button type="button" className="desk-button desk-button-solid" disabled={!changes.length} onClick={() => { setNotice(''); setStep('confirm'); }}>변경 확인</button>
        </div>
      </>}
      {step === 'confirm' && <>
        <div className="staff-table-wrap"><table className="staff-table" aria-label="권한 변경 내용">
          <thead><tr>{['역할', '변경 항목', '변경 전', '변경 후'].map(item => <th scope="col" key={item}>{item}</th>)}</tr></thead>
          <tbody>{changes.map(({ role, key, before, after }) => <tr key={role + key}><td>{role}</td><th scope="row">{rights[key]}</th><td>{label(before)}</td><td>{label(after)}</td></tr>)}</tbody>
        </table></div>
        <p>기본값이 바뀌는 기존 사원 {affected.length}명 · 해당 역할의 신규 사원에도 적용</p>
        <p className="desk-sub">개인 예외는 유지됩니다.</p>
        <div className="settings-actions">
          <button type="button" className="desk-button" onClick={() => { setNotice(''); setStep('edit'); }}>뒤로</button>
          <button type="button" className="desk-button desk-button-solid" onClick={save}>저장</button>
        </div>
      </>}
    </div>
  </>;
}
