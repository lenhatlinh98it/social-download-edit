"use strict";

/* ---------- Keys (owner edition): make license keys for buyers' Machine IDs ---------- */
const keyTab = { state: null };

async function loadKeys() {
  if (!api) return;
  keyTab.state = await api.keys_state();
  renderKeys();
}
window.loadKeys = loadKeys;

function renderKeys() {
  const st = keyTab.state;
  if (!st || !st.available) return;
  const status = $("#keyStatus");
  if (!st.hasKey) {
    status.innerHTML = `No signing key yet. <button class="btn primary sm" id="keyInit" type="button">Create signing key</button>
      <div class="muted">It is saved in <span class="mono">${esc(st.path)}</span> — back it up and never share it.</div>`;
    $("#keyInit").addEventListener("click", async () => {
      const res = await api.keys_init();
      keyTab.state = res;
      renderKeys();
      if (!res.matches) toast("info", "Signing key created", "Put its public key into license.py (PUBLIC_KEY) before building the commercial app.");
    });
  } else if (!st.matches) {
    status.innerHTML = `The signing key in <span class="mono">${esc(st.path)}</span> does not match <span class="mono">PUBLIC_KEY</span> in license.py,
      so the app would reject its keys. Public key of this signing key: <span class="mono">${esc(st.publicKey)}</span>`;
  } else {
    status.innerHTML = `Signing key OK (<span class="mono">${esc(st.path)}</span>) — back it up. This computer's Machine ID: <span class="mono">${esc(st.machine)}</span>`;
  }
  $("#keyForm button[type=submit]").disabled = !st.hasKey || !st.matches;
  $("#keyCount").textContent = `${st.issued.length} key${st.issued.length === 1 ? "" : "s"} — saved in issued.csv next to the signing key`;
  $("#keyList").innerHTML = st.issued.length ? st.issued.map((k, i) => `<div class="key-row">
      <span class="mono">${esc(k.date)}</span>
      <span class="mono">${esc(k.machine_id)}</span>
      <span class="note" title="${esc(k.note)}">${esc(k.note || "—")} <span class="muted">· ${esc(k.days === "lifetime" ? "lifetime" : `${k.days} days`)}</span></span>
      <button class="btn ghost sm" data-i="${i}" type="button">Copy key</button>
    </div>`).join("") : '<div class="tt-empty">No keys issued yet.</div>';
}

async function copyKeyText(text, label) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.append(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
  toast("success", label);
}

$("#keyForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const machine = $("#keyMachine").value.trim();
  if (!machine) { toast("error", "Paste the buyer's Machine ID"); return; }
  const res = await api.keys_issue(machine, Number($("#keyDays").value), $("#keyNote").value);
  if (!res.ok) { toast("error", "Key not created", res.error); return; }
  $("#keyResultMachine").textContent = machine.toUpperCase();
  $("#keyResultText").value = res.key;
  $("#keyResult").hidden = false;
  keyTab.state.issued = res.issued;
  renderKeys();
  copyKeyText(res.key, "Key created and copied");
  $("#keyMachine").value = "";
  $("#keyNote").value = "";
});
$("#keyCopy").addEventListener("click", () => copyKeyText($("#keyResultText").value, "Key copied"));
$("#keyList").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-i]");
  if (b) copyKeyText(keyTab.state.issued[Number(b.dataset.i)].key, "Key copied");
});
