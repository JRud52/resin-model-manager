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

// creator: null = all creators, "" = releases without a creator
const state = { release: null, creator: null, q: "", sup: "", tags: [], offset: 0, items: [], total: 0, model: null, releases: [] };
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
  ul.innerHTML = `<li data-r="" class="${state.release === null ? "active" : ""}"><span class="name">All releases</span><span class="muted">${total}</span></li>` +
    rels.map((r) => `<li data-r="${esc(r.release)}" class="${state.release === r.release ? "active" : ""}" title="${esc(r.creator ? `${r.release} by ${r.creator}` : r.release)}">
      <span class="name">${esc(r.release)}${by(r)}</span><span class="muted">${r.models}</span></li>`).join("");
  $("#releaseList").innerHTML = rels.map((r) => `<option value="${esc(r.release)}">`).join("");
}

$("#releases").addEventListener("click", (e) => {
  const li = e.target.closest("li"); if (!li) return;
  state.release = li.dataset.r === "" ? null : li.dataset.r;
  $("#sidebar").classList.remove("open");
  refresh();
});

async function loadCreators() {
  const p = filterParams(); p.delete("creator");
  const list = await api(`/api/creators?${p}`);
  const named = list.filter((c) => c.creator);
  const none = list.find((c) => !c.creator);
  $("#creatorOptions").innerHTML = named.map((c) => `<option value="${esc(c.creator)}">`).join("");
  const li = (value, label, n, cls = "") => `<li data-c="${esc(value)}" class="${cls} ${state.creator === value ? "active" : ""}" title="${esc(label)}">
      <span class="name">${esc(label)}</span><span class="muted">${n}</span></li>`;
  $("#creators").innerHTML = (named.length ? `<li data-all class="${state.creator === null ? "active" : ""}"><span class="name">All creators</span></li>` : "") +
    named.map((c) => li(c.creator, c.creator, c.releases)).join("") +
    (none && named.length ? li("", "No creator", none.releases, "muted") : "") +
    (named.length ? "" : `<li class="muted small" style="cursor:default">Set a creator from a release, or when importing.</li>`);
}

$("#creators").addEventListener("click", (e) => {
  const li = e.target.closest("li[data-c], li[data-all]"); if (!li) return;
  state.creator = li.hasAttribute("data-all") ? null : li.dataset.c;
  state.release = null;
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
  return `<div class="card" data-id="${m.id}">
    <div class="thumb">${thumb(m)}</div>
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
  if (state.creator !== null) p.set("creator", state.creator);
  if (!append) loadReleaseImages();
  const res = await api(`/api/models?${p}`);
  state.total = res.total;
  state.items = append ? state.items.concat(res.items) : res.items;
  const grid = $("#grid");
  grid.innerHTML = state.items.map(card).join("");
  $("#more").classList.toggle("hidden", state.items.length >= state.total);
  const rel = state.releases.find((r) => r.release === state.release);
  const who = state.release !== null ? rel?.creator : state.creator === "" ? "No creator" : state.creator;
  $("#crumbs").textContent = [who, state.release ?? "All releases"].filter(Boolean).join(" / ") + ` · ${state.total} models`;
  $("#releaseCreatorBtn").classList.toggle("hidden", state.release === null);
  $("#activeTags").innerHTML = state.tags.map((t) => `<span class="badge tag on" data-tag="${esc(t)}" title="Remove filter">${esc(t)} ✕</span>`).join("");
  $("#releaseTagBtn").classList.toggle("hidden", state.release === null);
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
  el.innerHTML = `<div class="side-title">Release pictures</div><div class="strip">` + imgs.map((i) => `<button class="rimg" data-full="/api/images/${i.id}/full" data-name="${esc(i.name)}" title="${esc(i.path)}">
    <img loading="lazy" src="/api/images/${i.id}/preview" alt="${esc(i.name)}" onerror="this.parentElement.remove()"></button>`).join("") + `</div>`;
  el.classList.toggle("hidden", !imgs.length);
}
$("#releaseImages").addEventListener("click", (e) => {
  const b = e.target.closest("[data-full]"); if (!b) return;
  $("#lightboxImg").src = b.dataset.full;
  $("#lightboxName").textContent = b.dataset.name;
  $("#lightbox").showModal();
});
$("#lightbox").addEventListener("click", () => $("#lightbox").close());

$("#more").onclick = () => { state.offset += PAGE; loadModels(true); };
$("#grid").addEventListener("click", (e) => { const c = e.target.closest(".card"); if (c) openModel(c.dataset.id); });

async function refresh() { loadCreators(); loadTags(); await loadReleases(); await loadModels(); }

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

// ------------------------------------------------------------ creators

async function openCreators(releases, preset = "") {
  const dlg = $("#creatorDlg"), form = $("form", dlg);
  const all = await api("/api/releases");
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
  if (state.creator !== null) state.creator = r.creator || null;  // follow the edited releases
  refresh();
});
$("#releaseCreatorBtn").onclick = () => {
  const rel = state.releases.find((r) => r.release === state.release);
  openCreators([state.release], rel?.creator || "");
};
$("#editCreatorsBtn").onclick = () =>
  openCreators(state.creator !== null ? state.releases.map((r) => r.release) : [], state.creator || "");

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
  const pics = m.images.length ? `<div class="group-title">Pictures</div>` + m.images.map((i) => `
      <div class="file" data-img="${i.id}">
        <img loading="lazy" class="photo" src="/api/images/${i.id}/preview" onerror="this.outerHTML='<div class=noimg>IMG</div>'">
        <div class="fname"><div title="${esc(i.path)}">${esc(i.name)}</div>
          <div class="muted small">${i.scope === "release" ? "Release picture" : "Model picture"}${i.archive ? " · in " + esc(i.archive.split("/").pop()) : ""}</div></div>
        <a href="/api/images/${i.id}/full" target="_blank" rel="noopener" title="Open full size">↗</a>
      </div>`).join("") : "";
  $("#mFiles").innerHTML = pics + keys.map((k) => {
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
  if (m.images.length) showImage(m.images[0]);
  else showFile(m.files.find((f) => f.id === m.cover) || m.files[0]);
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

function showImage(i) {
  stop3d();
  $(".viewer").classList.remove("three");
  state.current = null;
  const img = $("#mPreview");
  img.style.visibility = "visible";
  img.src = `/api/images/${i.id}/full`;
  img.onerror = () => { img.style.visibility = "hidden"; };
  $("#mPreviewName").textContent = i.name;
  $("#view3dBtn").disabled = true;
  document.querySelectorAll(".file").forEach((el) => el.classList.toggle("active", +el.dataset.img === i.id));
}

$("#mFiles").addEventListener("click", (e) => {
  if (e.target.closest("a")) return;
  const edit = e.target.closest("[data-edit]");
  if (edit) { openFileEdit(state.model.files.find((f) => f.id === +edit.dataset.edit)); return; }
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
    if (r.creator != null) ch.push(`creator → ${esc(r.creator)}`);
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
  for (const k of ["release_depth", "preview_workers", "preview_size", "max_preview_mb"]) body[k] = Number(f[k].value);
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
    ? `${q.length} file${q.length > 1 ? "s" : ""} ready (${fmtSize(total)})${upload.skipped ? `, ${upload.skipped} ignored (not model files, pictures or archives)` : ""}.`
    : upload.skipped ? `${upload.skipped} ignored (not model files, pictures or archives).` : "");
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

refresh();
pollStatus();
