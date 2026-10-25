/* Subidas privadas: FormData, texto seguro y aislamiento al cambiar de vista. */
(() => {
  'use strict';
  const byId = id => document.getElementById(id);
  const panel = byId('attachmentPanel'), input = byId('attachmentInput'), list = byId('attachmentFiles');
  const status = byId('attachmentStatus'), upload = byId('uploadAttachment'), shortcut = byId('attachFiles');
  if (!panel || !input || !list) return;
  let conversation = null, version = 0, visible = false, uploading = false, blocked = false, controller;
  function note(text, error = false) {status.textContent = text; status.classList.toggle('is-error', error);}
  function buttons() {upload.disabled = blocked || uploading; if (shortcut) shortcut.disabled = blocked || uploading;}
  function clear() {
    ++version; controller?.abort(); conversation = null; visible = false;
    panel.hidden = true; if (shortcut) shortcut.hidden = true;
    list.replaceChildren(); input.value = ''; note('');
  }
  async function request(path, options = {}) {
    const response = await window.projectApi(path, options);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'No se pudo completar la operación con el archivo.');
    return data;
  }
  function paint(files, id, ticket) {
    list.replaceChildren();
    for (const file of files) {
      const row = document.createElement('div'); row.className = 'attachment-file';
      if (file.has_preview) {
        const image = document.createElement('img'); image.alt = 'Vista previa de ' + file.name;
        image.src = `/api/agent/conversations/${id}/attachments/${file.id}/preview`; image.loading = 'lazy'; row.append(image);
      }
      const link = document.createElement('a'); link.textContent = file.name;
      link.href = `/api/agent/conversations/${id}/attachments/${file.id}/content`; link.setAttribute('download', file.name);
      const detail = document.createElement('small');
      detail.textContent = `${Math.max(1, Math.round(file.bytes / 1024))} KB · ${file.kind === 'image' ? 'análisis visual al preguntar' : file.characters + ' caracteres extraídos'}`;
      const meta = file.metadata || {};
      if (meta.truncated) detail.textContent += ' · lectura limitada';
      if (meta.scanned_pages?.length) detail.textContent += ' · contiene páginas escaneadas';
      const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'attachment-remove';
      remove.textContent = 'Eliminar'; remove.setAttribute('aria-label', 'Eliminar ' + file.name);
      remove.addEventListener('click', async () => {
        if (blocked || uploading) {note('Espera a que termine el trabajo o cancélalo antes de cambiar archivos.', true); return;}
        remove.disabled = true;
        try {
          await request(`/api/agent/conversations/${id}/attachments/${file.id}`, {method: 'DELETE'});
          if (visible && conversation === id && ticket === version) {await show(id); note('Archivo eliminado de esta conversación.');}
        } catch (error) {if (visible && conversation === id) {note(error.message, true); remove.disabled = false;}}
      });
      row.append(link, detail, remove); list.append(row);
    }
  }
  async function show(id = null) {
    visible = true; panel.hidden = false; if (shortcut) shortcut.hidden = false;
    if (conversation !== id) {list.replaceChildren(); note('');}
    conversation = id; const ticket = ++version;
    controller?.abort(); controller = new AbortController();
    if (!id) {note('Sube archivos y después escribe qué quieres analizar.'); return;}
    try {
      const data = await request(`/api/agent/conversations/${id}/attachments`, {signal: controller.signal});
      if (visible && conversation === id && ticket === version) paint(data.files, id, ticket);
    } catch (error) {if (visible && conversation === id && ticket === version && error.name !== 'AbortError') note(error.message, true);}
  }
  async function add(files) {
    if (!visible || uploading || blocked || !files.length) return;
    const candidates = [...files];
    if (candidates.some(file => file.size > 20 * 1024 * 1024)) {note('Cada archivo debe ocupar como máximo 20 MB.', true); return;}
    uploading = true; buttons(); window.dispatchEvent(new Event('attachment-state'));
    let id;
    try {
      const target = await window.agentChat.ensureConversation(); id = target.id;
      if (!window.agentChat.active || window.agentChat.conversationId !== id) return;
      conversation = id;
      const failures = [];
      for (let index = 0; index < candidates.length; index++) {
        if (!visible || conversation !== id) break;
        note(`Leyendo archivo ${index + 1} de ${candidates.length}: ${candidates[index].name}`);
        const body = new FormData(); body.append('file', candidates[index]);
        const timeout = new AbortController(), timer = setTimeout(() => timeout.abort(), 45000);
        try {await request(`/api/agent/conversations/${id}/attachments`, {method: 'POST', body, signal: timeout.signal});}
        catch (error) {failures.push(candidates[index].name + ': ' + (error.name === 'AbortError' ? 'La subida tardó demasiado. Comprueba la lista y reintenta.' : error.message));}
        finally {clearTimeout(timer);}
      }
      if (visible && conversation === id) {
        await show(id);
        note(failures.length ? failures.join(' ') : 'Archivos listos. Escribe qué necesitas analizar.', Boolean(failures.length));
      }
    } catch (error) {if (visible && (!id || conversation === id)) note(error.message, true);}
    finally {uploading = false; input.value = ''; buttons(); window.dispatchEvent(new Event('attachment-state'));}
  }
  function choose() {if (visible && !blocked && !uploading) input.click();}
  upload.addEventListener('click', choose); shortcut?.addEventListener('click', choose);
  input.addEventListener('change', () => {void add(input.files);});
  panel.addEventListener('dragover', event => {event.preventDefault();});
  panel.addEventListener('drop', event => {event.preventDefault(); void add(event.dataTransfer?.files || []);});
  byId('userInput')?.addEventListener('paste', event => {
    if (!visible || !event.clipboardData?.files?.length) return;
    event.preventDefault(); void add(event.clipboardData.files);
  });
  window.addEventListener('project-session-expired', clear);
  window.agentAttachments = {show, clear, get busy() {return uploading;}, setBusy(value) {blocked = value; buttons();}};
})();
