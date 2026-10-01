const $ = (s, el = document) => el.querySelector(s);
const api = async (url, opts = {}) => {
  const r = await fetch(url, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
};
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtSize = (b) => {
  if (!b) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return `${b.toFixed(b < 10 && i ? 1 : 0)} ${u[i]}`;
};

const state = { release: null, q: "", sup: "", tags: [], offset: 0, items: [], total: 0, model: null };
const PAGE = 120;

// ------------------------------------------------------------ releases & grid

async function loadReleases() {
  const rels = await api(`/api/releases?q=${encodeURIComponent(state.q)}&tags=${encodeURIComponent(state.tags.join(","))}`);
  const ul = $("#releases");
  const total = rels.reduce((a, r) => a + r.models, 0);
  ul.innerHTML = `<li data-r="" class="${state.release === null ? "active" : ""}"><span class="name">All releases</span><span class="muted">${total}</span></li>` +
    rels.map((r) => `<li data-r="${esc(r.release)}" class="${state.release === r.release ? "active" : ""}" title="${esc(r.release)}">
      <span class="name">${esc(r.release)}</span><span class="muted">${r.models}</span></li>`).join("");
  $("#releaseList").innerHTML = rels.map((r) => `<option value="${esc(r.release)}">`).join("");
}

$("#releases").addEventListener("click", (e) => {
  const li = e.target.closest("li"); if (!li) return;
  state.release = li.dataset.r === "" ? null : li.dataset.r;
  $("#sidebar").classList.remove("open");
  refresh();
});

function thumb(fileId, label) {
  if (!fileId) return `<div class="ph">No preview</div>`;
  return `<img loading="lazy" src="/api/files/${fileId}/preview" alt="${esc(label)}" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'ph',textContent:'No preview'}))">`;
}

function card(m) {
  const b = [];
  if (m.supported_files) b.push(`<span class="badge sup">supported</span>`);
  if (m.unsupported_files) b.push(`<span class="badge unsup">unsupported</span>`);
  if (m.options) b.push(`<span class="badge">${m.options} option groups</span>`);
  (m.tags || []).forEach((t) => b.push(`<span class="badge tag">${esc(t)}</span>`));
  m.exts.filter((x) => x && x !== ".stl").forEach((x) => b.push(`<span class="badge">${esc(x.slice(1).toUpperCase())}</span>`));
  return `<div class="card" data-id="${m.id}">
    <div class="thumb">${thumb(m.cover, m.model)}</div>
    <div class="meta">
      <div class="title" title="${esc(m.model)}">${esc(m.model)}</div>
      <div class="sub" title="${esc(m.release)}">${state.release === null ? esc(m.release) + " · " : ""}${m.files} files · ${fmtSize(m.size)}</div>
      <div class="badges">${b.join("")}</div>
    </div></div>`;
}

async function loadModels(append = false) {
  if (!append) state.offset = 0;
  const p = new URLSearchParams({ q: state.q, offset: state.offset, limit: PAGE });
  if (state.release !== null) p.set("release", state.release);
  if (state.sup) p.set("supported", state.sup);
  if (state.tags.length) p.set("tags", state.tags.join(","));
  const res = await api(`/api/models?${p}`);
  state.total = res.total;
  state.items = append ? state.items.concat(res.items) : res.items;
  const grid = $("#grid");
  grid.innerHTML = state.items.map(card).join("");
  $("#more").classList.toggle("hidden", state.items.length >= state.total);
  $("#crumbs").textContent = `${state.release ?? "All releases"} · ${state.total} models`;
  $("#activeTags").innerHTML = state.tags.map((t) => `<span class="badge tag on" data-tag="${esc(t)}" title="Remove filter">${esc(t)} ✕</span>`).join("");
  $("#releaseTagBtn").classList.toggle("hidden", state.release === null);
  $("#modelList").innerHTML = [...new Set(state.items.map((m) => m.model))].map((m) => `<option value="${esc(m)}">`).join("");
  const empty = $("#empty");
  if (!state.total) {
    const st = await api("/api/status");
    empty.innerHTML = st.files ? "No models match." :
      `Your library is empty.<br><br>Click <b>Import library</b> to copy files from <code>${esc(st.source_dir)}</code> into <code>${esc(st.library_dir)}</code>.<br>Your original files are only read, never changed.`;
    empty.classList.remove("hidden");
  } else empty.classList.add("hidden");
}

$("#more").onclick = () => { state.offset += PAGE; loadModels(true); };
$("#grid").addEventListener("click", (e) => { const c = e.target.closest(".card"); if (c) openModel(c.dataset.id); });

function refresh() { loadReleases(); loadModels(); loadTags(); }

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

let searchTimer;
$("#search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { state.q = e.target.value.trim(); refresh(); }, 250);
});
$("#supFilter").onchange = (e) => { state.sup = e.target.value; loadModels(); };
$("#menuBtn").onclick = () => $("#sidebar").classList.toggle("open");

// ------------------------------------------------------------ model dialog

const supLabel = (s) => (s === true ? "Supported" : s === false ? "Unsupported" : "Other files");

async function openModel(id) {
  const m = await api(`/api/models/${id}`);
  state.model = m;
  $("#mName").textContent = m.model;
  $("#mRelease").textContent = (m.creator ? m.creator + " / " : "") + m.release;
  $("#editForm").classList.add("hidden");
  $("#mTagInput").value = "";
  renderModelTags();
  const groups = new Map();
  for (const f of m.files) {
    const key = `${f.option || ""}\u0000${supLabel(f.supported)}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(f);
  }
  const rank = { Supported: 0, Unsupported: 1, "Other files": 2 };
  const keys = [...groups.keys()].sort((a, b) => {
    const [oa, sa] = a.split("\u0000"), [ob, sb] = b.split("\u0000");
    return oa.localeCompare(ob) || rank[sa] - rank[sb];
  });
  $("#mFiles").innerHTML = keys.map((k) => {
    const [opt, sup] = k.split("\u0000");
    const title = opt ? `${esc(opt)} · ${sup}` : sup;
    return `<div class="group-title">${title}</div>` + groups.get(k).map((f) => `
      <div class="file" data-id="${f.id}">
        ${f.preview === "none" || f.preview === "error" ? `<div class="noimg">${esc(f.ext.slice(1).toUpperCase())}</div>` : `<img loading="lazy" src="/api/files/${f.id}/preview" onerror="this.outerHTML='<div class=noimg>${esc(f.ext.slice(1).toUpperCase())}</div>'">`}
        <div class="fname"><div title="${esc(f.path)}">${esc(f.name)}</div>
          <div class="muted small">${fmtSize(f.size)}${f.archive ? " · in " + esc(f.archive.split("/").pop()) : ""}</div></div>
        <a href="/api/files/${f.id}/download" download title="Download">↓</a>
        <button data-edit="${f.id}" title="Correct this file">✎</button>
      </div>`).join("");
  }).join("");
  showFile(m.files.find((f) => f.id === m.cover) || m.files[0]);
  const dlg = $("#modelDlg");
  if (!dlg.open) dlg.showModal();
}

function showFile(f) {
  if (!f) return;
  stop3d();
  $(".viewer").classList.remove("three");
  state.current = f;
  const img = $("#mPreview");
  img.style.visibility = "visible";
  img.src = `/api/files/${f.id}/preview`;
  img.onerror = () => { img.style.visibility = "hidden"; };
  $("#mPreviewName").textContent = f.name;
  $("#view3dBtn").disabled = f.ext !== ".stl";
  document.querySelectorAll(".file").forEach((el) => el.classList.toggle("active", +el.dataset.id === f.id));
}

$("#mFiles").addEventListener("click", (e) => {
  if (e.target.closest("a")) return;
  const edit = e.target.closest("[data-edit]");
  if (edit) { openFileEdit(state.model.files.find((f) => f.id === +edit.dataset.edit)); return; }
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
  f.classList.toggle("hidden");
};
$("#cancelEdit").onclick = () => $("#editForm").classList.add("hidden");
$("#editForm").onsubmit = async (e) => {
  e.preventDefault();
  const f = e.target;
  const body = { prefixes: state.model.roots, from_model_id: state.model.id };
  if (f.model.value.trim() && f.model.value.trim() !== state.model.model) body.model = f.model.value.trim();
  if (f.release.value.trim() && f.release.value.trim() !== state.model.release) body.release = f.release.value.trim();
  if (!body.model && !body.release) { f.classList.add("hidden"); return; }
  if (!body.model) body.model = state.model.model;
  if (!body.release) body.release = state.model.release;
  await api("/api/overrides", { method: "POST", body: JSON.stringify(body) });
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
    if (r.model != null) ch.push(`model → ${esc(r.model)}`);
    if (r.option != null) ch.push(`option → ${esc(r.option)}`);
    if (r.supported != null) ch.push(sup[r.supported]);
    if (r.hidden) ch.push("hidden");
    return `<tr><td class="path">${esc(r.prefix)}</td><td>${ch.join("<br>")}</td><td><button data-del="${r.id}">Delete</button></td></tr>`;
  }).join("") : `<tr><td class="muted">No corrections yet. Use Edit on a model, or ✎ on a file.</td></tr>`;
  if (!$("#rulesDlg").open) $("#rulesDlg").showModal();
};
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
  setSelect(f.scan_interval_minutes, s.scan_interval_minutes);
  f.prerender.checked = !!s.prerender;
  f.preview_workers.value = s.preview_workers;
  f.preview_size.value = s.preview_size;
  f.max_preview_mb.value = s.max_preview_mb;
  $("#settingsErr").textContent = "";
  $("#settingsDlg").showModal();
};
$("#settingsForm").addEventListener("submit", async (e) => {
  if (e.submitter?.value !== "save") return;
  e.preventDefault();
  const f = e.target;
  const body = { prerender: f.prerender.checked ? 1 : 0 };
  for (const k of ["release_depth", "scan_interval_minutes", "preview_workers", "preview_size", "max_preview_mb"]) body[k] = Number(f[k].value);
  try {
    await api("/api/settings", { method: "PUT", body: JSON.stringify(body) });
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
    $("#importBtn").disabled = $("#scanBtn").disabled = !!j.running;
    $("#importBtn").title = s.source_available ? `Copy ${s.source_dir} into ${s.library_dir}` : `${s.source_dir} is not mounted`;
    if (wasRunning && !j.running) refresh();
    wasRunning = !!j.running;
    setTimeout(pollStatus, j.running ? 1000 : 4000);
  } catch { setTimeout(pollStatus, 5000); }
}

$("#importBtn").onclick = async () => {
  if (!confirm("Copy your existing library into the managed library folder? Originals are only read, never modified. Files already copied are skipped.")) return;
  try { await api("/api/import", { method: "POST" }); wasRunning = true; } catch (e) { alert(e.message); }
};
$("#scanBtn").onclick = async () => { try { await api("/api/scan", { method: "POST" }); wasRunning = true; } catch (e) { alert(e.message); } };

refresh();
pollStatus();
