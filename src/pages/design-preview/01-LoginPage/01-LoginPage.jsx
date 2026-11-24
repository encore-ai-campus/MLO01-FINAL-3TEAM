import React from 'react';
import { createRoot } from 'react-dom/client';
import { flushSync } from 'react-dom';

function LoginPage() {
  return (
    <>
  <main id="login-screen" className="screen">
    <section className="login-card" aria-labelledby="login-title">
      <div className="brand"><span className="logo" aria-hidden="true">?</span><span>섭명뭐</span></div>
      <h1 id="login-title">로그인</h1>
      <p className="intro">회사 이메일과 비밀번호를 입력해 주세요.</p>

      <form id="login-form" method="post" noValidate>
        <div className="field">
          <label htmlFor="email">회사 이메일</label>
          <input id="email" name="email" type="email" autoComplete="username"
            placeholder="name@company.com" aria-describedby="email-message auth-message" required />
          <p id="email-message" className="message field-message" aria-live="polite"></p>
        </div>

        <div className="field">
          <label htmlFor="password">비밀번호</label>
          <div className="password">
            <input id="password" name="password" type="password" autoComplete="current-password"
              placeholder="비밀번호를 입력해 주세요" aria-describedby="auth-message" required />
            <button id="visibility" className="text-button visibility" type="button"
              aria-label="비밀번호 표시" aria-controls="password">표시</button>
          </div>
        </div>

        <p id="auth-message" className="message auth-message" aria-live="polite"></p>
        <button id="submit" className="primary-button" type="submit">로그인</button>
      </form>

      <button id="open-help" className="text-button help-button" type="button"
        aria-haspopup="dialog">비밀번호를 잊으셨나요?</button>
    </section>
  </main>

  <a id="desk-demo-entry" className="demo-entry" href="../02-WorkHomePage/02-WorkHomePage.html#02-01">업무 홈 시안 보기(임시)</a>

<dialog id="password-help" className="help-dialog" aria-labelledby="help-title"
    aria-describedby="help-description">
    <h2 id="help-title">비밀번호 분실 안내</h2>
    <div id="help-description">
      <p>사내 관리자에게 비밀번호 초기화를 요청해 주세요. 요청할 때 회사 이메일을 함께 알려주면 관리자가 계정을 확인할 수 있습니다.</p>
      <p>관리자가 비밀번호를 초기화하면 전달받은 비밀번호로 로그인해 주세요.</p>
    </div>
    <form method="dialog" style={{ margin: 0 }}>
      <button className="primary-button" autoFocus>확인</button>
    </form>
  </dialog>
    </>
  );
}

const root = createRoot(document.getElementById('app'));
flushSync(() => root.render(<LoginPage />));

    const loginForm = document.querySelector('#login-form');
    const email = document.querySelector('#email');
    const password = document.querySelector('#password');
    const visibility = document.querySelector('#visibility');
    const emailMessage = document.querySelector('#email-message');
    const authMessage = document.querySelector('#auth-message');
    const submit = document.querySelector('#submit');
    const openHelp = document.querySelector('#open-help');
    const help = document.querySelector('#password-help');

    visibility.addEventListener('click', () => {
      const show = password.type === 'password';
      password.type = show ? 'text' : 'password';
      visibility.textContent = show ? '숨기기' : '표시';
      visibility.setAttribute('aria-label', show ? '비밀번호 숨기기' : '비밀번호 표시');
    });

    openHelp.addEventListener('click', () => help.showModal());

    function clearErrors() {
      email.removeAttribute('aria-invalid');
      password.removeAttribute('aria-invalid');
      emailMessage.textContent = '';
      authMessage.textContent = '';
    }

    loginForm.addEventListener('input', clearErrors);

    function setPending(pending) {
      submit.disabled = pending;
      email.readOnly = pending;
      password.readOnly = pending;
      submit.textContent = pending ? '로그인 중…' : '로그인';
      loginForm.setAttribute('aria-busy', String(pending));
    }

    loginForm.addEventListener('submit', async event => {
      event.preventDefault();
      if (submit.disabled) return;

      clearErrors();
      if (!email.value || email.validity.typeMismatch) {
        email.setAttribute('aria-invalid', 'true');
        emailMessage.textContent = email.value ? '회사 이메일 형식을 확인해 주세요.' : '회사 이메일을 입력해 주세요.';
        email.focus();
        return;
      }
      if (!password.value) {
        password.setAttribute('aria-invalid', 'true');
        authMessage.textContent = '비밀번호를 입력해 주세요.';
        password.focus();
        return;
      }

      // 시안 확인용으로 로그인 요청 중과 인증 실패 상태를 순서대로 표시한다.
      // 로그인 API 계약이 확정되면 이 지연을 실제 요청과 응답 처리로 교체한다.
      setPending(true);
      await new Promise(resolve => setTimeout(resolve, 800));
      setPending(false);
      email.setAttribute('aria-invalid', 'true');
      password.setAttribute('aria-invalid', 'true');
      authMessage.textContent = '이메일 또는 비밀번호를 확인해 주세요.';
    });
