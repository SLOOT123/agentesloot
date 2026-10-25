/* Integra chats privados sin sustituir el canal comunitario de app.js. */
(() => {
  'use strict';
  const byId = id => document.getElementById(id);
  const input = byId('userInput'), box = byId('chatBox'), sendButton = byId('sendBtn');
  const conversationScroll = byId('conversationScroll') || box;
  function scrollToMessages() {
    conversationScroll.scrollTop = Math.max(0, box.scrollHeight - conversationScroll.clientHeight);
  }
  const controls = byId('agentControls'), list = byId('agentConversations');
  const profile = byId('agentProfile'), backend = byId('agentBackend');
  const communityMode = byId('communityMode'), agentMode = byId('agentMode');
  if (!input || !box || !controls || !window.communityChat) return;
  const welcome = box.firstElementChild?.cloneNode(true);
  const state = {active: false, loading: false, epoch: 0, session: 0, conversation: null,
                 config: null, pending: new Map(), submitting: new Set(), jobs: new Map(),
                 drafts: {community: '', agent: ''}, agentDrafts: new Map()};
  let jobTicket = 0, jobTimer = null;
  const working = job => job && ['queued', 'running'].includes(job.status);
  const visible = id => state.active && state.conversation?.id === id;

  function botName() {return state.config?.profiles.find(item => item.id === (state.conversation?.profile || profile.value))?.display_name || 'Mi agente';}
  function paintBotName() {
    if (state.active) document.querySelector('.header-title h1').textContent = botName();
  }

  function stopJobPolling() { jobTicket += 1; clearTimeout(jobTimer); jobTimer = null; }

  function status(element, text, error = false) {
    element.textContent = text;
    element.classList.toggle('is-error', error);
  }

  async function api(path, options = {}) {
    const {timeout: wait = 15000, ...requestOptions} = options;
    const controller = new AbortController();
    // La ejecución continúa en el servidor; cada intercambio HTTP debe ser breve.
    const timer = setTimeout(() => controller.abort(), wait);
    try {
      const response = await window.projectApi(path, {
        ...requestOptions, signal: controller.signal,
        headers: {'Content-Type': 'application/json', ...(options.headers || {})}
      });
      const data = await response.json();
      if (!response.ok) {
        const error = new Error(typeof data.detail === 'string' ? data.detail : 'No se pudo completar la solicitud.');
        error.status = response.status; throw error;
      }
      return data;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('La solicitud tardó demasiado. Tu texto sigue aquí; puedes reintentar.');
      if (error instanceof TypeError) throw new Error('No hay conexión con el servidor. Tu texto sigue aquí.');
      throw error;
    } finally { clearTimeout(timer); }
  }

  function updateButton() {
    if (!state.active) return;
    const busy = state.submitting.has(state.conversation?.id || 'new') || working(state.jobs.get(state.conversation?.id)) || window.agentAttachments?.busy;
    sendButton.disabled = busy || state.loading || !input.value.trim();
    sendButton.setAttribute('aria-busy', String(busy));
    sendButton.setAttribute('aria-label', busy ? 'Trabajo del agente en curso' : 'Enviar al agente');
    sendButton.title = busy ? 'El trabajo continúa en segundo plano. Puedes abrir otra conversación.' : 'Enviar al agente';
    profile.disabled = state.loading;
    backend.disabled = state.loading;
    list.querySelectorAll('button').forEach(button => { button.disabled = state.loading; });
    window.agentAttachments?.setBusy(Boolean(state.loading || state.submitting.has(state.conversation?.id || 'new') || working(state.jobs.get(state.conversation?.id))));
  }

  function paintWelcome() {
    if (!welcome) return box.replaceChildren();
    const content = welcome.cloneNode(true);
    content.querySelector('.eyebrow').textContent = `${botName().toUpperCase()} · CONVERSACIÓN PRIVADA`;
    content.querySelector('.welcome-title').textContent = '¿Qué te gustaría resolver hoy?';
    content.querySelector('.welcome-description').textContent = 'Esta conversación y su contexto son privados. Elige un perfil y configura un modelo en el servidor para comenzar.';
    const suggestions = [
      ['Entender un tema', 'Con ejemplos sencillos', 'Explícame un tema paso a paso con ejemplos sencillos.'],
      ['Revisar mi código', 'Comparte el código en tu mensaje', 'Ayúdame a revisar este código: '],
      ['Consultar mi memoria', 'Usa tus documentos guardados', '¿Qué información tienes en mis documentos sobre '],
      ['Organizar una idea', 'Dar forma al siguiente paso', 'Ayúdame a organizar esta idea: ']
    ];
    content.querySelectorAll('.prompt-card').forEach((button, index) => {
      const suggestion = suggestions[index];
      if (!suggestion) return;
      const number = document.createElement('span');
      number.textContent = String(index + 1).padStart(2, '0');
      number.setAttribute('aria-hidden', 'true');
      const detail = document.createElement('small');
      detail.textContent = suggestion[1];
      button.replaceChildren(number, document.createTextNode(suggestion[0]), detail);
      button.dataset.prompt = suggestion[2];
    });
    box.replaceChildren(content);
  }

  function messageNode(message) {
    const row = document.createElement('div');
    row.className = 'message-row ' + (message.role === 'user' ? 'user' : 'assistant');
    const content = document.createElement('div');
    content.className = 'message-content';
    const avatar = document.createElement('div');
    avatar.className = 'avatar ' + (message.role === 'user' ? 'user-av' : 'gpt');
    avatar.setAttribute('aria-hidden', 'true');
    if (message.role === 'assistant') {
      const image = document.createElement('img');
      image.src = '/static/logo.jpg'; image.alt = ''; image.className = 'brand-image';
      avatar.append(image);
    } else avatar.textContent = 'Tú';
    const body = document.createElement('div');
    body.className = 'message-body';
    const author = document.createElement('p');
    author.className = 'message-author';
    author.textContent = message.role === 'user' ? 'Tú · privado' : message.notification_id ? botName() + ' · aviso de descarga' : botName();
    const text = document.createElement('p');
    text.className = 'message-text';
    text.textContent = message.content;
    body.append(author, text);
    if (message.download?.id) {
      const link = document.createElement('a'); link.className = 'scene-toggle';
      link.href = `/api/agent/conversations/${state.conversation.id}/downloads/${encodeURIComponent(message.download.id)}`;
      link.textContent = 'Descargar ' + message.download.filename; link.setAttribute('download', message.download.filename); body.append(link);
    }
    if (message.sources?.length) {
      const sources = document.createElement('ul');
      sources.className = 'message-sources';
      sources.setAttribute('aria-label', 'Fuentes consultadas');
      for (const source of message.sources) {
        const item = document.createElement('li');
        const label = `[${source.id}] ${source.title}`;
        if (source.kind === 'web' && /^https?:\/\//i.test(source.url)) {
          const link = document.createElement('a');
          link.href = source.url; link.textContent = label; link.target = '_blank'; link.rel = 'noopener noreferrer';
          item.append(link);
        } else if (source.kind === 'attachment' && /^\/api\/agent\/conversations\/[a-f0-9-]+\/attachments\/[a-f0-9-]+\/content$/i.test(source.url)) {
          const link = document.createElement('a'); link.href = source.url; link.textContent = label + ' · archivo adjunto'; link.setAttribute('download', ''); item.append(link);
        } else item.textContent = label + (source.kind === 'profile_reference' ? ' · documento enlazado' : ' · tu memoria');
        sources.append(item);
      }
      body.append(sources);
    }
    content.append(avatar, body); row.append(content);
    return row;
  }

  function jobStatus(job) {
    if (job.status === 'queued') return 'Trabajo en cola. Puedes abrir otra conversación; continuará en segundo plano.';
    if (job.status === 'running') return 'El equipo está trabajando en segundo plano. Puedes abrir otra conversación.';
    if (job.status === 'complete') return job.result?.review === 'unresolved' ? 'Respuesta parcial disponible. Consulta el avance y los pasos pendientes.' : `Respuesta recibida${job.result?.model ? ' · ' + job.result.model : ''}`;
    if (job.status === 'cancelled') return 'Trabajo cancelado.';
    if (window.agentMonitor?.promptRecovered(job.conversation_id || state.conversation?.id)) return 'El archivo de instrucciones ya se puede cargar. Reenvía tu mensaje.';
    return job.error || job.error_message || job.message || 'El agente no pudo completar el trabajo. Puedes reintentar.';
  }

  function showJob(conversation, job) {
    if (!visible(conversation.id)) return;
    status(byId('composerStatus'), jobStatus(job), job.status === 'failed' && !window.agentMonitor?.promptRecovered(conversation.id));
    window.agentActivity?.setJob?.(job, () => cancelJob(conversation, job));
    updateButton();
  }

  async function cancelJob(conversation, job) {
    const session = state.session;
    try {
      const data = await api(`/api/agent/conversations/${conversation.id}/jobs/${encodeURIComponent(job.request_id)}/cancel`, {method: 'POST'});
      if (session !== state.session) return;
      const updated = data.job || data;
      state.jobs.set(conversation.id, {...job, ...updated});
      if (visible(conversation.id)) {
        stopJobPolling();
        await window.agentActivity?.finish(conversation.id, job.request_id);
        showJob(conversation, state.jobs.get(conversation.id));
      }
    } catch (error) { if (visible(conversation.id)) status(byId('composerStatus'), error.message, true); }
  }

  function watchJob(conversation, job) {
    stopJobPolling();
    state.jobs.set(conversation.id, job);
    if (!visible(conversation.id)) return;
    showJob(conversation, job);
    if (!working(job)) return;
    window.agentActivity?.start(conversation.id, job.request_id);
    const version = jobTicket, session = state.session;
    async function pollJob() {
      if (version !== jobTicket || !visible(conversation.id)) return;
      try {
        const data = await api(`/api/agent/conversations/${conversation.id}/jobs/${encodeURIComponent(job.request_id)}`);
        if (session !== state.session || version !== jobTicket || !visible(conversation.id)) return;
        job = {...job, ...(data.job || data)};
        state.jobs.set(conversation.id, job); showJob(conversation, job);
        if (!working(job)) {
          if (job.status === 'complete') state.pending.delete(conversation.id);
          await window.agentActivity?.finish(conversation.id, job.request_id);
          if (version !== jobTicket || !visible(conversation.id)) return;
          await paintHistory(conversation, state.epoch);
          if (version === jobTicket && visible(conversation.id)) showJob(conversation, job);
          if (state.active && version === jobTicket) await refreshConversations();
          return;
        }
      } catch (error) {
        if (session !== state.session || version !== jobTicket || !visible(conversation.id)) return;
        if ([401, 403, 404].includes(error.status)) {
          state.jobs.delete(conversation.id); updateButton();
          status(byId('composerStatus'), error.message, true); return;
        }
        status(byId('composerStatus'), 'No se pudo actualizar el trabajo. Reintentando automáticamente…', true);
      }
      if (version === jobTicket && visible(conversation.id)) jobTimer = setTimeout(() => {void pollJob();}, 1500);
    }
    jobTimer = setTimeout(() => {void pollJob();}, 1500);
  }

  async function recoverJobs(conversation, epoch) {
    const data = await api(`/api/agent/conversations/${conversation.id}/jobs`);
    if (!visible(conversation.id) || epoch !== state.epoch) return;
    const jobs = Array.isArray(data) ? data : data.jobs || [];
    const job = jobs.find(working) || jobs[0];
    if (job) {
      state.jobs.set(conversation.id, job);
      await paintHistory(conversation, epoch);
      if (visible(conversation.id) && epoch === state.epoch) watchJob(conversation, job);
    }
    else { state.jobs.delete(conversation.id); window.agentActivity?.setJob?.(null); updateButton(); }
  }

  async function refreshConversations() {
    const session = state.session;
    const conversations = await api('/api/agent/conversations');
    if (!state.active || session !== state.session) return;
    list.replaceChildren();
    for (const conversation of conversations) {
      const button = document.createElement('button');
      button.type = 'button'; button.className = 'history-item';
      button.textContent = conversation.title;
      button.title = conversation.title;
      if (conversation.id === state.conversation?.id) button.setAttribute('aria-current', 'page');
      button.addEventListener('click', () => { void selectConversation(conversation); });
      list.append(button);
    }
    updateButton();
  }

  async function paintHistory(conversation, epoch) {
    const history = await api(`/api/agent/conversations/${conversation.id}/messages`);
    if (!state.active || epoch !== state.epoch || state.conversation?.id !== conversation.id) return;
    drawHistory(history, conversation);
    await Promise.all([window.agentActivity?.showLatest(conversation.id), window.agentProjects?.show(conversation.id), window.agentAttachments?.show(conversation.id)]);
    scrollToMessages();
  }

  function drawHistory(history, conversation) {
    if (history.length) box.replaceChildren(...history.map(messageNode));
    else paintWelcome();
    const job = state.jobs.get(conversation.id);
    if (job?.prompt && !history.some(item => item.role === 'user' && item.content === job.prompt)) box.append(messageNode({role: 'user', content: job.prompt}));
    scrollToMessages();
  }

  async function refreshDownloadNotices(id) {
    if (!visible(id)) return;
    const epoch = state.epoch, session = state.session, conversation = state.conversation;
    try {
      const history = await api(`/api/agent/conversations/${id}/messages`);
      if (visible(id) && epoch === state.epoch && session === state.session) drawHistory(history, conversation);
    } catch (error) {if (visible(id)) status(byId('composerStatus'), error.message, true);}
  }

  async function selectConversation(conversation) {
    if (!state.active || state.loading) return;
    state.agentDrafts.set(state.conversation?.id || 'new', input.value);
    const epoch = ++state.epoch;
    state.loading = true; state.conversation = conversation;
    stopJobPolling(); window.agentActivity?.clear();
    window.agentProjects?.clear();
    window.agentAttachments?.clear();
    input.value = state.agentDrafts.get(conversation.id) || '';
    state.drafts.agent = input.value;
    input.dispatchEvent(new Event('input', {bubbles: true}));
    profile.value = conversation.profile; backend.value = conversation.backend;
    paintBotName();
    updateButton();
    try {
      await paintHistory(conversation, epoch);
      if (visible(conversation.id) && epoch === state.epoch) await recoverJobs(conversation, epoch);
      if (state.active && epoch === state.epoch) await refreshConversations();
    } catch (error) { if (visible(conversation.id) && epoch === state.epoch) status(byId('composerStatus'), error.message, true); }
    finally { if (epoch === state.epoch) state.loading = false; updateButton(); }
  }

  async function ensureConfig() {
    if (state.config) return;
    const config = await api('/api/agent/config');
    state.config = config;
    for (const item of config.profiles) {
      const option = document.createElement('option');
      option.value = item.id; option.textContent = item.name; profile.append(option);
    }
    for (const item of config.backends) {
      const option = document.createElement('option');
      option.value = item.id; option.textContent = item.label; backend.append(option);
    }
    profile.value = config.profiles.some(item => item.id === config.default_profile) ? config.default_profile :
      config.profiles.some(item => item.id === 'general') ? 'general' : config.profiles[0]?.id;
    backend.value = config.default_backend;
    status(byId('modelStatus'), config.backends.some(item => item.configured)
      ? 'Pulsa Comprobar modelo para verificar sus capacidades.'
      : 'Proveedor pendiente: configura el modelo en config/agent.json.');
  }

  async function switchMode(agent) {
    if (state.active === agent) return;
    state.drafts[state.active ? 'agent' : 'community'] = input.value;
    if (state.active) state.agentDrafts.set(state.conversation?.id || 'new', input.value);
    state.active = agent;
    byId('chatMain').dataset.mode = agent ? 'private' : 'community';
    const epoch = ++state.epoch;
    stopJobPolling();
    window.communityChat.setActive(!agent);
    controls.hidden = !agent; list.hidden = !agent;
    communityMode.classList.toggle('active', !agent); agentMode.classList.toggle('active', agent);
    communityMode.removeAttribute('aria-current'); agentMode.removeAttribute('aria-current');
    (agent ? agentMode : communityMode).setAttribute('aria-current', 'page');
    document.querySelector('.header-title h1').textContent = agent ? 'Mi agente' : 'Comunidad Pro';
    document.querySelector('.header-title p').textContent = agent ? 'Tus conversaciones privadas' : 'Conversa con la comunidad y la IA';
    document.querySelector('.header-channel').textContent = agent ? 'Privado' : 'Canal general · IA';
    byId('composerHelp').textContent = agent ? 'Conversación privada. La IA puede equivocarse; comprueba las fuentes.' : 'Canal compartido: la IA responde automáticamente. Los mensajes y respuestas son visibles para la comunidad.';
    status(byId('composerStatus'), '');
    input.value = state.drafts[agent ? 'agent' : 'community'];
    input.dispatchEvent(new Event('input', {bubbles: true}));
    if (!agent) {state.loading = false; window.agentActivity?.hide(); window.agentProjects?.clear(); window.agentAttachments?.clear(); return;}
    void window.agentAttachments?.show(state.conversation?.id || null);
    paintWelcome();
    status(byId('syncStatus'), 'Privado');
    state.loading = true; updateButton();
    try {
      await ensureConfig();
      if (!state.active || epoch !== state.epoch) return;
      paintBotName();
      if (!state.conversation) paintWelcome();
      await refreshConversations();
      if (state.conversation) {
        await paintHistory(state.conversation, epoch);
        if (state.active && epoch === state.epoch) await recoverJobs(state.conversation, epoch);
      }
    } catch (error) { if (state.active) status(byId('modelStatus'), error.message, true); }
    finally { if (epoch === state.epoch) state.loading = false; updateButton(); }
  }

  async function createConversation(keepDraft = false) {
    const epoch = state.epoch, session = state.session;
    await ensureConfig();
    const previousId = state.conversation?.id || 'new';
    if (state.active) state.agentDrafts.set(previousId, input.value);
    const conversation = await api('/api/agent/conversations', {
      method: 'POST', body: JSON.stringify({profile: profile.value, backend: backend.value})
    });
    if (session !== state.session) throw new Error('Tu sesión ha caducado.');
    if (epoch === state.epoch && state.active) {state.conversation = conversation; paintBotName();}
    if (!keepDraft && epoch === state.epoch && state.active) {
      state.drafts.agent = '';
      if (state.active) { input.value = ''; input.dispatchEvent(new Event('input', {bubbles: true})); }
    }
    return conversation;
  }

  async function newConversation() {
    if (!state.active || state.loading) return;
    state.loading = true; const epoch = ++state.epoch; stopJobPolling(); updateButton();
    try {
      const created = await createConversation();
      if (state.active && epoch === state.epoch) {
        window.agentActivity?.clear();
        window.agentProjects?.clear();
        void window.agentAttachments?.show(state.conversation.id);
        paintWelcome(); status(byId('composerStatus'), 'Nueva conversación privada.');
        await refreshConversations();
      }
      return created;
    } catch (error) { if (state.active && epoch === state.epoch) status(byId('composerStatus'), error.message, true); }
    finally { if (epoch === state.epoch) state.loading = false; updateButton(); }
  }

  function canUseGeneral() {
    return Boolean(state.active && state.config?.profiles.some(item => item.id === 'general')
      && state.conversation?.profile !== 'general' && !working(state.jobs.get(state.conversation?.id)));
  }
  async function useGeneralProfile() {
    if (!canUseGeneral() || state.loading) return;
    const previous = state.conversation, previousProfile = profile.value, draft = input.value;
    profile.value = 'general';
    const created = await newConversation();
    if (!created) {
      if (state.active && state.conversation === previous) {profile.value = previousProfile; paintBotName();}
      return;
    }
    if (visible(created.id) && created.profile === 'general') {
      input.value = draft; state.drafts.agent = draft; state.agentDrafts.set(created.id, draft);
      input.dispatchEvent(new Event('input', {bubbles: true}));
      status(byId('composerStatus'), 'Nuevo chat con Mi agente. Tu texto está listo para enviar; la conversación anterior se conserva.');
    }
  }

  async function sendMessage() {
    const key = state.conversation?.id || 'new';
    if (!state.active || state.submitting.has(key) || working(state.jobs.get(key)) || state.loading || window.agentAttachments?.busy || !input.value.trim()) return;
    if (input.value.trim().length > 8000) {status(byId('composerStatus'), 'El mensaje admite 8000 caracteres. Sube el texto completo como archivo TXT o Markdown y escribe aquí tu pregunta.', true); return;}
    const draft = input.value, message = draft.trim(), session = state.session;
    state.submitting.add(key); updateButton();
    status(byId('composerStatus'), 'Enviando el trabajo al agente…');
    let conversation;
    try {
      conversation = state.conversation || await createConversation(true);
      if (session !== state.session) return;
      let pending = state.pending.get(conversation.id);
      if (!pending || pending.message !== message) {
        pending = {id: crypto.randomUUID(), message}; state.pending.set(conversation.id, pending);
      }
      state.submitting.add(conversation.id);
      if (visible(conversation.id)) {
        window.agentActivity?.start(conversation.id, pending.id);
        void window.agentProjects?.show(conversation.id);
      }
      const data = await api(`/api/agent/conversations/${conversation.id}/messages`, {
        method: 'POST', body: JSON.stringify({message, request_id: pending.id, background: true})
      });
      if (session !== state.session) return;
      const job = {...(data.job || {request_id: pending.id, status: data.status || 'complete', result: data.result || data}),
        prompt: data.job?.prompt || message};
      if (data.result) job.result = data.result;
      state.pending.delete(conversation.id);
      state.jobs.set(conversation.id, job);
      if (state.agentDrafts.get(conversation.id) === draft) state.agentDrafts.delete(conversation.id);
      if (state.drafts.agent === draft) state.drafts.agent = '';
      if (visible(conversation.id)) {
        if (input.value === draft) { input.value = ''; input.dispatchEvent(new Event('input', {bubbles: true})); }
        await paintHistory(conversation, state.epoch);
        if (visible(conversation.id)) watchJob(conversation, job);
      }
      await refreshConversations();
    } catch (error) {
      if (session === state.session && (conversation ? visible(conversation.id) : state.active)) {
        status(byId('composerStatus'), error.message, true);
        if (conversation) await window.agentActivity?.finish(conversation.id, state.pending.get(conversation.id)?.id);
      }
    }
    finally {
      state.submitting.delete(key);
      if (conversation) state.submitting.delete(conversation.id);
      updateButton();
    }
  }

  communityMode.addEventListener('click', event => { event.preventDefault(); void switchMode(false); });
  agentMode.addEventListener('click', () => { void switchMode(true); });
  profile.addEventListener('change', () => { void newConversation(); });
  backend.addEventListener('change', () => { void newConversation(); });
  input.addEventListener('input', updateButton);
  window.addEventListener('attachment-state', updateButton);
  byId('checkModel').addEventListener('click', async event => {
    event.target.disabled = true;
    status(byId('modelStatus'), 'Comprobando el modelo seleccionado…');
    try {
      const result = await api('/api/agent/check', {method: 'POST', timeout: ((state.config?.run_timeout_seconds || 90) + 20) * 1000, body: JSON.stringify({backend: backend.value})});
      status(byId('modelStatus'), `Texto: ${result.chat === 'verified' ? 'verificado' : 'declarado por el proveedor'}. Herramientas: ${result.tools ? 'sí' : 'no'} (${result.tools_evidence}). Contexto: ${result.context_window} (${result.context_evidence}).`);
    } catch (error) { status(byId('modelStatus'), error.message, true); }
    finally { event.target.disabled = false; }
  });

  async function loadMemory() {
    const documents = await api('/api/agent/memory');
    const memoryList = byId('memoryList'); memoryList.replaceChildren();
    for (const item of documents) {
      const row = document.createElement('li');
      const title = document.createElement('span'); title.textContent = `${item.title} · ${item.characters} caracteres`;
      const remove = document.createElement('button');
      remove.type = 'button'; remove.className = 'scene-toggle'; remove.textContent = 'Eliminar';
      remove.addEventListener('click', async () => {
        remove.disabled = true;
        try { await api(`/api/agent/memory/${item.id}`, {method: 'DELETE'}); await loadMemory(); }
        catch (error) { status(byId('memoryStatus'), error.message, true); remove.disabled = false; }
      });
      row.append(title, remove); memoryList.append(row);
    }
  }
  byId('openMemory').addEventListener('click', async () => {
    byId('memoryDialog').showModal();
    status(byId('memoryStatus'), '');
    try { await loadMemory(); } catch (error) { status(byId('memoryStatus'), error.message, true); }
  });
  byId('closeMemory').addEventListener('click', () => byId('memoryDialog').close());
  byId('memoryForm').addEventListener('submit', async event => {
    event.preventDefault();
    const submit = event.target.querySelector('button[type="submit"]'); submit.disabled = true;
    try {
      await api('/api/agent/memory', {method: 'POST', body: JSON.stringify({title: byId('memoryName').value, content: byId('memoryText').value})});
      event.target.reset(); await loadMemory(); status(byId('memoryStatus'), 'Documento guardado en tu memoria privada.');
    } catch (error) { status(byId('memoryStatus'), error.message, true); }
    finally { submit.disabled = false; }
  });
  window.addEventListener('project-session-expired', () => {
    ++state.epoch; ++state.session; stopJobPolling(); state.active = false; window.communityChat.setActive(false);
    state.conversation = null; state.pending.clear(); state.jobs.clear(); state.submitting.clear();
    state.drafts = {community: '', agent: ''}; state.agentDrafts.clear();
    box.replaceChildren(); list.replaceChildren(); byId('memoryList').replaceChildren();
    byId('memoryDialog').close(); input.disabled = true; sendButton.disabled = true;
    const link = document.createElement('a'); link.href = '/login'; link.textContent = 'Vuelve a iniciar sesión.';
    byId('composerStatus').replaceChildren(link);
  });
  window.projectSession().then(session => { byId('sessionUsername').textContent = session.username; })
    .catch(error => status(byId('composerStatus'), error.message, true));
  async function ensureConversation() {
    if (!state.active) throw new Error('Abre Mi agente para subir archivos privados.');
    if (state.conversation) return state.conversation;
    const conversation = await createConversation(true);
    if (!state.active || state.conversation?.id !== conversation.id) throw new Error('La conversación cambió; vuelve a elegir los archivos.');
    await refreshConversations(); return conversation;
  }
  window.agentChat = {get active() {return state.active;}, get conversationId() {return state.conversation?.id;},
    get canUseGeneral() {return canUseGeneral();}, useGeneralProfile, sendMessage, newConversation, ensureConversation, refreshDownloadNotices};
})();
