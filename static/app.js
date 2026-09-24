const state = { view: "archive", selected: null, videos: [], conversation: null, busy: false, lastSync: "", running: false, loaded: false, videoSignature: "" };
const $ = (selector) => document.querySelector(selector);

function element(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.textContent = text;
  return node;
}

function appendInline(parent, text, sources = []) {
  const byNumber = new Map(sources.map((source) => [source.number, source]));
  for (const part of text.split(/(\*\*[^*]+\*\*|`[^`]+`|\[\d+\])/g)) {
    const cite = /^\[(\d+)\]$/.exec(part);
    const source = cite && byNumber.get(Number(cite[1]));
    if (source) {
      const link = element("a", "citation", part);
      link.href = source.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.title = source.title;
      parent.append(link);
    } else if (part.startsWith("**") && part.endsWith("**")) parent.append(element("strong", "", part.slice(2, -2)));
    else if (part.startsWith("`") && part.endsWith("`")) parent.append(element("code", "", part.slice(1, -1)));
    else parent.append(document.createTextNode(part));
  }
}

function formattedText(text, sources = []) {
  const area = element("div", "formatted-text");
  let paragraph = [];
  let list = null;
  function flush() {
    if (paragraph.length) {
      const line = element("p");
      appendInline(line, paragraph.join(" "), sources);
      area.append(line);
      paragraph = [];
    }
    list = null;
  }
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (!line) { flush(); continue; }
    const heading = /^#{1,4}\s+(.+)$/.exec(line);
    const bullet = /^(?:[-*]\s+|\d+[.)]\s+)(.+)$/.exec(line);
    if (heading) {
      flush();
      const node = element("h4");
      appendInline(node, heading[1], sources);
      area.append(node);
    } else if (bullet) {
      if (paragraph.length) flush();
      const kind = /^\d/.test(line) ? "ol" : "ul";
      if (!list || list.tagName.toLowerCase() !== kind) { list = element(kind); area.append(list); }
      const node = element("li");
      appendInline(node, bullet[1], sources);
      list.append(node);
    } else { list = null; paragraph.push(line); }
  }
  flush();
  return area;
}

async function api(url, options = {}) {
  const response = await fetch(url, options);
  let data;
  try { data = await response.json(); } catch { throw new Error("Il server non ha risposto correttamente."); }
  if (!response.ok) throw new Error(data.error || "Qualcosa è andato storto.");
  return data;
}

function showNotice(message) {
  const notice = $("#notice");
  notice.textContent = message || "";
  notice.hidden = !message;
}

function prettyDate(value) {
  if (!value) return "Data non disponibile";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Data non disponibile" : new Intl.DateTimeFormat("it-IT", {day: "numeric", month: "long", year: "numeric"}).format(date);
}

function setView(view) {
  state.view = view;
  document.body.dataset.view = view;
  $("#archive-view").hidden = view !== "archive";
  $("#chat-view").hidden = view !== "chat";
  $("#settings-view").hidden = view !== "settings";
  document.querySelectorAll(".nav-link").forEach((link) => {
    const active = link.dataset.view === view;
    link.classList.toggle("selected", active);
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  if (view === "chat") {
    loadConversations();
    if (!state.conversation) renderWelcome();
  }
  if (view === "settings") { settingsFeedback(""); loadSettings(); }
}

function emptyReader() {
  const reader = $("#reader");
  reader.replaceChildren();
  const area = element("div", "reader-inner empty-state");
  area.append(element("div", "empty-art", "a."));
  area.append(element("h2", "", "C'è spazio per nuove idee."));
  area.append(element("p", "", "Qui troverai il riassunto e la trascrizione dei video. Inizia con «Controlla nuovi video»: la prima volta raccoglieremo gli ultimi dieci."));
  if (!state.hasKey) {
    const help = element("p", "empty-help", "Prima, aggiungi la chiave Gemini nelle Impostazioni.");
    const link = element("a", "settings-shortcut", "Apri Impostazioni ↗");
    link.href = "#settings";
    area.append(help, link);
  }
  reader.append(area);
}

function renderVideos() {
  const list = $("#video-list");
  list.replaceChildren();
  $("#video-count").textContent = String(state.videos.length);
  if (!state.videos.length) {
    state.selected = null;
    list.append(element("p", "index-empty", $("#search-input").value ? "Nessun video trovato. Prova con altre parole." : "Nessun video nell'archivio, per ora."));
    if (!$("#search-input").value) emptyReader();
    else $("#reader").replaceChildren(element("div", "reader-inner empty-state", "Nessun risultato per questa ricerca."));
    return;
  }
  if (!state.videos.some((video) => video.id === state.selected)) state.selected = state.videos[0].id;
  for (const video of state.videos) {
    const row = element("button", "video-row" + (video.id === state.selected ? " active" : ""));
    row.type = "button";
    row.setAttribute("aria-label", `Leggi: ${video.title}`);
    const thumb = element("img", "video-thumb");
    thumb.src = `https://i.ytimg.com/vi/${encodeURIComponent(video.id)}/mqdefault.jpg`;
    thumb.alt = "";
    thumb.loading = "lazy";
    thumb.onerror = () => { thumb.style.visibility = "hidden"; };
    const copy = element("span", "video-row-copy");
    copy.append(element("span", "video-row-title", video.title));
    copy.append(element("span", "video-row-meta", `${prettyDate(video.published)} · ${video.status === "ready" ? "Pronto" : video.status === "error" ? "Da riprovare" : "In lavorazione"}`));
    row.append(thumb, copy);
    row.addEventListener("click", () => selectVideo(video.id));
    list.append(row);
  }
  selectVideo(state.selected);
}

async function loadVideos() {
  const query = $("#search-input").value.trim();
  const videos = await api("/api/videos" + (query ? `?q=${encodeURIComponent(query)}` : ""));
  const signature = JSON.stringify([query, videos]);
  if (signature === state.videoSignature) return;
  state.videoSignature = signature;
  state.videos = videos;
  renderVideos();
}

async function selectVideo(ident) {
  state.selected = ident;
  document.querySelectorAll(".video-row").forEach((row, index) => row.classList.toggle("active", state.videos[index]?.id === ident));
  try {
    const video = await api(`/api/videos/${encodeURIComponent(ident)}`);
    if (state.selected !== ident) return;
    const area = element("div", "reader-inner");
    const line = element("div", "reader-topline");
    line.append(element("span", "reading-tag", "DAL CANALE DI SALVATORE"), element("span", "", prettyDate(video.published)));
    area.append(line, element("h2", "", video.title));
    const actions = element("div", "reader-actions");
    const watch = element("a", "watch-link", "Guarda su YouTube ↗");
    watch.href = video.url;
    watch.target = "_blank";
    watch.rel = "noopener noreferrer";
    const method = video.transcript_method === "video" ? "Trascrizione generata da Gemini sul video (audio YouTube bloccato)"
      : video.transcript_method === "captions" ? "Sottotitoli originali di YouTube (ripiego)" : "Trascrizione audio con Gemini Transcribe";
    actions.append(watch, element("span", "reader-meta", video.transcript ? method : "Stiamo preparando il testo"));
    area.append(actions);
    if (video.summary) {
      const summary = element("section", "summary");
      summary.append(element("h3", "", "In poche parole"), formattedText(video.summary));
      area.append(summary);
    }
    if (video.transcript) {
      const detail = element("details", "transcript");
      const heading = element("summary");
      heading.append(element("h3", "", "La trascrizione"), element("span", "", "+"));
      detail.append(heading, element("div", "prose", video.transcript));
      area.append(detail);
    }
    if (video.status !== "ready") {
      const pending = element("div", "video-pending");
      pending.append(element("strong", "", video.status === "error" ? "Questo video aspetta un altro tentativo." : "Questo video è in lavorazione."));
      pending.append(element("p", "", video.error || "Tieni aperta la pagina oppure torna più tardi: il testo apparirà qui quando sarà pronto."));
      area.append(pending);
    }
    $("#reader").replaceChildren(area);
  } catch (error) { showNotice(error.message); }
}

async function updateStatus() {
  try {
    const info = await api("/api/status");
    const count = info.stats.ready || 0;
    const working = (info.stats.pending || 0) + (info.stats.processing || 0);
    $("#status-text").textContent = info.running
      ? `Sto preparando l'archivio · ${count} pronti${working ? `, ${working} in corso` : ""}`
      : info.error ? `Ultimo controllo non riuscito · ${count} video pronti`
      : count ? `${count} video pronti da leggere${info.stats.error ? ` · ${info.stats.error} da riprovare` : ""}`
      : "L'archivio aspetta il primo video";
    $("#status-indicator").classList.toggle("busy", info.running);
    $("#sync-button").disabled = info.running || !info.has_key;
    $("#sync-button").title = info.has_key ? "" : "Aggiungi la chiave Gemini nelle Impostazioni";
    const keyChanged = state.hasKey !== info.has_key;
    state.hasKey = info.has_key;
    if (keyChanged && !state.videos.length && !$("#search-input").value) emptyReader();
    if (info.error) showNotice(info.error);
    else if (!state.busy) showNotice("");
    if (!state.loaded || state.running !== info.running || state.lastSync !== info.last_sync || info.running) {
      state.loaded = true;
      state.running = info.running;
      state.lastSync = info.last_sync;
      await loadVideos();
    }
  } catch { showNotice("Il server locale non risponde. Riavvia l'app e ricarica la pagina."); }
}

const modelFields = {TRANSCRIBE_MODEL: "#transcribe-model", CHAT_MODEL: "#chat-model", EMBED_MODEL: "#embed-model"};

function settingsFeedback(message, error = false) {
  const target = $("#settings-feedback");
  target.textContent = message;
  target.classList.toggle("error", error);
}

async function loadSettings() {
  try {
    const settings = await api("/api/settings");
    const locked = new Set(settings.locked);
    $("#key-status").textContent = locked.has("GEMINI_API_KEY")
      ? "Chiave configurata nell’ambiente del sistema: gestiscila lì."
      : settings.has_key ? "Chiave pronta · salvata su questo PC" : "Nessuna chiave salvata, per ora.";
    $("#api-key").disabled = locked.has("GEMINI_API_KEY");
    $("#toggle-key").disabled = locked.has("GEMINI_API_KEY");
    $("#api-key").value = "";
    $("#api-key").type = "password";
    $("#toggle-key").textContent = "Mostra";
    $("#toggle-key").setAttribute("aria-label", "Mostra la chiave");
    $("#remove-key").hidden = !settings.has_key || locked.has("GEMINI_API_KEY");
    for (const [name, selector] of Object.entries(modelFields)) {
      $(selector).value = settings.models[name];
      $(selector).disabled = locked.has(name);
      $(selector).title = locked.has(name) ? "Gestito dall’ambiente del sistema" : "";
    }
  } catch (error) { settingsFeedback(error.message, true); }
}

async function saveSettings(event) {
  event.preventDefault();
  const values = {};
  if (!$("#api-key").disabled) values.api_key = $("#api-key").value;
  for (const [name, selector] of Object.entries(modelFields)) {
    if (!$(selector).disabled) values[name] = $(selector).value;
  }
  $("#save-settings").disabled = true;
  settingsFeedback("Salvataggio in corso…");
  try {
    await api("/api/settings", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(values)});
    await loadSettings();
    await updateStatus();
    settingsFeedback("Impostazioni salvate. Sono già attive.");
  } catch (error) { settingsFeedback(error.message, true); }
  finally { $("#save-settings").disabled = false; }
}

async function removeKey() {
  $("#remove-key").disabled = true;
  settingsFeedback("Rimozione in corso…");
  try {
    await api("/api/settings", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({clear_key: true})});
    await loadSettings();
    await updateStatus();
    settingsFeedback("Chiave rimossa da questo PC.");
  } catch (error) { settingsFeedback(error.message, true); }
  finally { $("#remove-key").disabled = false; }
}

async function startSync() {
  try {
    showNotice("");
    $("#sync-button").disabled = true;
    await api("/api/sync", {method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"});
    await updateStatus();
  } catch (error) {
    showNotice(error.message);
    $("#sync-button").disabled = false;
  }
}

function renderWelcome() {
  const messages = $("#messages");
  const area = element("div", "chat-welcome");
  area.append(element("div", "empty-art", "?") , element("span", "eyebrow", "UNA DOMANDA, TANTE IDEE"));
  area.append(element("h2", "", "Chiedilo ai video."));
  area.append(element("p", "", "Cerca tra ciò che Salvatore ha detto nei video già acquisiti. Ogni risposta ti riporta alle sue fonti."));
  const suggestions = element("div", "suggestions");
  for (const question of ["Di cosa parla l'ultimo video?", "Che idee ha discusso sui modelli AI?", "Quali strumenti di programmazione ha citato?"]) {
    const button = element("button", "", question);
    button.type = "button";
    button.addEventListener("click", () => { $("#question").value = question; $("#question").focus(); });
    suggestions.append(button);
  }
  area.append(suggestions);
  messages.replaceChildren(area);
}

function renderMessages(messages) {
  const target = $("#messages");
  if (!messages.length) return renderWelcome();
  target.replaceChildren();
  for (const message of messages) {
    const item = element("div", `chat-message ${message.role}`);
    if (message.role === "user") item.append(element("div", "chat-bubble", message.content));
    else {
      item.append(element("span", "assistant-label", "DALL'ARCHIVIO"));
      const content = element("div", "assistant-body");
      content.append(formattedText(message.content, message.sources || []));
      item.append(content);
      if (message.sources?.length) {
        const sources = element("div", "sources");
        sources.append(element("span", "sources-title", "PASSAGGI CONSULTATI"));
        for (const source of message.sources) {
          const link = element("a", "source-link");
          link.href = source.url;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          link.append(element("span", "", `[${source.number}] `), document.createTextNode(source.title));
          link.title = source.excerpt;
          sources.append(link);
        }
        item.append(sources);
      }
    }
    target.append(item);
  }
  target.scrollTop = target.scrollHeight;
}

async function loadConversations() {
  try {
    const conversations = await api("/api/conversations");
    const list = $("#conversation-list");
    list.replaceChildren();
    for (const convo of conversations) {
      const row = element("button", `conversation-row${state.conversation === convo.id ? " active" : ""}`, convo.title);
      row.type = "button";
      row.title = convo.title;
      row.addEventListener("click", () => selectConversation(convo.id));
      list.append(row);
    }
  } catch (error) { showNotice(error.message); }
}

async function selectConversation(ident) {
  try {
    const convo = await api(`/api/conversations/${ident}`);
    state.conversation = ident;
    renderMessages(convo.messages);
    await loadConversations();
  } catch (error) { showNotice(error.message); }
}

async function ask(event) {
  event.preventDefault();
  if (state.busy) return;
  const question = $("#question").value.trim();
  if (!question) return;
  state.busy = true;
  $("#send-button").disabled = true;
  $("#send-button").textContent = "Ci penso…";
  showNotice("");
  try {
    const result = await api("/api/chat", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({question, conversation_id: state.conversation})});
    $("#question").value = "";
    await selectConversation(result.conversation_id);
  } catch (error) { showNotice(error.message); }
  finally {
    state.busy = false;
    $("#send-button").disabled = false;
    $("#send-button").textContent = "Chiedi ↗";
  }
}

function initTheme() {
  const saved = localStorage.getItem("antirez-theme");
  const dark = saved ? saved === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  const button = $("#theme-toggle");
  button.setAttribute("aria-label", dark ? "Passa al tema chiaro" : "Passa al tema scuro");
  button.addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("antirez-theme", next);
    button.setAttribute("aria-label", next === "dark" ? "Passa al tema chiaro" : "Passa al tema scuro");
  });
}

initTheme();
document.querySelectorAll(".nav-link").forEach((button) => button.addEventListener("click", () => {
  location.hash = button.dataset.view;
  if (state.view !== button.dataset.view) setView(button.dataset.view);
}));
window.addEventListener("hashchange", () => {
  const view = ["#archive", "#chat", "#settings"].includes(location.hash) ? location.hash.slice(1) : "archive";
  if (state.view !== view) setView(view);
});
$("#sync-button").addEventListener("click", startSync);
$("#settings-form").addEventListener("submit", saveSettings);
$("#remove-key").addEventListener("click", removeKey);
$("#toggle-key").addEventListener("click", () => {
  const input = $("#api-key");
  const visible = input.type === "password";
  input.type = visible ? "text" : "password";
  $("#toggle-key").textContent = visible ? "Nascondi" : "Mostra";
  $("#toggle-key").setAttribute("aria-label", visible ? "Nascondi la chiave" : "Mostra la chiave");
});
$("#new-chat").addEventListener("click", () => { state.conversation = null; renderWelcome(); loadConversations(); $("#question").focus(); });
$("#chat-form").addEventListener("submit", ask);
$("#question").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); $("#chat-form").requestSubmit(); }
});
let searchTimer;
$("#search-input").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => loadVideos().catch((error) => showNotice(error.message)), 250);
});
renderWelcome();
updateStatus();
if (location.hash === "#chat" || location.hash === "#settings") setView(location.hash.slice(1));
setInterval(updateStatus, 5000);
