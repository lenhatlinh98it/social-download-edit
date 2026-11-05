"use strict";
/* ---------- Accounts (owner edition): one sheet per country, edited like a spreadsheet ----------
   doc = {sheets: [{name, columns: [..], rows: [[..], ..]}]}, saved whole to accounts.json after every change. */
const acc = { doc: null, sheet: 0, query: "", showPw: false, loaded: false, saving: Promise.resolve() };
const ACC_PW = /pass|password|mật khẩu|mat khau|\bpw\b/i;
const ACC_X = '<svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg>';

async function loadAccounts() {
  if (!api) return;
  if (!acc.loaded) {
    const res = await api.accounts_load();
    if (!res.ok) { toast("error", "Accounts", res.error); return; }
    acc.doc = res.doc;
    acc.loaded = true;
  }
  renderAccounts();
}
window.loadAccounts = loadAccounts;

function accSheet() {
  return acc.doc.sheets[acc.sheet];
}

/* saves run one after another, so an older version can never overwrite a newer one */
function saveAccounts() {
  const snapshot = JSON.parse(JSON.stringify(acc.doc));
  acc.saving = acc.saving.then(async () => {
    const res = await api.accounts_save(snapshot);
    if (!res.ok) toast("error", "Accounts not saved", res.error);
  });
}

function accLinkify(escaped) {
  return escaped.replace(/(https?:\/\/[^\s<]+)/g, '<a data-url="$1">$1</a>');
}

function accCellHTML(value, column) {
  if (!value) return "";
  const shown = ACC_PW.test(column) && !acc.showPw ? '<span class="masked">••••••••</span>' : accLinkify(esc(value));
  return `${shown}<button class="acc-copy" type="button" data-copy>Copy</button>`;
}

function renderAccSheets() {
  $("#accSheets").innerHTML = acc.doc.sheets.map((s, i) =>
    `<button type="button" data-i="${i}" class="${i === acc.sheet ? "on" : ""}">${esc(s.name)} <span class="muted">${s.rows.length}</span></button>`).join("");
}

function renderAccounts() {
  if (!acc.doc) return;
  acc.sheet = Math.min(acc.sheet, acc.doc.sheets.length - 1);
  renderAccSheets();
  const sh = accSheet();
  const head = `<thead><tr><th class="acc-idx">#</th>${sh.columns.map((c, j) => `<th><div class="acc-head">
      <input data-col="${j}" value="${esc(c)}" spellcheck="false" aria-label="Column name">
      <button class="acc-x" type="button" data-del-col="${j}" title="Delete column">${ACC_X}</button></div></th>`).join("")}</tr></thead>`;
  const body = sh.rows.map((row, r) => `<tr data-r="${r}"><td class="acc-idx"><b>${r + 1}</b>
      <button class="acc-x" type="button" data-del-row="${r}" title="Delete row">${ACC_X}</button></td>${sh.columns.map((c, j) =>
        `<td><div class="acc-cell" data-r="${r}" data-c="${j}">${accCellHTML(row[j] || "", c)}</div></td>`).join("")}</tr>`).join("");
  $("#accTable").innerHTML = head + `<tbody>${body}</tbody>`;
  if (!sh.rows.length) {
    $("#accTable tbody").innerHTML = `<tr><td class="acc-idx"></td><td colspan="${Math.max(1, sh.columns.length)}">
      <div class="tt-empty">No accounts in ${esc(sh.name)} yet — press “+ Row”, or click here and paste rows copied from Google Sheets.</div></td></tr>`;
  }
  filterAccounts();
}

function filterAccounts() {
  const sh = accSheet();
  const q = acc.query.trim().toLowerCase();
  let shown = 0;
  $$("#accTable tbody tr[data-r]").forEach((tr) => {
    const hit = !q || sh.rows[Number(tr.dataset.r)].some((v) => (v || "").toLowerCase().includes(q));
    tr.classList.toggle("hidden", !hit);
    shown += hit;
  });
  $("#accCount").textContent = q ? `${shown} / ${sh.rows.length} accounts` : `${sh.rows.length} account${sh.rows.length === 1 ? "" : "s"}`;
}

/* ---------- editing a cell ---------- */
function accCellEl(r, c) {
  return $(`#accTable .acc-cell[data-r="${r}"][data-c="${c}"]`);
}

function startEdit(r, c) {
  const sh = accSheet();
  const cell = accCellEl(r, c);
  if (!cell || cell.querySelector("textarea")) return;
  const ta = document.createElement("textarea");
  ta.value = sh.rows[r][c] || "";
  ta.rows = Math.max(1, ta.value.split("\n").length);
  ta.spellcheck = false;
  cell.innerHTML = "";
  cell.append(ta);
  ta.focus({ preventScroll: true });  // a plain focus() also scrolls the whole window, hiding the top bar
  const wrap = $("#accWrap").getBoundingClientRect(), box = cell.getBoundingClientRect();
  if (box.bottom > wrap.bottom) $("#accWrap").scrollTop += box.bottom - wrap.bottom + 8;
  if (box.top < wrap.top + 40) $("#accWrap").scrollTop -= wrap.top + 40 - box.top;
  ta.setSelectionRange(ta.value.length, ta.value.length);
  let done = false;
  const finish = (save, next) => {
    if (done) return;
    done = true;
    if (save && ta.value !== (sh.rows[r][c] || "")) {
      sh.rows[r][c] = ta.value;
      saveAccounts();
    }
    cell.innerHTML = accCellHTML(sh.rows[r][c] || "", sh.columns[c]);
    filterAccounts();
    if (next) startEdit(...next);
  };
  ta.addEventListener("blur", () => finish(true));
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); finish(false); }
    else if (e.key === "Enter" && (e.altKey || e.shiftKey || e.ctrlKey)) {  // a new line inside the cell
      e.preventDefault();
      const at = ta.selectionStart;
      ta.value = ta.value.slice(0, at) + "\n" + ta.value.slice(ta.selectionEnd);
      ta.setSelectionRange(at + 1, at + 1);
      ta.rows = ta.value.split("\n").length;
    } else if (e.key === "Enter") {
      e.preventDefault();
      finish(true, r + 1 < sh.rows.length ? [r + 1, c] : null);
    } else if (e.key === "Tab") {
      e.preventDefault();
      const step = e.shiftKey ? -1 : 1;
      let nr = r, nc = c + step;
      if (nc >= sh.columns.length) { nc = 0; nr += 1; }
      if (nc < 0) { nc = sh.columns.length - 1; nr -= 1; }
      finish(true, nr >= 0 && nr < sh.rows.length ? [nr, nc] : null);
    }
  });
  ta.addEventListener("paste", (e) => {
    const grid = parseTSV(e.clipboardData.getData("text/plain"));
    if (grid.length < 2 && (grid[0] || []).length < 2) return;  // one value: a normal paste
    e.preventDefault();
    done = true;
    pasteGrid(r, c, grid);
  });
}

/* Google Sheets copies cells as tab-separated text; a cell with line breaks is quoted ("a\nb", "" = ") */
function parseTSV(text) {
  text = (text || "").replace(/\r\n?/g, "\n");
  const rows = [];
  let row = [], field = "", quoted = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (quoted) {
      if (ch === '"' && text[i + 1] === '"') { field += '"'; i++; }
      else if (ch === '"') quoted = false;
      else field += ch;
    } else if (ch === '"' && field === "") quoted = true;
    else if (ch === "\t") { row.push(field); field = ""; }
    else if (ch === "\n") { row.push(field); rows.push(row); row = []; field = ""; }
    else field += ch;
  }
  if (field !== "" || row.length) { row.push(field); rows.push(row); }
  return rows;
}

function pasteGrid(r, c, grid) {
  const sh = accSheet();
  const width = Math.max(...grid.map((g) => g.length));
  while (sh.columns.length < c + width) {
    sh.columns.push(`Column ${sh.columns.length + 1}`);
    sh.rows.forEach((row) => row.push(""));
  }
  grid.forEach((values, i) => {
    while (sh.rows.length <= r + i) sh.rows.push(sh.columns.map(() => ""));
    values.forEach((v, j) => { sh.rows[r + i][c + j] = v; });
  });
  saveAccounts();
  renderAccounts();
  toast("success", `Pasted ${grid.length} row${grid.length === 1 ? "" : "s"}`, `${width} column${width === 1 ? "" : "s"} from cell ${r + 1}, ${sh.columns[c]}`);
}

/* ---------- table clicks: edit, copy, open links, delete ---------- */
$("#accTable").addEventListener("click", (e) => {
  const sh = accSheet();
  const link = e.target.closest("a[data-url]");
  if (link) { api.accounts_open_link(link.dataset.url); return; }
  const copy = e.target.closest("[data-copy]");
  if (copy) {
    const cell = copy.closest(".acc-cell");
    const c = Number(cell.dataset.c);
    copyKeyText(sh.rows[Number(cell.dataset.r)][c], `${sh.columns[c] || "Value"} copied`);
    return;
  }
  const delRow = e.target.closest("[data-del-row]");
  if (delRow) {
    confirmButton(delRow, "Delete?", () => {
      sh.rows.splice(Number(delRow.dataset.delRow), 1);
      saveAccounts();
      renderAccounts();
    });
    return;
  }
  const delCol = e.target.closest("[data-del-col]");
  if (delCol) {
    confirmButton(delCol, "Delete?", () => {
      const j = Number(delCol.dataset.delCol);
      sh.columns.splice(j, 1);
      sh.rows.forEach((row) => row.splice(j, 1));
      saveAccounts();
      renderAccounts();
    });
    return;
  }
  const cell = e.target.closest(".acc-cell");
  if (cell) { startEdit(Number(cell.dataset.r), Number(cell.dataset.c)); return; }
  if (e.target.closest(".tt-empty")) {  // empty sheet: a first row to type or paste into
    sh.rows.push(sh.columns.map(() => ""));
    renderAccounts();
    startEdit(0, 0);
  }
});

/* column names */
$("#accTable").addEventListener("change", (e) => {
  const input = e.target.closest("input[data-col]");
  if (!input) return;
  const sh = accSheet();
  const j = Number(input.dataset.col);
  sh.columns[j] = input.value.trim() || `Column ${j + 1}`;
  saveAccounts();
  renderAccounts();
});
$("#accTable").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && e.target.matches("input[data-col]")) e.target.blur();
});

/* ---------- toolbar ---------- */
$("#accAddRow").addEventListener("click", () => {
  const sh = accSheet();
  sh.rows.push(sh.columns.map(() => ""));
  acc.query = $("#accSearch").value = "";
  saveAccounts();
  renderAccounts();
  $("#accWrap").scrollTop = $("#accWrap").scrollHeight;
  if (sh.columns.length) startEdit(sh.rows.length - 1, 0);
});
$("#accAddCol").addEventListener("click", () => {
  const sh = accSheet();
  sh.columns.push(`Column ${sh.columns.length + 1}`);
  sh.rows.forEach((row) => row.push(""));
  saveAccounts();
  renderAccounts();
  const input = $(`#accTable input[data-col="${sh.columns.length - 1}"]`);
  $("#accWrap").scrollLeft = $("#accWrap").scrollWidth;
  input.focus({ preventScroll: true });
  input.select();
});
$("#accSearch").addEventListener("input", (e) => { acc.query = e.target.value; filterAccounts(); });
$("#accShowPw").addEventListener("change", (e) => { acc.showPw = e.target.checked; renderAccounts(); });

/* ---------- sheets (countries) ---------- */
$("#accSheets").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-i]");
  if (!b) return;
  acc.sheet = Number(b.dataset.i);
  acc.query = $("#accSearch").value = "";
  renderAccounts();
});

function renameSheet(i) {
  const btn = $(`#accSheets button[data-i="${i}"]`);
  if (!btn) return;
  const input = document.createElement("input");
  input.value = acc.doc.sheets[i].name;
  input.spellcheck = false;
  input.setAttribute("aria-label", "Sheet name");
  btn.replaceWith(input);
  input.focus({ preventScroll: true });
  input.select();
  let done = false;
  const finish = (save) => {
    if (done) return;
    done = true;
    const name = input.value.trim().slice(0, 40);
    if (save && name && name !== acc.doc.sheets[i].name) {
      acc.doc.sheets[i].name = name;
      saveAccounts();
    }
    renderAccSheets();
  };
  input.addEventListener("blur", () => finish(true));
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); finish(true); }
    if (e.key === "Escape") { e.preventDefault(); finish(false); }
  });
}

$("#accSheets").addEventListener("dblclick", (e) => {
  const b = e.target.closest("button[data-i]");
  if (b) renameSheet(Number(b.dataset.i));
});
$("#accRenameSheet").addEventListener("click", () => renameSheet(acc.sheet));
$("#accAddSheet").addEventListener("click", () => {
  const columns = [...accSheet().columns];  // same columns as the sheet in view
  acc.doc.sheets.push({ name: `Sheet ${acc.doc.sheets.length + 1}`, columns, rows: [] });
  acc.sheet = acc.doc.sheets.length - 1;
  saveAccounts();
  renderAccounts();
  renameSheet(acc.sheet);
});
$("#accDeleteSheet").addEventListener("click", (e) => {
  if (acc.doc.sheets.length < 2) { toast("info", "Keep at least one sheet", "Add another sheet before deleting this one."); return; }
  const sh = accSheet();
  confirmButton(e.currentTarget, `Delete ${sh.name} (${sh.rows.length} rows)?`, () => {
    acc.doc.sheets.splice(acc.sheet, 1);
    acc.sheet = Math.max(0, acc.sheet - 1);
    saveAccounts();
    renderAccounts();
  });
});
