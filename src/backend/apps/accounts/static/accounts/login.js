const password = document.getElementById('password');
const visibility = document.getElementById('visibility');
visibility.addEventListener('click', () => {
  const show = password.type === 'password';
  password.type = show ? 'text' : 'password';
  visibility.textContent = show ? '숨기기' : '표시';
  visibility.setAttribute('aria-pressed', String(show));
  visibility.setAttribute('aria-label', show ? '비밀번호 숨기기' : '비밀번호 표시');
});
document.getElementById('open-help').addEventListener('click', () => {
  document.getElementById('password-help').showModal();
});
