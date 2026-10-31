"use strict";
const $ = (id) => document.getElementById(id);
const state = {chats: [], page: "profiles", offset: 0, total: 0, request: 0};
const names = {self_quote: "Своя цитата", self_pattern: "Повторяющаяся реплика", imported_quote: "Цитата из импорта", third_party_joke: "Слова другого участника"};
const dates = value => value ? new Date(value).toLocaleString("ru-RU") : "Пока нет";
function node(tag, text, cls) { const el = document.createElement(tag); if (text !== undefined) el.textContent = text; if (cls) el.className = cls; return el; }
async function api(path, options) {
  const response = await fetch(path, {cache: "no-store", ...options});
  if (response.status === 401) { lock(); throw new Error("Войдите в пульт: сессия завершилась или пароль неверен."); }
  if (!response.ok) throw new Error(response.status === 503 ? "База недоступна или её схема устарела. Проверьте путь и миграции бота." : "Не удалось загрузить данные. Попробуйте обновить страницу.");
  return response.json();
}
function lock() { state.request++; $("dashboard").hidden = true; $("login").hidden = false; $("profiles").replaceChildren(); $("chats").replaceChildren(); $("prompt").textContent = ""; }
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
  $("chat").replaceChildren(...state.chats.map(chat => { const option = node("option", `Чат ${chat.chat_id}${chat.allowed && chat.enabled ? " · сбор включён" : " · сбор выключен"}`); option.value = String(chat.chat_id); return option; }));
  if (state.chats.some(c => String(c.chat_id) === selected)) $("chat").value = selected;
  $("profile-note").textContent = result.profiles_enabled ? "Сбор карточек включён в конфигурации. Видны участники с согласием; цитаты проверяются по сроку хранения и исходным сообщениям." : "Сбор карточек выключен в конфигурации (PROFILE_CARDS_ENABLED=false). Уже собранные актуальные карточки доступны ниже. Для новых включите сбор и перезапустите бот.";
  renderChats(); await page(state.page);
}
function renderChats() {
  $("chats").replaceChildren(...state.chats.map(chat => {
    const row = node("tr"); row.append(node("td", `# ${chat.chat_id}`));
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
    const el = node("article", undefined, "profile"), head = node("div", undefined, "person");
    const name = member.display_name || `Участник ${member.user_id}`;
    const info = node("div"); info.append(node("h3", name), node("div", `${member.current_username ? "@" + member.current_username + " · " : ""}ID ${member.user_id}`, "meta"));
    head.append(node("div", name.slice(0, 2).toUpperCase(), "avatar"), info); el.append(head);
    el.append(node("p", `Чат ${member.chat_id} · ${member.cards.length} карточек · был(а) ${dates(member.last_seen)}`));
    if (!member.cards.length) el.append(node("p", "Портрет ещё не собран или источники уже недоступны. Нужны согласие, сообщения и включённый сбор карточек.", "note"));
    for (const card of member.cards) {
      const item = node("div", undefined, "card");
      item.append(node("span", names[card.kind] || card.kind, "tag"));
      if (card.excluded) item.append(node("span", "Исключено из подбора", "tag off"));
      item.append(node("blockquote", card.summary));
      const details = node("details"); details.append(node("summary", `Источники: ${card.sources.length} · эпизодов: ${card.episodes}`));
      for (const source of card.sources) { details.append(node("p", `${dates(source.occurred_at)} · автор ${source.author_id ?? "неизвестен"} · сообщение ${source.source_message_id}\n${source.text}`)); }
      item.append(details); el.append(item);
    }
    return el;
  }));
  if (!data.profiles.length) $("profiles").append(empty("Портретов не найдено", "Измените поиск или выберите другой чат. Участники без согласия и отказавшиеся от сбора здесь не отображаются."));
  $("previous").disabled = state.offset === 0; $("next").disabled = state.offset + 30 >= data.total;
  $("pagination").textContent = data.total ? `${state.offset + 1}–${Math.min(state.offset + 30, data.total)} из ${data.total}` : "";
}
async function page(name) {
  state.page = name; state.request++; $("error").hidden = true;
  const titles = {profiles: ["ПОРТРЕТЫ", "АРХИВ НАБЛЮДЕНИЙ", "Люди в контексте.", "У каждого чата — свой портрет. У каждой цитаты — свой источник."], chats: ["ЧАТЫ", "ТЕРРИТОРИЯ БОТА", "Места встречи.", "Известные боту группы и настройки сбора данных."], prompt: ["ПРОМПТ", "РЕЖИССУРА ПРОЖАРКИ", "Слова перед словами.", "Инструкция, которая задаёт тон, формат и границы каждой шутки."]};
  ["breadcrumb", "eyebrow", "title", "subtitle"].forEach((id, i) => $(id).textContent = titles[name][i]);
  ["profiles", "chats", "prompt"].forEach(p => $(p + "-page").hidden = p !== name);
  document.querySelectorAll("[data-page]").forEach(button => button.classList.toggle("active", button.dataset.page === name));
  if (name === "profiles") await profiles();
  if (name === "prompt") { const request = state.request; const prompt = await api("/api/prompt"); if (request !== state.request) return; $("prompt").textContent = prompt.system; $("prompt-hash").textContent = prompt.sha256; $("prompt-source").textContent = prompt.source; }
}
$("login-form").onsubmit = async event => { event.preventDefault(); $("login-error").textContent = ""; try { await api("/api/login", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({password: $("password").value})}); $("password").value = ""; await boot(); } catch (error) { $("login-error").textContent = error.message; } };
document.querySelectorAll("[data-page]").forEach(button => button.onclick = () => page(button.dataset.page).catch(failure));
$("chat").onchange = () => { state.offset = 0; profiles().catch(failure); };
let debounce; $("search").oninput = () => { state.request++; clearTimeout(debounce); debounce = setTimeout(() => { state.offset = 0; profiles().catch(failure); }, 250); };
$("previous").onclick = () => { state.offset = Math.max(0, state.offset - 30); profiles().catch(failure); };
$("next").onclick = () => { state.offset += 30; profiles().catch(failure); };
$("refresh").onclick = () => { state.offset = 0; boot().catch(failure); };
$("logout").onclick = async () => { try { await api("/api/logout", {method: "POST"}); lock(); } catch (error) { failure(error); } };
$("copy").onclick = async () => { try { await navigator.clipboard.writeText($("prompt").textContent); $("copy").textContent = "Скопировано ✓"; setTimeout(() => $("copy").textContent = "Копировать", 1800); } catch { failure(new Error("Не удалось скопировать. Выделите текст промпта вручную.")); } };
boot().catch(error => { if (!$("login").hidden) { if (!error.message.startsWith("Войдите")) $("login-error").textContent = error.message; } else failure(error); });
