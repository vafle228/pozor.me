"use strict";
const $ = (id) => document.getElementById(id);
const state = {chats: [], page: "profiles", offset: 0, total: 0, request: 0};
const names = {self_quote: "Своя цитата", self_pattern: "Повторяющаяся реплика", imported_quote: "Цитата из импорта", third_party_joke: "Слова другого участника"};
const dates = value => value ? new Date(value).toLocaleString("ru-RU") : "Пока нет";
function node(tag, text, cls) { const el = document.createElement(tag); if (text !== undefined) el.textContent = text; if (cls) el.className = cls; return el; }
async function api(path, options) {
  const response = await fetch(path, {cache: "no-store", ...options});
  if (response.status === 401) { lock(); throw new Error("Войдите в пульт: сессия завершилась или пароль неверен."); }
  if (!response.ok) { const detail = await response.json().catch(() => ({})); throw new Error(detail.error && !["database_unavailable", "not_found", "invalid_query"].includes(detail.error) ? detail.error : response.status === 503 ? "База недоступна или её схема устарела. Проверьте путь и миграции бота." : "Не удалось загрузить данные. Попробуйте обновить страницу."); }
  return response.json();
}
function lock() { state.request++; if (typeof resetTelegram === "function") resetTelegram(); $("dashboard").hidden = true; $("login").hidden = false; $("profiles").replaceChildren(); $("chats").replaceChildren(); $("prompt").textContent = ""; $("members").replaceChildren(); $("members-status").textContent = ""; }
function failure(error) { $("error").textContent = error.message; $("error").hidden = false; }
function empty(title, description) { const el = node("div", undefined, "empty"); el.append(node("h2", title), node("div", description)); return el; }
async function boot() {
  const result = await api("/api/chats"); state.chats = result.chats;
  $("dashboard").hidden = false; $("login").hidden = true; $("error").hidden = true;
  $("stats").replaceChildren(...[
    [state.chats.length, "чатов в реестре"],
    [state.chats.filter(c => c.allowed && c.enabled).length, "чатов со сбором данных"],
    [state.chats.reduce((s, c) => s + c.members, 0), "участников · с учётом чатов"]
  ].map(([value, label]) => { const el = node("div", undefined, "stat"); el.append(node("strong", value), node("span", label)); return el; }));
  const selected = $("chat").value;
  $("chat").replaceChildren(...state.chats.map(chat => { const option = node("option", `${chat.title || "Чат"} · ${chat.chat_id}${chat.allowed && chat.enabled ? " · сбор включён" : " · сбор выключен"}`); option.value = String(chat.chat_id); return option; }));
  if (state.chats.some(c => String(c.chat_id) === selected)) $("chat").value = selected;
  $("profile-note").textContent = result.semantic_profiles_enabled !== false ? "Портреты составляются автоматически от 10 собственных сообщений. Обновление — после 20 новых сообщений или через 10 минут при наличии новых данных. Анализ до 300 последних сообщений; прожарки имеют приоритет. Нажмите «Обновить», чтобы увидеть прогресс." : "Автоматическое составление портретов выключено. Ручной анализ доступен в разделе «История Telegram».";
  const memberChat = $("members-chat").value;
  $("members-chat").replaceChildren(...state.chats.map(chat => { const option = node("option", `${chat.title || "Чат"} · ${chat.chat_id}`); option.value = String(chat.chat_id); return option; }));
  if (state.chats.some(c => String(c.chat_id) === memberChat)) $("members-chat").value = memberChat;
  renderChats(); await page(state.page);
}
function renderChats() {
  $("chats").replaceChildren(...state.chats.map(chat => {
    const row = node("tr"); row.append(node("td", `${chat.title || "Чат"} · ${chat.chat_id}`));
    const status = node("td"); status.append(node("span", !chat.allowed ? "Не разрешён" : chat.enabled ? "Включён" : "Выключен", `tag${chat.allowed && chat.enabled ? "" : " off"}`)); row.append(status);
    row.append(node("td", chat.members), node("td", `${chat.retention_days} дн.`), node("td", dates(chat.last_message)));
    const action = node("td"), button = node("button", "Портреты ↗", "ghost"); button.onclick = () => { $("chat").value = String(chat.chat_id); state.offset = 0; page("profiles").catch(failure); }; action.append(button); row.append(action); return row;
  }));
  if (!state.chats.length) { const row = node("tr"), cell = node("td", "Чаты появятся после того, как бот зарегистрирует их в этой базе."); cell.colSpan = 6; row.append(cell); $("chats").append(row); }
}
async function profiles() {
  const request = ++state.request;
  if (!$("chat").value) { $("profiles").replaceChildren(empty("Здесь пока тихо", "В базе ещё нет чатов. Запустите бот и добавьте его в группу.")); $("profile-count").textContent = "0 участников"; $("previous").disabled = $("next").disabled = true; $("pagination").textContent = ""; return; }
  $("profiles").replaceChildren(node("p", "Загружаем портреты…"));
  const query = new URLSearchParams({chat: $("chat").value, q: $("search").value, offset: state.offset});
  const data = await api(`/api/profiles?${query}`); if (request !== state.request) return;
  state.total = data.total;
  $("profile-count").textContent = `${data.total} участников с согласием`;
  $("profiles").replaceChildren(...data.profiles.map(member => {
    const el = node("details", undefined, "profile"), heading = node("summary"), head = node("div", undefined, "person");
    const name = member.display_name || `Участник ${member.user_id}`;
    const info = node("div"); info.append(node("h3", name), node("div", `${member.current_username ? "@" + member.current_username + " · " : ""}ID ${member.user_id}`, "meta"));
    head.append(node("div", name.slice(0, 2).toUpperCase(), "avatar"), info);
    heading.append(head, node("p", `${member.cards.length} наблюдений · Открыть профиль и цитаты ▾`, "meta")); el.append(heading);
    el.append(node("p", `Чат ${member.chat_id} · был(а) ${dates(member.last_seen)}`));
    const quotes = node("div"); let loaded = false;
    el.ontoggle = async () => {
      if (!el.open || loaded) return;
      loaded = true; quotes.replaceChildren(node("p", "Загружаем цитаты…"));
      try {
        const evidence = await api(`/api/evidence?${new URLSearchParams({chat: member.chat_id, target: member.user_id})}`);
        if (request !== state.request || !el.isConnected) return;
        quotes.replaceChildren(node("h3", "Сообщения участника"), node("p", "До 100 последних сообщений, написанных самим участником, включая импортированные.", "note"));
        if (!evidence.quotes.length) quotes.append(node("p", "Собственных сообщений пока нет. Ответы и упоминания других людей не считаются словами участника.", "note"));
        for (const quote of evidence.quotes) {
          const item = node("div", undefined, "card"); item.append(node("p", `${dates(quote.occurred_at)} · автор ${quote.author_id} · ${quote.origin} · #${quote.source_message_id}`, "meta"), node("blockquote", quote.text)); quotes.append(item);
        }
        if (evidence.related?.length) {
          const context = node("details");
          context.append(node("summary", `Контекст: чужие ответы и упоминания · ${evidence.related.length}`), node("p", "Эти сообщения написали другие люди. Они доступны прожарке как контекст с указанием авторства и не используются для автоматического портрета участника.", "note"));
          for (const quote of evidence.related) {
            const item = node("div", undefined, "card");
            item.append(node("span", quote.relation === "reply" ? "Ответ участнику · чужие слова" : "Упоминание участника · чужие слова", "tag"), node("p", `${dates(quote.occurred_at)} · автор ${quote.author_id} · #${quote.source_message_id}`, "meta"), node("blockquote", quote.text));
            context.append(item);
          }
          quotes.append(context);
        }
      } catch (error) { loaded = false; quotes.replaceChildren(); failure(error); }
    };
    const automatic = member.automatic;
    const portraitStates = {pending: "Портрет ожидает свободную модель.", running: `Составляем портрет: ${automatic?.analyzed || 0}/${automatic?.sampled || 0} сообщений.`, insufficient: `Для первого портрета нужно 10 собственных сообщений; доступно ${automatic?.sampled || 0}.`, error: "Анализ не завершился. Бот автоматически повторит попытку.", empty: "Сообщения проанализированы, но обоснованных наблюдений пока нет.", ready: `Портрет обновлён ${dates(automatic?.updated_at ? automatic.updated_at * 1000 : null)}.`};
    el.append(node("p", automatic ? portraitStates[automatic.state] || "Ожидаем анализ." : member.cards.some(card => card.profile_version === 3) ? "Автоматический портрет составлен." : "Автоматический анализ ещё не запускался. Для него нужны включённый сбор в чате и 10 собственных сообщений.", "note"));
    for (const card of [...member.cards].sort((a, b) => Number(b.profile_version === 4) - Number(a.profile_version === 4))) {
      const item = node("div", undefined, "card");
      item.append(node("span", card.profile_version === 4 ? "Досье · связный пересказ с источниками" : card.profile_version === 3 ? "Наблюдение · составлено автоматически" : card.profile_version === 2 ? "Наблюдение · проверено оператором" : names[card.kind] || card.kind, "tag"));
      if (card.excluded) item.append(node("span", "Исключено из подбора", "tag off"));
      item.append(node("blockquote", card.summary));
      const details = node("details"); details.append(node("summary", `Источники: ${card.sources.length} · эпизодов: ${card.episodes}`));
      for (const source of card.sources) { details.append(node("p", `${dates(source.occurred_at)} · автор ${source.author_id ?? "неизвестен"} · сообщение ${source.source_message_id}\n${source.text}`)); }
      item.append(details); el.append(item);
    }
    el.append(quotes);
    return el;
  }));
  if (!data.profiles.length) $("profiles").append(empty("Портретов не найдено", "Измените поиск или выберите другой чат. Участники без согласия и отказавшиеся от сбора здесь не отображаются."));
  $("previous").disabled = state.offset === 0; $("next").disabled = state.offset + 30 >= data.total;
  $("pagination").textContent = data.total ? `${state.offset + 1}–${Math.min(state.offset + 30, data.total)} из ${data.total}` : "";
}
async function members(clearStatus = true) {
  const request = ++state.request, chatId = $("members-chat").value;
  if (clearStatus) $("members-status").textContent = "";
  $("members").replaceChildren();
  if (!chatId) { $("members-status").textContent = "Добавьте чат, чтобы управлять участниками."; return; }
  const data = await api(`/api/members?${new URLSearchParams({chat: chatId})}`);
  if (request !== state.request) return;
  const chat = state.chats.find(c => String(c.chat_id) === chatId);
  const search = $("members-search").value.trim().toLocaleLowerCase();
  for (const member of data.members) {
    if (!`${member.display_name || ""} ${member.current_username || ""} ${member.user_id}`.toLocaleLowerCase().includes(search)) continue;
    const enabled = !!(member.consent && !member.opt_out && !member.forgotten);
    const row = node("tr"), action = node("td");
    row.append(node("td", `${member.display_name || "Участник"}${member.current_username ? " · @" + member.current_username : ""} · ${member.user_id}`), node("td", enabled ? "Включён" : "Выключен"));
    const button = node("button", enabled ? "Выключить roast" : "Включить roast", "ghost");
    button.disabled = !!member.forgotten || !chat?.allowed || !chat?.enabled;
    button.setAttribute("aria-label", `${button.textContent}: ${member.display_name || member.user_id}`);
    button.onclick = async () => {
      button.disabled = true;
      try {
        await api("/api/members/roast", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({chat: Number(chatId), user: member.user_id, enabled: !enabled})});
        if (request !== state.request) return;
        $("members-status").textContent = enabled ? "Roast выключен; активные прожарки участника завершены." : "Roast включён для участника в этом чате.";
        await members(false);
      } catch (error) { if (request === state.request) { button.disabled = false; failure(error); } }
    };
    action.append(button);
    if (member.forgotten) action.append(node("p", "Нужна команда /roast_on от участника", "note"));
    else if (!chat?.allowed || !chat?.enabled) action.append(node("p", "Сначала включите сбор в чате", "note"));
    row.append(action); $("members").append(row);
  }
  if (!$("members").children.length) { const row = node("tr"), cell = node("td", "Участники не найдены. Здесь появятся пользователи, которых бот увидел в чате."); cell.colSpan = 3; row.append(cell); $("members").append(row); }
}
async function page(name) {
  if (typeof stopTelegram === "function") stopTelegram();
  state.page = name; state.request++; $("error").hidden = true;
  const titles = {profiles: ["ПОРТРЕТЫ", "АРХИВ НАБЛЮДЕНИЙ", "Люди в контексте.", "У каждого чата — свой портрет. У каждой цитаты — свой источник."], chats: ["ЧАТЫ", "ТЕРРИТОРИЯ БОТА", "Места встречи.", "Известные боту группы и настройки сбора данных."], prompt: ["ПРОМПТ", "РЕЖИССУРА ПРОЖАРКИ", "Слова перед словами.", "Инструкция, которая задаёт тон, формат и границы каждой шутки."]};
  titles.members = ["УЧАСТНИКИ", "УПРАВЛЕНИЕ ROAST", "Кто участвует.", "Включайте и выключайте roast для участников выбранного чата."];
  titles.history = ["ИСТОРИЯ", "ПОДКЛЮЧЕНИЕ TELEGRAM", "Прошлое в контексте.", "Выберите группу и участников — историю загрузим сами."];
  ["breadcrumb", "eyebrow", "title", "subtitle"].forEach((id, i) => $(id).textContent = titles[name][i]);
  ["profiles", "chats", "prompt", "history", "members"].forEach(p => $(p + "-page").hidden = p !== name);
  document.querySelectorAll("[data-page]").forEach(button => button.classList.toggle("active", button.dataset.page === name));
  if (name === "profiles") await profiles();
  if (name === "members") await members();
  if (name === "history") await loadTelegram();
  if (name === "prompt") { const request = state.request; const prompt = await api("/api/prompt"); if (request !== state.request) return; $("prompt").textContent = prompt.system; $("prompt-hash").textContent = prompt.sha256; $("prompt-source").textContent = prompt.source; }
}
$("login-form").onsubmit = async event => { event.preventDefault(); $("login-error").textContent = ""; try { await api("/api/login", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({password: $("password").value})}); $("password").value = ""; await boot(); } catch (error) { $("login-error").textContent = error.message; } };
document.querySelectorAll("[data-page]").forEach(button => button.onclick = () => page(button.dataset.page).catch(failure));
$("members-chat").onchange = () => members().catch(failure);
$("members-search").oninput = () => members().catch(failure);
$("chat").onchange = () => { state.offset = 0; profiles().catch(failure); };
let debounce; $("search").oninput = () => { state.request++; clearTimeout(debounce); debounce = setTimeout(() => { state.offset = 0; profiles().catch(failure); }, 250); };
$("previous").onclick = () => { state.offset = Math.max(0, state.offset - 30); profiles().catch(failure); };
$("next").onclick = () => { state.offset += 30; profiles().catch(failure); };
$("refresh").onclick = () => { state.offset = 0; boot().catch(failure); };
$("logout").onclick = async () => { try { await api("/api/logout", {method: "POST"}); lock(); } catch (error) { failure(error); } };
$("copy").onclick = async () => { try { await navigator.clipboard.writeText($("prompt").textContent); $("copy").textContent = "Скопировано ✓"; setTimeout(() => $("copy").textContent = "Копировать", 1800); } catch { failure(new Error("Не удалось скопировать. Выделите текст промпта вручную.")); } };
boot().catch(error => { if (!$("login").hidden) { if (!error.message.startsWith("Войдите")) $("login-error").textContent = error.message; } else failure(error); });
