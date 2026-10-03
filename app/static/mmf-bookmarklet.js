/* Sync bookmarklet: runs on www.myminifactory.com in the user's logged-in browser.
   app.js turns this file into a javascript: link, with __APP__ replaced by the
   app's address. It reads the library the way the site's own Library page does
   and hands it to the app's /mmf-sync window (postMessage, because an https page
   can't post to a plain-http NAS address). If that window can't be reached, the
   library is saved as a file to upload in the app instead. Afterwards it reads the
   full-size images of items that don't have them yet. Files aren't downloaded:
   MyMiniFactory serves them from a host page scripts can't read. */
(function () {
  var APP = "__APP__";
  if (!/(^|\.)myminifactory\.com$/.test(location.hostname)) {
    alert("Open www.myminifactory.com, log in, and click this bookmark there.");
    return;
  }
  var win = window.open(APP + "/mmf-sync", "rmm_mmf_sync");
  var ready = false, queue = [], finished = false;
  function send(m) {
    if (ready && win && !win.closed) win.postMessage(m, APP); else queue.push(m);
  }
  window.addEventListener("message", function (e) {
    if (e.origin !== APP || !e.data) return;
    if (e.data.type === "mmf-ready") { ready = true; queue.splice(0).forEach(send); }
    if (e.data.type === "mmf-images") {
      finished = true;
      galleries(e.data.gallery || []).then(function () { send({ type: "mmf-images-done" }); });
    }
    if (e.data.type === "mmf-done") { finished = true; box.textContent = e.data.text; setTimeout(function () { box.remove(); }, 8000); }
  });
  var box = document.createElement("div");
  box.style.cssText = "position:fixed;z-index:2147483647;right:16px;bottom:16px;max-width:360px;padding:12px 16px;" +
    "background:#1b1e23;color:#e6e9ee;border:1px solid #5aa9e6;border-radius:10px;font:14px/1.4 system-ui,sans-serif;" +
    "box-shadow:0 4px 20px rgba(0,0,0,.4)";
  document.body.appendChild(box);
  function say(text) { box.textContent = text; send({ type: "mmf-progress", text: text }); }

  function list(x) {
    if (Array.isArray(x)) return x;
    return x && typeof x === "object" ? Object.keys(x).map(function (k) { return x[k]; }) : [];
  }
  async function getJSON(path, page) {
    var url = path + (path.indexOf("?") < 0 ? "?" : "&") + "page=" + page;
    var r = await fetch(url, { credentials: "include", headers: { Accept: "application/json", "X-Requested-With": "XMLHttpRequest" } });
    if (/\/login/.test(r.url)) throw new Error("not logged in");
    if (!r.ok) throw new Error(path + ": HTTP " + r.status);
    return r.json();
  }
  async function pages(path, label) {
    var out = [], seen = {}, total = Infinity;
    for (var page = 1; out.length < total && page <= 500; page++) {
      var d = await getJSON(path, page);
      var got = list(d.items), added = 0;
      got.forEach(function (o) {
        var id = o && (o.id != null ? String(o.id) : JSON.stringify(o));
        if (!seen[id]) { seen[id] = 1; out.push(o); added++; }
      });
      if (!added) break;
      total = Number(d.total_count) || out.length;
      say("Reading " + label + "… " + out.length + (isFinite(total) ? " of " + total : ""));
    }
    return out;
  }
  function picture(o) {
    if (o.obj_img) return o.obj_img;
    var imgs = list(o.images && (o.images.items || o.images));
    var p = imgs.filter(function (i) { return i && i.is_primary; })[0] || imgs[0];
    if (!p) return "";
    var f = p.standard || p.thumbnail || p.large || p.original || {};
    return f.url || "";
  }
  function pictures(o) {
    return list(o.images && (o.images.items || o.images)).map(function (i) {
      var f = (i && (i.large || i.standard || i.original || i.thumbnail)) || {};
      return typeof i === "string" ? i : f.url;
    }).filter(Boolean);
  }

  function slim(o, source, collection) {
    var pledge = list(o.pledges && (o.pledges.items || o.pledges))[0];
    return {
      images: pictures(o), downloads: [],
      id: o.id, name: o.name, source: source,
      collection: collection || (pledge && pledge.name) || "",
      // Display name first ("The Print Goes Ever On"), the account slug only as a fallback.
      creator: (o.designer && o.designer.name) || o.user_name || o.username || (o.designer && o.designer.username) || "",
      creator_url: o.user_url || "", url: o.absolute_url || o.url || o.show_url || "", image: picture(o)
    };
  }

  // A tribe group's objects: inline in the tribes list when the site includes them,
  // else from a link the group carries, else /data-library/group/{id}.
  async function groupObjects(g, label) {
    var inline = list(g.items || (g.objects && (g.objects.items || g.objects)));
    if (inline.length && inline.length >= (Number(g.total_count) || inline.length)) return inline;
    var links = [];
    list(g.apis).concat([g.url, g.objects_url, g.api_url, g.data_url]).forEach(function (u) {
      u = u && (u.url || u);
      if (typeof u === "string" && /^(\/|https:\/\/www\.myminifactory\.com\/)/.test(u) && !/^\/?$/.test(u)) links.push(u);
    });
    links.push("/data-library/group/" + encodeURIComponent(g.id));
    var last;
    for (var u of links) {
      try { return await pages(u, label); } catch (err) { last = err; }
    }
    if (inline.length) return inline;
    throw last;
  }
  // The keys and short values of a group, to see what the site sends when a group can't be read.
  function sample(g) {
    var out = {};
    Object.keys(g || {}).forEach(function (k) {
      var v = g[k];
      out[k] = v && typeof v === "object" ? (Array.isArray(v) ? "[" + v.length + "]" : "{" + Object.keys(v).slice(0, 6).join(",") + "}") : v;
    });
    return out;
  }
  // The library lists only a small picture per item. MyMiniFactory's own object data
  // (/api/v2/objects/{id}, read with the user's login) lists just the listing's images, in
  // several sizes; the item page also shows other people's prints, so it isn't used.
  async function pageImages(it) {
    var r = await fetch("/api/v2/objects/" + encodeURIComponent(it.id), {
      credentials: "include", headers: { Accept: "application/json" } });
    if (!r.ok) throw new Error("HTTP " + r.status);
    var o = await r.json();
    return list(o.images && (o.images.items || o.images)).map(function (img) {
      var f = img && (img.large || img.standard || img.original) || {};
      return typeof f === "string" ? f : f.url;
    }).filter(function (u) { return /^https:\/\//.test(u || ""); });
  }
  async function galleries(items) {
    items = items.slice(0, 400);  // a big first sync reads the rest on the next runs
    var next = 0, done = 0, batch = [];
    async function worker() {
      while (next < items.length) {
        var it = items[next++];
        try { batch.push({ id: it.id, images: await pageImages(it) }); } catch (err) { /* read again next sync */ }
        done++;
        if (batch.length >= 20) send({ type: "mmf-gallery", items: batch.splice(0) });
        say("Reading full-size images… " + done + " of " + items.length);
      }
    }
    await Promise.all([worker(), worker(), worker()]);
    if (batch.length) send({ type: "mmf-gallery", items: batch });
  }
  // ---- The library as the site's Library page reads it now (2026): one list of every item
  // (objectPreviews), the names of tribe, group, MMF+ and campaign releases from their own
  // metadata lists, and names, links and pictures from /api/data-library/objects.
  var KINDS = { PURCHASE: "purchase", FRONTIER: "pledge", TRIBE: "tribe", USER_GROUP: "group",
                MMFPLUS: "mmfplus", DOWNLOAD: "free" };
  async function api(path) {
    var r = await fetch(path, { credentials: "include", headers: { Accept: "application/json" } });
    if (/\/login/.test(r.url)) throw new Error("not logged in");
    if (!r.ok) throw new Error(path + ": HTTP " + r.status);
    return r.json();
  }
  // Release id -> name, for each source that has releases. A list that can't be read only
  // leaves those releases unnamed.
  async function releaseNames(problems) {
    var names = {}, base = "/api/data-library/";
    async function each(listPath, releasesPath, label, nameOf) {
      try {
        var owners = list(await api(base + listPath));
        for (var i = 0; i < owners.length; i++) {
          var o = owners[i];
          say("Reading " + label + " releases… " + (i + 1) + " of " + owners.length);
          try {
            var rel = await api(base + releasesPath + encodeURIComponent(o.id));
            var rels = Array.isArray(rel) ? rel : list(rel.pledges).concat(list(rel.addons));
            rels.forEach(function (x) {
              if (!x || x.id == null) return;
              names[label + ":" + x.id] = nameOf(o, x.label || x.name || "");
              names[label + ":of:" + x.id] = o;
            });
            names[label + ":owner:" + o.id] = o.name || "";
          } catch (err) { problems.push(label + " " + (o.name || o.id) + ": " + err.message); }
        }
      } catch (err) { problems.push(label + ": " + err.message); }
    }
    await each("tribes_metadata", "tribe_releases_metadata/", "tribe", function (o, l) { return l; });
    await each("userGroups_metadata", "userGroup_releases_metadata/", "group", function (o, l) { return l || o.name; });
    await each("frontiers_metadata", "frontier_releases_metadata/", "pledge", function (o) { return o.name; });
    try {
      list(await api(base + "mmfplus_releases_metadata")).forEach(function (x) { names["mmfplus:" + x.id] = x.label || x.name || ""; });
    } catch (err) { problems.push("MMF+: " + err.message); }
    return names;
  }
  function field(release, key) {  // "type:campaign-tier;orderId:1;tierId:83699" -> "83699"
    var m = new RegExp("(?:^|;)" + key + ":([^;]+)").exec(release == null ? "" : String(release));
    return m && m[1];
  }
  async function readLibrary(items, complete, problems) {
    var previews = list(await api("/api/data-library/objectPreviews"));
    if (!previews.length) throw new Error("the library list was empty");
    say("Found " + previews.length + " library entries. Reading release names…");
    var names = await releaseNames(problems);
    var objects = previews.filter(function (p) { return p && (p.type || "object") === "object" && p.originalId; });
    var ids = Object.keys(objects.reduce(function (a, p) { a[p.originalId] = 1; return a; }, {}));
    var details = {};
    for (var i = 0; i < ids.length; i += 200) {
      say("Reading item details… " + Math.min(i + 200, ids.length) + " of " + ids.length);
      try {
        var q = ids.slice(i, i + 200).map(function (id) { return "ids[]=" + encodeURIComponent(id); }).join("&");
        list(await api("/api/data-library/objects?" + q)).forEach(function (d) { if (d) details[d.originalId] = d; });
      } catch (err) { problems.push("item details: " + err.message); }
    }
    // Bundles bought from the store: their names, for the parts listed one by one.
    var bundleIds = {};
    previews.forEach(function (p) {
      var b = field(p.release, "bundleId") || p.bundleId;
      if (b) bundleIds[b] = 1;
    });
    var bundles = {}, bids = Object.keys(bundleIds);
    for (var j = 0; j < bids.length; j += 100) {
      try {
        var bq = bids.slice(j, j + 100).map(function (id) { return "ids[]=" + encodeURIComponent(id); }).join("&");
        var got = await api("/api/data-library/bundles_metadata?" + bq);
        (Array.isArray(got) ? got : list(got)).forEach(function (b) {  // id "bundle-1123", originalId 1123
          var id = b && (b.originalId != null ? b.originalId : String(b.id).replace(/^bundle-/, ""));
          if (id != null) bundles[id] = b.name || b.label || b.title || "";
        });
      } catch (err) { problems.push("bundle names: " + err.message); }
    }
    // Tribes are named after the creator's account ("midguardminiatures's Tribe"); use their display name.
    var creatorById = {};
    objects.forEach(function (p) {
      var c = (details[p.originalId] || {}).creator || {};
      if (p.creatorId != null && (c.name || p.creatorName)) creatorById[p.creatorId] = c.name || p.creatorName;
    });
    function releaseName(source, p) {
      if (source === "pledge" && names["pledge:" + p.release] === undefined) return names["pledge:owner:" + p.campaignId];
      var label = names[source + ":" + p.release];
      if (label !== undefined && source === "tribe") {
        var t = names["tribe:of:" + p.release] || {};
        var who = creatorById[t.id] ? creatorById[t.id] + "'s Tribe" : t.name;
        return [who, label].filter(Boolean).join(" · ");
      }
      if (label !== undefined) return label;
      // Tribe and store entries bought through a campaign or as a bundle say so in their release.
      var tier = field(p.release, "tierId"), bundle = field(p.release, "bundleId") || p.bundleId;
      return (tier && names["pledge:" + tier]) || (bundle && bundles[bundle]) || "";
    }
    objects.forEach(function (p) {
      var source = KINDS[p.source];
      if (!source) return;
      var d = details[p.originalId] || {}, creator = d.creator || {};
      var pics = [d.previewUrl].concat(list(d.images).map(function (im) { return im && im.url; })).filter(Boolean);
      var thumb = list(d.images).map(function (im) { return im && (im.thumbnailUrl || im.url); }).filter(Boolean)[0];
      items.push({
        id: p.originalId, name: d.name || p.name, source: source,
        collection: p.release != null || p.campaignId != null || p.bundleId != null ? releaseName(source, p) || "" : "",
        creator: creator.name || p.creatorName || creator.username || p.creatorUsername || "",
        creator_url: creator.username ? "https://www.myminifactory.com/users/" + encodeURIComponent(creator.username) : "",
        url: d.url ? "https://www.myminifactory.com/object/3d-print-" + d.url : "/object/" + p.originalId,
        image: thumb || d.previewUrl || "", images: pics, downloads: []
      });
    });
    Object.keys(KINDS).forEach(function (k) { complete.push(KINDS[k]); });
  }

  (async function () {
    var items = [], complete = [], problems = [];
    try {
      await readLibrary(items, complete, problems);
    } catch (err) {  // the older library pages, for a site that doesn't have the new list
      items = []; complete = [];
      problems.push("library list: " + err.message);
      await readOldLibrary(items, complete, problems);
    }
    finishRead(items, complete, problems);
  })();

  async function readOldLibrary(items, complete, problems) {
    async function section(source, label, fn) {
      try { await fn(); complete.push(source); }
      catch (err) { problems.push(label + ": " + err.message); }
    }
    await section("purchase", "purchases", async function () {
      (await pages("/data-library/purchases", "purchases")).forEach(function (o) { items.push(slim(o, "purchase")); });
    });
    await section("pledge", "pledges", async function () {
      (await pages("/data-library/campaigns", "pledges")).forEach(function (o) { items.push(slim(o, "pledge")); });
    });
    await section("tribe", "tribes", async function () {
      var tribes = await pages("/data-library/tribes", "tribes"), failed = [];
      for (var t of tribes) {
        var groups = list(t.groups && (t.groups.items || t.groups));
        var own = groups.filter(function (g) { return !/^all\//.test(String(g.id)); });
        for (var g of (own.length ? own : groups)) {
          var name = [t.name, own.length > 1 ? g.name : ""].filter(Boolean).join(" · ");
          try {
            (await groupObjects(g, (t.name || "tribe") + " · " + (g.name || ""))).forEach(function (o) { items.push(slim(o, "tribe", name)); });
          } catch (err) {
            failed.push(err.message + " " + JSON.stringify(sample(g)).slice(0, 300));
          }
        }
      }
      // Items from the groups that worked are kept; the section counts as incomplete so
      // tribe items found by earlier syncs aren't dropped.
      if (failed.length) throw new Error(failed.length + " tribe group(s) failed: " + failed.slice(0, 3).join(" | "));
    });
  }

  function finishRead(items, complete, problems) {
    if (problems.length > 5) problems = problems.slice(0, 5).concat(["and " + (problems.length - 5) + " more"]);
    var payload = { type: "mmf-library", items: items, complete: complete, problems: problems };
    if (!complete.length) {
      say("Couldn't read your MyMiniFactory library. " + problems.join("; "));
      send(payload);
      return;
    }
    say("Found " + items.length + " items. Sending them to Resin Models…");
    send(payload);
    setTimeout(function () {
      if (ready || finished) return;
      var a = document.createElement("a");
      a.href = URL.createObjectURL(new Blob([JSON.stringify(payload)], { type: "application/json" }));
      a.download = "myminifactory-library.json";
      document.body.appendChild(a); a.click(); a.remove();
      box.textContent = "Resin Models didn't answer, so the list was saved as myminifactory-library.json. " +
        "Upload it in Resin Models under MyMiniFactory › Sync.";
    }, 15000);
  }
})();
