const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtMoney = (v) => (v == null ? "—" : new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 0 }).format(v) + " ₽");
const fmtDate = (d) => (d ? d.slice(0, 10).split("-").reverse().join(".") : "—");
const fmtNum = (n) => Number(n || 0).toLocaleString("ru-RU");

const EXAMPLES = [
  "Поставка бумаги для офисной техники", "картриджы для принтера", "Услуги по физической охране",
  "Поставка молока", "Ремонт кровли", "ГСМ бензин аи-92", "Техническое обслуживание лифтов",
  "Поставка медицинских перчаток",
];
const STATUS = {
  verified: ["Проверенный", "b-verified"],
  experienced: ["С опытом", "b-experienced"],
  participant: ["Участник", "b-participant"],
  category: ["Профильный", "b-category"],
  new: ["Новый", "b-new"],
};
const INTENT = { goods: "Поставка товаров", services: "Работы / услуги" };
const TAB_HINT = {
  dataset: "Поставщики, которые уже участвовали в похожих закупках АИС ГЗ и Электронного магазина. Нажмите на название, чтобы открыть карточку.",
  external: "Компании Санкт-Петербурга и Ленобласти из реестра МСП, которых нет в истории закупок, но вид деятельности подходит.",
};

// результаты хранятся отдельно для каждого режима, чтобы вкладки не показывали чужую выдачу
const state = { mode: "card", tab: "dataset", role: "", data: null, results: { card: null, quick: null, file: null }, batch: null };

// ------------------------------------------------------------------ init
$("#examples").innerHTML = EXAMPLES.map((e) => `<button type="button">${esc(e)}</button>`).join("");
$("#examples").parentElement.addEventListener("click", (ev) => {
  if (ev.target.tagName === "BUTTON") { $("#q").value = ev.target.textContent; runQuick(); }
});
$$(".mode").forEach((b) => b.addEventListener("click", () => setMode(b.dataset.mode)));
$$(".tab").forEach((t) => t.addEventListener("click", () => setTab(t.dataset.tab)));
$("#role-filter").addEventListener("change", (e) => { state.role = e.target.value; renderList(); });
$("#modal-close").addEventListener("click", closeModal);
$("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });

fetch("/api/health").then((r) => r.json()).then((h) => {
  $("#health").textContent = `${fmtNum(h.lots)} закупок · ${fmtNum(h.suppliers)} поставщиков в базе`;
}).catch(() => {});

const params = new URLSearchParams(location.search);
if (params.get("q")) { $("#q").value = params.get("q"); setMode("quick"); runQuick(); }

// ------------------------------------------------------------------ режимы
function setMode(mode) {
  state.mode = mode;
  $$(".mode").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
  $("#card-form").classList.toggle("hidden", mode !== "card");
  $("#file-form").classList.toggle("hidden", mode !== "file");
  $("#form").classList.toggle("hidden", mode !== "quick");
  $("#state").innerHTML = "";
  $("#batch").classList.toggle("hidden", !(mode === "file" && state.batch));
  const data = state.results[mode];
  if (data) showResult(data, false); else { $("#result").classList.add("hidden"); $("#about").classList.remove("hidden"); }
  $("#csv").classList.toggle("hidden", mode !== "quick");
}

function setTab(tab) {
  state.tab = tab;
  $$(".tab").forEach((x) => x.classList.toggle("active", x.dataset.tab === tab));
  $("#tab-hint").textContent = TAB_HINT[tab];
  renderList();
}

// ------------------------------------------------------------------ запросы
async function request(btn, doFetch, onOk) {
  btn.disabled = true;
  $("#state").innerHTML = `<div class="spinner"></div>Ищем похожие закупки и проверяем поставщиков по реестрам…`;
  $("#result").classList.add("hidden");
  $("#about").classList.add("hidden");
  if (state.mode === "file") $("#batch").classList.add("hidden");
  $("#state").scrollIntoView({ behavior: "smooth", block: "center" });
  try {
    const r = await doFetch();
    if (!r.ok) {
      let msg = r.statusText;
      try { const j = await r.json(); msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch (_) { /* не JSON */ }
      throw new Error(msg);
    }
    $("#state").innerHTML = "";
    onOk(await r.json());
  } catch (e) {
    $("#state").innerHTML = `<span class="error-box">Не получилось: ${esc(e.message)}</span>`;
  } finally {
    btn.disabled = false;
  }
}

function showResult(data, scroll = true) {
  state.data = data;
  state.results[state.mode === "file" ? "file" : state.mode] = data;
  renderKpis();
  renderUnderstanding();
  setTab(state.tab === "external" && !(data.external || []).length ? "dataset" : state.tab);
  $("#result").classList.remove("hidden");
  $("#about").classList.add("hidden");
  if (scroll) (state.mode === "file" ? $("#kpis") : $("#main")).scrollIntoView({ behavior: "smooth", block: "start" });
}

// быстрый поиск
function quickParams() {
  const p = new URLSearchParams({ q: $("#q").value.trim() });
  const platform = $("input[name=platform]:checked").value;
  if (platform) p.set("platform", platform);
  if ($("#region_only").checked) p.set("region_only", "true");
  p.set("external", $("#external").checked ? "true" : "false");
  return p;
}
async function runQuick() {
  const p = quickParams();
  if (!p.get("q")) return;
  history.replaceState(null, "", "?q=" + encodeURIComponent(p.get("q")));
  $("#csv").href = "/api/search.csv?" + p.toString();
  await request($("#form button[type=submit]"), () => fetch("/api/search?" + p.toString()), showResult);
}
$("#form").addEventListener("submit", (ev) => { ev.preventDefault(); runQuick(); });
$$("input[name=platform], #region_only, #external").forEach((el) =>
  el.addEventListener("change", () => { if ($("#q").value.trim() && state.results.quick) runQuick(); }));

// карточка закупки
const OKPD_RE = /^\d{2}(\.\d+)*$/;
function parseItems(text) {
  const items = [], codes = [];
  for (const line of text.split("\n").map((x) => x.trim()).filter(Boolean)) {
    const parts = line.split(/[;\t]/).map((x) => x.trim());
    const code = parts.length > 1 && OKPD_RE.test(parts[parts.length - 1]) ? parts.pop() : "";
    items.push(parts.join("; "));
    codes.push(code);
  }
  return { items, codes };
}
const num = (v) => { const s = String(v || "").replace(/[^\d,.]/g, "").replace(",", "."); return s ? Number(s) : null; };

$("#card-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const { items, codes } = parseItems($("#c-items").value);
  const body = {
    text: $("#c-text").value.trim(), items, okpd_codes: codes,
    price: num($("#c-price").value), customer_inn: $("#c-customer").value.replace(/\D/g, "") || null,
    platform: $("input[name=c-platform]:checked").value || null, is_smp: $("#c-smp").checked || null,
  };
  if (!body.text && !items.length) {
    $("#state").innerHTML = `<span class="error-box">Укажите наименование закупки или хотя бы одну позицию</span>`;
    $("#c-text").focus();
    return;
  }
  history.replaceState(null, "", location.pathname);
  await request($("#card-form button[type=submit]"),
    () => fetch("/api/procurement", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
    showResult);
});
$("#c-example").addEventListener("click", () => {
  $("#c-text").value = "Поставка бумаги для офисной техники";
  $("#c-items").value = "Бумага для офисной техники А4, 500 листов; 17.12.14.129\nБумага для офисной техники А3, 500 листов; 17.12.14.129";
  $("#c-price").value = "45 000";
  $("#c-customer").value = "7802141070";
  $("input[name=c-platform][value='ЭМ']").checked = true;
});
$("#c-clear").addEventListener("click", () => {
  $("#card-form").reset();
  state.results.card = null;
  $("#result").classList.add("hidden");
  $("#about").classList.remove("hidden");
  $("#state").innerHTML = "";
});

// файл
const drop = $("#drop");
function setFile(f) {
  drop.classList.toggle("has-file", !!f);
  $("#drop-title").textContent = f ? f.name : "Перетащите файл сюда или нажмите";
  $("#drop-sub").textContent = f ? `${(f.size / 1024).toFixed(0)} КБ · нажмите «Обработать файл»` : "Excel или CSV, до 300 закупок";
}
$("#f-file").addEventListener("change", (e) => setFile(e.target.files[0]));
["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (e) => {
  if (e.dataTransfer.files.length) { $("#f-file").files = e.dataTransfer.files; setFile(e.dataTransfer.files[0]); }
});
$("#file-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = $("#f-file").files[0];
  if (!f) { $("#state").innerHTML = `<span class="error-box">Сначала выберите файл с закупками</span>`; return; }
  const fd = new FormData();
  fd.append("file", f);
  await request($("#file-form button[type=submit]"), () => fetch("/api/batch", { method: "POST", body: fd }), renderBatch);
});

function renderBatch(b) {
  state.batch = b;
  const rows = b.procedures.map((p, i) => {
    const r = p.result, hist = r.suppliers || [];
    const meta = [`${p.input.items.length} поз.`];
    if (p.input.customer_inn) meta.push("заказчик " + esc(p.input.customer_inn));
    if (p.input.price) meta.push("НМЦ " + fmtMoney(p.input.price));
    if (p.input.platform) meta.push(esc(p.input.platform));
    return `<tr data-i="${i}">
      <td>${esc(p.procedure_id)}</td>
      <td>${esc(p.input.text || p.input.items.slice(0, 2).join("; "))}<div class="sub2">${meta.join(" · ")}</div></td>
      <td>${(r.okpd2 || []).slice(0, 2).map((o) => esc(o.code)).join("<br>") || "—"}</td>
      <td class="sub2">${hist.slice(0, 3).map((s, k) => `${k + 1}. ${esc(s.name)}`).join("<br>") || "не найдено"}</td>
      <td>${hist.length}<span class="sub2"> + ${(r.external || []).length} новых</span></td>
    </tr>`;
  }).join("");
  const errs = (b.errors || []).length ? `<div class="batch-errors"><b>Замечания по файлу (${b.errors.length})</b><ul>${b.errors.map((e) =>
    `<li>${e.row ? "строка " + e.row + ": " : ""}${e.procedure_id ? "закупка " + esc(e.procedure_id) + " — " : ""}${esc(e.problem)}</li>`).join("")}</ul></div>` : "";
  $("#batch").innerHTML = `
    <div class="batch-head">
      <h2>Обработано закупок: ${b.procedures.length} ${b.filename ? `<small>${esc(b.filename)}</small>` : ""}</h2>
      <div class="results-actions">
        <a class="btn btn-primary btn-sm" href="/api/batch/${b.batch_id}.xlsx">Скачать Excel</a>
        <a class="btn btn-ghost btn-sm" href="/api/batch/${b.batch_id}.csv">CSV</a>
      </div>
    </div>
    ${b.procedures.length ? `<div class="table-scroll"><table class="batch-table">
      <tr><th>№</th><th>Закупка</th><th>ОКПД2</th><th>Топ-3 поставщика</th><th>Найдено</th></tr>${rows}
    </table></div><p class="batch-hint">Нажмите на строку, чтобы увидеть полный рейтинг по закупке.</p>` : `<div class="empty">В файле не нашлось ни одной закупки</div>`}
    ${errs}`;
  $("#batch").classList.remove("hidden");
  $$("tr[data-i]", $("#batch")).forEach((tr) => tr.addEventListener("click", () => {
    $$("tr", $("#batch")).forEach((x) => x.classList.remove("sel"));
    tr.classList.add("sel");
    showResult(b.procedures[Number(tr.dataset.i)].result, tr.dataset.auto !== "1");
    tr.dataset.auto = "";
  }));
  const first = $("tr[data-i]", $("#batch"));
  if (first) { first.dataset.auto = "1"; first.click(); }
  $("#batch").scrollIntoView({ behavior: "smooth", block: "start" });
}

// ------------------------------------------------------------------ отрисовка результата
function renderKpis() {
  const d = state.data, all = [...d.suppliers, ...(d.external || [])];
  const verified = d.suppliers.filter((s) => s.status === "verified").length;
  const flagged = all.filter((s) => s.rnp && s.rnp.in_rnp).length;
  const manuf = all.filter((s) => s.role === "manufacturer").length;
  $("#kpis").innerHTML = [
    [fmtNum(d.total_candidates), "кандидатов рассмотрено"],
    [verified, "проверенных поставщиков в топе"],
    [(d.external || []).length, "новых компаний из реестров"],
    flagged ? [flagged, "в реестре недобросовестных"] : [manuf, "производителей среди найденных"],
  ].map(([v, l]) => `<div class="kpi"><b>${v}</b><span>${l}</span></div>`).join("");
}

function renderUnderstanding() {
  const d = state.data, q = d.query, pr = q.procurement;
  let html = `<p class="side-title">Как система поняла закупку</p>`;
  if (q.was_corrected) html += `<div class="corr">Исправили опечатки: <b>${esc(q.corrected)}</b></div>`;
  html += `<h3>Ключевые слова</h3><div class="terms">${q.terms.slice(0, 14).map((t) => `<span class="term">${esc(t)}</span>`).join("") || "—"}</div>`;
  if (q.intent) html += `<h3>Тип закупки</h3><span class="intent">${INTENT[q.intent]}</span>`;
  if (pr && (pr.items || pr.customer_inn || pr.price || pr.platform || pr.is_smp)) {
    html += `<h3>Данные закупки</h3><div class="proc-data">
      ${pr.items ? `<span>Позиций</span><span>${pr.items}</span>` : ""}
      ${pr.customer_inn ? `<span>Заказчик</span><span>ИНН ${esc(pr.customer_inn)}</span>` : ""}
      ${pr.price ? `<span>НМЦ</span><span>${fmtMoney(pr.price)}</span>` : ""}
      ${pr.platform ? `<span>Площадка</span><span>${esc(pr.platform)}</span>` : ""}
      ${pr.is_smp ? `<span>Участники</span><span>только малый бизнес</span>` : ""}
    </div>`;
  }
  if (d.okpd2.length) {
    html += `<h3>Коды ОКПД2 ${pr && pr.okpd_from_spec ? "(из спецификации)" : "(определены по тексту)"}</h3>` + d.okpd2.map((o) => `
      <div class="okpd">
        <div class="okpd-top"><span>${esc(o.code)}</span><span>${Math.round(o.share * 100)}%</span></div>
        <div>${esc(o.name)}</div>
        ${o.matched_item && o.matched_item !== o.name ? `<div class="item">позиция: ${esc(o.matched_item)}</div>` : ""}
        <div class="share"><span style="width:${Math.round(o.share * 100)}%"></span></div>
      </div>`).join("");
  }
  if ((q.explain || []).length) {
    html += `<h3>Разбор текста</h3><ul class="explain">${q.explain.slice(0, 8).map((x) => `<li>${esc(x)}</li>`).join("")}</ul>`;
  }
  html += `<div class="meta">Подбор занял ${d.took_ms} мс</div>`;
  $("#understanding").innerHTML = html;
  $("#cnt-dataset").textContent = d.suppliers.length;
  $("#cnt-external").textContent = (d.external || []).length;
}

function renderList() {
  const d = state.data;
  if (!d) return;
  let items = state.tab === "dataset" ? d.suppliers : (d.external || []);
  if (state.role) items = items.filter((s) => s.role === state.role);
  if (!items.length) {
    $("#list").innerHTML = `<div class="empty">${state.role ? "Нет поставщиков с такой ролью — сбросьте фильтр «Все роли»"
      : state.tab === "external" ? "Новые компании не найдены" : "Поставщики не найдены — попробуйте уточнить название или добавить позиции"}</div>`;
    return;
  }
  $("#list").innerHTML = items.map((s, i) => card(s, i + 1, d)).join("");
  $$("[data-inn]", $("#list")).forEach((el) => el.addEventListener("click", () => openSupplier(el.dataset.inn)));
}

function initials(name) {
  const clean = String(name || "").replace(/^(ООО|АО|ПАО|ЗАО|ОАО|ИП|ГУП|ФГУП|АНО|ПК|ФБУЗ|ФГКУ|СПБ ГБУ|ГБУ)\s*/i, "").replace(/["«»']/g, "").trim();
  return (clean[0] || "?").toUpperCase();
}

function card(s, rank, d) {
  const [stLabel, stCls] = STATUS[s.status] || [s.status, ""];
  const st = s.stats;
  const inRnp = s.rnp && s.rnp.in_rnp;
  const roleTitle = (s.role_reasons || []).join("; ") || "недостаточно данных";
  const factors = Object.entries(s.factors || {}).filter(([k]) => d.weights[k]).map(([k, v]) => [k, v, v * d.weights[k] * 100])
    .sort((a, b) => b[2] - a[2]).map(([k, v, c]) => `<div class="factor"><span>${esc(d.factor_labels[k] || k)}</span>
      <div class="share"><span style="width:${Math.round(v * 100)}%"></span></div><b>+${c.toFixed(1)}</b></div>`).join("");
  const sub = [`ИНН ${esc(s.inn)}`];
  if (s.city) sub.push(esc(s.city));
  if (s.okved) sub.push(`ОКВЭД ${esc(s.okved.code)}${s.okved.name ? " · " + esc(s.okved.name) : ""}`);
  const contacts = [];
  if (s.phone) contacts.push(`<a href="tel:${esc(s.phone)}">☎ ${esc(s.phone)}</a>`);
  if (s.email) contacts.push(`<a href="mailto:${esc(s.email)}">✉ ${esc(s.email)}</a>`);
  const stats = st ? `<div class="stats">
      <span>Закупок <b>${st.n_lots}</b></span>
      <span>Побед <b>${st.n_wins}</b> (${Math.round(st.win_rate * 100)}%)</span>
      <span>Заказчиков <b>${st.n_customers}</b></span>
      <span>ЭМ / АИС ГЗ <b>${st.n_eshop} / ${st.n_aisgz}</b></span>
      <span>Последняя <b>${fmtDate(st.last_date)}</b></span>
    </div>` : "";
  const extra = [];
  if (s.msp_category) extra.push(`${esc(s.msp_category)} предприятие`);
  if (s.employees) extra.push(`${s.employees} сотр.`);
  const reasons = (s.reasons || []).map((r) => `<li${/^Внимание/.test(r) ? ' class="alert"' : ""}>${esc(r)}</li>`).join("");
  const evidence = (s.evidence || []).length ? `
    <details class="evidence"><summary>Похожие закупки этого поставщика (${s.evidence.length})</summary>
      ${s.evidence.map((e) => `<div class="lot"><div>${esc(e.subject)}</div>
        <div class="meta">${fmtDate(e.date)} · ${esc(e.platform)} · ${fmtMoney(e.price)}
        ${e.is_winner ? ' · <span class="won">победа</span>' : " · участие"}</div></div>`).join("")}
    </details>` : "";
  const ringCls = s.score >= 75 ? "hi" : s.score < 45 ? "lo" : "";
  return `
  <article class="card${inRnp ? " flag" : ""}">
    <div class="card-head">
      <div class="avatar"><span class="rank">${rank}</span>${esc(initials(s.name))}</div>
      <div>
        <button type="button" class="name" data-inn="${esc(s.inn)}">${esc(s.name)}</button>
        <div class="sub">${sub.map((x) => `<span>${x}</span>`).join("")}</div>
        <div class="badges">
          ${inRnp ? `<span class="badge b-bad" title="Действующая запись в реестре недобросовестных поставщиков (ЕИС)">В реестре недобросовестных</span>` : ""}
          <span class="badge ${stCls}" title="${esc(s.status_reason)}">${stLabel}</span>
          <span class="badge" title="${esc(roleTitle)}">${esc(s.role_label || "Роль не определена")}${s.confidence ? ` · ${Math.round(s.confidence * 100)}%` : ""}</span>
          ${s.is_active === false ? `<span class="badge b-bad" title="Ликвидирован ${esc(s.liquidated || "")}">Ликвидирован</span>` : ""}
          ${extra.length ? `<span class="badge">${extra.join(", ")}</span>` : ""}
          ${(s.enrich_sources || []).map((x) => `<span class="badge b-src" title="Источник данных">${esc(x)}</span>`).join("")}
        </div>
        ${contacts.length ? `<div class="contacts">${contacts.join("")}</div>` : ""}
      </div>
      <div class="score" title="Итоговый балл релевантности, 0–100">
        <div class="ring ${ringCls}" style="--p:${Math.min(s.score, 100)}"><b>${Math.round(s.score)}</b></div>
        <small>балл</small>
      </div>
    </div>
    <div class="card-body">
      <div class="why">
        <h4>Почему рекомендован</h4>
        <ul>${reasons}
        ${(s.role_reasons || []).length ? `<li>Роль «${esc(s.role_label)}»: ${esc(s.role_reasons.join("; "))}</li>` : ""}</ul>
        ${stats}
        ${evidence}
      </div>
      ${factors ? `<div class="factors"><h4>Из чего сложился балл</h4>${factors}</div>` : ""}
    </div>
  </article>`;
}

// ------------------------------------------------------------------ карточка поставщика
async function openSupplier(inn) {
  $("#modal-body").innerHTML = `<div class="spinner"></div>`;
  $("#modal").classList.remove("hidden");
  try {
    const s = await (await fetch("/api/supplier/" + inn)).json();
    const rnp = s.rnp ? (s.rnp.in_rnp ? "состоит в реестре" : s.rnp.was_in_rnp ? "был в реестре, исключён" : "записей нет") : null;
    const kv = [
      ["ИНН", s.inn], ["ОГРН", s.ogrn], ["Полное название", s.full_name !== s.name ? s.full_name : null],
      ["Роль", s.role_label ? `${s.role_label} (${Math.round((s.confidence || 0) * 100)}%)` : null],
      ["Основной ОКВЭД", s.okved ? `${s.okved.code} ${s.okved.name || ""}` : null],
      ["Город", s.city], ["Категория МСП", s.msp_category], ["Сотрудников", s.employees],
      ["Руководитель", s.head], ["Телефон", s.phone], ["Email", s.email],
      ["Реестр недобросовестных", rnp],
      ["Источники", (s.enrich_sources || []).join(", ")],
    ].filter(([, v]) => v != null && v !== "");
    $("#modal-body").innerHTML = `
      <h2>${esc(s.name)}</h2>
      <div class="sub">${esc(s.role_reasons ? s.role_reasons.join("; ") : "")}</div>
      <h3>Реквизиты</h3>
      <div class="kv">${kv.map(([k, v]) => `<div>${esc(k)}</div><div>${esc(v)}</div>`).join("")}</div>
      ${s.rnp && s.rnp.records.length ? `<h3>Записи в реестре недобросовестных поставщиков</h3><div class="table-scroll"><table><tr><th>№ записи</th><th>Закон</th><th>Включено</th><th>Исключено</th></tr>
        ${s.rnp.records.map((r) => `<tr><td>${esc(r.number)}</td><td>${esc(r.law || "")}</td><td>${esc(r.included || "")}</td><td>${esc(r.excluded || "действует")}</td></tr>`).join("")}</table></div>` : ""}
      ${s.okpd2 && s.okpd2.length ? `<h3>Опыт по кодам ОКПД2</h3><div class="table-scroll"><table><tr><th>Код</th><th>Пример позиции</th><th>Закупок</th><th>Побед</th><th>Последняя</th></tr>
        ${s.okpd2.map((o) => `<tr><td>${esc(o.code)}</td><td>${esc(o.name)}</td><td>${o.n_lots}</td><td>${o.n_wins}</td><td>${fmtDate(o.last_date)}</td></tr>`).join("")}</table></div>` : ""}
      ${s.recent_lots && s.recent_lots.length ? `<h3>Последние закупки</h3><div class="table-scroll"><table><tr><th>Дата</th><th>Предмет</th><th>Площадка</th><th>НМЦ</th><th></th></tr>
        ${s.recent_lots.map((l) => `<tr><td>${fmtDate(l.date)}</td><td>${esc(l.subject)}</td><td>${esc(l.platform)}</td><td>${fmtMoney(l.price)}</td><td>${l.is_winner ? '<span class="won">победа</span>' : ""}</td></tr>`).join("")}</table></div>` : ""}`;
  } catch (e) {
    $("#modal-body").innerHTML = `<span class="error-box">Не удалось загрузить карточку: ${esc(e.message)}</span>`;
  }
}
function closeModal() { $("#modal").classList.add("hidden"); }
