// Run the production controller in a small DOM harness. No network or personal data.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function harness() {
  const elements = new Map(), timers = new Map(), pending = [];
  let timerId = 0;
  class Element {
    constructor() {
      this.value = ''; this.hidden = true; this.attrs = {}; this.handlers = {}; this.children = [];
      this.classList = {add(){}, remove(){}, toggle(){}}; this.dataset = {};
    }
    set innerHTML(value) { this.children = []; }
    setAttribute(k,v) { this.attrs[k] = v; }
    removeAttribute(k) { delete this.attrs[k]; }
    appendChild(el) { this.children.push(el); }
    addEventListener(k, fn) { this.handlers[k] = fn; }
    querySelectorAll() { return this.children; }
    fire(type, extra={}) { return this.handlers[type]?.({preventDefault(){}, ...extra}); }
  }
  const document = {
    getElementById(id) { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); },
    createElement() { return new Element(); }, addEventListener(){},
  };
  const context = vm.createContext({document, localStorage:{getItem(){return null;}}, window:{}, console,
    URLSearchParams, AbortController,
    setTimeout(fn, ms) { const id = ++timerId; timers.set(id, {fn,ms}); return id; },
    clearTimeout(id) { timers.delete(id); },
    fetch(url, options) { return new Promise(resolve => pending.push({url,options,resolve})); },
  });
  vm.runInContext(fs.readFileSync('static/app.js','utf8'), context);
  vm.runInContext('initAddressAutocomplete()', context);
  return {context, pending, elements, get:id=>document.getElementById(id),
    async debounce() { for (const [id,t] of timers) if (t.ms === 140) { timers.delete(id); t.fn(); } await Promise.resolve(); },
    async respond(index, suggestions) {
      pending[index].resolve({ok:true,json:async()=>({suggestions})});
      await new Promise(setImmediate);
    }};
}
const suggestion = {street:'12 Example St',city:'Example City',state:'FL',zip_code:'32000'};

test('selection fills every field and late response cannot reopen dropdown', async () => {
  const h = harness(), street = h.get('streetInput'), list = h.get('addressSuggestionsList');
  street.value = '12 Ex'; street.fire('input'); await h.debounce();
  await h.respond(0,[suggestion]);
  assert.equal(list.hidden,false);
  // Start a second request while first suggestions are still visible.
  street.value = '12 Exam'; street.fire('input'); await h.debounce();
  list.children[0].fire('pointerdown');
  assert.equal(street.value,suggestion.street);
  assert.equal(h.get('cityInput').value,suggestion.city);
  assert.equal(h.get('stateInput').value,'FL');
  assert.equal(h.get('zipInput').value,'32000');
  assert.equal(h.pending[1].options.signal.aborted,true);
  // Simulate a transport that ignores abort: generation guard must still work.
  await h.respond(1,[{...suggestion,street:'Wrong old address'}]);
  assert.equal(list.hidden,true);
  assert.equal(street.value,suggestion.street);
});

test('older response arriving last cannot replace newer suggestions', async () => {
  const h = harness(), street=h.get('streetInput');
  street.value='12 Ex'; street.fire('input'); await h.debounce();
  street.value='12 Exam'; street.fire('input'); await h.debounce();
  await h.respond(1,[suggestion]);
  await h.respond(0,[{...suggestion,state:'NC'}]);
  street.fire('keydown',{key:'ArrowDown'});
  assert.equal(street.attrs['aria-activedescendant'],'address-option-0');
  street.fire('keydown',{key:'Enter'});
  assert.equal(h.get('stateInput').value,'FL');
});

test('Escape invalidates requests even when dropdown has not opened yet', async () => {
  const h=harness(), street=h.get('streetInput');
  street.value='12 Ex'; street.fire('input'); await h.debounce();
  street.fire('keydown',{key:'Escape'});
  await h.respond(0,[suggestion]);
  assert.equal(h.get('addressSuggestionsList').hidden,true);
});

test('batch defaults stay blank even when single address form contains NC', () => {
  const h=harness();
  h.get('cityInput').value='Raleigh'; h.get('stateInput').value='NC'; h.get('zipInput').value='27601';
  const defaults = vm.runInContext('getBatchFallbackDefaults()', h.context);
  assert.equal(defaults.dState,''); assert.equal(defaults.dCity,''); assert.equal(defaults.dZip,'');
});
