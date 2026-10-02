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
  approx: ["Близкий профиль", "b-participant"],
};
const CONF_TITLE = { medium: "Совпадение неполное", low: "Точных совпадений в истории нет" };
const INTENT = { goods: "Поставка товаров", services: "Работы / услуги" };
const TAB_HINT = {
  dataset: "Поставщики, которые уже участвовали в похожих закупках АИС ГЗ и Электронного магазина. Нажмите на название, чтобы открыть карточку.",
  external: "Компании из реестра МСП, которых нет в истории закупок, но вид деятельности подходит: местные — для любых закупок, производители и оптовики из других регионов — для товаров.",
};

const FIRST = 3, MORE = 5;
const state = { mode: "card", tab: "dataset", role: "", size: "", visible: FIRST, files: [], data: null, results: { card: null, quick: null, file: null }, batch: null,
  rerun: { card: null, quick: null, file: null } };

$("#examples").innerHTML = EXAMPLES.map((e) => `<button type="button">${esc(e)}</button>`).join("");
$("#examples").parentElement.addEventListener("click", (ev) => {
  if (ev.target.tagName === "BUTTON") { $("#q").value = ev.target.textContent; runQuick(); }
});
$$(".mode").forEach((b) => b.addEventListener("click", () => setMode(b.dataset.mode)));
$$(".tab").forEach((t) => t.addEventListener("click", () => setTab(t.dataset.tab)));
$("#role-filter").addEventListener("change", (e) => { state.role = e.target.value; state.visible = FIRST; renderList(); });
$("#size-filter").addEventListener("change", (e) => { state.size = e.target.value; state.visible = FIRST; renderList(); });
$("#modal-close").addEventListener("click", closeModal);
$("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });

fetch("/api/health").then((r) => r.json()).then((h) => {
  $("#health").textContent = `${fmtNum(h.lots)} закупок · ${fmtNum(h.suppliers)} поставщиков в базе`;
}).catch(() => {});

window.addEventListener("pageshow", () => {
  const q = new URLSearchParams(location.search).get("q");
  $$("form").forEach((f) => f.reset());
  setFile(null);
  history.replaceState(null, "", location.pathname);
  if (q) { $("#q").value = q; setMode("quick"); runQuick(); }
});

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
  state.visible = FIRST;
  $$(".tab").forEach((x) => x.classList.toggle("active", x.dataset.tab === tab));
  $("#tab-hint").textContent = TAB_HINT[tab];
  renderList();
}

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
      try { const j = await r.json(); msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch (_) { }
      throw new Error(msg);
    }
    const data = await r.json();
    $("#state").innerHTML = "";
    onOk(data);
  } catch (e) {
    $("#state").innerHTML = `<span class="error-box">Не получилось: ${esc(e.message)}</span>`;
  } finally {
    btn.disabled = false;
  }
}

function showResult(data, scroll = true) {
  state.data = data;
  state.results[state.mode] = data;
  renderConfidence();
  renderKpis();
  renderUnderstanding();
  setTab(state.tab === "external" && !(data.external || []).length ? "dataset" : state.tab);
  $("#result").classList.remove("hidden");
  $("#about").classList.add("hidden");
  if (scroll) (state.mode === "file" ? $("#kpis") : $("#main")).scrollIntoView({ behavior: "smooth", block: "start" });
}

function quickParams() {
  const p = new URLSearchParams({ q: $("#q").value.trim() });
  const platform = $("input[name=platform]:checked").value;
  if (platform) p.set("platform", platform);
  if ($("#region_only").checked) p.set("region_only", "true");
  p.set("external", $("#external").checked ? "true" : "false");
  p.set("limit", "40");
  return p;
}
async function runQuick(exclude = []) {
  const p = quickParams();
  if (!p.get("q")) return;
  $("#csv").href = "/api/search.csv?" + p.toString();
  if (exclude.length) p.set("exclude", exclude.join(","));
  state.rerun.quick = (ex) => runQuick(ex);
  await request($("#form button[type=submit]"), () => fetch("/api/search?" + p.toString()), showResult);
}
$("#form").addEventListener("submit", (ev) => { ev.preventDefault(); runQuick(); });
$$("input[name=platform], #region_only, #external").forEach((el) =>
  el.addEventListener("change", () => { if ($("#q").value.trim() && state.results.quick) runQuick(); }));

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
  const run = (exclude) => request($("#card-form button[type=submit]"),
    () => fetch("/api/procurement?limit=40&n_new=15", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...body, exclude_terms: exclude }) }),
    showResult);
  state.rerun.card = run;
  await run([]);
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

const drop = $("#drop");
function renderFiles() {
  drop.classList.toggle("has-file", state.files.length > 0);
  $("#drop-title").textContent = state.files.length ? "Добавить ещё файл" : "Перетащите файл сюда или нажмите";
  $("#drop-sub").textContent = state.files.length > 1 ? "файлы будут связаны по номеру лота или закупки"
    : "Excel или CSV; выгрузку АИС ГЗ можно добавить двумя файлами: извещения и позиции";
  $("#file-list").innerHTML = state.files.map((f, i) => `<span class="file-chip">${esc(f.name)}
    <small>${(f.size / 1024).toFixed(0)} КБ</small><button type="button" data-rm="${i}" aria-label="Убрать файл">×</button></span>`).join("");
  $$("[data-rm]", $("#file-list")).forEach((el) => el.addEventListener("click", () => {
    state.files.splice(Number(el.dataset.rm), 1);
    renderFiles();
  }));
}
function addFiles(list) {
  for (const f of [...(list || [])]) {
    if (!state.files.some((x) => x.name === f.name && x.size === f.size)) state.files.push(f);
  }
  renderFiles();
}
function setFile(files) {
  state.files = [];
  addFiles(files);
}
$("#f-file").addEventListener("change", (e) => { addFiles(e.target.files); e.target.value = ""; });
["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (e) => addFiles(e.dataTransfer.files));
$("#file-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const files = state.files;
  if (!files.length) { $("#state").innerHTML = `<span class="error-box">Сначала выберите файл с закупками</span>`; return; }
  const fd = new FormData();
  files.forEach((f) => fd.append("file", f));
  const job = Math.random().toString(36).slice(2, 12);
  let active = true, total = 0;
  const timer = setInterval(async () => {
    try {
      const p = await (await fetch("/api/batch/progress/" + job)).json();
      if (!active || !(p.total || total)) return;
      const ready = !p.total || p.done >= p.total;
      total = p.total || total;
      $("#state").innerHTML = `<div class="spinner"></div>${ready ? `Обработано закупок: ${total} из ${total}. Получаем результат…` : `Обработано закупок: ${p.done} из ${total}`}
        <div class="share batch-progress"><span style="width:${ready ? 100 : Math.round(p.done / total * 100)}%"></span></div>`;
    } catch (_) { }
  }, 1500);
  try {
    await request($("#file-form button[type=submit]"), () => fetch("/api/batch?limit=25&n_new=5&job=" + job, { method: "POST", body: fd }), renderBatch);
  } finally {
    active = false;
    clearInterval(timer);
    if ($(".batch-progress", $("#state"))) $("#state").innerHTML = "";
  }
});

function batchSuppliers(hist, n) {
  if (!hist.length) return "не найдено";
  const rest = hist.length - Math.min(n, hist.length);
  return hist.slice(0, n).map((s, k) => `${k + 1}. ${esc(s.name)}`).join("<br>")
    + (rest > 0 || n > FIRST ? `<div class="row-btns">
        ${rest > 0 ? `<button type="button" class="row-more">Загрузить ещё ${Math.min(MORE, rest)}</button>` : ""}
        ${n > FIRST ? `<button type="button" class="row-less">Свернуть</button>` : ""}</div>` : "");
}

function batchRow(p, n) {
  const r = p.result, hist = r.suppliers || [];
  const meta = [`${p.input.items.length} поз.`];
  if (p.input.customer_inn) meta.push("заказчик " + esc(p.input.customer_inn));
  if (p.input.price) meta.push("НМЦ " + fmtMoney(p.input.price));
  if (p.input.platform) meta.push(esc(p.input.platform));
  return `
      <td>${esc(p.procedure_id)}${p.input.lot_id && p.input.lot_id !== p.procedure_id ? `<div class="sub2">лот ${esc(p.input.lot_id)}</div>` : ""}</td>
      <td>${esc(p.input.text || p.input.items.slice(0, 2).join("; "))}<div class="sub2">${meta.join(" · ")}</div></td>
      <td>${(r.okpd2 || []).slice(0, 2).map((o) => esc(o.code)).join("<br>") || "—"}</td>
      <td class="sub2 batch-sup">${batchSuppliers(hist, n)}</td>
      <td>${hist.length}<span class="sub2"> + ${(r.external || []).length} новых</span>
        ${r.confidence && r.confidence.level !== "high" ? `<div class="sub2 conf-mark" title="${esc(r.confidence.message)}">${r.confidence.level === "low" ? "нет точных совпадений" : "совпадение неполное"}</div>` : ""}</td>`;
}

function renderBatch(b) {
  state.batch = b;
  const shown = b.procedures.map(() => FIRST);
  const rows = b.procedures.map((p, i) => `<tr data-i="${i}">${batchRow(p, FIRST)}</tr>`).join("");
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
      <tr><th>№</th><th>Закупка</th><th>ОКПД2</th><th>Поставщики</th><th>Найдено</th></tr>${rows}
    </table></div><p class="batch-hint">Нажмите на строку, чтобы увидеть полный рейтинг по закупке.</p>` : `<div class="empty">В файле не нашлось ни одной закупки</div>`}
    ${errs}`;
  $("#batch").classList.remove("hidden");
  $$("tr[data-i]", $("#batch")).forEach((tr) => tr.addEventListener("click", (ev) => {
    const i = Number(tr.dataset.i);
    const more = ev.target.closest(".row-more"), less = ev.target.closest(".row-less");
    if (more || less) {
      shown[i] = less ? FIRST : shown[i] + MORE;
      $(".batch-sup", tr).innerHTML = batchSuppliers(b.procedures[i].result.suppliers || [], shown[i]);
      return;
    }
    $$("tr", $("#batch")).forEach((x) => x.classList.remove("sel"));
    tr.classList.add("sel");
    state.rerun.file = async (exclude, btn) => {
      const p = b.procedures[i];
      await request(btn, () => fetch(`/api/procurement?limit=25&n_new=5&batch_id=${b.batch_id}&index=${i}`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...p.input, exclude_terms: exclude }),
      }), (data) => {
        p.result = data;
        shown[i] = FIRST;
        tr.innerHTML = batchRow(p, FIRST);
        showResult(data);
      });
      $("#batch").classList.remove("hidden");
    };
    showResult(b.procedures[i].result, tr.dataset.auto !== "1");
    tr.dataset.auto = "";
  }));
  const first = $("tr[data-i]", $("#batch"));
  if (first) { first.dataset.auto = "1"; first.click(); }
  $("#batch").scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderConfidence() {
  const c = state.data.confidence;
  const el = $("#conf");
  if (!c || c.level === "high" || !c.message) { el.classList.add("hidden"); return; }
  el.className = "conf conf-" + c.level;
  el.innerHTML = `<b>${CONF_TITLE[c.level]}</b><span>${esc(c.message)}</span>`;
}

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
  const excluded = q.excluded || [], canRerun = !!state.rerun[state.mode];
  const chip = (t, off) => canRerun
    ? `<button type="button" class="term${off ? " off" : ""}" data-term="${esc(t)}" title="${off ? "Вернуть слово в поиск" : "Убрать слово из поиска"}">${esc(t)}</button>`
    : `<span class="term">${esc(t)}</span>`;
  html += `<h3>Ключевые слова</h3><div class="terms">${q.terms.slice(0, 14).map((t) => chip(t, false)).join("")}${excluded.map((t) => chip(t, true)).join("") || (q.terms.length ? "" : "—")}</div>`;
  if (canRerun) html += `<p class="terms-hint">Слово не относится к предмету закупки? Нажмите на него, чтобы убрать, и запустите поиск заново.</p>
    <button type="button" class="btn btn-primary btn-sm hidden" id="re-search">Искать заново</button>`;
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
  const btn = $("#re-search");
  if (btn) {
    const off = () => $$(".term.off", $("#understanding")).map((el) => el.dataset.term);
    $$("button.term", $("#understanding")).forEach((el) => el.addEventListener("click", () => {
      el.classList.toggle("off");
      el.title = el.classList.contains("off") ? "Вернуть слово в поиск" : "Убрать слово из поиска";
      btn.classList.toggle("hidden", off().slice().sort().join(",") === excluded.slice().sort().join(","));
    }));
    btn.addEventListener("click", () => state.rerun[state.mode](off(), btn));
  }
  $("#cnt-dataset").textContent = d.suppliers.length;
  $("#cnt-external").textContent = (d.external || []).length;
}

function renderList() {
  const d = state.data;
  if (!d) return;
  let items = state.tab === "dataset" ? d.suppliers : (d.external || []);
  if (state.role) items = items.filter((s) => s.role === state.role);
  if (state.size) items = items.filter((s) => s.size === state.size);
  if (!items.length) {
    $("#list").innerHTML = `<div class="empty">${state.role || state.size ? "Нет поставщиков с такими фильтрами — сбросьте роль и размер"
      : state.tab === "external" ? "Новые компании не найдены" : "Поставщики не найдены — попробуйте уточнить название или добавить позиции"}</div>`;
    return;
  }
  const shown = items.slice(0, state.visible);
  const rest = items.length - shown.length;
  $("#list").innerHTML = shown.map((s, i) => card(s, i + 1, d)).join("")
    + (rest > 0 ? `<button type="button" class="btn btn-ghost more" id="more">Показать ещё ${Math.min(MORE, rest)} <small>осталось ${rest}, по убыванию балла</small></button>`
      : items.length > FIRST ? `<p class="list-end">Показаны все ${items.length}</p>` : "");
  $$("[data-inn]", $("#list")).forEach((el) => el.addEventListener("click", () => openSupplier(el.dataset.inn)));
  const more = $("#more");
  if (more) more.addEventListener("click", () => {
    const from = state.visible;
    state.visible += MORE;
    renderList();
    const next = $$(".card", $("#list"))[from];
    if (next) next.scrollIntoView({ behavior: "smooth", block: "start" });
  });
}

function initials(name) {
  const clean = String(name || "").replace(/^(ООО|АО|ПАО|ЗАО|ОАО|ИП|ГУП|ФГУП|АНО|ПК|ФБУЗ|ФГКУ|СПБ ГБУ|ГБУ)\s*/i, "").replace(/["«»']/g, "").trim();
  return (clean[0] || "?").toUpperCase();
}

function checks(s) {
  const out = [];
  if (s.rnp) {
    out.push(s.rnp.in_rnp ? `<span class="chk bad">Реестр недобросовестных: действующая запись</span>`
      : s.rnp.was_in_rnp ? `<span class="chk warn">Реестр недобросовестных: был, исключён</span>`
      : `<span class="chk ok">Реестр недобросовестных: записей нет</span>`);
  }
  if ((s.enrich_sources || []).length) out.push(`<span class="chk">Данные: ${esc(s.enrich_sources.join(", "))}</span>`);
  if (s.confidence_label) out.push(`<span class="chk">Роль определена с уверенностью: ${esc(s.confidence_label)}</span>`);
  return out.join("");
}

function card(s, rank, d) {
  const [stLabel, stCls] = STATUS[s.status] || [s.status, ""];
  const st = s.stats;
  const inRnp = s.rnp && s.rnp.in_rnp;
  const roleTitle = (s.role_reasons || []).join("; ") || "недостаточно данных";
  const factors = Object.entries(s.factors || {}).filter(([k]) => d.weights[k]).map(([k, v]) => [k, v, v * d.weights[k] * 100])
    .sort((a, b) => b[2] - a[2]).map(([k, v, c]) => `<div class="factor"><span>${esc(d.factor_labels[k] || k)}</span>
      <div class="share"><span style="width:${Math.round(v * 100)}%"></span></div><b>+${c.toFixed(1)}</b></div>`).join("");
  const points = (s.points || []).map(([label, v]) => `<div class="factor"><span>${esc(label)}</span>
      <div class="share"><span style="width:${Math.round(Math.min(v / 50, 1) * 100)}%"></span></div><b>+${Number(v).toFixed(1)}</b></div>`).join("");
  const sub = [`ИНН ${esc(s.inn)}`];
  if (s.city) sub.push(esc(s.city));
  if (s.okved) sub.push(`ОКВЭД ${esc(s.okved.code)}${s.okved.name ? " · " + esc(s.okved.name) : ""}`);
  const contacts = [];
  if (s.phone) contacts.push(`<a href="tel:${esc(s.phone)}">☎ ${esc(s.phone)}</a>`);
  if (s.email) contacts.push(`<a href="mailto:${esc(s.email)}">✉ ${esc(s.email)}</a>`);
  const stats = st ? `<div class="stats">
      <span>Закупок <b>${st.n_lots}</b></span>
      ${st.n_eshop ? `<span title="В Электронном магазине видны все участники, поэтому доля побед считается только по нему">ЭМ: побед <b>${st.n_eshop_wins ?? "—"}</b> из ${st.n_eshop}${st.n_eshop_wins != null ? ` (${Math.round(st.n_eshop_wins / st.n_eshop * 100)}%)` : ""}</span>` : ""}
      ${st.n_aisgz ? `<span title="По АИС ГЗ в данных есть только победители процедур">АИС ГЗ: победитель в <b>${st.n_aisgz}</b></span>` : ""}
      <span>Заказчиков <b>${st.n_customers}</b></span>

      <span>Последняя <b>${fmtDate(st.last_date)}</b></span>
    </div>` : "";
  const extra = [];
  if (s.msp_category) extra.push(`${esc(s.msp_category)} предприятие`);
  if (s.source === "external" && s.region_name && !/Петербург|Ленинград/.test(s.region_name)) extra.push(esc(s.region_name));
  if (s.employees) extra.push(`${s.employees} сотр.`);
  const reasons = (s.reasons || []).map((r) => `<li${/^Внимание/.test(r) ? ' class="alert"' : ""}>${esc(r)}</li>`).join("");
  const evidence = (s.evidence || []).length ? `
    <details class="evidence"><summary>Примеры похожих закупок этого поставщика (${s.evidence.length})</summary>
      ${s.evidence.map((e) => `<div class="lot"><div>${e.same_customer ? '<span class="won">ваш заказчик · </span>' : ""}${esc(e.subject)}</div>
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
          <span class="badge" title="${esc(roleTitle)}">${esc(s.role_label || "Роль не определена")}</span>
          ${s.is_active === false ? `<span class="badge b-bad" title="Ликвидирован ${esc(s.liquidated || "")}">Ликвидирован</span>` : ""}
          ${extra.length ? `<span class="badge">${extra.join(", ")}</span>` : ""}
        </div>
        <div class="checks">${checks(s)}</div>
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
      ${factors || points ? `<div class="factors"><h4>Из чего сложился балл</h4>${factors || points}
        ${s.penalty ? `<div class="factor"><span class="chk bad">${esc(s.penalty.label)}</span><div></div><b class="chk bad">${Number(s.penalty.points).toFixed(1)}</b></div>` : ""}
        ${points ? `<p class="note">У новых компаний балл не выше 80: истории закупок нет.</p>` : ""}</div>` : ""}
    </div>
  </article>`;
}

async function openSupplier(inn) {
  $("#modal-body").innerHTML = `<div class="spinner"></div>`;
  $("#modal").classList.remove("hidden");
  try {
    const codes = ((state.data || {}).okpd2 || []).map((o) => o.code).join(",");
    const s = await (await fetch("/api/supplier/" + inn + (codes ? "?okpd=" + encodeURIComponent(codes) : ""))).json();
    const lotRows = (lots) => lots.map((l) => `<tr><td>${fmtDate(l.date)}</td><td>${esc(l.subject)}</td><td>${esc(l.platform)}</td><td>${fmtMoney(l.price)}</td>
      <td>${l.is_winner ? `<span class="won">победа</span>${l.n_bidders === 1 ? " (единственный участник)" : ""}` : "участие"}</td></tr>`).join("");
    const rnp = s.rnp ? (s.rnp.in_rnp ? "состоит в реестре" : s.rnp.was_in_rnp ? "был в реестре, исключён" : "записей нет") : null;
    const kv = [
      ["ИНН", s.inn], ["ОГРН", s.ogrn], ["Полное название", s.full_name !== s.name ? s.full_name : null],
      ["Роль", s.role_label ? `${s.role_label}${s.confidence_label ? " — уверенность " + s.confidence_label : ""}` : null],
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
      ${s.relevant_lots && s.relevant_lots.length ? `<h3>Закупки по категории текущей закупки</h3><div class="table-scroll"><table><tr><th>Дата</th><th>Предмет</th><th>Площадка</th><th>НМЦ</th><th>Итог</th></tr>
        ${lotRows(s.relevant_lots)}</table></div>` : ""}
      ${s.okpd2 && s.okpd2.length ? `<h3>Опыт по кодам ОКПД2</h3><div class="table-scroll"><table><tr><th>Код</th><th>Название по классификатору</th><th>Закупок</th><th>Побед</th><th>Последняя</th></tr>
        ${s.okpd2.map((o) => `<tr${o.relevant ? ' class="rel"' : ""}><td>${esc(o.code)}${o.relevant ? '<div class="won">эта закупка</div>' : ""}</td><td>${esc(o.official_name || o.name)}</td><td>${o.n_lots}</td><td>${o.n_wins}</td><td>${fmtDate(o.last_date)}</td></tr>`).join("")}</table></div>` : ""}
      ${s.recent_lots && s.recent_lots.length ? `<h3>Последние закупки (все категории)</h3><div class="table-scroll"><table><tr><th>Дата</th><th>Предмет</th><th>Площадка</th><th>НМЦ</th><th>Итог</th></tr>
        ${lotRows(s.recent_lots)}</table></div>` : ""}`;
  } catch (e) {
    $("#modal-body").innerHTML = `<span class="error-box">Не удалось загрузить карточку: ${esc(e.message)}</span>`;
  }
}
function closeModal() { $("#modal").classList.add("hidden"); }
