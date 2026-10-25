/* Módulo real, FormData real y DOM/HTTP simulados. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {File} = require('node:buffer');
class Element extends EventTarget {
  constructor() {super(); this.children = []; this.hidden = false; this.disabled = false; this.value = ''; this.classes = new Set(); this.classList = {toggle: (key, value) => value ? this.classes.add(key) : this.classes.delete(key)};}
  append(...items) {this.children.push(...items);}
  replaceChildren(...items) {this.children = items; this.ownText = '';}
  set textContent(value) {this.ownText = String(value); this.children = [];}
  get textContent() {return (this.ownText || '') + this.children.map(item => item.textContent).join('');}
  set innerHTML(value) {throw Error('HTML no confiable');}
  setAttribute(key, value) {this[key] = value;}
  click() {this.dispatchEvent(new Event('click'));}
}
const ids = ['attachmentPanel', 'attachmentInput', 'attachmentFiles', 'attachmentStatus', 'uploadAttachment', 'attachFiles', 'userInput'];
const elements = Object.fromEntries(ids.map(id => [id, new Element()]));
const window = new EventTarget();
const document = {getElementById: id => elements[id], createElement: () => new Element()};
let target = '00000000-0000-4000-8000-000000000001', pending, hold = false;
const calls = [], files = new Map([[target, []]]);
window.agentChat = {active: true, get conversationId() {return target;}, ensureConversation: async () => ({id: target})};
window.projectApi = async (path, options = {}) => {
  const id = path.split('/')[4], method = options.method || 'GET'; calls.push({path, options});
  if (method === 'POST') {
    assert(options.body instanceof FormData);
    assert.equal(options.headers?.['Content-Type'], undefined, 'El navegador debe generar el límite multipart');
    const upload = options.body.get('file');
    if (hold) await new Promise(resolve => {pending = resolve;});
    const item = {id: '00000000-0000-4000-8000-000000000002', name: upload.name, bytes: upload.size, kind: 'image', has_preview: true, metadata: {}};
    files.get(id).push(item); return {ok: true, json: async () => item};
  }
  if (method === 'DELETE') {files.set(id, []); return {ok: true, json: async () => ({status: 'deleted'})};}
  return {ok: true, json: async () => ({files: files.get(id) || []})};
};
const context = vm.createContext({window, document, Event, FormData, AbortController, setTimeout, clearTimeout});
vm.runInContext(fs.readFileSync('static/attachments.js', 'utf8'), context);
async function flush() {for (let i = 0; i < 100; i++) await Promise.resolve();}
async function select(file) {elements.attachmentInput.files = [file]; elements.attachmentInput.dispatchEvent(new Event('change')); await flush();}
(async () => {
  await window.agentAttachments.show(target);
  assert.equal(elements.attachmentPanel.hidden, false);
  await select(new File(['imagen simulada'], '<img onerror=alert(1)>.png', {type: 'image/png'}));
  assert(elements.attachmentFiles.textContent.includes('<img onerror=alert(1)>.png'));
  assert(elements.attachmentStatus.textContent.includes('Archivos listos'));
  assert.equal(window.agentAttachments.busy, false);
  const row = elements.attachmentFiles.children[0];
  assert(row.children[0].src.endsWith('/preview'));
  row.children.at(-1).click(); await flush();
  assert.equal(elements.attachmentFiles.children.length, 0);
  const uploads = () => calls.filter(call => call.options.method === 'POST').length;
  const count = uploads();
  window.agentAttachments.setBusy(true);
  await select(new File(['datos'], 'nota.txt'));
  assert.equal(uploads(), count, 'No debe cambiar entradas de un trabajo activo');
  window.agentAttachments.setBusy(false);
  await select({name: 'enorme.pdf', size: 21 * 1024 * 1024});
  assert.equal(uploads(), count);
  assert(elements.attachmentStatus.textContent.includes('20 MB'));
  hold = true;
  await select(new File(['imagen'], 'PRIVADO_ANTERIOR.png'));
  assert.equal(window.agentAttachments.busy, true);
  window.agentAttachments.clear();
  target = '00000000-0000-4000-8000-000000000003'; files.set(target, []);
  await window.agentAttachments.show(target);
  pending(); await flush(); hold = false;
  assert.equal(elements.attachmentFiles.children.length, 0, 'Una subida atrasada no debe aparecer en otra conversación');
  assert(!elements.attachmentStatus.textContent.includes('PRIVADO_ANTERIOR'));
  await select(new File(['imagen'], 'FOTO_PRIVADA.png')); await flush();
  assert(elements.attachmentFiles.textContent.includes('FOTO_PRIVADA'));
  window.dispatchEvent(new Event('project-session-expired'));
  assert.equal(elements.attachmentPanel.hidden, true);
  assert.equal(elements.attachFiles.hidden, true);
  assert.equal(elements.attachmentFiles.children.length, 0);
  assert.equal(elements.attachmentStatus.textContent, '');
  console.log('OK: FormData, subidas, nombres seguros, vista previa, eliminación, límites, navegación y privacidad.');
})().catch(error => {console.error(error); process.exitCode = 1;});
