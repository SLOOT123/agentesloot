/* Chat comunitario: conserva GET/POST /api/messages del servidor original. */
(() => {
  'use strict';
  const POLL_MS = 3000;
  const TIMEOUT_MS = 15000;
  const state = {
    initialized: false, active: true, sending: false, revision: 0, timer: null, pollEpoch: 0,
    loadPromise: null, loadController: null, latest: null,
    hiddenCounts: new Map(), clearOnNextLoad: false, rendered: [], pending: null
  };
  let input, chatBox, sendButton, syncStatus, composerStatus, welcome;

  function status(element, message, error = false) {
    if (!element) return;
    if (element.textContent !== message) element.textContent = message;
    element.classList.toggle('is-error', error);
  }

  function updateButton() {
    if (!state.active) return;
    if (!sendButton || !input) return;
    sendButton.disabled = state.sending || !input.value.trim();
    sendButton.setAttribute('aria-busy', String(state.sending));
    sendButton.setAttribute('aria-label', state.sending ? 'Enviando mensaje' : 'Enviar mensaje');
    sendButton.title = state.sending ? 'Enviando mensaje…' : 'Enviar mensaje';
  }

  async function request(url, options = {}, controller = new AbortController()) {
    const timeout = window.setTimeout(() => controller.abort(), TIMEOUT_MS);
    try {
      return await window.projectApi(url, {
        credentials: 'same-origin', cache: 'no-store', ...options,
        signal: controller.signal
      });
    } finally {
      window.clearTimeout(timeout);
    }
  }

  function messageKey(message) {
    if (typeof message.id === 'string' || typeof message.id === 'number') {
      return 'id:' + JSON.stringify(message.id);
    }
    // Compatibilidad con la respuesta original sin identificador de mensaje.
    return JSON.stringify([message.username, message.message]);
  }

  function countsFor(messages) {
    const counts = new Map();
    for (const message of messages) {
      const key = messageKey(message);
      counts.set(key, (counts.get(key) || 0) + 1);
    }
    return counts;
  }

  function visibleMessages(messages) {
    const seen = new Map();
    return messages.filter(message => {
      const key = messageKey(message);
      const occurrence = (seen.get(key) || 0) + 1;
      seen.set(key, occurrence);
      return occurrence > (state.hiddenCounts.get(key) || 0);
    });
  }

  function createMessage(message) {
    const isAgent = message.role === 'assistant';
    const isMe = !isAgent && typeof message.current_user === 'string' && message.username === message.current_user;
    const row = document.createElement('div');
    row.className = 'message-row ' + (isMe ? 'user' : 'assistant');
    const content = document.createElement('div');
    content.className = 'message-content';
    const avatar = document.createElement('div');
    avatar.className = 'avatar ' + (isAgent ? 'gpt' : isMe ? 'user-av' : 'peer-av');
    avatar.setAttribute('aria-hidden', 'true');
    if (isAgent) {
      const image = document.createElement('img'); image.src = '/static/logo.jpg'; image.alt = ''; image.className = 'brand-image';
      avatar.append(image);
    } else avatar.textContent = Array.from(message.username.trim()).slice(0, 2).join('').toUpperCase() || '?';
    const body = document.createElement('div');
    body.className = 'message-body';
    const author = document.createElement('p');
    author.className = 'message-author';
    author.textContent = isAgent ? 'Agente' + (message.reply_username ? ' · para ' + message.reply_username : '') : message.username + (isMe ? ' · Tú' : '');
    const text = document.createElement('p');
    text.className = 'message-text';
    // Los mensajes se presentan como texto, nunca como HTML ejecutable.
    text.textContent = message.message;
    body.append(author, text);
    if (isAgent && message.sources?.length) {
      const sources = document.createElement('ul'); sources.className = 'message-sources';
      for (const source of message.sources) {
        if (source.kind !== 'web' || !/^https?:\/\//i.test(source.url)) continue;
        const item = document.createElement('li'), link = document.createElement('a');
        link.href = source.url; link.textContent = `[${source.id}] ${source.title}`;
        link.target = '_blank'; link.rel = 'noopener noreferrer'; item.append(link); sources.append(item);
      }
      body.append(sources);
    }
    if (!isAgent && ['queued', 'running', 'failed'].includes(message.assistant_status)) {
      const note = document.createElement('p'); note.className = 'community-agent-status';
      note.setAttribute('role', 'status');
      note.textContent = message.assistant_status === 'failed' ? 'La IA no pudo responder: ' + (message.assistant_error || 'Inténtalo de nuevo.') :
        message.assistant_status === 'running' ? 'La IA está respondiendo…' : 'Respuesta de la IA en cola…';
      note.classList.toggle('is-error', message.assistant_status === 'failed');
      body.append(note);
    }
    content.append(avatar, body);
    row.append(content);
    return row;
  }

  function renderMessages(messages) {
    const signatures = messages.map(message => JSON.stringify([
      messageKey(message), message.username, message.message, message.current_user, message.role,
      message.reply_username, message.assistant_status, message.assistant_error, message.sources, message.team_status
    ]));
    const unchanged = signatures.length === state.rendered.length &&
      signatures.every((signature, index) => signature === state.rendered[index]);
    if (unchanged) return;

    const nearBottom = chatBox.scrollHeight - chatBox.scrollTop - chatBox.clientHeight < 96;
    const oldTop = chatBox.scrollTop;
    const appendOnly = state.rendered.length > 0 &&
      state.rendered.length <= signatures.length &&
      state.rendered.every((signature, index) => signature === signatures[index]);
    const fragment = document.createDocumentFragment();
    const start = appendOnly ? state.rendered.length : 0;
    for (const message of messages.slice(start)) fragment.append(createMessage(message));
    if (appendOnly) chatBox.append(fragment);
    else if (messages.length) chatBox.replaceChildren(fragment);
    else if (welcome) chatBox.replaceChildren(welcome.cloneNode(true));
    else chatBox.replaceChildren();
    state.rendered = signatures;
    // No interrumpe a quien está leyendo mensajes anteriores.
    chatBox.scrollTop = nearBottom ? chatBox.scrollHeight : oldTop;
  }

  function loadMessages() {
    if (!state.initialized || !state.active || state.sending) return Promise.resolve();
    if (state.loadPromise) return state.loadPromise;
    const revision = state.revision;
    const controller = new AbortController();
    state.loadController = controller;
    state.loadPromise = (async () => {
      try {
        const response = await request('/api/messages', { headers: { Accept: 'application/json' } }, controller);
        if (revision !== state.revision || !state.active) return;
        if (!response.ok) {
          throw new Error(response.status === 401 ? 'Tu sesión ha caducado. Vuelve a iniciar sesión.' : 'No se pudieron actualizar los mensajes. Reintentando…');
        }
        const messages = await response.json();
        if (revision !== state.revision || !state.active) return;
        if (!Array.isArray(messages) || !messages.every(message => message &&
          typeof message.username === 'string' && typeof message.message === 'string')) {
          throw new Error('El servidor devolvió un formato de mensajes inesperado.');
        }
        state.latest = messages;
        if (state.clearOnNextLoad) {
          state.hiddenCounts = countsFor(messages);
          state.clearOnNextLoad = false;
        }
        renderMessages(visibleMessages(messages));
        status(syncStatus, 'Actualizado · cada 3 s');
      } catch (error) {
        if (revision !== state.revision || !state.active) return;
        const text = error.name === 'AbortError' ? 'La conexión tardó demasiado. Reintentando…' :
          error instanceof TypeError ? 'Sin conexión con el servidor. Reintentando…' : error.message;
        status(syncStatus, text, true);
      } finally {
        if (state.loadController === controller) state.loadController = null;
      }
    })().finally(() => { state.loadPromise = null; });
    return state.loadPromise;
  }

  async function sendMessage() {
    if (!state.initialized || !state.active || state.sending) return;
    const originalDraft = input.value;
    const message = originalDraft.trim();
    if (!message) return;
    state.sending = true;
    updateButton();
    status(composerStatus, 'Enviando mensaje…');
    let sent = false;
    try {
      if (!state.pending || state.pending.message !== message) state.pending = {id: crypto.randomUUID(), message};
      const headers = { 'Content-Type': 'application/json', Accept: 'application/json' };
      // Opcional: tu servidor puede renderizar este token en una etiqueta meta.
      const csrf = document.querySelector('meta[name="csrf-token"]')?.content;
      if (csrf) headers['X-CSRFToken'] = csrf;
      const response = await request('/api/messages', {
        method: 'POST', headers, body: JSON.stringify({message, request_id: state.pending.id})
      });
      if (!response.ok) {
        if (response.status === 401) throw new Error('Tu sesión ha caducado. Vuelve a iniciar sesión; tu texto sigue aquí.');
        if (response.status === 429) throw new Error('Hay demasiados envíos. Espera un momento antes de intentarlo otra vez.');
        throw new Error('El servidor no confirmó el envío. Tu texto se ha conservado.');
      }
      sent = true;
      state.pending = null;
      state.revision += 1;
      // Descarta una lectura anterior al envío para no pintar datos atrasados.
      if (state.loadController) state.loadController.abort();
      if (state.loadPromise) await state.loadPromise;
      if (state.active && input.value === originalDraft) {
        input.value = '';
        input.dispatchEvent(new Event('input', { bubbles: true }));
      }
      if (state.active) status(composerStatus, 'Mensaje enviado. La IA responderá aquí.');
      if (state.active && document.activeElement === sendButton) input.focus();
    } catch (error) {
      const uncertain = error.name === 'AbortError' || error instanceof TypeError;
      if (state.active) status(composerStatus, uncertain ?
        'No pudimos confirmar el envío. Revisa el canal antes de reintentarlo; tu texto sigue aquí.' : error.message, true);
    } finally {
      state.sending = false;
      updateButton();
    }
    if (sent) await loadMessages();
  }

  function clearScreen() {
    if (!state.initialized || !state.active) return;
    state.hiddenCounts = countsFor(state.latest || []);
    state.clearOnNextLoad = state.latest === null;
    state.rendered = [];
    if (welcome) chatBox.replaceChildren(welcome.cloneNode(true));
    else chatBox.replaceChildren();
    status(composerStatus, 'Vista limpia. Los mensajes siguen en el servidor; aquí verás los nuevos.');
    input.focus();
  }

  async function poll() {
    const epoch = ++state.pollEpoch;
    window.clearTimeout(state.timer);
    if (document.hidden || !state.active) return;
    await loadMessages();
    if (!document.hidden && state.active && epoch === state.pollEpoch) state.timer = window.setTimeout(poll, POLL_MS);
  }

  function initialize() {
    if (state.initialized) return;
    input = document.getElementById('userInput');
    chatBox = document.getElementById('chatBox');
    sendButton = document.getElementById('sendBtn');
    syncStatus = document.getElementById('syncStatus');
    composerStatus = document.getElementById('composerStatus');
    if (!input || !chatBox || !sendButton) return;
    state.initialized = true;
    welcome = chatBox.firstElementChild?.cloneNode(true);
    input.addEventListener('input', updateButton);
    input.addEventListener('keydown', event => {
      if (event.key !== 'Enter' || event.shiftKey || event.isComposing || event.keyCode === 229) return;
      event.preventDefault();
      if (!event.repeat) void window.sendMessage();
    });
    document.addEventListener('visibilitychange', () => {
      state.pollEpoch += 1;
      window.clearTimeout(state.timer);
      if (!document.hidden) void poll();
    });
    window.addEventListener('pagehide', () => {
      window.clearTimeout(state.timer);
      state.revision += 1;
      state.pollEpoch += 1;
      if (state.loadController) state.loadController.abort();
    });
    window.addEventListener('pageshow', event => { if (event.persisted) void poll(); });
    updateButton();
    void poll();
  }

  // Conserva las funciones utilizadas por los botones del HTML.
  window.communityChat = {
    setActive(active) {
      state.active = active;
      state.revision += 1;
      state.pollEpoch += 1;
      window.clearTimeout(state.timer);
      state.loadController?.abort();
      if (active) {
        state.rendered = [];
        if (welcome) chatBox.replaceChildren(welcome.cloneNode(true));
        updateButton();
        // Una lectura cancelada puede seguir resolviéndose; espera antes de reiniciar.
        Promise.resolve(state.loadPromise).finally(() => { if (state.active) void poll(); });
      }
    }
  };
  window.sendMessage = () => window.agentChat?.active ? window.agentChat.sendMessage() : sendMessage();
  window.clearScreen = () => window.agentChat?.active ? window.agentChat.newConversation() : clearScreen();
  window.loadMessages = loadMessages;
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initialize, { once: true });
  else initialize();
})();
