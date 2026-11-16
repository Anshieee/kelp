/** Dependency-free Kelp pet. Import once, then use <kelp-pet state="idle">. */
const STATES = Object.freeze(['idle', 'working', 'waiting', 'done', 'offline', 'curious', 'happy', 'excited', 'thinking', 'surprised']);
const LABELS = {idle: 'Ready when you are', working: 'Working', waiting: 'Approval needed', done: 'Task completed', offline: 'Disconnected', curious: 'Listening', happy: 'Happy', excited: 'Good news', thinking: 'Thinking', surprised: 'Something new'};
export class KelpPet extends HTMLElement {
  static observedAttributes = ['state', 'paused', 'asset-base', 'label'];
  constructor() {
    super();
    this.attachShadow({mode: 'open'});
    this.shadowRoot.innerHTML = `<style>:host{display:inline-block;width:160px;aspect-ratio:1;contain:layout style}img{display:block;width:100%;height:100%;object-fit:contain;user-select:none;pointer-events:none}</style><img alt="" draggable="false">`;
    this._image = this.shadowRoot.querySelector('img');
    this._motion = matchMedia('(prefers-reduced-motion: reduce)');
    this._update = () => this.render();
  }
  connectedCallback() {
    this._motion.addEventListener('change', this._update);
    document.addEventListener('visibilitychange', this._update);
    // Do not announce a 64-frame loop to screen readers. The host has one label.
    this.setAttribute('role', 'img');
    this.render();
  }
  disconnectedCallback() {
    this._motion.removeEventListener('change', this._update);
    document.removeEventListener('visibilitychange', this._update);
  }
  attributeChangedCallback() { if (this.isConnected) this.render(); }
  get state() { const s = this.getAttribute('state'); return STATES.includes(s) ? s : 'idle'; }
  set state(value) { this.setAttribute('state', value); }
  render() {
    const state = this.state;
    // Relative to this module by default; override for copied/CDN asset folders.
    const base = this.getAttribute('asset-base');
    const root = base ? new URL(base.endsWith('/') ? base : base + '/', document.baseURI) : new URL('./', import.meta.url);
    const still = this.hasAttribute('paused') || this._motion.matches || document.hidden;
    const src = new URL(`${state}.${still ? 'svg' : 'webp'}`, root).href;
    if (this._image.src !== src) this._image.src = src;
    this.setAttribute('aria-label', this.getAttribute('label') || `Kelp: ${LABELS[state]}`);
  }
}
if (!customElements.get('kelp-pet')) customElements.define('kelp-pet', KelpPet);
export {STATES};
