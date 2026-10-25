/* El módulo real debe trasladar el token al formulario y quitarlo de la URL. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/account.js', 'utf8');

function render(hash, existing = '') {
  const submit = {disabled: false}, help = {hidden: true};
  const token = {value: existing, form: {querySelector: () => submit}};
  const calls = [];
  const context = {URLSearchParams,
    window: {location: {hash, pathname: '/reset-password'}, history: {replaceState: (...args) => calls.push(args)}},
    document: {querySelector: () => token, querySelectorAll: () => [], getElementById: () => help}};
  vm.runInNewContext(source, context);
  return {token, submit, help, calls};
}

const secret = 'a'.repeat(43);
const loaded = render('#token=' + secret);
assert.equal(loaded.token.value, secret);
assert.equal(loaded.submit.disabled, false);
assert.equal(loaded.calls.length, 1);
assert.equal(loaded.calls[0][2], '/reset-password');
assert(!JSON.stringify(loaded.calls).includes(secret));
const missing = render('');
assert.equal(missing.submit.disabled, true);
assert.equal(missing.help.hidden, false);
const malformed = render('#token=invalid');
assert.equal(malformed.submit.disabled, true);
assert.equal(malformed.token.value, '');
const retry = render('', secret);
assert.equal(retry.token.value, secret);
assert.equal(retry.submit.disabled, false);
console.log('OK: token del correo en el formulario, URL limpia y enlaces ausentes o incorrectos bloqueados.');
