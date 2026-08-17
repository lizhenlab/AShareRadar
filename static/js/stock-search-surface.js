const INITIAL_SEARCH_VIEW = Object.freeze({
  phase: "idle",
  query: "",
  items: [],
  activeIndex: -1,
  message: "",
});

export function createStockSearchSurface(options) {
  const settings = normalizeOptions(options);
  const bindings = [];
  const main = createSearchBinding(settings, {
    inputId: "symbolInput",
    listId: "symbolSuggestions",
    onSelect(symbol, item) {
      clearSymbolError(settings);
      settings.onMainSelect(symbol, item);
    },
  });
  bindings.push(main);
  const history = settings.createSearchHistory({
    root: settings.root,
    onSelect(symbol, item) {
      main.close();
      clearSymbolError(settings);
      settings.onHistorySelect(symbol, item);
    },
  });
  const watch = createSearchBinding(settings, {
    inputId: "watchSymbolInput",
    listId: "watchSymbolSuggestions",
    onSelect: settings.onWatchSelect,
  });
  bindings.push(watch);
  const removeSurfaceListeners = bindSurfaceEvents(settings, main, watch);

  function closeSuggestions() {
    bindings.forEach((binding) => binding.close());
  }

  function close() {
    closeSuggestions();
    history.close();
  }

  function destroy() {
    removeSurfaceListeners();
    bindings.forEach((binding) => binding.destroy());
    history.destroy();
  }

  function handlePageHide(event) {
    if (!event?.persisted) {
      destroy();
      return;
    }
    close();
  }

  function clearError() {
    clearSymbolError(settings);
  }

  return { main, watch, history, close, closeSuggestions, clearError, destroy, handlePageHide };
}

function normalizeOptions(options) {
  if (!options || typeof options !== "object" || Array.isArray(options)) {
    throw new TypeError("Stock search surface options must be an object");
  }
  const requiredFunctions = [
    "createSearchController",
    "createSearchHistory",
    "validateSymbol",
    "escapeHtml",
    "compactErrorMessage",
    "setTimer",
    "clearTimer",
    "onMainSelect",
    "onHistorySelect",
    "onQuickSelect",
  ];
  if (!options.root || typeof options.root.getElementById !== "function") {
    throw new TypeError("Stock search surface root is required");
  }
  for (const name of requiredFunctions) {
    if (typeof options[name] !== "function") throw new TypeError(`${name} must be a function`);
  }
  return {
    ...options,
    onWatchSelect: typeof options.onWatchSelect === "function" ? options.onWatchSelect : () => {},
    onInvalidMain: typeof options.onInvalidMain === "function" ? options.onInvalidMain : () => {},
  };
}

function createSearchBinding(settings, { inputId, listId, onSelect }) {
  const input = settings.root.getElementById(inputId);
  const list = settings.root.getElementById(listId);
  let view = { ...INITIAL_SEARCH_VIEW };
  const controller = settings.createSearchController({
    onState(nextView) {
      view = nextView;
      renderSearchView(settings, input, list, nextView);
    },
    onSelect(symbol, item) {
      if (input) input.value = item.code;
      onSelect(symbol, item);
    },
  });
  const events = bindSearchEvents(settings, input, list, controller, () => view);

  return {
    input(value) {
      events.clearBlurTimer();
      return controller.input(value);
    },
    close() {
      events.clearBlurTimer();
      return controller.close();
    },
    destroy() {
      events.destroy();
      return controller.destroy();
    },
    selectDefault() {
      if (view.phase !== "ready" || !view.items.length) return null;
      return controller.selectIndex(view.activeIndex >= 0 ? view.activeIndex : 0);
    },
    validationMessage(fallback) {
      if (view.phase === "loading") return "正在搜索股票，请稍候。";
      if (view.phase === "empty") return "未找到匹配股票，请检查名称或输入6位代码。";
      if (view.phase === "unavailable") return "股票搜索暂不可用，请输入6位代码。";
      return fallback;
    },
  };
}

function bindSearchEvents(settings, input, list, controller, currentView) {
  let blurTimer = null;
  const clearBlurTimer = () => {
    if (blurTimer !== null) settings.clearTimer(blurTimer);
    blurTimer = null;
  };
  const handleKeydown = (event) => handleSearchKeydown(event, controller, currentView());
  const handleBlur = () => {
    clearBlurTimer();
    blurTimer = settings.setTimer(() => {
      blurTimer = null;
      controller.close();
    }, 120);
  };
  const handleClick = (event) => {
    const option = event.target?.closest?.("button[data-stock-index]");
    if (option) controller.selectIndex(Number(option.dataset.stockIndex));
  };
  const preventPointerFocus = (event) => event.preventDefault();
  input.addEventListener("keydown", handleKeydown);
  input.addEventListener("focus", clearBlurTimer);
  input.addEventListener("blur", handleBlur);
  list.addEventListener("pointerdown", preventPointerFocus);
  list.addEventListener("click", handleClick);
  return {
    clearBlurTimer,
    destroy() {
      clearBlurTimer();
      input.removeEventListener?.("keydown", handleKeydown);
      input.removeEventListener?.("focus", clearBlurTimer);
      input.removeEventListener?.("blur", handleBlur);
      list.removeEventListener?.("pointerdown", preventPointerFocus);
      list.removeEventListener?.("click", handleClick);
    },
  };
}

function handleSearchKeydown(event, controller, view) {
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    controller.move(event.key === "ArrowDown" ? 1 : -1);
  } else if (event.key === "Enter" && view.phase === "ready" && view.items.length) {
    event.preventDefault();
    controller.selectIndex(view.activeIndex >= 0 ? view.activeIndex : 0);
  } else if (event.key === "Escape") {
    event.preventDefault();
    controller.close();
  }
}

function renderSearchView(settings, input, list, view) {
  if (!input || !list) return;
  const open = ["loading", "ready", "empty", "unavailable"].includes(view.phase);
  list.hidden = !open;
  setElementAttribute(input, "aria-expanded", String(open));
  if (!open) {
    list.innerHTML = "";
    setElementAttribute(input, "aria-activedescendant", "");
    return;
  }
  if (view.phase === "ready") {
    list.innerHTML = view.items
      .map((item, index) => suggestionHtml(settings.escapeHtml, item, index, list.id, index === view.activeIndex))
      .join("");
    const activeId = view.activeIndex >= 0 ? `${list.id}-option-${view.activeIndex}` : "";
    setElementAttribute(input, "aria-activedescendant", activeId);
    return;
  }
  setElementAttribute(input, "aria-activedescendant", "");
  const states = {
    loading: ["正在搜索股票...", ""],
    empty: ["未找到匹配股票", ""],
    unavailable: ["股票搜索暂不可用，请输入6位代码。", "is-unavailable"],
  };
  const [message, className] = states[view.phase] || ["", ""];
  list.innerHTML = `<div class="stock-suggestion-state ${className}" role="option" aria-disabled="true">${settings.escapeHtml(message)}</div>`;
}

function suggestionHtml(escapeHtml, item, index, listId, active) {
  const detail = [item.industry, item.source]
    .filter((value) => typeof value === "string" && value.trim())
    .join(" · ");
  return `
    <button type="button" class="stock-suggestion${active ? " is-active" : ""}" id="${escapeHtml(listId)}-option-${index}" role="option" aria-selected="${active ? "true" : "false"}" data-stock-index="${index}">
      <strong>${escapeHtml(item.name)}</strong>
      <span>${escapeHtml(item.code)}.${escapeHtml(item.market)}</span>
      <small>${escapeHtml(detail || "行业信息暂缺")}</small>
    </button>`;
}

function bindSurfaceEvents(settings, main, watch) {
  const removers = [
    bindMainForm(settings, main),
    bindMainInput(settings, main),
    bindWatchInput(settings, watch),
    bindQuickList(settings),
  ];
  return () => removers.forEach((remove) => remove());
}

function bindMainForm(settings, main) {
  const form = settings.root.getElementById("searchForm");
  const input = settings.root.getElementById("symbolInput");
  const handleSubmit = (event) => {
    event.preventDefault();
    let symbol;
    try {
      symbol = settings.validateSymbol(input.value);
    } catch (error) {
      if (main.selectDefault()) {
        clearSymbolError(settings);
        return;
      }
      settings.onInvalidMain();
      showSymbolError(settings, main.validationMessage(error?.message));
      return;
    }
    clearSymbolError(settings);
    settings.onMainSelect(symbol, null);
  };
  form.addEventListener("submit", handleSubmit);
  return () => form.removeEventListener?.("submit", handleSubmit);
}

function bindMainInput(settings, main) {
  const input = settings.root.getElementById("symbolInput");
  const handleInput = (event) => {
    try {
      settings.validateSymbol(event.currentTarget.value);
      main.close();
      clearSymbolError(settings);
    } catch (error) {
      main.input(event.currentTarget.value);
    }
  };
  input.addEventListener("input", handleInput);
  return () => input.removeEventListener?.("input", handleInput);
}

function bindWatchInput(settings, watch) {
  const input = settings.root.getElementById("watchSymbolInput");
  const handleInput = (event) => {
    try {
      settings.validateSymbol(event.currentTarget.value);
      setElementAttribute(event.currentTarget, "aria-invalid", "false");
      watch.close();
    } catch (error) {
      watch.input(event.currentTarget.value);
    }
  };
  input.addEventListener("input", handleInput);
  return () => input.removeEventListener?.("input", handleInput);
}

function bindQuickList(settings) {
  const list = settings.root.getElementById("quickList");
  const handleClick = (event) => {
    const button = event.target.closest("button[data-symbol]");
    if (!button) return;
    clearSymbolError(settings);
    settings.onQuickSelect(button.dataset.symbol);
  };
  list.addEventListener("click", handleClick);
  return () => list.removeEventListener?.("click", handleClick);
}

function clearSymbolError(settings) {
  const input = settings.root.getElementById("symbolInput");
  const error = settings.root.getElementById("symbolError");
  setElementAttribute(input, "aria-invalid", "false");
  if (!error) return;
  error.textContent = "";
  error.hidden = true;
}

function showSymbolError(settings, message) {
  const input = settings.root.getElementById("symbolInput");
  const error = settings.root.getElementById("symbolError");
  setElementAttribute(input, "aria-invalid", "true");
  if (error) {
    error.textContent = settings.compactErrorMessage(message);
    error.hidden = false;
  }
  if (input && typeof input.focus === "function") input.focus({ preventScroll: true });
}

function setElementAttribute(element, name, value) {
  if (!element) return;
  if (typeof element.setAttribute === "function") {
    element.setAttribute(name, value);
    return;
  }
  if (name === "aria-invalid") element.ariaInvalid = value;
  if (name === "aria-expanded") element.ariaExpanded = value;
  if (name === "aria-activedescendant") element.ariaActiveDescendant = value;
}
