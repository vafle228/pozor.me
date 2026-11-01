"use strict";
let telegramTimer, telegramGeneration = 0, telegramConnected = false, renderedDrafts = "";
let historyRunning = false, historyMembersLoading = false;
function updateHistoryStart() {
  const count = $("history-members").querySelectorAll("input:checked").length;
  $("history-start").disabled = historyRunning || historyMembersLoading || count < 1 || count > 10;
}
const tgPost = (action, body = {}) => api(`/api/telegram/${action}`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
function stopTelegram() { clearTimeout(telegramTimer); telegramGeneration++; }
function resetTelegram() {
  stopTelegram(); telegramConnected = false; renderedDrafts = "";
  ["history-drafts", "history-members", "history-group"].forEach(id => $(id).replaceChildren());
  $("available-group").replaceChildren(); $("add-chat-form").hidden = true; $("add-chat-note").textContent = "";
  ["tg-api-id", "tg-api-hash", "tg-password"].forEach(id => $(id).value = "");
  $("tg-qr").removeAttribute("src"); $("tg-qr").hidden = true;
  $("tg-state").textContent = ""; $("history-progress").textContent = "Задач пока нет.";
}
async function historyMembers() {
  const chat = $("history-group").value, generation = telegramGeneration;
  const selected = new Set($("history-members").dataset.chat === chat
    ? [...$("history-members").querySelectorAll("input:checked")].map(input => String(input.value)) : []);
  $("history-members").dataset.chat = chat;
  historyMembersLoading = true;
  updateHistoryStart();
  $("history-members").replaceChildren();
  const enabled = $("history-group").selectedOptions[0]?.dataset.enabled === "true";
  $("history-enable").hidden = !chat || enabled;
  $("history-group-note").textContent = !chat ? "Добавьте группу в разделе «Чаты»." : enabled ? "Сбор включён. Участники разрешают своё участие через /roast_on." : "Бот видит эту группу, но сбор выключен. Нажмите «Включить выбранный чат».";
  if (!chat || !enabled) { historyMembersLoading = false; updateHistoryStart(); return; }
  const data = await api(`/api/telegram/members?chat=${encodeURIComponent(chat)}`);
  if (generation !== telegramGeneration || $("history-group").value !== chat) return;
  for (const member of data.members) {
    const label = node("label"), input = node("input"); input.type = "checkbox"; input.value = member.user_id;
    input.checked = selected.has(String(member.user_id));
    label.append(input, node("span", member.display_name || member.current_username || `Участник ${member.user_id}`)); $("history-members").append(label);
  }
  if (!data.members.length) $("history-members").append(node("p", "Пока нет участников с согласием. Участник должен выполнить /roast_on в этой группе.", "note"));
  historyMembersLoading = false; updateHistoryStart();
}
async function historyGroups() {
  const selected = $("history-group").value;
  const generation = telegramGeneration, data = await api("/api/telegram/groups");
  if (generation !== telegramGeneration) return;
  // A running admin process may still serve the older API until it is restarted.
  const groups = data.groups.map(group => ({...group, enabled: group.enabled !== false}));
  $("history-group").replaceChildren(...groups.map(group => {
    const option = node("option", `${group.title}${group.enabled ? "" : " · сбор выключен"}`);
    option.value = group.id; option.dataset.enabled = String(group.enabled); return option;
  }));
  $("history-group").value = String(groups.find(group => String(group.id) === selected)?.id || groups.find(group => group.enabled)?.id || groups[0]?.id || "");
  await historyMembers();
}
function showAccount(account) {
  const labels = {disconnected: "Аккаунт не подключён", connected: "Telegram подключён", qr: "Ожидаем сканирования", password: "Нужен облачный пароль", expired: "Код истёк. Получите новый."};
  $("tg-state").textContent = labels[account.state] || "Нет соединения";
  $("tg-credentials").hidden = account.configured; $("tg-credentials").open = !account.configured;
  $("tg-connect").hidden = account.state === "connected" || account.state === "password";
  $("tg-connect").textContent = account.state === "qr" ? "Получить новый QR-код" : "Подключить по QR-коду";
  $("tg-disconnect").hidden = !account.configured;
  $("tg-password-form").hidden = account.state !== "password";
  $("tg-qr").hidden = $("tg-qr-note").hidden = !account.qr;
  if (account.qr) $("tg-qr").src = account.qr; else $("tg-qr").removeAttribute("src");
  $("history-form").hidden = account.state !== "connected";
}
function showDrafts(drafts) {
  const key = drafts.map(d => d.id).join(","); if (key === renderedDrafts) return;
  renderedDrafts = key; $("history-drafts").replaceChildren();
  const kinds = {interest: "Интерес", story: "История", phrase: "Характерная фраза", habit: "Повторяющаяся привычка"};
  for (const draft of drafts) {
    const form = node("form", undefined, "review-draft");
    form.append(node("h3", draft.name || `Участник ${draft.target}`), node("p", `Проанализировано сообщений: ${draft.sampled}. Черновик действует 30 минут. Сохранение заменит предыдущие проверенные наблюдения этого участника.`, "note"));
    const controls = [];
    draft.observations.forEach((observation, index) => {
      const item = node("div", undefined, "card"), label = node("label"), selected = node("input"); selected.type = "checkbox"; selected.checked = true;
      label.append(selected, node("span", `${kinds[observation.kind]} · ${observation.topic}`));
      const text = node("textarea"); text.value = observation.summary; text.maxLength = 240; text.rows = 3; text.setAttribute("aria-label", `Наблюдение: ${observation.topic}`);
      const sources = node("details"); sources.append(node("summary", `Проверить цитаты (${observation.sources.length})`));
      observation.sources.forEach(source => sources.append(node("p", `${dates(source.occurred_at)} · сообщение ${source.source_message_id}\n${source.text}`)));
      item.append(label, text, sources); form.append(item); controls.push({index, selected, text});
    });
    if (!controls.length) form.append(node("p", "Недостаточно сообщений для обоснованных наблюдений.", "note"));
    else {
      const button = node("button", "Сохранить выбранное"), message = node("p", "", "note"); form.append(button, message);
      form.onsubmit = async event => {
        event.preventDefault(); button.disabled = true;
        try {
          const result = await tgPost("save", {draft: draft.id, selections: controls.filter(c => c.selected.checked).map(c => ({index: c.index, summary: c.text.value}))});
          if (state.page !== "history" || $("dashboard").hidden) return;
          message.textContent = `Сохранено наблюдений: ${result.saved}. Они доступны в разделе «Портреты».`;
          form.querySelectorAll("input,textarea").forEach(input => input.disabled = true); button.remove();
          // Keep the confirmation until the next explicit page refresh.
        } catch (error) { button.disabled = false; failure(error); }
      };
    }
    $("history-drafts").append(form);
  }
}
async function pollTelegram(generation) {
  const account = await api("/api/telegram/status");
  if (generation !== telegramGeneration) return;
  showAccount(account);
  if (account.state === "connected" && !telegramConnected) { telegramConnected = true; await historyGroups(); }
  if (account.state !== "connected") telegramConnected = false;
  const job = await api("/api/telegram/job");
  if (generation !== telegramGeneration) return;
  const stages = {history: "Загружаем историю", import: "Сохраняем цитаты", analysis: "Составляем профиль"};
  $("history-progress").textContent = job.state === "running" ? `${stages[job.stage]}… просмотрено ${job.scanned || 0}, импортировано ${job.imported || 0}${job.stage === "analysis" ? `, проанализировано ${job.analyzed || 0}/${job.sampled || 0}` : ""}` : job.message || ({idle: "Задач пока нет.", review: "Черновики готовы. Проверьте цитаты, отредактируйте текст и сохраните выбранное."}[job.state] || "");
  $("history-cancel").hidden = job.state !== "running" && !job.drafts.length;
  historyRunning = job.state === "running"; updateHistoryStart();
  $("history-form").querySelectorAll("select,input").forEach(input => input.disabled = job.state === "running");
  showDrafts(job.drafts);
  if (generation === telegramGeneration && state.page === "history") telegramTimer = setTimeout(() => pollTelegram(generation).catch(failure), 2000);
}
async function loadTelegram() {
  telegramConnected = false; // Re-read groups when opening or explicitly refreshing the page.
  if (!$("history-from").value) {
    $("history-from").value = new Date(Date.now() - 90 * 86400000).toISOString().slice(0, 10);
    $("history-until").value = new Date(Date.now() + 86400000).toISOString().slice(0, 10);
  }
  await pollTelegram(telegramGeneration);
}
$("history-group").onchange = () => historyMembers().catch(failure);
$("history-members").onchange = () => updateHistoryStart();
$("history-enable").onclick = async () => {
  const chat = $("history-group").value;
  if (!chat) return;
  $("history-enable").disabled = true;
  try {
    await tgPost("add_chat", {chat: Number(chat)});
    if (state.page !== "history" || $("dashboard").hidden) return;
    await historyGroups();
  } catch (error) { failure(error); }
  finally { $("history-enable").disabled = false; }
};
$("tg-connect").onclick = async () => {
  $("tg-connect").disabled = true;
  try {
    const body = $("tg-api-id").value || $("tg-api-hash").value ? {api_id: Number($("tg-api-id").value), api_hash: $("tg-api-hash").value.trim()} : {};
    showAccount(await tgPost("connect", body)); $("tg-api-hash").value = ""; stopTelegram(); await loadTelegram();
  } catch (error) { failure(error); } finally { $("tg-connect").disabled = false; }
};
$("tg-password-form").onsubmit = async event => {
  event.preventDefault(); const password = $("tg-password").value; $("tg-password").value = "";
  try { showAccount(await tgPost("password", {password})); stopTelegram(); await loadTelegram(); } catch (error) { failure(error); }
};
$("tg-disconnect").onclick = async () => {
  try { await tgPost("disconnect"); resetTelegram(); await loadTelegram(); } catch (error) { failure(error); }
};
$("history-form").onsubmit = async event => {
  event.preventDefault();
  const targets = [...$("history-members").querySelectorAll("input:checked")].map(input => Number(input.value));
  if (targets.length < 1 || targets.length > 10) {
    updateHistoryStart(); failure(new Error("Отметьте от 1 до 10 участников в списке.")); return;
  }
  $("history-start").disabled = true;
  try {
    await tgPost("start", {chat: Number($("history-group").value), targets, from: $("history-from").value, until: $("history-until").value});
    stopTelegram(); await loadTelegram();
  } catch (error) { $("history-start").disabled = false; failure(error); }
};
$("history-cancel").onclick = async () => { try { await tgPost("cancel"); stopTelegram(); await loadTelegram(); } catch (error) { failure(error); } };

$("load-groups").onclick = async () => {
  $("load-groups").disabled = true; $("add-chat-note").textContent = "Загружаем группы…";
  try {
    const data = await api("/api/telegram/available_groups");
    if (state.page !== "chats" || $("dashboard").hidden) return;
    $("available-group").replaceChildren(...data.groups.map(group => { const option = node("option", `${group.title} · ${group.id}`); option.value = group.id; return option; }));
    $("add-chat-form").hidden = !data.groups.length;
    $("add-chat-note").textContent = data.groups.length ? "Нужно право приглашать участников в выбранную группу." : "Групп не найдено. Подключите нужный аккаунт в разделе «История Telegram».";
  } catch (error) { $("add-chat-note").textContent = "Подключите аккаунт в разделе «История Telegram» и повторите загрузку."; failure(error); }
  finally { $("load-groups").disabled = false; }
};
$("add-chat-form").onsubmit = async event => {
  event.preventDefault(); $("add-chat").disabled = true;
  try {
    const result = await tgPost("add_chat", {chat: Number($("available-group").value)});
    if (state.page !== "chats" || $("dashboard").hidden) return;
    $("add-chat-note").textContent = `Бот добавлен в «${result.title}». Сбор включён. Участникам нужно выполнить /roast_on.`;
    await boot();
  } catch (error) { failure(error); } finally { $("add-chat").disabled = false; }
};
