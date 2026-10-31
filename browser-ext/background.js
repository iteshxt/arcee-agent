// Arcee browser bridge (spec section 9, level 1, in the user's own Chrome).
//
// Chrome starts the native host `org.arcee.browser` for this extension; the host
// forwards requests from Arcee. Each request is {id, method, params}; each reply is
// {id, ok, result} or {id, ok: false, error}. Page work runs as small functions
// injected into the tab, so nothing stays resident in pages between requests.

const HOST = "org.arcee.browser";
const NAV_TIMEOUT_MS = 15000;
let port = null;

function connect() {
  port = chrome.runtime.connectNative(HOST);
  port.onMessage.addListener(async (message) => {
    const { id, method, params } = message;
    try {
      const result = await handle(method, params || {});
      port.postMessage({ id, ok: true, result });
    } catch (error) {
      port.postMessage({ id, ok: false, error: String(error && error.message ? error.message : error) });
    }
  });
  port.onDisconnect.addListener(() => {
    port = null;
    setTimeout(connect, 5000); // the host restarts with Arcee; keep trying quietly
  });
}

async function activeTab(tabId) {
  if (tabId) return chrome.tabs.get(tabId);
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (tab) return tab;
  // Chrome keeps running with every window closed; open one to work in.
  const created = await chrome.windows.create({ focused: true });
  return created.tabs[0];
}

// A new tab, in a new window when Chrome has none open.
async function newTab(url) {
  const windows = await chrome.windows.getAll({ windowTypes: ["normal"] });
  if (windows.length === 0) {
    const created = await chrome.windows.create({ url: url || "about:blank", focused: true });
    return created.tabs[0];
  }
  return chrome.tabs.create({ url: url || "about:blank" });
}

async function inPage(tabId, func, args = []) {
  const [frame] = await chrome.scripting.executeScript({ target: { tabId }, func, args });
  return frame ? frame.result : null;
}

function waitForLoad(tabId) {
  return new Promise((resolve) => {
    const timer = setTimeout(done, NAV_TIMEOUT_MS);
    function done() {
      clearTimeout(timer);
      chrome.tabs.onUpdated.removeListener(listener);
      resolve();
    }
    function listener(id, info) {
      if (id === tabId && info.status === "complete") done();
    }
    chrome.tabs.onUpdated.addListener(listener);
  });
}

async function handle(method, params) {
  switch (method) {
    case "ping":
      return { version: chrome.runtime.getManifest().version };
    case "tabs": {
      const tabs = await chrome.tabs.query({});
      return tabs.map((t) => ({ id: t.id, title: t.title, url: t.url, active: t.active }));
    }
    case "new_tab": {
      const tab = await newTab(params.url);
      if (params.url) await waitForLoad(tab.id);
      return { id: tab.id };
    }
    case "switch_tab": {
      const tab = await chrome.tabs.update(params.tab, { active: true });
      await chrome.windows.update(tab.windowId, { focused: true });
      return { id: tab.id, title: tab.title };
    }
    case "close_tab":
      await chrome.tabs.remove(params.tab);
      return {};
    case "navigate": {
      const tab = await activeTab(params.tab);
      if (tab.url !== params.url) {
        const loaded = waitForLoad(tab.id);
        await chrome.tabs.update(tab.id, { url: params.url });
        // A change of only the #fragment never fires a load event: do not wait for one.
        const sameDocument = tab.url && tab.url.split("#")[0] === params.url.split("#")[0];
        if (!sameDocument) await loaded;
      }
      const after = await chrome.tabs.get(tab.id);
      return { id: after.id, url: after.url, title: after.title };
    }
    case "back": {
      const tab = await activeTab(params.tab);
      await chrome.tabs.goBack(tab.id);
      await waitForLoad(tab.id);
      return {};
    }
    case "elements": {
      const tab = await activeTab(params.tab);
      return inPage(tab.id, snapshot, [params.limit || 150, params.text_chars || 1500]);
    }
    case "click": {
      const tab = await activeTab(params.tab);
      return inPage(tab.id, clickElement, [params.target]);
    }
    case "fill": {
      const tab = await activeTab(params.tab);
      return inPage(tab.id, fillElement, [params.target, params.text, !!params.submit]);
    }
    case "read_text": {
      const tab = await activeTab(params.tab);
      return inPage(tab.id, readText, [params.selector || "", params.max_chars || 4000]);
    }
    case "scroll": {
      const tab = await activeTab(params.tab);
      return inPage(tab.id, scrollPage, [params.direction || "down", params.amount || 0.8]);
    }
    default:
      throw new Error(`unknown method ${method}`);
  }
}

// --- Functions injected into pages: self-contained, no closures -----------------

function snapshot(limit, textChars) {
  const SELECTOR = [
    "a[href]", "button", "input:not([type=hidden])", "select", "textarea", "summary",
    "[role=button]", "[role=link]", "[role=checkbox]", "[role=menuitem]", "[role=tab]",
    "[role=option]", "[role=row]", "[role=textbox]", "[role=combobox]", "[role=switch]",
    "[contenteditable=''], [contenteditable=true]", "[onclick]",
  ].join(",");
  document.querySelectorAll("[data-arcee-id]").forEach((el) => el.removeAttribute("data-arcee-id"));
  const label = (el) => {
    const by = el.getAttribute("aria-labelledby");
    const text =
      el.getAttribute("aria-label") ||
      (by && by.split(" ").map((id) => document.getElementById(id)?.innerText || "").join(" ")) ||
      el.innerText || el.getAttribute("placeholder") || el.getAttribute("title") ||
      el.getAttribute("alt") || el.value || "";
    const max = el.getAttribute("role") === "row" || el.tagName === "TR" ? 240 : 160;
    return text.replace(/\s+/g, " ").trim().slice(0, max);
  };
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const s = getComputedStyle(el);
    return s.visibility !== "hidden" && s.display !== "none" && s.opacity !== "0";
  };
  const inView = (el) => {
    const r = el.getBoundingClientRect();
    return r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth;
  };
  // Main content first (an inbox's rows, an article's links), then the rest of what is
  // on screen, then what is off screen: a short limit should still show what matters.
  const main = (el) => !!el.closest("main, [role=main]");
  const rank = (el) => (inView(el) ? 0 : 2) + (main(el) ? 0 : 1);
  // A row (an email in a list, a table line) is one element: its text already says
  // what it is, and the checkboxes and buttons inside it only crowd the list. Fields
  // inside rows stay, since typing needs them.
  const insideRow = (el) => {
    const row = el.parentElement && el.parentElement.closest("[role=row], tr");
    return !!row && row !== el && !/^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName);
  };
  const all = [...document.querySelectorAll(SELECTOR)].filter((el) => visible(el) && !insideRow(el));
  all.sort((a, b) => rank(a) - rank(b));
  const elements = [];
  for (const el of all.slice(0, limit)) {
    const id = `el_${elements.length + 1}`;
    el.setAttribute("data-arcee-id", id);
    const item = { id, role: el.getAttribute("role") || el.tagName.toLowerCase(), name: label(el) };
    if (el.type && el.tagName === "INPUT") item.type = el.type;
    if ("value" in el && el.value && el.type !== "password") item.value = String(el.value).slice(0, 100);
    if (el.isContentEditable) item.editable = true;
    if (el.getAttribute("autocomplete")) item.autocomplete = el.getAttribute("autocomplete");
    if (el.checked || el.getAttribute("aria-checked") === "true") item.checked = true;
    if (el.disabled || el.getAttribute("aria-disabled") === "true") item.disabled = true;
    if (!inView(el)) item.offscreen = true;
    elements.push(item);
  }
  const body = document.querySelector("main, [role=main], article") || document.body;
  const text = (body.innerText || "").replace(/\n{3,}/g, "\n\n").trim();
  return {
    url: location.href,
    title: document.title,
    count: all.length,
    elements,
    text_excerpt: text.slice(0, textChars),
  };
}

function clickElement(target) {
  const el = document.querySelector(`[data-arcee-id="${target}"]`);
  if (!el) throw new Error(`${target} is gone; get the elements again`);
  el.scrollIntoView({ block: "center" });
  el.click();
  return { clicked: target, url: location.href, title: document.title };
}

function fillElement(target, text, submit) {
  const el = document.querySelector(`[data-arcee-id="${target}"]`);
  if (!el) throw new Error(`${target} is gone; get the elements again`);
  el.scrollIntoView({ block: "center" });
  el.focus();
  if (el.isContentEditable) {
    document.execCommand("selectAll", false);
    document.execCommand("insertText", false, text);
  } else {
    // The native setter keeps frameworks (React, Angular) in step with the value.
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value").set.call(el, text);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }
  if (submit) {
    if (el.form) el.form.requestSubmit();
    else el.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", bubbles: true }));
  }
  return { filled: target, submitted: submit };
}

function readText(selector, maxChars) {
  const root = selector ? document.querySelector(selector) : document.querySelector("main, [role=main], article") || document.body;
  if (!root) throw new Error(`nothing matches ${selector}`);
  const text = (root.innerText || "").replace(/\n{3,}/g, "\n\n").trim();
  return { url: location.href, title: document.title, text: text.slice(0, maxChars), length: text.length };
}

function scrollPage(direction, amount) {
  const dy = (direction === "up" ? -1 : 1) * innerHeight * amount;
  scrollBy({ top: dy, behavior: "instant" });
  return { scroll_y: Math.round(scrollY), page_height: document.documentElement.scrollHeight };
}

connect();
