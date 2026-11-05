// Sync between PCs (owner edition): encrypted copy of the data files in a private GitHub repo
const sy = { pending: null };

function syncMsg(text, kind = "") {
  $("#syncMsg").textContent = text;
  $("#syncMsg").className = `sync-msg ${kind}`;
}

function syncWhen(iso) {
  return iso ? new Date(iso).toLocaleString() : "never";
}

function syncBusy(on) {
  ["#syncUpload", "#syncFetch", "#syncSave"].forEach((s) => ($(s).disabled = on));
}

function syncReset() {
  sy.pending = null;
  $("#syncUpload").textContent = "Upload";
  $("#syncFetch").textContent = "Fetch";
}

async function openSync() {
  syncReset();
  syncMsg("");
  const st = await api.sync_state();
  const ready = st.repo && st.token && st.hasPass;
  $("#syncRepo").value = st.repo;
  $("#syncToken").value = "";
  $("#syncToken").placeholder = st.token ? `In use: ${st.token} — leave empty to keep it` : "Fine-grained token: only this repo, Contents read and write";
  $("#syncPass").value = "";
  $("#syncPass").placeholder = st.hasPass ? "Saved — leave empty to keep it" : "Same on both PCs, at least 8 characters";
  $("#syncSetup").open = !ready;
  $("#syncStatus").textContent = ready ? `${st.repo} · last sync on this PC: ${syncWhen(st.lastSync)}` : "Not set up yet.";
  $("#syncUpload").disabled = $("#syncFetch").disabled = !ready;
  $("#syncModal").hidden = false;
}

function closeSync() { $("#syncModal").hidden = true; }

$("#syncBtn").addEventListener("click", openSync);
$("#syncClose").addEventListener("click", closeSync);
$("#syncModal").addEventListener("mousedown", (e) => { if (e.target.id === "syncModal") closeSync(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("#syncModal").hidden) closeSync(); });

$("#syncSave").addEventListener("click", async () => {
  syncBusy(true);
  syncMsg("Checking the repo…");
  const res = await api.sync_setup($("#syncRepo").value, $("#syncToken").value, $("#syncPass").value);
  syncBusy(false);
  if (!res.ok) { syncMsg(res.error, "err"); return; }
  await openSync();
  syncMsg("Saved. Upload on the PC that has the newest data, then Fetch on the other.");
});

async function syncRun(kind) {
  const force = sy.pending === kind;
  syncReset();
  syncBusy(true);
  syncMsg(kind === "upload" ? "Encrypting and uploading…" : "Downloading and decrypting…");
  const res = kind === "upload" ? await api.sync_upload(force) : await api.sync_fetch(force);
  syncBusy(false);
  if (res.conflict) {
    sy.pending = kind;
    const who = `${res.meta.host} at ${syncWhen(res.meta.time)}`;
    if (kind === "upload") {
      syncMsg(`The cloud copy was uploaded by ${who}, after this PC last synced. Fetch it first, or replace it with this PC's data.`, "warn");
      $("#syncUpload").textContent = "Replace cloud copy";
    } else {
      syncMsg(`This PC has changes that were not uploaded. Fetching replaces them with the copy from ${who} (the old files are kept as *.before-sync).`, "warn");
      $("#syncFetch").textContent = "Replace this PC's data";
    }
    return;
  }
  if (!res.ok) { syncMsg(res.error, "err"); return; }
  if (kind === "upload") {
    toast("success", "Uploaded", `${res.files} data files are in the cloud.`);
    await openSync();
  } else if (res.upToDate) {
    syncMsg("Already up to date.");
  } else {
    toast("success", "Fetched", `Data from ${res.meta.host} — reloading…`);
    setTimeout(() => location.reload(), 600);
  }
}

$("#syncUpload").addEventListener("click", () => syncRun("upload"));
$("#syncFetch").addEventListener("click", () => syncRun("fetch"));
