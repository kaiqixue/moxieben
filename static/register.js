const form = document.querySelector('#register-form, #login-form');
const submitButton = form.querySelector('button[type="submit"]');
const submitLabel = submitButton.textContent;

document.querySelectorAll('.password-toggle').forEach((button) => {
  button.addEventListener('click', () => {
    const input = document.getElementById(button.getAttribute('aria-controls'));
    const visible = input.type === 'password';
    input.type = visible ? 'text' : 'password';
    button.textContent = visible ? '隐藏' : '显示';
    button.setAttribute('aria-pressed', String(visible));
    button.setAttribute('aria-label', `${visible ? '隐藏' : '显示'}${input.name === 'confirm_password' ? '确认密码' : '密码'}`);
  });
});

form.addEventListener('submit', () => {
  submitButton.disabled = true;
  submitButton.setAttribute('aria-busy', 'true');
  submitButton.textContent = form.id === 'register-form' ? '正在注册…' : '正在登录…';
});

window.addEventListener('pageshow', () => {
  submitButton.disabled = false;
  submitButton.removeAttribute('aria-busy');
  submitButton.textContent = submitLabel;
});
