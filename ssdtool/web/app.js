"use strict";

// ---------------------------------------------------------------- basics
const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const token = (() => {
  const m = location.hash.match(/token=([\w-]+)/);
  if (m) {
    try { sessionStorage.setItem("ssdtool-token", m[1]); } catch (e) { /* ignore */ }
    history.replaceState(null, "", location.pathname);
    return m[1];
  }
  try { return sessionStorage.getItem("ssdtool-token") || ""; } catch (e) { return ""; }
})();

async function api(method, path, body) {
  const res = await fetch("/api/" + path, {
    method,
    headers: { "X-Token": token, "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = {};
  try { data = await res.json(); } catch (e) { /* ignore */ }
  if (!res.ok) throw new Error(data.error || `Fehler ${res.status}`);
  return data;
}

function fmtSize(n) {
  if (n == null) return "";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i === 0 ? n : n.toFixed(n < 10 ? 1 : 0)).toLocaleString("de-DE") + " " + u[i];
}
function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d) ? "" : d.toLocaleString("de-DE", { dateStyle: "short", timeStyle: "short" });
}
const fmtNum = (n) => Number(n || 0).toLocaleString("de-DE");

const STATUS = {
  good: ["Gut", "Daten sind noch vorhanden"],
  partial: ["Teilweise", "Teile wurden von anderen Dateien überschrieben"],
  overwritten: ["Überschrieben", "Speicherbereich gehört inzwischen anderen Dateien"],
  trimmed: ["TRIM", "Die SSD hat die Blöcke bereits gelöscht (liest nur Nullen)"],
  encrypted: ["Verschlüsselt", "EFS-verschlüsselt – Inhalt nicht lesbar"],
  damaged: ["Unvollständig", "Verwaltungsdaten sind unvollständig"],
  empty: ["Leer", "Datei hatte 0 Byte"],
};
const ICONS = { image: "🖼", video: "🎞", audio: "♪", document: "📄", archive: "🗜", program: "⚙", other: "•" };
const badge = (s) => `<span class="badge ${s}" title="${esc(STATUS[s]?.[1] || "")}">${esc(STATUS[s]?.[0] || s)}</span>`;

function toast(msg, ms = 4000) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add("hidden"), ms);
}
function show(el) { el.classList.remove("hidden"); }
function hide(el) { el.classList.add("hidden"); }

document.addEventListener("click", (ev) => {
  const closer = ev.target.closest("[data-close]");
  if (closer) hide(closer.closest(".overlay"));
});
document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") document.querySelectorAll(".overlay:not(#job)").forEach(hide);
});

// ---------------------------------------------------------------- state
const state = {
  info: null,
  drive: null,        // selected drive object
  scanId: null,
  scanSource: null,
  q: "", status: "", category: "",
  sort: "path", dir: "asc",
  offset: 0, limit: 200, total: 0,
  items: [],
  selected: new Set(),
  active: null,
};

// ---------------------------------------------------------------- info / drives
async function loadInfo() {
  try {
    state.info = await api("GET", "info");
  } catch (e) {
    $("#drives").innerHTML = `<p class="error">${esc(e.message)}</p>`;
    return;
  }
  const info = state.info;
  document.querySelectorAll(".confirm-word").forEach((el) => (el.textContent = info.confirmWord));
  const chips = [];
  chips.push(info.admin ? `<span class="chip ok">Administrator ✓</span>` : `<span class="chip bad">Kein Administrator</span>`);
  if (info.trim === true) chips.push(`<span class="chip" title="Windows sendet TRIM-Befehle an SSDs">TRIM aktiv</span>`);
  if (info.trim === false) chips.push(`<span class="chip ok" title="TRIM ist deaktiviert – bessere Chancen zur Wiederherstellung">TRIM deaktiviert</span>`);
  chips.push(`<span class="chip">v${esc(info.version)}</span>`);
  $("#status-chips").innerHTML = chips.join("");
  if (info.windows && !info.admin) show($("#admin-banner"));
  renderDrives();
}

function renderDrives() {
  const drives = state.info.drives || [];
  if (!drives.length) {
    $("#drives").innerHTML = `<p class="muted small">${state.info.windows ? "Keine Laufwerke gefunden." : "Laufwerksliste nur unter Windows – unten eine Abbild-Datei angeben."}</p>`;
    if (!state.info.windows) $(".image-src").open = true;
    return;
  }
  $("#drives").innerHTML = drives.map((d, i) => {
    const ntfs = d.filesystem.toUpperCase() === "NTFS";
    const used = d.total ? Math.round(100 * (d.total - d.free) / d.total) : 0;
    const media = d.media === "SSD" ? `<span class="tag ssd">SSD</span>` : d.media === "HDD" ? `<span class="tag">HDD</span>` : "";
    return `<button class="drive ${ntfs ? "" : "disabled"}" data-i="${i}" ${ntfs ? "" : `title="Nur NTFS wird unterstützt"`}>
      <span class="letter">${esc(d.letter)}:</span>
      <span class="name">${esc(d.label || (d.removable ? "Wechseldatenträger" : "Lokaler Datenträger"))}</span>
      <span class="meta">${fmtSize(d.free)} frei von ${fmtSize(d.total)}</span>
      <span class="tags">${media}<span class="tag">${esc(d.filesystem)}</span>${d.system ? `<span class="tag sys">System</span>` : ""}${d.bus ? `<span class="tag">${esc(d.bus)}</span>` : ""}</span>
      <span class="bar"><i style="width:${used}%"></i></span>
    </button>`;
  }).join("");
  document.querySelectorAll(".drive").forEach((el) => el.addEventListener("click", () => {
    const d = drives[+el.dataset.i];
    if (d.filesystem.toUpperCase() !== "NTFS") return toast(`${d.letter}: verwendet ${d.filesystem} – unterstützt wird nur NTFS.`);
    state.drive = d;
    $("#image-path").value = "";
    document.querySelectorAll(".drive").forEach((x) => x.classList.toggle("selected", x === el));
    updateSourceButtons();
  }));
  if (state.drive) {  // keep the selection after a refresh
    const i = drives.findIndex((d) => d.letter === state.drive.letter);
    state.drive = i >= 0 ? drives[i] : null;
    if (i >= 0) document.querySelector(`.drive[data-i="${i}"]`).classList.add("selected");
    updateSourceButtons();
  }
}

function currentSource() {
  const img = $("#image-path").value.trim();
  if (img) return img;
  return state.drive ? state.drive.letter + ":" : "";
}
function updateSourceButtons() {
  $("#btn-scan").disabled = !currentSource();
  $("#btn-wipe-free").disabled = !state.drive || !!$("#image-path").value.trim();
}
$("#image-path").addEventListener("input", () => {
  if ($("#image-path").value.trim()) {
    state.drive = null;
    document.querySelectorAll(".drive").forEach((x) => x.classList.remove("selected"));
  }
  updateSourceButtons();
});

$("#btn-elevate").addEventListener("click", async () => {
  try {
    const r = await api("POST", "elevate");
    if (r.ok) toast("Das Tool startet mit Administratorrechten neu – dieses Fenster kann geschlossen werden.", 10000);
    else toast("Neustart als Administrator wurde abgebrochen.");
  } catch (e) { toast(e.message); }
});

// ---------------------------------------------------------------- jobs
const JOB_TITLES = {
  scan: "Laufwerk wird durchsucht …",
  recover: "Dateien werden wiederhergestellt …",
  shred: "Dateien werden endgültig gelöscht …",
  wipe_free: "Freier Speicher wird bereinigt …",
};

function runJob(job) {
  return new Promise((resolve, reject) => {
    $("#job-title").textContent = JOB_TITLES[job.kind] || "Bitte warten …";
    $("#job-bar").style.width = "0%";
    $("#job-msg").textContent = job.message || "";
    $("#job-cancel").disabled = false;
    show($("#job"));
    $("#job-cancel").onclick = async () => {
      $("#job-cancel").disabled = true;
      $("#job-msg").textContent = "Wird abgebrochen …";
      try { await api("POST", `jobs/${job.id}/cancel`); } catch (e) { /* ignore */ }
    };
    const poll = async () => {
      let j;
      try { j = await api("GET", `jobs/${job.id}`); } catch (e) { hide($("#job")); return reject(e); }
      $("#job-bar").style.width = (j.progress * 100).toFixed(1) + "%";
      $("#job-msg").textContent = j.message;
      if (j.status === "running") return setTimeout(poll, 400);
      hide($("#job"));
      if (j.status === "done") resolve(j);
      else if (j.status === "cancelled") reject(new Error("Vorgang abgebrochen."));
      else reject(new Error(j.error || "Unbekannter Fehler"));
    };
    poll();
  });
}

function showResult(title, msg, files, folder) {
  $("#res-title").textContent = title;
  $("#res-msg").textContent = msg;
  $("#res-list").innerHTML = (files || []).map((f) =>
    `<div class="${f.ok ? "ok" : "fail"}">${esc(f.name)}${f.target ? ` → ${esc(f.target)}` : ""}${f.message ? `<small>${esc(f.message)}</small>` : ""}</div>`
  ).join("");
  $("#res-list").classList.toggle("hidden", !files || !files.length);
  const open = $("#res-open");
  if (folder && state.info?.windows) {
    show(open);
    open.onclick = () => api("POST", "open-folder", { path: folder }).catch(() => {});
  } else hide(open);
  show($("#dlg-result"));
}

// ---------------------------------------------------------------- scan
$("#btn-scan").addEventListener("click", async () => {
  const source = currentSource();
  if (!source) return;
  try {
    const job = await api("POST", "scan", { source, check_trim: $("#check-trim").checked });
    const done = await runJob(job);
    state.scanId = done.result.scan;
    state.scanSource = source;
    state.selected.clear();
    state.offset = 0;
    state.active = null;
    hide($("#detail"));
    hide($("#empty-state"));
    show($("#results"));
    await loadFiles();
  } catch (e) {
    toast(e.message, 8000);
  }
});

async function loadFiles() {
  if (!state.scanId) return;
  const params = new URLSearchParams({
    q: state.q, status: state.status, category: state.category,
    sort: state.sort, dir: state.dir, offset: state.offset, limit: state.limit,
  });
  let data;
  try {
    data = await api("GET", `scans/${state.scanId}/files?${params}`);
  } catch (e) { return toast(e.message); }
  state.items = data.items;
  state.total = data.total;
  renderSummary(data.summary, data.remaining);
  renderTable();
}

function renderSummary(s, remaining) {
  if (!s) return;
  const by = s.by_status || {};
  const stats = [
    ["", fmtNum(remaining), `gelöschte Dateien · ${esc(state.scanSource.split(/[\\/]/).filter(Boolean).pop() || state.scanSource)}`],
    ["good", fmtNum(by.good), "gut wiederherstellbar"],
    ["partial", fmtNum((by.partial || 0) + (by.damaged || 0)), "teilweise / unvollständig"],
    ["trimmed", fmtNum(by.trimmed), "per TRIM gelöscht"],
    ["overwritten", fmtNum(by.overwritten), "überschrieben"],
  ];
  $("#summary").innerHTML = stats.map(([k, v, l]) => `<div class="stat" data-status="${k}"><b>${v}</b><span>${l}</span></div>`).join("");
  document.querySelectorAll(".stat").forEach((el) => el.addEventListener("click", () => {
    state.status = el.dataset.status;
    $("#f-status").value = state.status;
    state.offset = 0;
    loadFiles();
  }));
}

function renderTable() {
  const rows = state.items.map((e) => {
    const sel = state.selected.has(e.id);
    return `<tr data-id="${e.id}" class="${sel ? "sel" : ""} ${state.active === e.id ? "active" : ""}">
      <td class="c-check"><input type="checkbox" ${sel ? "checked" : ""}></td>
      <td title="${esc(e.name)}"><span class="fname"><span class="ficon">${ICONS[e.category] || "•"}</span>${esc(e.name)}</span></td>
      <td class="path" title="${esc(e.path)}">${esc(e.path)}</td>
      <td class="num">${fmtSize(e.size)}</td>
      <td>${fmtDate(e.modified)}</td>
      <td>${badge(e.status)}${e.status === "partial" ? ` <span class="muted small">${e.overwritten_pct}%</span>` : ""}</td>
    </tr>`;
  });
  $("#tbody").innerHTML = rows.join("") || `<tr><td colspan="6" class="muted" style="text-align:center;padding:30px">Keine Treffer</td></tr>`;
  const from = state.total ? state.offset + 1 : 0;
  const to = Math.min(state.offset + state.limit, state.total);
  $("#page-info").textContent = `${fmtNum(from)}–${fmtNum(to)} von ${fmtNum(state.total)}`;
  $("#prev").disabled = state.offset === 0;
  $("#next").disabled = to >= state.total;
  document.querySelectorAll("thead th[data-sort]").forEach((th) => {
    th.classList.toggle("sorted", th.dataset.sort === state.sort);
    th.classList.toggle("desc", th.dataset.sort === state.sort && state.dir === "desc");
  });
  $("#check-page").checked = state.items.length > 0 && state.items.every((e) => state.selected.has(e.id));
  updateSelection();
}

function updateSelection() {
  const n = state.selected.size;
  $("#sel-info").textContent = n ? `${fmtNum(n)} Datei${n === 1 ? "" : "en"} ausgewählt` : "Keine Datei ausgewählt";
  $("#btn-recover").disabled = !n;
  $("#btn-shred").disabled = !n;
}

$("#tbody").addEventListener("click", (ev) => {
  const tr = ev.target.closest("tr[data-id]");
  if (!tr) return;
  const id = +tr.dataset.id;
  if (ev.target.matches("input[type=checkbox]")) {
    if (ev.target.checked) state.selected.add(id); else state.selected.delete(id);
    tr.classList.toggle("sel", ev.target.checked);
    updateSelection();
    return;
  }
  showDetail(state.items.find((e) => e.id === id));
});
$("#check-page").addEventListener("change", (ev) => {
  state.items.forEach((e) => (ev.target.checked ? state.selected.add(e.id) : state.selected.delete(e.id)));
  renderTable();
});
$("#select-all-filtered").addEventListener("click", async () => {
  const params = new URLSearchParams({ q: state.q, status: state.status, category: state.category, all_ids: "1" });
  try {
    const r = await api("GET", `scans/${state.scanId}/files?${params}`);
    r.ids.forEach((id) => state.selected.add(id));
    renderTable();
  } catch (e) { toast(e.message); }
});
$("#select-none").addEventListener("click", () => { state.selected.clear(); renderTable(); });
$("#prev").addEventListener("click", () => { state.offset = Math.max(0, state.offset - state.limit); loadFiles(); });
$("#next").addEventListener("click", () => { state.offset += state.limit; loadFiles(); });
document.querySelectorAll("thead th[data-sort]").forEach((th) => th.addEventListener("click", () => {
  if (state.sort === th.dataset.sort) state.dir = state.dir === "asc" ? "desc" : "asc";
  else { state.sort = th.dataset.sort; state.dir = th.dataset.sort === "size" || th.dataset.sort === "modified" ? "desc" : "asc"; }
  state.offset = 0;
  loadFiles();
}));
let searchTimer;
$("#q").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { state.q = $("#q").value; state.offset = 0; loadFiles(); }, 250);
});
$("#f-status").addEventListener("change", () => { state.status = $("#f-status").value; state.offset = 0; loadFiles(); });
$("#f-category").addEventListener("change", () => { state.category = $("#f-category").value; state.offset = 0; loadFiles(); });

// ---------------------------------------------------------------- detail
async function showDetail(e) {
  if (!e) return;
  state.active = e.id;
  document.querySelectorAll("#tbody tr").forEach((tr) => tr.classList.toggle("active", +tr.dataset.id === e.id));
  show($("#detail"));
  $("#d-name").textContent = e.name;
  const meta = [
    ["Ordner", e.path + (e.orphan ? " (Ordner nicht mehr vorhanden)" : "")],
    ["Größe", `${fmtSize(e.size)} (${fmtNum(e.size)} Byte)`],
    ["Zustand", badge(e.status) + (e.overwritten_pct ? ` ${e.overwritten_pct}% überschrieben` : ""), true],
    ["Erstellt", fmtDate(e.created)],
    ["Geändert", fmtDate(e.modified)],
    ["Letzter Zugriff", fmtDate(e.accessed)],
    ["Speicherort", e.resident ? "Direkt im MFT-Eintrag" : "Datencluster" + (e.compressed ? " (NTFS-komprimiert)" : "")],
    ["MFT-Eintrag", "#" + e.id],
  ];
  $("#d-meta").innerHTML = meta.map(([k, v, html]) => `<dt>${k}</dt><dd>${html ? v : esc(v)}</dd>`).join("");
  const pv = $("#d-preview");
  if (["overwritten", "encrypted", "empty"].includes(e.status)) {
    pv.innerHTML = `<p class="muted small">Keine Vorschau – ${esc(STATUS[e.status][1])}.</p>`;
    return;
  }
  pv.innerHTML = `<p class="muted small">Vorschau wird geladen …</p>`;
  try {
    const p = await api("GET", `scans/${state.scanId}/preview/${e.id}`);
    if (state.active !== e.id) return;
    if (p.kind === "image") pv.innerHTML = `<img alt="Vorschau" src="data:${p.mime};base64,${p.data}">`;
    else if (p.kind === "text") pv.innerHTML = `<pre>${esc(p.text)}</pre>`;
    else pv.innerHTML = (p.zero ? `<p class="muted small">Der Inhalt besteht nur aus Nullbytes.</p>` : "") + `<pre>${esc(p.hex)}</pre>`;
  } catch (err) {
    pv.innerHTML = `<p class="error">${esc(err.message)}</p>`;
  }
}
$("#d-close").addEventListener("click", () => {
  hide($("#detail"));
  state.active = null;
  document.querySelectorAll("#tbody tr.active").forEach((tr) => tr.classList.remove("active"));
});

// ---------------------------------------------------------------- recover
$("#btn-recover").addEventListener("click", () => {
  const n = state.selected.size;
  $("#rec-count").textContent = `${fmtNum(n)} Datei${n === 1 ? "" : "en"} werden in den Zielordner kopiert.`;
  $("#rec-error").textContent = "";
  hide($("#rec-same-wrap"));
  $("#rec-same").checked = false;
  show($("#dlg-recover"));
  $("#rec-target").focus();
});
$("#rec-pick").addEventListener("click", async () => {
  try {
    const r = await api("POST", "pick-folder", { initial: $("#rec-target").value });
    if (r.path) $("#rec-target").value = r.path;
    else if (r.path === null && !state.info?.windows) toast("Ordnerauswahl nicht verfügbar – bitte Pfad eintippen.");
  } catch (e) { toast(e.message); }
});
$("#rec-go").addEventListener("click", async () => {
  $("#rec-error").textContent = "";
  const body = {
    scan: state.scanId,
    ids: [...state.selected],
    target: $("#rec-target").value,
    keep_paths: $("#rec-keep").checked,
    allow_same_drive: $("#rec-same").checked,
  };
  let job;
  try {
    job = await api("POST", "recover", body);
  } catch (e) {
    $("#rec-error").textContent = e.message;
    if (/demselben Laufwerk/.test(e.message)) show($("#rec-same-wrap"));
    return;
  }
  hide($("#dlg-recover"));
  try {
    const done = await runJob(job);
    showResult("Wiederherstellung abgeschlossen", done.message, done.result.files, done.result.target);
  } catch (e) { toast(e.message, 8000); }
});

// ---------------------------------------------------------------- shred
function bindConfirm(input, button) {
  input.addEventListener("input", () => { button.disabled = input.value.trim() !== state.info?.confirmWord; });
}
bindConfirm($("#shred-confirm"), $("#shred-go"));
bindConfirm($("#wipe-confirm"), $("#wipe-go"));

$("#btn-shred").addEventListener("click", () => {
  const n = state.selected.size;
  $("#shred-count").innerHTML = `<b>${fmtNum(n)} Datei${n === 1 ? "</b> wird" : "en</b> werden"} so vernichtet, dass sie mit keinem Programm mehr wiederhergestellt werden können.`;
  $("#shred-confirm").value = "";
  $("#shred-go").disabled = true;
  $("#shred-error").textContent = "";
  show($("#dlg-shred"));
  $("#shred-confirm").focus();
});
$("#shred-go").addEventListener("click", async () => {
  let job;
  try {
    job = await api("POST", "shred", {
      scan: state.scanId, ids: [...state.selected], passes: +$("#shred-passes").value,
      confirm: $("#shred-confirm").value.trim(), retrim: true,
    });
  } catch (e) { $("#shred-error").textContent = e.message; return; }
  hide($("#dlg-shred"));
  try {
    const done = await runJob(job);
    const okIds = done.result.files.filter((f) => f.ok).map((f) => f.id);
    okIds.forEach((id) => state.selected.delete(id));
    showResult("Endgültig gelöscht", done.message, done.result.files);
    await loadFiles();
  } catch (e) {
    showResult("Löschen nicht möglich", e.message, []);
  }
});

// ---------------------------------------------------------------- wipe free space
$("#btn-wipe-free").addEventListener("click", () => {
  if (!state.drive) return;
  $("#wipe-drive").textContent = `${state.drive.letter}: ${state.drive.label || ""} (${fmtSize(state.drive.free)} frei)`;
  $("#wipe-confirm").value = "";
  $("#wipe-go").disabled = true;
  $("#wipe-error").textContent = "";
  show($("#dlg-wipe"));
  $("#wipe-confirm").focus();
});
$("#wipe-go").addEventListener("click", async () => {
  let job;
  try {
    job = await api("POST", "wipe-free", {
      drive: state.drive.letter, mft: $("#wipe-mft").checked, retrim: $("#wipe-trim").checked,
      confirm: $("#wipe-confirm").value.trim(),
    });
  } catch (e) { $("#wipe-error").textContent = e.message; return; }
  hide($("#dlg-wipe"));
  try {
    const done = await runJob(job);
    const notes = [done.result.note, done.result.retrim].filter(Boolean).join("\n\n");
    showResult("Freier Speicher bereinigt", done.message + (notes ? "\n\n" + notes : ""), []);
    if (state.scanSource && state.scanSource[0] === state.drive.letter) {
      state.scanId = null;
      hide($("#results"));
      show($("#empty-state"));
      toast("Bitte das Laufwerk erneut scannen – die bisherigen Ergebnisse sind veraltet.", 8000);
    }
    loadInfo();
  } catch (e) { toast(e.message, 8000); }
});

loadInfo();
