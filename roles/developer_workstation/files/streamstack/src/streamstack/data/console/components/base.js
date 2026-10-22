// components/base.js — foundation for every StreamStack custom element.
//
// Conventions (read this before adding a component):
//  * Every element is `ss-<name>`, defined in `components/ss-<name>.js`, imported by `components/index.js`.
//  * Shadow DOM + a constructable stylesheet (`adoptedStyleSheets`) — the CSP forbids <style> and style="".
//    Dynamic values go through CSS custom properties: `el.style.setProperty('--pos', v)` (CSSOM is allowed).
//  * Styles use only design tokens from theme.css (var(--…)), so both themes and 4K scaling just work.
//  * Attributes are the public API (strings/booleans), mirrored by properties where it makes sense.
//    Boolean attributes: present = true. Re-render is cheap and batched in a microtask.
//  * Events bubble and are composed; detail documents the payload. Native `click` is kept for buttons.
//  * Each file starts with a comment block: attributes, properties, events, slots, parts.
//  * Accessibility: role + aria-* on the focusable element, visible focus ring (`:focus-visible`),
//    never colour alone.

const sheets = new Map();

/** `css\`…\`` — tagged template returning text; `sheet(text)` caches the CSSStyleSheet. */
export const css = (strings, ...values) => String.raw({ raw: strings }, ...values);

export function sheet(text) {
  let s = sheets.get(text);
  if (!s) {
    s = new CSSStyleSheet();
    s.replaceSync(text);
    sheets.set(text, s);
  }
  return s;
}

/** Shared by all components: reset, focus ring, visually-hidden helper. */
export const baseStyles = css`
  :host { box-sizing: border-box; }
  *, *::before, *::after { box-sizing: inherit; }
  :host([hidden]) { display: none !important; }
  button { font: inherit; color: inherit; background: none; border: 0; padding: 0; margin: 0; cursor: pointer; -webkit-tap-highlight-color: transparent; }
  button:disabled { cursor: not-allowed; }
  :focus { outline: none; }
  :focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
  .sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; }
`;

export class SSElement extends HTMLElement {
  /** Component CSS text (use the `css` tag). */
  static styles = '';
  /** Attribute names that trigger a re-render. */
  static observedAttributes = [];
  /** Open the shadow root with delegatesFocus (button-like components). */
  static delegatesFocus = false;

  constructor() {
    super();
    const root = this.attachShadow({ mode: 'open', delegatesFocus: this.constructor.delegatesFocus });
    root.adoptedStyleSheets = [sheet(baseStyles), sheet(this.constructor.styles)];
    this._queued = false;
    this._ready = false;
  }

  connectedCallback() {
    if (!this._ready) {
      this._ready = true;
      this.build(this.shadowRoot);
    }
    this.connected?.();
    this.update();
  }

  disconnectedCallback() { this.disconnected?.(); }

  attributeChangedCallback(name, oldV, newV) {
    if (oldV === newV || !this._ready) return;
    this.queueUpdate();
  }

  /** Create the static shadow DOM once. */
  build(root) {}

  /** Apply attributes/properties to the DOM. Must be idempotent. */
  update() {}

  queueUpdate() {
    if (this._queued) return;
    this._queued = true;
    queueMicrotask(() => { this._queued = false; if (this.isConnected) this.update(); });
  }

  bool(name) { return this.hasAttribute(name) && this.getAttribute(name) !== 'false'; }
  num(name, fallback) {
    const v = this.getAttribute(name);
    const n = v === null || v === '' ? NaN : Number(v);
    return Number.isFinite(n) ? n : fallback;
  }

  setBool(name, on) { if (on) this.setAttribute(name, ''); else this.removeAttribute(name); }

  emit(type, detail, init = {}) {
    return this.dispatchEvent(new CustomEvent(type, { detail, bubbles: true, composed: true, ...init }));
  }
}

/** Define once, tolerating double import. */
export function define(name, ctor) {
  if (!customElements.get(name)) customElements.define(name, ctor);
}
