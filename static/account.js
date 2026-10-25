/* Los enlaces llevan el secreto en el fragmento: no aparece en los logs HTTP. */
(() => {
  'use strict';
  const token = document.querySelector('input[name="token"]');
  if (token) {
    const fragment = new URLSearchParams(window.location.hash.slice(1));
    const received = fragment.get('token');
    if (received && /^[A-Za-z0-9_-]{43}$/.test(received)) token.value = received;
    if (window.location.hash) window.history.replaceState(null, '', window.location.pathname);
    if (!token.value) {
      document.getElementById('tokenHelp').hidden = false;
      token.form.querySelector('button[type="submit"]').disabled = true;
    }
  }
  for (const button of document.querySelectorAll('[data-toggle-password]')) {
    button.hidden = false;
    button.addEventListener('click', () => {
      const input = document.getElementById(button.dataset.togglePassword);
      const visible = input.type === 'password';
      input.type = visible ? 'text' : 'password';
      button.textContent = visible ? 'Ocultar contraseña' : 'Mostrar contraseña';
      button.setAttribute('aria-pressed', String(visible));
    });
  }
})();
