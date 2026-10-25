/* La sesión y el CSRF son del servidor. Aquí nunca se guardan claves de modelos. */
(() => {
  'use strict';
  let sessionPromise;
  window.projectSession = function () {
    if (!sessionPromise) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 15000);
      sessionPromise = fetch('/api/session', {credentials: 'same-origin', cache: 'no-store', signal: controller.signal})
        .then(async response => {
          if (!response.ok) throw new Error('Inicia sesión para continuar.');
          return response.json();
        }).catch(error => { sessionPromise = null; throw error; }).finally(() => clearTimeout(timer));
    }
    return sessionPromise;
  };
  window.projectApi = async function (path, options = {}) {
    const session = await window.projectSession();
    const headers = new Headers(options.headers || {});
    headers.set('X-CSRF-Token', session.csrf_token);
    const response = await fetch(path, {...options, headers, credentials: 'same-origin', cache: 'no-store'});
    if (response.status === 401) window.dispatchEvent(new Event('project-session-expired'));
    if (response.status === 403) {
      const error = await response.clone().json().catch(() => ({}));
      if (error.code === 'csrf_required') window.dispatchEvent(new Event('project-session-expired'));
    }
    return response;
  };
})();
