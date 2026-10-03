const $ = (s, el = document) => el.querySelector(s);
const api = async (url, opts = {}) => {
  const r = await fetch(url, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!r.ok) {
    const detail = (await r.json().catch(() => ({}))).detail;
    // FastAPI sends a list of {loc, msg} when the request itself doesn't fit the endpoint.
    throw new Error(Array.isArray(detail) ? detail.map((d) => d.msg || JSON.stringify(d)).join("; ") : detail || r.statusText);
  }
  return r.json();
};
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtSize = (b) => {
  if (!b) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return `${b.toFixed(b < 10 && i ? 1 : 0)} ${u[i]}`;
};

// creator: null = all creators, "" = releases without a creator
// source: null = local library, "mmf" = the synced MyMiniFactory library (mmfMissing: only items not in it)
const state = { source: null, mmfMissing: false, release: null, creator: null, q: "", sup: "", tags: [], offset: 0, items: [], total: 0, model: null, releases: [] };
const PAGE = 120;

// ------------------------------------------------------------ releases & grid

const filterParams = () => {
  const p = new URLSearchParams({ q: state.q, tags: state.tags.join(",") });
  if (state.creator !== null) p.set("creator", state.creator);
  return p;
};

async function loadReleases() {
  const rels = await api(`/api/releases?${filterParams()}`);
  state.releases = rels;
  const ul = $("#releases");
  const total = rels.reduce((a, r) => a + r.models, 0);
  const by = (r) => state.creator === null && r.creator ? `<span class="by">${esc(r.creator)}</span>` : "";
  ul.innerHTML = `<li data-r="" class="${state.release === null && state.source !== "mmf" ? "active" : ""}"><span class="name">All releases</span><span class="muted">${total}</span></li>` +
    rels.map((r) => `<li data-r="${esc(r.release)}" class="${state.release === r.release ? "active" : ""}" title="${esc(r.creator ? `${r.release} by ${r.creator}` : r.release)}">
      <span class="name">${esc(r.release)}${by(r)}</span><span class="muted">${r.models}</span></li>`).join("");
  $("#releaseList").innerHTML = rels.map((r) => `<option value="${esc(r.release)}">`).join("");
}

$("#releases").addEventListener("click", (e) => {
  const li = e.target.closest("li"); if (!li) return;
  state.release = li.dataset.r === "" ? null : li.dataset.r;
  state.source = null;
  $("#sidebar").classList.remove("open");
  refresh();
});

async function loadCreators() {
  const p = filterParams(); p.delete("creator");
  const list = await api(`/api/creators?${p}`);
  const named = list.filter((c) => c.creator);
  const none = list.find((c) => !c.creator);
  $("#creatorOptions").innerHTML = named.map((c) => `<option value="${esc(c.creator)}">`).join("");
  // Creators only on MyMiniFactory are listed too; picking one shows their MyMiniFactory items.
  const li = (c, label, cls = "") => {
    const n = c.releases || c.mmf;
    const tip = [label, c.releases && `${c.releases} release${c.releases === 1 ? "" : "s"}`,
      c.mmf && `${c.mmf} MyMiniFactory item${c.mmf === 1 ? "" : "s"}`].filter(Boolean).join(" · ");
    return `<li data-c="${esc(c.creator)}" data-local="${c.releases ? 1 : ""}" class="${cls} ${state.creator === c.creator ? "active" : ""}" title="${esc(tip)}">
      <span class="name">${esc(label)}${c.releases ? "" : `<span class="by">only on MyMiniFactory</span>`}</span><span class="muted">${n}</span></li>`;
  };
  // "No creator" sits at the top so releases still missing one are easy to work through.
  $("#creators").innerHTML = (list.length ? `<li data-all class="${state.creator === null ? "active" : ""}"><span class="name">All creators</span></li>` : "") +
    (none ? li(none, "No creator", "muted") : "") +
    named.map((c) => li(c, c.creator)).join("") +
    (named.length ? "" : `<li class="muted small" style="cursor:default">Set a creator from a release, or when importing.</li>`);
}

$("#creators").addEventListener("click", (e) => {
  const li = e.target.closest("li[data-c], li[data-all]"); if (!li) return;
  state.creator = li.hasAttribute("data-all") ? null : li.dataset.c;
  state.release = null;
  // The MyMiniFactory view stays on and filters by creator; a creator with nothing local opens it.
  if (state.source !== "mmf" && state.creator !== null && !li.dataset.local) Object.assign(state, { source: "mmf", mmfMissing: false });
  $("#sidebar").classList.remove("open");
  refresh();
});

function thumb(m) {
  // Pictures that came with the model win over rendered STL previews.
  const src = m.cover_image ? `/api/images/${m.cover_image}/preview` : m.cover ? `/api/files/${m.cover}/preview` : null;
  if (!src) return `<div class="ph">No preview</div>`;
  return `<img loading="lazy" src="${src}" alt="${esc(m.model)}" class="${m.cover_image ? "photo" : ""}" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'ph',textContent:'No preview'}))">`;
}

function card(m) {
  const b = [];
  if (m.supported_files) b.push(`<span class="badge sup">supported</span>`);
  if (m.unsupported_files) b.push(`<span class="badge unsup">unsupported</span>`);
  if (m.options) b.push(`<span class="badge">${m.options} option groups</span>`);
  (m.tags || []).forEach((t) => b.push(`<span class="badge tag">${esc(t)}</span>`));
  m.exts.filter((x) => x && x !== ".stl").forEach((x) => b.push(`<span class="badge">${esc(x.slice(1).toUpperCase())}</span>`));
  return `<div class="card ${state.picked?.has(m.id) ? "picked" : ""}" data-id="${m.id}">
    <div class="thumb">${thumb(m)}</div>
    <div class="meta">
      <div class="title" title="${esc(m.model)}">${esc(m.model)}</div>
      <div class="sub" title="${esc(m.release)}">${state.release === null ? esc(m.release) + " · " : ""}${m.files} files · ${fmtSize(m.size)}</div>
      <div class="badges">${b.join("")}</div>
    </div></div>`;
}

async function loadModels(append = false) {
  if (!append) state.offset = 0;
  if (state.source === "mmf") return loadMmf(append);
  if (!append) loadMmfStrip();
  const p = new URLSearchParams({ q: state.q, offset: state.offset, limit: PAGE });
  if (state.release !== null) p.set("release", state.release);
  if (state.sup) p.set("supported", state.sup);
  if (state.tags.length) p.set("tags", state.tags.join(","));
  if (state.creator !== null) p.set("creator", state.creator);
  if (!append) loadReleaseImages();
  const res = await api(`/api/models?${p}`);
  state.total = res.total;
  state.items = append ? state.items.concat(res.items) : res.items;
  const grid = $("#grid");
  grid.innerHTML = state.items.map(card).join("");
  $("#more").classList.toggle("hidden", state.items.length >= state.total);
  const rel = state.releases.find((r) => r.release === state.release);
  const who = (state.release !== null ? rel?.creator : state.creator) || (state.creator === "" ? "No creator" : "");
  $("#crumbs").textContent = [who, state.release ?? "All releases"].filter(Boolean).join(" / ") + ` · ${state.total} models`;
  $("#releaseCreatorBtn").classList.toggle("hidden", state.release === null && state.creator !== "");
  $("#releaseCreatorBtn").textContent = state.release === null ? "Set creator for several" : "Set creator";
  $("#renameCreatorBtn").classList.toggle("hidden", !state.creator || state.release !== null);
  $("#activeTags").innerHTML = state.tags.map((t) => `<span class="badge tag on" data-tag="${esc(t)}" title="Remove filter">${esc(t)} ✕</span>`).join("");
  $("#releaseTagBtn").classList.toggle("hidden", state.release === null);
  $("#combineBtn").classList.toggle("hidden", state.total < 2 || !!state.picked);
  $("#modelList").innerHTML = [...new Set(state.items.map((m) => m.model))].map((m) => `<option value="${esc(m)}">`).join("");
  const empty = $("#empty");
  if (!state.total) {
    const st = await api("/api/status");
    empty.innerHTML = st.files ? "No models match." :
      `Your library is empty.<br><br>Click <b>Import files</b>, or drop model files, folders or .zip / .7z archives anywhere on this page.`;
    empty.classList.remove("hidden");
  } else empty.classList.add("hidden");
}

async function loadReleaseImages() {
  const el = $("#releaseImages");
  const release = state.release;
  const imgs = release === null ? [] : await api(`/api/releases/images?release=${encodeURIComponent(release)}`);
  if (release !== state.release) return;
  state.releaseImages = imgs;
  el.innerHTML = `<div class="side-title">Release images</div><div class="strip">` + imgs.map((i) => `<button class="rimg ${i.main ? "main" : ""}" data-pic="${i.id}" title="${esc(i.path)}">
    <img loading="lazy" src="/api/images/${i.id}/preview" alt="${esc(i.name)}" onerror="this.parentElement.remove()"></button>`).join("") + `</div>`;
  el.classList.toggle("hidden", !imgs.length);
}
$("#releaseImages").addEventListener("click", (e) => {
  const b = e.target.closest("[data-pic]");
  if (b) openPicture(state.releaseImages.find((i) => i.id === +b.dataset.pic));
});

// Full-size picture, with a picker to attach it to a model of its release (or to the release).
async function openPicture(pic) {
  state.picture = pic;
  $("#lightboxImg").src = `/api/images/${pic.id}/full`;
  $("#lightboxName").textContent = pic.name;
  const sel = $("#lightboxModel");
  sel.innerHTML = `<option value="">Whole release</option>`;
  // "Main for model" only makes sense from a model's window.
  const fromModel = $("#modelDlg").open && state.model;
  $("#lbMainModel").classList.toggle("hidden", !fromModel);
  $("#lbMainModel").classList.toggle("on", !!(fromModel && pic.main_model));
  $("#lbMainRelease").classList.toggle("on", !!(pic.main_release || pic.main));
  $("#lightbox").showModal();
  const res = await api(`/api/models?${new URLSearchParams({ release: pic.release, limit: 1000 })}`);
  if (state.picture !== pic) return;
  sel.innerHTML += res.items.map((m) => `<option value="${esc(m.id)}">${esc(m.model)}</option>`).join("");
  sel.value = pic.scope === "model" ? pic.model_id : "";
  sel.models = res.items;
}
$("#lightbox").addEventListener("click", (e) => { if (!e.target.closest(".lb-bar")) $("#lightbox").close(); });

// Pick (or un-pick, when it already is) the picture shown first for a release or a model.
async function setMainPicture(kind, pic, on) {
  const key = kind === "model" ? state.model.id : (state.model && $("#modelDlg").open ? state.model.release : pic.release);
  await api("/api/main-picture", { method: "PUT", body: JSON.stringify({ kind, key, image_id: on ? pic.id : null }) });
  refresh();
  if ($("#modelDlg").open) { try { await openModel(state.model.id); } catch { $("#modelDlg").close(); } }
}
$("#lbMainModel").onclick = async (e) => {
  const on = !e.target.classList.contains("on");
  await setMainPicture("model", state.picture, on);
  e.target.classList.toggle("on", on);
};
$("#lbMainRelease").onclick = async (e) => {
  const on = !e.target.classList.contains("on");
  await setMainPicture("release", state.picture, on);
  e.target.classList.toggle("on", on);
};
$("#lightboxModel").addEventListener("change", async (e) => {
  const pic = state.picture;
  const m = e.target.models.find((x) => x.id === e.target.value);
  await api("/api/overrides", { method: "POST", body: JSON.stringify({ prefixes: [pic.path], release: pic.release, model: m ? m.model : "" }) });
  $("#lightbox").close();
  refresh();
  if ($("#modelDlg").open) { try { await openModel(state.model.id); } catch { $("#modelDlg").close(); } }
});

$("#more").onclick = () => { state.offset += PAGE; loadModels(true); };
$("#grid").addEventListener("click", (e) => {
  if (e.target.closest("a")) return;
  const c = e.target.closest(".card"); if (!c) return;
  if (c.dataset.mmf) { openMmfItem(state.items.find((m) => String(m.id) === c.dataset.mmf)); return; }
  if (!state.picked) { openModel(c.dataset.id); return; }
  const m = state.items.find((x) => x.id === c.dataset.id);
  if (state.picked.has(m.id)) state.picked.delete(m.id); else state.picked.set(m.id, m);
  c.classList.toggle("picked", state.picked.has(c.dataset.id));
  showPicked();
});

// ------------------------------------------------------------ combining models

// state.picked: model id -> model, ticked in the grid to edit together or combine.
// Picks are kept while browsing other releases until Cancel.
function endPicking() {
  state.picked = null;
  $("#grid").classList.remove("selecting");
  $("#selectBar").classList.add("hidden");
  document.querySelectorAll("#grid .card.picked").forEach((c) => c.classList.remove("picked"));
  $("#combineBtn").classList.toggle("hidden", state.total < 2);
}
const pickedReleases = () => new Set([...state.picked.values()].map((m) => m.release));
function showPicked() {
  const n = state.picked.size;
  $("#selectCount").textContent = n ? `${n} model${n === 1 ? "" : "s"} picked` : "Click models to pick them";
  $("#bulkGo").disabled = !n;
  // Only models of one release can become one model.
  $("#combineGo").disabled = n < 2 || pickedReleases().size > 1;
}
$("#combineBtn").onclick = () => {
  state.picked = new Map();
  $("#grid").classList.add("selecting");
  $("#selectBar").classList.remove("hidden");
  $("#combineBtn").classList.add("hidden");
  showPicked();
};
$("#selectCancel").onclick = endPicking;
$("#selectAll").onclick = () => {
  state.items.forEach((m) => state.picked.set(m.id, m));
  document.querySelectorAll("#grid .card").forEach((c) => c.classList.add("picked"));
  showPicked();
};
$("#combineGo").onclick = () => {
  const form = $("#combineDlg form");
  const parts = [...state.picked.values()];
  form.name.value = parts[0].release;  // a release that is really one model is usually named after it
  $("#combineParts").innerHTML = parts.map((m) => `<li>${esc(m.model)}</li>`).join("");
  $("#combineErr").textContent = "";
  $("#combineDlg").showModal();
  form.name.select();
};
$("#bulkGo").onclick = () => {
  const form = $("#bulkDlg form");
  form.reset();
  const n = state.picked.size, rels = pickedReleases();
  $("#bulkTitle").textContent = `Edit ${n} model${n === 1 ? "" : "s"}` + (rels.size > 1 ? ` in ${rels.size} releases` : "");
  $("#bulkErr").textContent = "";
  $("#bulkDlg").showModal();
};
$("#bulkDlg form").addEventListener("submit", async (e) => {
  if (e.submitter?.value !== "save") return;
  e.preventDefault();
  const f = e.target;
  const list = (v) => v.split(",").map((t) => t.trim()).filter(Boolean);
  const body = { model_ids: [...state.picked.keys()], add_tags: list(f.add.value), remove_tags: list(f.remove.value) };
  if (f.release.value.trim()) body.release = f.release.value.trim();
  if (f.clearCreator.checked) body.creator = "";
  else if (f.creator.value.trim()) body.creator = f.creator.value.trim();
  if (f.supported.value !== "") body.supported = +f.supported.value;
  if (f.hide.checked) {
    if (!confirm(`Hide ${body.model_ids.length} models from the library? (Files stay on disk; undo from Corrections.)`)) return;
    body.hidden = 1;
  }
  try {
    await api("/api/models/bulk", { method: "POST", body: JSON.stringify(body) });
    $("#bulkDlg").close();
    endPicking();
    refresh();
  } catch (err) { $("#bulkErr").textContent = err.message; }
});
$("#combineDlg form").addEventListener("submit", async (e) => {
  if (e.submitter?.value !== "save") return;
  e.preventDefault();
  const form = e.target;
  try {
    const res = await api("/api/combines", { method: "POST", body: JSON.stringify({
      release: [...pickedReleases()][0], name: form.name.value, model_ids: [...state.picked.keys()] }) });
    $("#combineDlg").close();
    endPicking();
    await refresh();
    openModel(res.id);
  } catch (err) { $("#combineErr").textContent = err.message; }
});

async function refresh() { loadCreators(); loadTags(); loadMmfSide(); await loadReleases(); await loadModels(); }

// ------------------------------------------------------------ MyMiniFactory

const MMF_SOURCES = { purchase: "Purchased", pledge: "Pledge", tribe: "Tribe" };

async function loadMmfSide() {
  const s = await api("/api/mmf/status");
  state.mmf = s;
  const li = (missing, label, n) => `<li data-mmf-missing="${missing ? 1 : ""}" class="${state.source === "mmf" && state.mmfMissing === missing ? "active" : ""}">
      <span class="name">${label}</span><span class="muted">${n}</span></li>`;
  $("#mmfList").innerHTML = s.total
    ? li(false, "All items", s.total) + li(true, "Not in your library", s.missing)
    : `<li class="muted small" style="cursor:default"><span>Click <b>Sync</b> to list your purchases, pledges and tribes here.</span></li>`;
}
$("#mmfList").addEventListener("click", (e) => {
  const li = e.target.closest("li[data-mmf-missing]"); if (!li) return;
  showMmf(!!li.dataset.mmfMissing);
});
function showMmf(missing) {
  Object.assign(state, { source: "mmf", mmfMissing: missing, release: null, tags: [] });
  $("#sidebar").classList.remove("open");
  refresh();
}

function mmfCard(m) {
  const b = [m.local ? `<span class="badge sup">in your library</span>` : m.queued ? `<span class="badge tag">waiting to download</span>` : `<span class="badge unsup">not downloaded</span>`];
  [...new Set(m.sources.map((s) => s.source))].forEach((s) => b.push(`<span class="badge">${MMF_SOURCES[s] || esc(s)}</span>`));
  const where = [...new Set(m.sources.map((s) => s.collection).filter(Boolean))].join(", ");
  const img = m.image ? `<img loading="lazy" referrerpolicy="no-referrer" src="${esc(m.image)}" alt="${esc(m.name)}" class="photo"
      onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'ph',textContent:'No preview'}))">` : `<div class="ph">No preview</div>`;
  return `<div class="card mmf" data-mmf="${m.id}" title="${esc(m.local ? `In your library: ${m.local.release}` : "Open on MyMiniFactory")}">
    <div class="thumb">${img}</div>
    <div class="meta">
      <div class="title" title="${esc(m.name)}">${esc(m.name)}</div>
      <div class="sub" title="${esc(where)}">${esc([m.creator, where].filter(Boolean).join(" · ") || "MyMiniFactory")}</div>
      <div class="badges">${b.join("")}<a class="badge link" href="${esc(m.url)}" target="_blank" rel="noopener noreferrer">MyMiniFactory ↗</a></div>
    </div></div>`;
}

async function loadMmf(append) {
  const p = new URLSearchParams({ q: state.q, offset: state.offset, limit: PAGE });
  if (state.mmfMissing) p.set("missing", "true");
  if (state.creator !== null) p.set("creator", state.creator);
  const res = await api(`/api/mmf?${p}`);
  if (state.source !== "mmf") return;
  state.total = res.total;
  state.items = append ? state.items.concat(res.items) : res.items;
  $("#grid").innerHTML = state.items.map(mmfCard).join("");
  $("#more").classList.toggle("hidden", state.items.length >= state.total);
  const who = state.creator === null ? "" : `${state.creator || "No creator"} / `;
  $("#crumbs").textContent = `MyMiniFactory / ${who}${state.mmfMissing ? "Not in your library" : "All items"} · ${state.total} items`;
  if (state.picked) endPicking();
  for (const id of ["#releaseCreatorBtn", "#releaseTagBtn", "#releaseImages", "#mmfStrip", "#combineBtn"]) $(id).classList.add("hidden");
  $("#renameCreatorBtn").classList.toggle("hidden", !state.creator);
  $("#activeTags").innerHTML = "";
  const empty = $("#empty");
  empty.innerHTML = state.mmf?.total ? "Nothing on MyMiniFactory matches." : "Your MyMiniFactory library hasn't been synced yet. Click <b>Sync</b> next to MyMiniFactory in the sidebar.";
  empty.classList.toggle("hidden", !!state.total);
}

// Local results come first; matching MyMiniFactory items (by the search, or by the picked
// creator) are summed up above them.
async function loadMmfStrip() {
  const el = $("#mmfStrip");
  const q = state.q, creator = state.creator;
  if ((!q && !creator) || !state.mmf?.total) { el.classList.add("hidden"); return; }
  const p = new URLSearchParams({ q, limit: 8 });
  if (creator) p.set("creator", creator);
  const res = await api(`/api/mmf?${p}`);
  if (q !== state.q || creator !== state.creator || state.source === "mmf") return;
  el.innerHTML = `<div class="side-title">On MyMiniFactory · ${res.total} ${q ? `match${res.total === 1 ? "" : "es"}` : `item${res.total === 1 ? "" : "s"}`}
      <button class="link" id="mmfStripAll">Show all</button></div>
    <div class="strip">${res.items.map((m) => `<button class="mmf-chip" data-mmf-item="${m.id}" title="${esc(m.name)}${m.local ? " · in your library" : " · not downloaded"}">
      ${m.image ? `<img loading="lazy" referrerpolicy="no-referrer" src="${esc(m.image)}" alt="" onerror="this.remove()">` : ""}
      <span>${esc(m.name)}</span>${m.local ? "" : `<span class="badge unsup">not downloaded</span>`}</button>`).join("")}</div>`;
  el.items = res.items;
  el.classList.toggle("hidden", !res.total);
}
$("#mmfStrip").addEventListener("click", (e) => {
  if (e.target.closest("#mmfStripAll")) { showMmf(false); return; }
  const b = e.target.closest("[data-mmf-item]");
  if (b) openMmfItem($("#mmfStrip").items.find((m) => String(m.id) === b.dataset.mmfItem));
});

function openMmfItem(m) { if (m) openMmfModel(m.id); }

// MyMiniFactory items open in the model window too: their pictures, where they
// come from, and Download to library (done by the bookmarklet on its next run).
async function openMmfModel(id) {
  const m = await api(`/api/mmf/${id}`);
  state.mmfItem = m;
  state.model = null;
  stop3d();
  const dlg = $("#modelDlg");
  dlg.classList.add("mmf-mode");
  const where = [...new Set(m.sources.map((s) => s.collection).filter(Boolean))];
  $("#mName").textContent = m.name;
  $("#mRelease").textContent = [m.creator, ...where].filter(Boolean).join(" / ") + " · MyMiniFactory";
  $("#editForm").classList.add("hidden");
  $("#mFilter").innerHTML = "";
  renderMmfModel();
  if (m.images.length) showMmfPicture(0); else { $("#mPreview").style.visibility = "hidden"; $("#mPreviewName").textContent = "No images"; }
  if (!dlg.open) dlg.showModal();
}

function renderMmfModel() {
  const m = state.mmfItem;
  const kinds = [...new Set(m.sources.map((s) => MMF_SOURCES[s.source] || s.source))];
  const dl = m.local
    ? `<p class="small">This is in your library${m.local.release ? ` as <b>${esc(m.local.release)}</b>` : ""}.</p>
       <div class="row"><button id="mmfOpenLocal" class="primary">Open in your library</button>
       <button id="mmfDownload">Download again</button></div>`
    : m.queued
      ? `<p class="small"><b>Waiting to download.</b> Go to myminifactory.com and click your <b>Sync to Resin Models</b> bookmark;
         it downloads everything waiting into your library.</p>
         <div class="row"><a class="button-like" href="https://www.myminifactory.com/library" target="_blank" rel="noopener">Open MyMiniFactory ↗</a>
         <button id="mmfCancel">Don't download</button></div>`
      : `<div class="row"><button id="mmfDownload" class="primary">Download to library</button></div>`;
  const note = m.download_note ? `<p class="small err">Last download failed: ${esc(m.download_note)}. You can also download it on MyMiniFactory and drop the file into <b>Import files</b>.</p>` : "";
  $("#mFiles").innerHTML = `<div class="mmf-info">
      <div class="badges">${kinds.map((k) => `<span class="badge">${esc(k)}</span>`).join("")}
        <a class="badge link" href="${esc(m.url)}" target="_blank" rel="noopener noreferrer">Open on MyMiniFactory ↗</a></div>
      ${dl}${note}
    </div>
    <div class="group-title">Images (${m.images.length})</div>
    <div class="mmf-pics">${m.images.map((u, i) => `<button data-mmf-pic="${i}" title="Image ${i + 1}">
      <img loading="lazy" referrerpolicy="no-referrer" src="${esc(u)}" alt="" onerror="this.parentElement.remove()"></button>`).join("")}</div>`;
  markMmfPic();
}

function showMmfPicture(i) {
  state.mmfPic = i;
  const img = $("#mPreview");
  img.referrerPolicy = "no-referrer";
  img.style.visibility = "visible";
  img.src = state.mmfItem.images[i];
  img.onerror = () => { img.style.visibility = "hidden"; };
  $("#mPreviewName").textContent = `Image ${i + 1} of ${state.mmfItem.images.length}`;
  markMmfPic();
}
function markMmfPic() {
  document.querySelectorAll("#mFiles [data-mmf-pic]").forEach((b) => b.classList.toggle("active", +b.dataset.mmfPic === state.mmfPic));
}

async function setMmfQueued(queued) {
  state.mmfItem = { ...state.mmfItem, ...(await api(`/api/mmf/${state.mmfItem.id}/queue`, { method: "POST", body: JSON.stringify({ queued }) })) };
  renderMmfModel();
  state.dirty = true;
}

async function mmfBookmarklet() {
  const src = (await (await fetch("/static/mmf-bookmarklet.js")).text())
    .replace(/^\/\*[\s\S]*?\*\/\s*/, "").replace("__APP__", location.origin);
  return "javascript:" + encodeURIComponent(src);
}
function renderMmfInfo() {
  const s = state.mmf || {};
  const when = s.synced ? new Date(s.synced * 1000).toLocaleString() : null;
  const parts = Object.entries(s.sources || {}).map(([k, n]) => `${n} ${(MMF_SOURCES[k] || k).toLowerCase()}`);
  $("#mmfInfo").textContent = when
    ? `Last synced ${when}: ${s.total} items (${parts.join(", ")}), ${s.missing} not in your library` +
      (s.queued ? `, ${s.queued} waiting to download (click the bookmark on myminifactory.com).` : ".")
    : "Not synced yet.";
  $("#mmfClear").disabled = !s.total;
}
$("#mmfSyncBtn").onclick = async () => {
  $("#mmfBookmarklet").href = await mmfBookmarklet();
  await loadMmfSide();
  renderMmfInfo();
  $("#mmfDlg").showModal();
};
$("#mmfBookmarklet").onclick = (e) => { e.preventDefault(); alert("Drag this button to your bookmarks bar, then click the bookmark on myminifactory.com."); };
$("#mmfFile").onchange = async (e) => {
  const file = e.target.files[0]; e.target.value = "";
  if (!file) return;
  try {
    const data = JSON.parse(await file.text());
    await api("/api/mmf/import", { method: "POST", body: JSON.stringify({ items: data.items, complete: data.complete }) });
    await refresh();
    renderMmfInfo();
  } catch (err) { alert("Couldn't read that file: " + err.message); }
};
$("#mmfClear").onclick = async () => {
  if (!confirm("Remove the MyMiniFactory list from Resin Models? Your files aren't touched, and you can sync again any time.")) return;
  await api("/api/mmf", { method: "DELETE" });
  if (state.source === "mmf") state.source = null;
  await refresh();
  renderMmfInfo();
};
// The sync window (mmf-sync.html) is a separate tab; pick up its result when coming back.
window.addEventListener("focus", () => { if (!document.querySelector("dialog[open]:not(#mmfDlg)")) { loadCreators(); loadMmfSide().then(() => { if ($("#mmfDlg").open) renderMmfInfo(); }); } });

// ------------------------------------------------------------ tags

async function loadTags() {
  const tags = await api("/api/tags");
  $("#tagOptions").innerHTML = tags.map((t) => `<option value="${esc(t.tag)}">`).join("");
  $("#tagList").innerHTML = tags.length
    ? tags.map((t) => `<li class="badge tag ${state.tags.some((x) => x.toLowerCase() === t.tag.toLowerCase()) ? "on" : ""}" data-tag="${esc(t.tag)}">${esc(t.tag)} <span class="muted">${t.models}</span></li>`).join("")
    : `<li class="muted small" style="padding:0 8px">Add tags from a model's page.</li>`;
}

function toggleTag(tag) {
  const i = state.tags.findIndex((x) => x.toLowerCase() === tag.toLowerCase());
  if (i >= 0) state.tags.splice(i, 1); else state.tags.push(tag);
  state.source = null;
  refresh();
}
$("#tagList").addEventListener("click", (e) => { const li = e.target.closest("[data-tag]"); if (li) { $("#sidebar").classList.remove("open"); toggleTag(li.dataset.tag); } });
$("#activeTags").addEventListener("click", (e) => { const b = e.target.closest("[data-tag]"); if (b) toggleTag(b.dataset.tag); });

const splitTags = (s) => s.split(",").map((t) => t.trim()).filter(Boolean);

function renderModelTags() {
  $("#mTags").innerHTML = state.model.tags.map((t) => `<span class="badge tag">${esc(t)}<button data-rm="${esc(t)}" title="Remove tag">✕</button></span>`).join("");
}
async function changeModelTags(add, remove) {
  const r = await api(`/api/models/${state.model.id}/tags`, { method: "POST", body: JSON.stringify({ add, remove }) });
  state.model.tags = r.tags;
  renderModelTags();
  state.dirty = true;
}
$("#mTags").addEventListener("click", (e) => { const b = e.target.closest("[data-rm]"); if (b) changeModelTags([], [b.dataset.rm]); });
$("#mTagInput").addEventListener("keydown", (e) => {
  if (e.key !== "Enter" && e.key !== ",") return;
  e.preventDefault();
  const tags = splitTags(e.target.value);
  e.target.value = "";
  if (tags.length) changeModelTags(tags, []);
});
$("#mTagInput").addEventListener("change", (e) => {  // picking from the suggestion list
  const tags = splitTags(e.target.value);
  if (tags.length && state.model) { e.target.value = ""; changeModelTags(tags, []); }
});

$("#releaseTagBtn").onclick = () => {
  const dlg = $("#releaseTagDlg");
  const form = $("form", dlg);
  form.reset();
  $("#rtRelease").textContent = state.release;
  $("#rtCount").textContent = `Applies to all ${state.total} models in this release${state.tags.length || state.q ? " (ignores the current search and tag filters)" : ""}.`;
  dlg.showModal();
};
$("#releaseTagDlg").addEventListener("close", async () => {
  const dlg = $("#releaseTagDlg");
  const tags = splitTags($("form", dlg).tags.value);
  if (!tags.length || !["add", "remove"].includes(dlg.returnValue)) return;
  const body = { release: state.release, add: dlg.returnValue === "add" ? tags : [], remove: dlg.returnValue === "remove" ? tags : [] };
  await api("/api/releases/tags", { method: "POST", body: JSON.stringify(body) });
  refresh();
});

// ------------------------------------------------------------ creators

async function openCreators(releases, preset = "", onlyWithout = false) {
  const dlg = $("#creatorDlg"), form = $("form", dlg);
  const all = await api(onlyWithout ? "/api/releases?creator=" : "/api/releases");
  form.reset();
  form.creator.value = preset;
  const checked = new Set(releases);
  $("#creatorReleases").innerHTML = `<label class="all"><input type="checkbox" data-all> <span class="name">Select all shown</span></label>` +
    all.map((r) => `<label data-name="${esc(r.release.toLowerCase())}"><input type="checkbox" value="${esc(r.release)}" ${checked.has(r.release) ? "checked" : ""}>
      <span class="name">${esc(r.release)}</span><span class="muted small">${esc(r.creator || "")}</span></label>`).join("");
  updateCreatorCount();
  dlg.showModal();
  if (releases.length === 1) form.creator.select();
}
function creatorBoxes() { return [...$("#creatorReleases").querySelectorAll("input[value]")]; }
function updateCreatorCount() {
  const on = creatorBoxes().filter((b) => b.checked), n = on.length;
  const hidden = on.filter((b) => b.closest("label").hidden).length;
  $("#creatorCount").textContent = n ? `${n} release${n === 1 ? "" : "s"} selected${hidden ? ` (${hidden} hidden by the filter)` : ""}.` : "Tick the releases to change.";
  $("#creatorDlg button[value=save]").disabled = !n;
}
$("#creatorReleases").addEventListener("change", (e) => {
  if (e.target.hasAttribute("data-all")) creatorBoxes().forEach((b) => { if (!b.closest("label").hidden) b.checked = e.target.checked; });
  updateCreatorCount();
});
$("#creatorDlg form").filter.addEventListener("input", (e) => {
  const q = e.target.value.trim().toLowerCase();
  creatorBoxes().forEach((b) => (b.closest("label").hidden = !!q && !b.closest("label").dataset.name.includes(q)));
  updateCreatorCount();
});
$("#creatorDlg form").filter.addEventListener("keydown", (e) => { if (e.key === "Enter") e.preventDefault(); });
$("#creatorDlg").addEventListener("close", async () => {
  const dlg = $("#creatorDlg");
  if (dlg.returnValue !== "save") return;
  const releases = creatorBoxes().filter((b) => b.checked).map((b) => b.value);
  if (!releases.length) return;
  const r = await api("/api/releases/creator", { method: "POST", body: JSON.stringify({ releases, creator: $("form", dlg).creator.value }) });
  if (state.creator === "") {
    // Working through "No creator": stay in it and move on to the next release still without one.
    if (state.release !== null && r.creator) {
      const order = state.releases.map((x) => x.release);
      const i = order.indexOf(state.release);
      state.release = [...order.slice(i + 1), ...order.slice(0, i)].find((x) => !releases.includes(x)) ?? null;
    }
  } else if (state.creator !== null) state.creator = r.creator || null;  // follow the edited releases
  refresh();
});
$("#releaseCreatorBtn").onclick = () => {
  if (state.release === null) { openCreators([], "", true); return; }  // "No creator" view
  const rel = state.releases.find((r) => r.release === state.release);
  openCreators([state.release], rel?.creator || "");
};
$("#renameCreatorBtn").onclick = async () => {
  const dlg = $("#renameCreatorDlg"), form = $("form", dlg);
  const { folders } = await api(`/api/creators/folders?creator=${encodeURIComponent(state.creator)}`).catch(() => ({ folders: [] }));
  form.reset();
  $("#rcOld").textContent = state.creator;
  $("#rcFolders").textContent = folders.join(", ");
  for (const id of ["#rcFolderRow", "#rcFolderNote"]) $(id).classList.toggle("hidden", !folders.length);
  form.name.value = state.creator;
  dlg.showModal();
  form.name.select();
};
$("#renameCreatorDlg").addEventListener("close", async () => {
  const dlg = $("#renameCreatorDlg"), action = dlg.returnValue;
  const form = $("form", dlg);
  const name = action === "reset" ? "" : form.name.value.trim();
  const folders = action === "save" && form.folders.checked && !$("#rcFolderRow").classList.contains("hidden");
  if (!["save", "reset"].includes(action) || (action === "save" && (!name || (name === state.creator && !folders)))) return;
  try {
    const r = await api("/api/creators/rename", { method: "POST", body: JSON.stringify({ creator: state.creator, name, folders }) });
    state.creator = r.creator || null;  // after an undo the original name isn't known here; show everyone
  } catch (err) { alert(err.message); }
  refresh();
});
$("#editCreatorsBtn").onclick = () => state.creator === ""
  ? openCreators([], "", true)
  : openCreators(state.creator !== null ? state.releases.map((r) => r.release) : [], state.creator || "");

let searchTimer;
$("#search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { state.q = e.target.value.trim(); refresh(); }, 250);
});
$("#supFilter").onchange = (e) => { state.sup = e.target.value; loadModels(); };
$("#menuBtn").onclick = () => $("#sidebar").classList.toggle("open");

// Sidebar width: drag the handle on its right edge; remembered in this browser only.
(() => {
  const handle = $("#sideResize"), layout = $("#layout");
  const setW = (w) => layout.style.setProperty("--side-w", Math.max(180, Math.min(600, w)) + "px");
  try { const w = +localStorage.getItem("sideWidth"); if (w) setW(w); } catch {}
  const save = () => { try { localStorage.setItem("sideWidth", parseInt($("#sidebar").offsetWidth)); } catch {} };
  handle.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    handle.setPointerCapture(e.pointerId);
    handle.classList.add("dragging"); document.body.classList.add("resizing");
    const move = (ev) => setW(ev.clientX - $("#sidebar").getBoundingClientRect().left);
    const up = () => {
      handle.removeEventListener("pointermove", move); handle.removeEventListener("pointerup", up);
      handle.classList.remove("dragging"); document.body.classList.remove("resizing"); save();
    };
    handle.addEventListener("pointermove", move); handle.addEventListener("pointerup", up);
  });
  handle.addEventListener("dblclick", () => {
    layout.style.removeProperty("--side-w");
    try { localStorage.removeItem("sideWidth"); } catch {}
  });
})();

// ------------------------------------------------------------ model dialog

const supLabel = (s) => (s === true ? "Supported" : s === false ? "Unsupported" : "Other files");

// Which files the model window shows: one format and one version at a time,
// starting from the preferences in Settings. Switching here is per view only.
const FORMAT_ORDER = ["stl", "lys", "ctx", "ctb", "chitubox", "3mf", "obj"];
const fmtOf = (f) => f.ext.replace(/^\./, "").toLowerCase();
let prefs = { preferred_format: "stl", preferred_support: "supported" };
const loadPrefs = async () => { try { prefs = await api("/api/settings"); } catch {} };

function countBy(files, key) {
  const n = new Map();
  for (const f of files) n.set(key(f), (n.get(key(f)) || 0) + 1);
  return n;
}
// Files marked "support unknown" in their correction show in both versions.
const supKey = (f) => (f.supported === true ? "supported" : f.supported === false ? "unsupported" : "");
const inVersion = (f, v) => v === "all" || supKey(f) === "" || supKey(f) === v;

function pickFormat(formats, wanted) {
  if (formats.size < 2) return "all";
  if (wanted === "all" || formats.has(wanted)) return wanted;
  if (wanted === "") return "all";
  return FORMAT_ORDER.find((x) => formats.has(x)) || [...formats.keys()].sort()[0];
}
function pickVersion(sups, wanted) {
  if (!sups.has("supported") || !sups.has("unsupported")) return "all";
  return wanted || "all";
}

function viewFiles() {
  const m = state.model, v = state.view;
  // v holds what was asked for; fmt / sup are what this model can actually show.
  const formats = countBy(m.files, fmtOf);
  const fmt = pickFormat(formats, v.fmt);
  const inFmt = m.files.filter((f) => fmt === "all" || fmtOf(f) === fmt);
  const sups = countBy(inFmt, supKey);
  const sup = pickVersion(sups, v.sup);
  return { formats, sups, fmt, sup, files: inFmt.filter((f) => inVersion(f, sup)) };
}

function renderFilter(formats, sups, v) {
  const btn = (kind, val, label, n) =>
    `<button type="button" data-${kind}="${val}" class="${v[kind] === val ? "on" : ""}">${label}${n != null ? `<span class="muted">${n}</span>` : ""}</button>`;
  let html = "";
  if (formats.size > 1) {
    const keys = [...formats.keys()].sort((a, b) =>
      (FORMAT_ORDER.indexOf(a) + 1 || 99) - (FORMAT_ORDER.indexOf(b) + 1 || 99) || a.localeCompare(b));
    html += `<div class="seg">${keys.map((k) => btn("fmt", k, esc(k.toUpperCase()), formats.get(k))).join("")}${btn("fmt", "all", "All formats")}</div>`;
  }
  if (sups.has("supported") && sups.has("unsupported")) {
    html += `<div class="seg">${btn("sup", "supported", "Supported", sups.get("supported"))}${btn("sup", "unsupported", "Unsupported", sups.get("unsupported"))}${btn("sup", "all", "Both")}</div>`;
  }
  $("#mFilter").innerHTML = html;
}

async function openModel(id) {
  const m = await api(`/api/models/${id}`);
  $("#modelDlg").classList.remove("mmf-mode");
  state.mmfItem = null;
  $("#mPreview").referrerPolicy = "";
  const same = state.model && state.model.id === id && $("#modelDlg").open;
  state.model = m;
  if (!same) state.view = { fmt: prefs.preferred_format, sup: prefs.preferred_support };
  $("#mName").textContent = m.model;
  $("#mRelease").textContent = (m.creator ? m.creator + " / " : "") + m.release;
  $("#editForm").classList.add("hidden");
  $("#mTagInput").value = "";
  renderModelTags();
  const shown = renderFiles();
  const mainFile = m.main_file && m.files.find((f) => f.id === m.main_file);
  if (same && state.current && shown.some((f) => f.id === state.current.id)) showFile(state.current);
  else if (mainFile) showFile(mainFile);
  else if (m.images.length) showImage(m.images[0]);
  else showFile(shown.find((f) => f.id === m.cover) || shown[0] || m.files[0]);
  const dlg = $("#modelDlg");
  if (!dlg.open) dlg.showModal();
}

function renderFiles() {
  const m = state.model;
  const { formats, sups, fmt, sup, files } = viewFiles();
  renderFilter(formats, sups, { fmt, sup });
  const groups = new Map();
  for (const f of files) {
    const key = `${f.option || ""}\u0000${supLabel(f.supported)}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(f);
  }
  const rank = { Supported: 0, Unsupported: 1, "Other files": 2 };
  const keys = [...groups.keys()].sort((a, b) => {
    const [oa, sa] = a.split("\u0000"), [ob, sb] = b.split("\u0000");
    return oa.localeCompare(ob) || rank[sa] - rank[sb];
  });
  // Collapsed by default (rebuilt on every open); the main preview still shows the first picture.
  const pics = m.images.length ? `<details class="pics"><summary class="group-title">Images (${m.images.length})</summary>` + m.images.map((i) => `
      <div class="file" data-img="${i.id}">
        <img loading="lazy" class="photo" src="/api/images/${i.id}/preview" onerror="this.outerHTML='<div class=noimg>IMG</div>'">
        <div class="fname"><div title="${esc(i.path)}">${esc(i.name)}</div>
          <div class="muted small">${i.scope === "release" ? "Release image" : "Model image"}${i.archive ? " · in " + esc(i.archive.split("/").pop()) : ""}</div></div>
        <a href="/api/images/${i.id}/full" target="_blank" rel="noopener" title="Open full size">↗</a>
        <button data-pic-main="${i.id}" class="star ${i.main_model ? "on" : ""}" title="${i.main_model ? "Main image of this model (click to unset)" : "Use as this model's main image"}">${i.main_model ? "★" : "☆"}</button>
        <button data-pic-rel="${i.id}" class="relmain ${i.main_release ? "on" : ""}" title="${i.main_release ? "Main image of the release (click to unset)" : "Use as the release's main image"}">Release</button>
        <button data-pic-edit="${i.id}" title="Choose which model this image belongs to, or make it the main image">✎</button>
      </div>`).join("") + `</details>` : "";
  $("#mFiles").innerHTML = pics + keys.map((k) => {
    const [opt, sup] = k.split("\u0000");
    const title = opt ? `${esc(opt)} · ${sup}` : sup;
    return `<div class="group-title">${title}</div>` + groups.get(k).map((f) => `
      <div class="file" data-id="${f.id}">
        ${f.preview === "none" || f.preview === "error" ? `<div class="noimg">${esc(f.ext.slice(1).toUpperCase())}</div>` : `<img loading="lazy" src="/api/files/${f.id}/preview" onerror="this.outerHTML='<div class=noimg>${esc(f.ext.slice(1).toUpperCase())}</div>'">`}
        <div class="fname"><div title="${esc(f.path)}">${esc(f.name)}</div>
          <div class="muted small">${fmtSize(f.size)}${f.archive ? " · in " + esc(f.archive.split("/").pop()) : ""}</div></div>
        <a href="/api/files/${f.id}/download" download title="Download">↓</a>
        ${f.preview === "none" || f.preview === "error" ? "" : `<button data-file-main="${f.id}" class="star ${m.main_file === f.id ? "on" : ""}" title="${m.main_file === f.id ? "Main image of this model (click to unset)" : "Use this preview as the model's main image"}">${m.main_file === f.id ? "★" : "☆"}</button>`}
        <button data-edit="${f.id}" title="Correct this file">✎</button>
      </div>`).join("");
  }).join("");
  markActive();
  return files;
}

$("#mFilter").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  if (b.dataset.fmt) state.view.fmt = b.dataset.fmt;
  if (b.dataset.sup) state.view.sup = b.dataset.sup;
  const shown = renderFiles();
  // Keep what's in the viewer unless that file was just filtered out.
  if (state.current && !shown.some((f) => f.id === state.current.id) && shown.length) showFile(shown[0]);
});

function markActive() {
  document.querySelectorAll("#mFiles .file").forEach((el) => el.classList.toggle("active", el.dataset.id
    ? state.current?.id === +el.dataset.id : state.currentImage?.id === +el.dataset.img));
}

function showFile(f) {
  if (!f) return;
  stop3d();
  $(".viewer").classList.remove("three");
  state.current = f;
  state.currentImage = null;
  const img = $("#mPreview");
  img.style.visibility = "visible";
  img.src = `/api/files/${f.id}/preview`;
  img.onerror = () => { img.style.visibility = "hidden"; };
  $("#mPreviewName").textContent = f.name;
  $("#view3dBtn").disabled = f.ext !== ".stl";
  markActive();
}

function showImage(i) {
  stop3d();
  $(".viewer").classList.remove("three");
  state.current = null;
  state.currentImage = i;
  const img = $("#mPreview");
  img.style.visibility = "visible";
  img.src = `/api/images/${i.id}/full`;
  img.onerror = () => { img.style.visibility = "hidden"; };
  $("#mPreviewName").textContent = i.name;
  $("#view3dBtn").disabled = true;
  markActive();
}

$("#mFiles").addEventListener("click", (e) => {
  if (e.target.closest("a")) return;
  if (state.mmfItem && $("#modelDlg").classList.contains("mmf-mode")) {
    const pic = e.target.closest("[data-mmf-pic]");
    if (pic) showMmfPicture(+pic.dataset.mmfPic);
    if (e.target.closest("#mmfDownload")) setMmfQueued(true);
    if (e.target.closest("#mmfCancel")) setMmfQueued(false);
    if (e.target.closest("#mmfOpenLocal")) {
      const local = state.mmfItem.local;
      if (local.model_id) openModel(local.model_id);
      else { $("#modelDlg").close(); Object.assign(state, { source: null, release: local.release, creator: null, tags: [] }); refresh(); }
    }
    return;
  }
  const edit = e.target.closest("[data-edit]");
  if (edit) { openFileEdit(state.model.files.find((f) => f.id === +edit.dataset.edit)); return; }
  // ☆ picks the model's main image (its card and the first image here); "Release" picks the release's.
  const fileMain = e.target.closest("[data-file-main]");
  if (fileMain) {
    const id = +fileMain.dataset.fileMain;
    const on = state.model.main_file !== id;
    api("/api/main-picture", { method: "PUT", body: JSON.stringify({ kind: "model", key: state.model.id, file_id: on ? id : null }) })
      .then(() => { refresh(); return openModel(state.model.id); })
      .then(() => { const f = state.model.files.find((x) => x.id === id); if (f) showFile(f); });
    return;
  }
  const picMain = e.target.closest("[data-pic-main], [data-pic-rel]");
  if (picMain) {
    const forModel = picMain.hasAttribute("data-pic-main");
    const pic = state.model.images.find((i) => i.id === +(picMain.dataset.picMain || picMain.dataset.picRel));
    const on = forModel ? !pic.main_model : !pic.main_release;
    setMainPicture(forModel ? "model" : "release", pic, on).then(() => { const d = $(".pics"); if (d) d.open = true; });
    return;
  }
  const picEdit = e.target.closest("[data-pic-edit]");
  if (picEdit) { openPicture(state.model.images.find((i) => i.id === +picEdit.dataset.picEdit)); return; }
  const pic = e.target.closest("[data-img]");
  if (pic) { showImage(state.model.images.find((i) => i.id === +pic.dataset.img)); return; }
  const row = e.target.closest(".file");
  if (row) showFile(state.model.files.find((f) => f.id === +row.dataset.id));
});

document.querySelectorAll("[data-close]").forEach((b) => (b.onclick = () => b.closest("dialog").close()));
$("#modelDlg").addEventListener("close", () => {
  stop3d();
  if (state.dirty) { state.dirty = false; refresh(); }
});

// Model edit
$("#mEditBtn").onclick = () => {
  const f = $("#editForm");
  f.model.value = state.model.model;
  f.release.value = state.model.release;
  f.creator.value = state.model.creator || "";
  $("#splitBtn").classList.toggle("hidden", !state.model.combine);
  f.classList.toggle("hidden");
};
$("#cancelEdit").onclick = () => $("#editForm").classList.add("hidden");
$("#editForm").onsubmit = async (e) => {
  e.preventDefault();
  const f = e.target;
  const body = { prefixes: state.model.roots, from_model_id: state.model.id };
  if (f.model.value.trim() && f.model.value.trim() !== state.model.model) body.model = f.model.value.trim();
  if (f.release.value.trim() && f.release.value.trim() !== state.model.release) body.release = f.release.value.trim();
  const creator = f.creator.value.trim();
  const creatorChanged = creator !== (state.model.creator || "");
  if (!body.model && !body.release && !creatorChanged) { f.classList.add("hidden"); return; }
  const cb = state.model.combine;
  if (cb && (body.model || body.release)) {
    // A combined model is renamed or moved through its combine, so its option groups stay.
    if (body.release) await api("/api/overrides", { method: "POST", body: JSON.stringify({ prefixes: state.model.roots, release: body.release }) });
    await api(`/api/combines/${cb.id}`, { method: "PUT", body: JSON.stringify({
      release: body.release || state.model.release, name: body.model || state.model.model }) });
  } else if (body.model || body.release) {
    if (!body.model) body.model = state.model.model;
    if (!body.release) body.release = state.model.release;
    await api("/api/overrides", { method: "POST", body: JSON.stringify(body) });
  }
  // The creator belongs to the release the model ends up in.
  if (creatorChanged) {
    await api("/api/releases/creator", { method: "POST",
      body: JSON.stringify({ releases: [body.release || state.model.release], creator }) });
  }
  $("#modelDlg").close();
  refresh();
};
$("#splitBtn").onclick = async () => {
  const cb = state.model.combine;
  if (!confirm(`Split ${cb.name} back into ${cb.parts.length} models (${cb.parts.join(", ")})?`)) return;
  await api(`/api/combines/${cb.id}`, { method: "DELETE" });
  $("#modelDlg").close();
  refresh();
};
$("#hideModelBtn").onclick = async () => {
  if (!confirm("Hide this model from the library? (Files stay on disk; undo from Corrections.)")) return;
  await api("/api/overrides", { method: "POST", body: JSON.stringify({ prefixes: state.model.roots, hidden: 1 }) });
  $("#modelDlg").close();
  refresh();
};

// File edit
function openFileEdit(f) {
  const form = $("#fileForm");
  form.reset();
  $("#fName").textContent = f.name;
  $("#fPath").textContent = f.path;
  const parts = f.path.split("/");
  const scopes = [[f.path, "Just this file"]];
  for (let i = parts.length - 1; i >= 1; i--) scopes.push([parts.slice(0, i).join("/"), `Everything in ${parts.slice(0, i).join(" / ")}`]);
  form.scope.innerHTML = scopes.map(([v, l]) => `<option value="${esc(v)}">${esc(l)}</option>`).join("");
  $("#fileDlg").showModal();
  form.onsubmit = null;
  $("#fileDlg").onclose = async () => {
    const action = $("#fileDlg").returnValue;
    if (action === "map") { openMapper(f.path); return; }
    if (action !== "save" && action !== "hide") return;
    const body = { prefixes: [form.scope.value] };
    if (action === "hide") body.hidden = 1;
    else {
      if (form.supported.value !== "") body.supported = +form.supported.value;
      if (form.model.value.trim()) body.model = form.model.value.trim();
      if (form.release.value.trim()) body.release = form.release.value.trim();
      if (form.option.value.trim()) body.option = form.option.value.trim();
      if (Object.keys(body).length === 1) return;
      if (body.model && !body.release) body.release = state.model.release;
    }
    await api("/api/overrides", { method: "POST", body: JSON.stringify(body) });
    refresh();
    try { await openModel(state.model.id); } catch { $("#modelDlg").close(); }
  };
}

// ------------------------------------------------------------ folder mappings

const ROLE_LABELS = { creator: "Creator", release: "Release", model: "Model", ignore: "Skip", auto: "Automatic" };
const mapper = { parts: [], name: "", map: null, scopeTouched: false, timer: null, seq: 0 };

function mapRoles() { return [...$("#mapSegs").querySelectorAll("select")].map((s) => s.value); }
function mapPrefix() { return $("#mapForm").scope.value; }
function defaultScope(roles) {
  // The folder holding the release, so the mapping covers that creator's other releases too.
  const r = roles.indexOf("release");
  return mapper.parts.slice(0, Math.max(r, 1)).join("/");
}
function renderMapSegs(roles) {
  $("#mapSegs").innerHTML = mapper.parts.map((p, i) => `
    <div class="map-seg role-${roles[i] || "auto"}" style="--lvl:${i}">
      <span class="seg-name" title="${esc(p)}">${esc(p)}</span>
      <select data-i="${i}">${Object.entries(ROLE_LABELS).map(([v, l]) => `<option value="${v}" ${v === (roles[i] || "auto") ? "selected" : ""}>${l}</option>`).join("")}</select>
    </div>`).join("") + `<div class="map-seg" style="--lvl:${mapper.parts.length}"><span class="seg-name muted">${esc(mapper.name)}</span></div>`;
}
async function openMapper(path) {
  const parts = path.split("/");
  mapper.name = parts.pop();
  mapper.parts = parts;
  if (!parts.length) { alert("This file sits at the top of the library, so there are no folders to map."); return; }
  const s = await api(`/api/maps/suggest?path=${encodeURIComponent(path)}`);
  mapper.map = s.map;
  mapper.scopeTouched = !!s.map;
  const roles = s.map ? s.map.roles : s.roles;
  renderMapSegs(roles);
  const sel = $("#mapForm").scope;
  sel.innerHTML = parts.map((_, i) => {
    const pre = parts.slice(0, i + 1).join("/");
    return `<option value="${esc(pre)}">Everything in ${esc(parts.slice(0, i + 1).join(" / "))}</option>`;
  }).join("");
  sel.value = s.map ? s.map.prefix : defaultScope(roles);
  $("#mapRemove").classList.toggle("hidden", !s.map);
  $("#mapErr").textContent = "";
  $("#mapPreview").innerHTML = "";
  if (!$("#mapDlg").open) $("#mapDlg").showModal();
  previewMap();
}
function groupList(groups, limit = 40) {
  const li = groups.slice(0, limit).map((g) => `<li>${esc([g.creator, g.release, g.model].filter(Boolean).join(" / "))}<span class="muted">${g.files} file${g.files === 1 ? "" : "s"}</span></li>`).join("");
  return `<ul>${li}${groups.length > limit ? `<li class="muted">and ${groups.length - limit} more</li>` : ""}</ul>`;
}
function previewMap() {
  clearTimeout(mapper.timer);
  mapper.timer = setTimeout(async () => {
    const seq = ++mapper.seq;
    try {
      const p = await api("/api/maps/preview", { method: "POST", body: JSON.stringify({ prefix: mapPrefix(), roles: mapRoles() }) });
      if (seq !== mapper.seq) return;
      $("#mapErr").textContent = "";
      const n = (g) => `${g.length} model${g.length === 1 ? "" : "s"}`;
      $("#mapPreview").innerHTML = `<div><b>${p.files} file${p.files === 1 ? "" : "s"}</b> in this folder. With this mapping they group into ${n(p.after)}:</div>` +
        groupList(p.after) + `<details><summary class="muted small">Now: ${n(p.before)}</summary>${groupList(p.before)}</details>`;
    } catch (err) {
      if (seq !== mapper.seq) return;
      $("#mapErr").textContent = err.message;
      $("#mapPreview").innerHTML = "";
    }
  }, 200);
}
$("#mapSegs").addEventListener("change", (e) => {
  const sel = e.target.closest("select");
  if (!sel) return;
  // One release and one model: picking one moves it off the folder that had it.
  const roles = mapRoles();
  if (sel.value === "release" || sel.value === "model") roles.forEach((r, i) => { if (r === sel.value && i !== +sel.dataset.i) roles[i] = "ignore"; });
  const m = roles.indexOf("model"), end = m >= 0 ? m : roles.indexOf("release");
  roles.forEach((r, i) => {
    if (m >= 0 && i > m) roles[i] = "auto";  // below the model: read automatically
    else if (i < end && r === "auto") roles[i] = "ignore";  // above it, a folder without a role is skipped
  });
  renderMapSegs(roles);
  if (!mapper.scopeTouched) $("#mapForm").scope.value = defaultScope(roles);
  previewMap();
});
$("#mapForm").scope.addEventListener("change", () => { mapper.scopeTouched = true; previewMap(); });
$("#mapForm").addEventListener("submit", async (e) => {
  const action = e.submitter?.value;
  if (action !== "save" && action !== "remove") return;
  e.preventDefault();
  try {
    if (action === "remove") await api(`/api/maps/${mapper.map.id}`, { method: "DELETE" });
    else await api("/api/maps", { method: "POST", body: JSON.stringify({ prefix: mapPrefix(), roles: mapRoles() }) });
  } catch (err) { $("#mapErr").textContent = err.message; return; }
  $("#mapDlg").close();
  $("#modelDlg").close();  // the model may have been regrouped under a new name
  if ($("#rulesDlg").open) $("#rulesBtn").onclick();
  refresh();
});
$("#mMapBtn").onclick = () => {
  const m = state.model;
  const f = m.files.find((x) => x.id === m.cover) || m.files[0];
  if (f) openMapper(f.path);
};

// ------------------------------------------------------------ 3D viewer (three.js from CDN, optional)

let three = null;
async function start3d(f) {
  const el = $("#viewer3d");
  $(".viewer").classList.add("three");
  el.innerHTML = `<div class="ph muted" style="padding:20px">Loading ${fmtSize(f.size)}…</div>`;
  try {
    const THREE = await import("three");
    const { STLLoader } = await import("three/addons/loaders/STLLoader.js");
    const { OrbitControls } = await import("three/addons/controls/OrbitControls.js");
    const buf = await (await fetch(`/api/files/${f.id}/download`)).arrayBuffer();
    if (state.current !== f) return;
    const geom = new STLLoader().parse(buf);
    geom.computeVertexNormals();
    geom.center();
    geom.computeBoundingSphere();
    const r = geom.boundingSphere.radius || 1;
    el.innerHTML = "";
    const w = el.clientWidth;
    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    renderer.setPixelRatio(window.devicePixelRatio);
    renderer.setSize(w, w);
    el.appendChild(renderer.domElement);
    const scene = new THREE.Scene();
    const cam = new THREE.PerspectiveCamera(35, 1, r / 100, r * 100);
    cam.up.set(0, 0, 1);
    cam.position.set(r * 1.6, -r * 2.6, r * 1.3);
    scene.add(new THREE.HemisphereLight(0xffffff, 0x334455, 1.2));
    const d = new THREE.DirectionalLight(0xffffff, 1.6); d.position.set(-1, -2, 3); cam.add(d); scene.add(cam);
    scene.add(new THREE.Mesh(geom, new THREE.MeshStandardMaterial({ color: 0xc8d4e2, roughness: .6, metalness: .05, side: THREE.DoubleSide })));
    const controls = new OrbitControls(cam, renderer.domElement);
    controls.enableDamping = true;
    let alive = true;
    const loop = () => { if (!alive) return; controls.update(); renderer.render(scene, cam); requestAnimationFrame(loop); };
    loop();
    three = { stop() { alive = false; renderer.dispose(); geom.dispose(); el.innerHTML = ""; } };
  } catch (err) {
    el.innerHTML = `<div class="ph muted" style="padding:20px">3D view unavailable: ${esc(err.message)}</div>`;
  }
}
function stop3d() { if (three) { three.stop(); three = null; } }
$("#view3dBtn").onclick = () => {
  if ($(".viewer").classList.contains("three")) showFile(state.current);
  else if (state.current) start3d(state.current);
};

// ------------------------------------------------------------ corrections

$("#rulesBtn").onclick = async () => {
  const rules = await api("/api/overrides");
  const sup = { 1: "supported", 0: "unsupported", "-1": "unknown" };
  $("#rulesTable").innerHTML = rules.length ? `<tr><th>Path</th><th>Change</th><th></th></tr>` + rules.map((r) => {
    const ch = [];
    if (r.release != null) ch.push(`release → ${esc(r.release)}`);
    if (r.model != null) ch.push(r.model === "" ? "image → whole release" : `model → ${esc(r.model)}`);
    if (r.option != null) ch.push(`option → ${esc(r.option)}`);
    if (r.creator != null) ch.push(`creator → ${esc(r.creator)}`);
    if (r.supported != null) ch.push(sup[r.supported]);
    if (r.hidden) ch.push("hidden");
    return `<tr><td class="path">${esc(r.prefix)}</td><td>${ch.join("<br>")}</td><td><button data-del="${r.id}">Delete</button></td></tr>`;
  }).join("") : `<tr><td class="muted">No corrections yet. Use Edit on a model, or ✎ on a file.</td></tr>`;
  const maps = await api("/api/maps");
  $("#mapsTable").innerHTML = maps.length ? `<tr><th>Folder</th><th>Levels</th><th>Files</th><th></th></tr>` + maps.map((m) =>
    `<tr><td class="path">${esc(m.prefix)}</td><td>${m.roles.map((r) => ROLE_LABELS[r]).join(" / ")}</td><td>${m.files}</td><td><button data-del-map="${m.id}">Delete</button></td></tr>`).join("")
    : `<tr><td class="muted">No folder mappings yet.</td></tr>`;
  const cbs = await api("/api/combines");
  $("#combinesTable").innerHTML = cbs.length ? `<tr><th>Release</th><th>Model</th><th>Option groups</th><th></th></tr>` + cbs.map((c) =>
    `<tr><td>${esc(c.release)}</td><td>${esc(c.name)}</td><td>${c.parts.map(esc).join(", ")}</td><td><button data-split="${c.id}">Split</button></td></tr>`).join("")
    : `<tr><td class="muted">No combined models yet.</td></tr>`;
  if (!$("#rulesDlg").open) $("#rulesDlg").showModal();
};
$("#combinesTable").addEventListener("click", async (e) => {
  const id = e.target.dataset.split; if (!id) return;
  await api(`/api/combines/${id}`, { method: "DELETE" });
  $("#rulesBtn").onclick();
  refresh();
});
$("#mapsTable").addEventListener("click", async (e) => {
  const id = e.target.dataset.delMap; if (!id) return;
  await api(`/api/maps/${id}`, { method: "DELETE" });
  $("#rulesBtn").onclick();
  refresh();
});
$("#rulesTable").addEventListener("click", async (e) => {
  const id = e.target.dataset.del; if (!id) return;
  await api(`/api/overrides/${id}`, { method: "DELETE" });
  $("#rulesBtn").onclick();
  refresh();
});
$("#retryBtn").onclick = async () => { await api("/api/previews/retry", { method: "POST" }); $("#retryBtn").textContent = "Queued"; };

// ------------------------------------------------------------ settings

function setSelect(sel, value) {
  // Keep values saved outside the presets (e.g. from an old env var) selectable.
  if (![...sel.options].some((o) => o.value === String(value))) sel.add(new Option(String(value), value));
  sel.value = value;
}
$("#settingsBtn").onclick = async () => {
  const s = await api("/api/settings");
  const f = $("#settingsForm");
  setSelect(f.release_depth, s.release_depth);
  f.prerender.checked = !!s.prerender;
  f.preview_workers.value = s.preview_workers;
  f.preview_size.value = s.preview_size;
  f.max_preview_mb.value = s.max_preview_mb;
  f.preferred_format.value = s.preferred_format;
  f.preferred_support.value = s.preferred_support;
  f.ignored_folders.value = s.ignored_folders;
  $("#settingsErr").textContent = "";
  $("#settingsDlg").showModal();
};
$("#settingsForm").addEventListener("submit", async (e) => {
  if (e.submitter?.value !== "save") return;
  e.preventDefault();
  const f = e.target;
  const body = { prerender: f.prerender.checked ? 1 : 0, preferred_format: f.preferred_format.value, preferred_support: f.preferred_support.value, ignored_folders: f.ignored_folders.value };
  for (const k of ["release_depth", "preview_workers", "preview_size", "max_preview_mb"]) body[k] = Number(f[k].value);
  try {
    prefs = await api("/api/settings", { method: "PUT", body: JSON.stringify(body) });
    $("#settingsDlg").close();
    wasRunning = true;
  } catch (err) { $("#settingsErr").textContent = err.message; }
});

// ------------------------------------------------------------ jobs & status

let wasRunning = false;
async function pollStatus() {
  try {
    const s = await api("/api/status");
    const j = s.job;
    const el = $("#jobStatus");
    const pv = s.previews;
    const pending = pv.pending || 0;
    if (j.running) {
      el.textContent = `${j.message}${j.total ? ` (${j.done}/${j.total})` : ""}`;
    } else if (pending) {
      el.textContent = `Rendering previews: ${pv.ok || 0} done, ${pending} to go`;
    } else {
      el.textContent = j.last?.error ? `Last ${j.last.job} failed: ${j.last.message}` : (j.last?.message || `${s.models} models · ${s.files} files`);
    }
    el.title = el.textContent;
    $("#version").textContent = `v${s.version}${s.commit ? ` (${s.commit})` : ""}`;
    $("#sourceImportBtn").disabled = !!j.running;
    $("#sourceImport").classList.toggle("hidden", !s.source_available);
    $("#sourceDir").textContent = s.source_dir;
    if (wasRunning && !j.running) refresh();
    wasRunning = !!j.running;
    setTimeout(pollStatus, j.running ? 1000 : 4000);
  } catch { setTimeout(pollStatus, 5000); }
}

// ------------------------------------------------------------ import (upload)

const UPLOAD_EXTS = [".stl", ".lys", ".ctx", ".ctb", ".chitubox", ".obj", ".3mf", ".zip", ".7z",
  ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"];
const upload = { queue: [], skipped: 0, busy: false };
const extOf = (n) => (n.match(/\.[^.]+$/)?.[0] || "").toLowerCase();

function addToQueue(items) { // items: [{ file, rel }]
  for (const it of items) {
    const name = it.rel.split("/").pop();
    if (name.startsWith(".") || !UPLOAD_EXTS.includes(extOf(name))) upload.skipped++;
    else upload.queue.push(it);
  }
  renderQueue();
}
function renderQueue(msg) {
  const q = upload.queue;
  const total = q.reduce((n, it) => n + it.file.size, 0);
  $("#uploadQueue").textContent = msg ?? (q.length
    ? `${q.length} file${q.length > 1 ? "s" : ""} ready (${fmtSize(total)})${upload.skipped ? `, ${upload.skipped} ignored (not model files, images or archives)` : ""}.`
    : upload.skipped ? `${upload.skipped} ignored (not model files, images or archives).` : "");
  $("#uploadBtn").disabled = upload.busy || !q.length;
  $("#clearQueue").disabled = upload.busy || !q.length;
}
function openImport() { if (!$("#importDlg").open) $("#importDlg").showModal(); renderQueue(); }
$("#importBtn").onclick = openImport;
$("#pickFiles").onclick = () => $("#fileInput").click();
$("#pickFolder").onclick = () => $("#folderInput").click();
$("#fileInput").onchange = (e) => { addToQueue([...e.target.files].map((f) => ({ file: f, rel: f.name }))); e.target.value = ""; };
$("#folderInput").onchange = (e) => { addToQueue([...e.target.files].map((f) => ({ file: f, rel: f.webkitRelativePath || f.name }))); e.target.value = ""; };
$("#clearQueue").onclick = () => { upload.queue = []; upload.skipped = 0; renderQueue(); };

// Drag and drop anywhere on the page, folders included.
async function entriesToItems(entry, prefix = "") {
  if (entry.isFile) return [{ file: await new Promise((ok, err) => entry.file(ok, err)), rel: prefix + entry.name }];
  const reader = entry.createReader(), out = [];
  for (;;) {
    const batch = await new Promise((ok, err) => reader.readEntries(ok, err));
    if (!batch.length) break;
    for (const e of batch) out.push(...await entriesToItems(e, `${prefix}${entry.name}/`));
  }
  return out;
}
let dragDepth = 0;
const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
window.addEventListener("dragenter", (e) => { if (hasFiles(e)) { dragDepth++; document.body.classList.add("dragging"); } });
window.addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; document.body.classList.remove("dragging"); } });
window.addEventListener("dragover", (e) => { if (hasFiles(e)) e.preventDefault(); });
window.addEventListener("drop", async (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault();
  dragDepth = 0; document.body.classList.remove("dragging");
  const entries = [...e.dataTransfer.items].map((i) => i.webkitGetAsEntry?.()).filter(Boolean);
  const items = [];
  if (entries.length) for (const en of entries) items.push(...await entriesToItems(en));
  else items.push(...[...e.dataTransfer.files].map((f) => ({ file: f, rel: f.name })));
  openImport();
  addToQueue(items);
});

function putFile(path, depth, file, onProgress) {
  return new Promise((resolve, reject) => {
    const x = new XMLHttpRequest();
    x.open("PUT", `/api/upload?path=${encodeURIComponent(path)}&size=${file.size}&depth=${depth}`);
    x.upload.onprogress = (e) => onProgress(e.loaded);
    x.onload = () => x.status < 300 ? resolve(JSON.parse(x.responseText))
      : reject(new Error(JSON.parse(x.responseText || "{}").detail || x.statusText));
    x.onerror = () => reject(new Error("network error"));
    x.send(file);
  });
}
$("#uploadBtn").onclick = async () => {
  const folder = $("#uploadFolder").value.trim().replace(/^\/+|\/+$/g, "");
  // With a creator, uploads are filed as Creator / Release / ..., next to that creator's other releases.
  const creator = $("#uploadCreator").value.trim().replace(/[\/\\]+/g, "-").replace(/^\.+/, "");
  const prefix = [creator, folder].filter(Boolean).join("/");
  const items = upload.queue.splice(0);
  const total = items.reduce((n, it) => n + it.file.size, 0) || 1;
  const bar = $("#uploadProgress");
  let sent = 0, saved = 0, existed = 0;
  const failed = [];
  upload.busy = true; bar.classList.remove("hidden"); bar.value = 0;
  for (const [i, it] of items.entries()) {
    renderQueue(`Uploading ${i + 1} of ${items.length}: ${it.rel}`);
    try {
      const r = await putFile(prefix ? `${prefix}/${it.rel}` : it.rel, creator ? 1 : 0, it.file, (n) => (bar.value = (sent + n) / total));
      r.status === "exists" ? existed++ : saved++;
    } catch (err) { failed.push(`${it.rel}: ${err.message}`); }
    sent += it.file.size;
  }
  bar.classList.add("hidden");
  upload.busy = false; upload.skipped = 0;
  if (saved) { await api("/api/index", { method: "POST" }); wasRunning = true; }
  renderQueue(`Uploaded ${saved} file${saved === 1 ? "" : "s"}${existed ? `, ${existed} already in the library` : ""}${failed.length ? `, ${failed.length} failed` : ""}.` +
    (saved ? " Indexing now; previews follow in the background." : ""));
  if (failed.length) alert(`These files were not uploaded:\n\n${failed.join("\n")}`);
};

$("#sourceImportBtn").onclick = async () => {
  if (!confirm("Copy everything in the NAS folder into the library? Originals are only read, never modified. Files already copied are skipped.")) return;
  try { await api("/api/import", { method: "POST" }); wasRunning = true; } catch (e) { alert(e.message); }
};

loadPrefs();
refresh();
pollStatus();
