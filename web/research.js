"use strict";

/* ---------- Research: hot niches -> promising Shorts channels -> not on TikTok yet ---------- */
const rs = { market: "US", state: null, niches: [], run: null, check: null, running: false };
const VERDICTS = {
  exists: { label: "On TikTok", cls: "bad" },
  likely: { label: "Likely on TikTok", cls: "bad" },
  suspect: { label: "Check suspects", cls: "warn" },
  reup: { label: "Only re-uploads", cls: "warn" },
  none: { label: "Not on TikTok", cls: "good" },
  error: { label: "Check failed", cls: "bad" },
};
const rsNum = (n) => (n == null ? "—" : Number(n).toLocaleString("en-US"));
const short = (n) => (n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${(n / 1e3).toFixed(n >= 1e4 ? 0 : 1)}K` : rsNum(n));

async function loadResearch() {
  if (!api) return;
  rs.state = await api.research_state(rs.market);
  rs.niches = rs.state.niches;
  rs.run = rs.state.last;
  rs.running = rs.state.running;
  $("#rsNicheList").innerHTML = rs.state.catalog.map((n) => `<option value="${esc(n.name)}">${esc(n.keyword)}</option>`).join("");
  renderResearch();
}

function renderResearch() {
  const st = rs.state;
  if (!st) return;
  $$("#rsMarket button").forEach((b) => b.classList.toggle("on", b.dataset.m === rs.market));
  $("#rsQuota").textContent = st.hasKey ? `API today: ${rsNum(st.quota)} / ${rsNum(st.quotaMax)}` : "No API key";
  $("#rsQuota").classList.toggle("warn", !st.hasKey || st.quota > st.quotaMax * 0.8);
  $("#rsNoKey").hidden = st.hasKey;
  ["#rsNichesBtn", "#rsFind", "#rsCheckBtn"].forEach((s) => ($(s).disabled = rs.running || !st.hasKey));
  $("#rsCancel").hidden = !rs.running;
  $("#rsProgress").classList.toggle("on", rs.running);

  const niches = rs.niches.slice(0, 5);
  $("#rsNiches").innerHTML = niches.length ? niches.map((n, i) => `
    <div class="rs-niche glass" data-id="${esc(n.id)}">
      <div class="rs-niche-head"><span class="rs-rank">#${i + 1}</span><b>${esc(n.name)}</b><span class="rs-score">${n.score}</span></div>
      <div class="rs-kw mono">“${esc(n.keyword)}”</div>
      <div class="rs-metrics">
        <span title="Median views per day of the top Shorts of the last 14 days"><b>${short(n.medianViewsPerDay)}</b> views/day</span>
        <span title="Shorts with 100K+ views in the last 14 days"><b>${n.hits}</b> hits ≥100K</span>
        <span title="Channels created in the last 24 months"><b>${n.newChannels}/${n.channels}</b> new channels</span>
      </div>
      <div class="rs-examples">${n.examples.map((v) => `<a href="#" data-url="https://www.youtube.com/shorts/${esc(v.id)}" title="${esc(v.title)}">${short(v.views)} · ${esc(v.channel)}</a>`).join("")}</div>
      <button class="btn primary sm rs-pick">Find channels</button>
    </div>`).join("")
    : `<div class="tt-empty">${st.hasKey ? "Press <b>Research niches</b> to score niches for this market (about 1,000 API units for 10 niches; results are kept 24 h)." : "Add your YouTube API key first."}</div>`;
  $("#rsNicheMore").textContent = rs.niches.length > 5 ? `${rs.niches.length} niches scored for ${rs.market} in the last 24 h — top 5 shown` : "";

  const box = $("#rsResults");
  const parts = [];
  if (rs.check) parts.push(`<h3 class="rs-h">Channel check</h3>${channelCard(rs.check, true)}`);
  if (rs.run) {
    const picks = rs.run.picks.map((id) => rs.run.channels.find((c) => c.id === id)).filter(Boolean);
    const others = rs.run.channels.filter((c) => !rs.run.picks.includes(c.id));
    parts.push(`<h3 class="rs-h">${esc(rs.run.niche)} · ${esc(rs.run.market)} <span class="muted">— ${rs.run.found} promising Shorts channels found, ${esc(rs.run.at)}</span></h3>`);
    parts.push(picks.length ? `<div class="rs-sub">Top ${picks.length} not on TikTok yet</div>${picks.map((c, i) => channelCard(c, true, i + 1)).join("")}`
      : '<div class="tt-empty">No checked channel is free on TikTok — try another niche.</div>');
    if (others.length) {
      parts.push(`<details class="rs-others"><summary>${others.length} other channels (on TikTok, or not checked)</summary>${others.map((c) => channelCard(c, false)).join("")}</details>`);
    }
  }
  box.innerHTML = parts.join("") || "";
}

function channelCard(c, open, rank) {
  const t = c.tiktok;
  const v = t ? VERDICTS[t.verdict] || VERDICTS.error : { label: "Not checked", cls: "" };
  const age = c.ageDays ? `${Math.round(c.ageDays / 30)} mo old` : (c.created || "").slice(0, 10);
  const cands = t ? [...(t.candidates || []), ...(t.lookalikes || []).map((x) => ({ ...x, verdict: "lookalike" }))] : [];
  return `<div class="rs-chan">
    <div class="rs-chan-head">
      ${rank ? `<span class="rs-rank">#${rank}</span>` : ""}
      <img src="${esc(c.avatar)}" referrerpolicy="no-referrer" alt="">
      <div class="rs-chan-info">
        <a href="#" class="rs-title" data-url="https://www.youtube.com/channel/${esc(c.id)}">${esc(c.title)}</a>
        <div class="rs-stats mono">${esc(c.handle || "")} · ${age} · ${short(c.subs)} subs${c.shortsShare != null ? ` · ${Math.round(c.shortsShare * 100)}% Shorts` : ""}${
          c.viewsPerDay != null ? ` · ${short(c.viewsPerDay)} views/day · avg ${short(c.avgRecentViews)}/Short · ${c.uploads30} uploads/30d` : ""}</div>
      </div>
      ${c.score != null ? `<span class="rs-score" title="Growth score">${c.score}</span>` : ""}
      <span class="rs-verdict ${v.cls}">${v.label}</span>
    </div>
    ${t?.error ? `<div class="tt-err">${esc(t.error)}</div>` : ""}
    ${cands.length ? `<details ${open && t.verdict !== "none" ? "open" : ""}><summary>TikTok accounts to verify (${cands.length})</summary>
      ${cands.map((x) => `<div class="rs-cand">
        <span class="rs-cv ${esc(x.verdict)}">${esc(x.verdict)}</span>
        <a href="#" data-url="${esc(x.url)}">@${esc(x.handle)}</a> <span class="muted">${esc(x.nickname || "")} · ${short(x.followers)} followers</span>
        <div class="rs-ev">${(x.evidence || []).map(esc).join(" · ")}${(x.examples || []).map((u, k) => ` <a href="#" data-url="${esc(u)}">video ${k + 1}</a>`).join("")}</div>
      </div>`).join("")}</details>` : t ? '<div class="rs-ev muted">No matching TikTok account found (YouTube links, handle, name search, title search).</div>' : ""}
  </div>`;
}

$("#rsMarket").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b || rs.running) return;
  rs.market = b.dataset.m;
  rs.check = null;
  loadResearch();
});
$("#rsNichesBtn").addEventListener("click", async () => {
  const res = await api.research_niches(rs.market, 10);
  if (!res.ok) { toast("error", "Cannot research", res.error); return; }
  rs.running = true;
  setRsProgress("Scoring niches…", null);
  renderResearch();
});
async function findChannels(niche) {
  const res = await api.research_channels(rs.market, niche);
  if (!res.ok) { toast("error", "Cannot research", res.error); return; }
  rs.running = true;
  rs.check = null;
  setRsProgress(`Finding channels for “${niche}”…`, null);
  renderResearch();
}
$("#rsFind").addEventListener("click", () => {
  const typed = $("#rsNiche").value.trim();
  if (!typed) { toast("error", "Type or pick a niche"); return; }
  const known = rs.state.catalog.find((n) => n.name.toLowerCase() === typed.toLowerCase());
  findChannels(known ? known.id : typed);
});
$("#rsNiche").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#rsFind").click(); });
$("#rsCheckBtn").addEventListener("click", async () => {
  const url = $("#rsCheckUrl").value.trim();
  if (!url) { toast("error", "Paste a YouTube channel link"); return; }
  const res = await api.research_check(url);
  if (!res.ok) { toast("error", "Cannot check", res.error); return; }
  rs.running = true;
  setRsProgress("Checking the channel on TikTok…", null);
  renderResearch();
});
$("#rsCheckUrl").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#rsCheckBtn").click(); });
$("#rsCancel").addEventListener("click", () => { api.research_cancel(); setRsProgress("Cancelling…", null); });
$("#rsNiches").addEventListener("click", (e) => {
  const pick = e.target.closest(".rs-pick");
  if (pick) { findChannels(pick.closest(".rs-niche").dataset.id); return; }
});
$("#view-research").addEventListener("click", (e) => {
  const a = e.target.closest("a[data-url]");
  if (a) { e.preventDefault(); api.research_open(a.dataset.url); }
});

function setRsProgress(text, value) {
  $("#rsProgressText").textContent = text;
  const bar = $("#rsProgress i");
  bar.parentElement.classList.toggle("indeterminate", value == null);
  if (value != null) bar.style.width = `${Math.round(value * 100)}%`;
}

Object.assign(handlers, {
  researchProgress: (e) => setRsProgress(e.text, e.value),
  researchQuota: (e) => { if (rs.state) { rs.state.quota = e.quota; renderResearch(); } },
  researchDone: (e) => {
    rs.running = false;
    if (e.cancelled) toast("info", "Research cancelled");
    else if (e.error) toast("error", "Research failed", e.error, { label: "Open logs", run: () => setView("logs") });
    else if (e.kind === "niches") { rs.niches = e.niches; toast("success", "Niches scored", `Top 5 for ${e.market} are shown`); }
    else if (e.kind === "channels") {
      rs.run = e.run;
      toast(e.run.picks.length ? "success" : "info", `${e.run.picks.length} channels not on TikTok yet`, `${e.run.niche} · ${e.run.market}`);
    } else if (e.kind === "check") {
      rs.check = e.channel;
      toast("success", `${e.channel.title}: ${(VERDICTS[e.channel.tiktok?.verdict] || VERDICTS.error).label}`);
    }
    setRsProgress("", 0);
    renderResearch();
  },
});
