export async function createAppHarness(options = {}) {
  const harness = installAppDom(options);
  const { __appTest } = await import("../static/app.js");
  return { ...harness, __appTest };
}

export function installAppDom({ canvasContext } = {}) {
  const elements = new Map();
  const streams = [];

  function element(id) {
    if (!elements.has(id)) {
      elements.set(id, createElement(id, element, canvasContext));
    }
    return elements.get(id);
  }

  globalThis.__ASHARE_RADAR_DISABLE_AUTOLOAD__ = true;
  globalThis.window = globalThis;
  globalThis.window.addEventListener = () => {};
  globalThis.setInterval = () => 1;
  globalThis.clearInterval = () => {};
  globalThis.requestAnimationFrame = (callback) => callback();
  globalThis.document = {
    hidden: false,
    body: element("body"),
    getElementById: element,
    querySelector(selector) {
      if (selector === ".workspace-tabs") return element("workspaceTabs");
      if (selector === ".monitor-actions") return element("monitorActions");
      return element(selector);
    },
    querySelectorAll() {
      return [];
    },
    addEventListener() {},
  };
  globalThis.EventSource = class {
    constructor(url) {
      this.url = url;
      this.closed = false;
      this.listeners = {};
      streams.push(this);
    }

    addEventListener(name, handler) {
      this.listeners[name] = handler;
    }

    close() {
      this.closed = true;
    }
  };

  return { elements, element, streams, waitFor, jsonResponse };
}

function createElement(id, element, canvasContext) {
  return {
    id,
    value: "",
    innerHTML: "",
    textContent: "",
    className: "",
    dataset: {},
    disabled: false,
    width: 920,
    height: 300,
    clientWidth: 920,
    clientHeight: 300,
    classList: classList(),
    addEventListener(type, handler) {
      this.listeners = this.listeners || {};
      this.listeners[type] = handler;
    },
    querySelector() {
      return element(`${id}-button`);
    },
    querySelectorAll() {
      return [];
    },
    closest(selector) {
      return selector === ".metric-card" ? { classList: classList() } : null;
    },
    getContext() {
      return canvasContext === null ? null : canvasContext || { clearRect() {} };
    },
  };
}

function classList() {
  const values = new Set();
  return {
    add(value) {
      values.add(value);
    },
    remove(value) {
      values.delete(value);
    },
    toggle(value, active) {
      if (active) values.add(value);
      else values.delete(value);
    },
    contains(value) {
      return values.has(value);
    },
  };
}

export async function waitFor(condition, label) {
  for (let index = 0; index < 20; index += 1) {
    if (condition()) return;
    await Promise.resolve();
  }
  throw new Error(`timed out waiting for ${label}`);
}

export function jsonResponse(payload) {
  return {
    ok: true,
    async json() {
      return payload;
    },
  };
}
