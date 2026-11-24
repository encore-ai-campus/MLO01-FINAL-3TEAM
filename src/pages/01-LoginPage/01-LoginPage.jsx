import { useEffect, useRef, useState } from 'react';
import './01-LoginPage.css';

const domainLabel = '[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?';
const companyEmail = new RegExp(`^[A-Za-z0-9]{5,15}@${domainLabel}(?:\\.${domainLabel})+$`);

// The parent can provide onLoginSuccess({ email }) when the work home is ready.
export default function LoginPage({ onLoginSuccess }) {
  const emailInput = useRef(null);
  const passwordInput = useRef(null);
  const helpDialog = useRef(null);
  const [showPassword, setShowPassword] = useState(false);
  const [pending, setPending] = useState(false);
  const [feedback, setFeedback] = useState(null);

  useEffect(() => {
    if (feedback?.field === 'email') emailInput.current.focus();
    if (feedback?.field === 'password') passwordInput.current.focus();
  }, [feedback]);

  async function handleSubmit(event) {
    event.preventDefault();
    if (pending) return;

    setFeedback(null);
    const email = emailInput.current.value;
    const password = passwordInput.current.value;
    if (email.length > 254 || !companyEmail.test(email)) {
      setFeedback({
        field: 'email',
        message: email ? '회사 이메일 형식을 확인해 주세요.' : '회사 이메일을 입력해 주세요.',
      });
      return;
    }
    if (!password) {
      setFeedback({ field: 'password', message: '비밀번호를 입력해 주세요.' });
      return;
    }

    setPending(true);
    let account;
    try {
      const response = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, password }),
      });

      if (response.status === 401 || response.status === 422) {
        const { detail } = await response.json();
        if (typeof detail?.message !== 'string') throw new Error('Invalid login response');
        const field = {
          INVALID_EMAIL: 'email',
          PASSWORD_REQUIRED: 'password',
          INVALID_CREDENTIALS: 'both',
        }[detail.code];
        setFeedback({ field, message: detail.message });
        return;
      }
      if (!response.ok) throw new Error('Login request failed');

      account = await response.json();
      if (typeof account.email !== 'string' || !account.email) {
        throw new Error('Invalid login response');
      }
    } catch {
      setFeedback({ message: '로그인 요청을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요.' });
      return;
    } finally {
      setPending(false);
    }

    passwordInput.current.value = '';
    setShowPassword(false);
    if (onLoginSuccess) {
      onLoginSuccess(account);
    } else {
      setFeedback({ success: true, message: '로그인에 성공했습니다.' });
    }
  }

  const emailInvalid = feedback?.field === 'email' || feedback?.field === 'both';
  const passwordInvalid = feedback?.field === 'password' || feedback?.field === 'both';

  return (
    <div className="login-page">
      <main id="login-screen" className="screen">
        <section className="login-card" aria-labelledby="login-title">
          <div className="brand">
            <span className="logo" aria-hidden="true">?</span><span>섭명뭐</span>
          </div>
          <h1 id="login-title">로그인</h1>
          <p className="intro">회사 이메일과 비밀번호를 입력해 주세요.</p>

          <form id="login-form" method="post" noValidate onSubmit={handleSubmit}
            onInput={() => setFeedback(null)} aria-busy={pending}>
            <div className="field">
              <label htmlFor="email">회사 이메일</label>
              <input ref={emailInput} id="email" name="email" type="email" autoComplete="username"
                placeholder="name@company.com" aria-describedby="email-message auth-message"
                aria-invalid={emailInvalid || undefined} readOnly={pending} required />
              <p id="email-message" className="message field-message" aria-live="polite">
                {feedback?.field === 'email' ? feedback.message : null}
              </p>
            </div>

            <div className="field">
              <label htmlFor="password">비밀번호</label>
              <div className="password">
                <input ref={passwordInput} id="password" name="password"
                  type={showPassword ? 'text' : 'password'} autoComplete="current-password"
                  placeholder="비밀번호를 입력해 주세요" aria-describedby="auth-message"
                  aria-invalid={passwordInvalid || undefined} readOnly={pending} required />
                <button id="visibility" className="text-button visibility" type="button"
                  aria-label={showPassword ? '비밀번호 숨기기' : '비밀번호 표시'}
                  aria-controls="password" onClick={() => setShowPassword(value => !value)}>
                  {showPassword ? '숨기기' : '표시'}
                </button>
              </div>
            </div>

            <p id="auth-message" className={feedback?.success ? 'message auth-message success' : 'message auth-message'}
              aria-live="polite">
              {feedback?.field === 'email' ? null : feedback?.message}
            </p>
            <button id="submit" className="primary-button" type="submit" disabled={pending}>
              {pending ? '로그인 중…' : '로그인'}
            </button>
          </form>

          <button id="open-help" className="text-button help-button" type="button"
            aria-haspopup="dialog" onClick={() => helpDialog.current.showModal()}>
            비밀번호를 잊으셨나요?
          </button>
        </section>
      </main>

      {import.meta.env.DEV && (
        <a id="desk-demo-entry" className="demo-entry"
          href={new URL('../02-WorkHomePage/02-WorkHomePage.html#02-01', import.meta.url).href}>
          업무 홈 시안 보기(임시)
        </a>
      )}

      <dialog ref={helpDialog} id="password-help" className="help-dialog"
        aria-labelledby="help-title" aria-describedby="help-description">
        <h2 id="help-title">비밀번호 분실 안내</h2>
        <div id="help-description">
          <p>사내 관리자에게 비밀번호 초기화를 요청해 주세요. 요청할 때 회사 이메일을 함께 알려주면 관리자가 계정을 확인할 수 있습니다.</p>
          <p>관리자가 비밀번호를 초기화하면 전달받은 비밀번호로 로그인해 주세요.</p>
        </div>
        <form method="dialog">
          <button className="primary-button" autoFocus>확인</button>
        </form>
      </dialog>
    </div>
  );
}
