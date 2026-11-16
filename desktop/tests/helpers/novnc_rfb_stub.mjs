// Test-only stand-in for `@novnc/novnc`. The loader maps the bare specifier
// here, so `useViewer`'s dynamic import resolves without a real VNC server or
// a browser WebSocket. Instances record their constructor args and expose a
// `fire(type, detail)` helper to drive the listener paths the hook wires up.
export const instances = [];

export function resetInstances() {
  instances.length = 0;
}

export default class RFB {
  constructor(target, url, options) {
    this.target = target;
    this.url = url;
    this.options = options;
    this.viewOnly = false;
    this.scaleViewport = false;
    this.resizeSession = false;
    this.listeners = {};
    this.disconnected = false;
    this.sentCredentials = null;
    instances.push(this);
  }

  addEventListener(type, listener) {
    (this.listeners[type] ??= []).push(listener);
  }

  removeEventListener() {}

  disconnect() {
    this.disconnected = true;
  }

  sendCredentials(credentials) {
    this.sentCredentials = credentials;
  }

  fire(type, detail) {
    for (const listener of this.listeners[type] ?? []) listener({ detail });
  }
}
