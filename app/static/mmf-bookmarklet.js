/* Sync bookmarklet: runs on www.myminifactory.com in the user's logged-in browser.
   app.js turns this file into a javascript: link, with __APP__ replaced by the
   app's address. It reads the library the way the site's own Library page does
   and hands it to the app's /mmf-sync window (postMessage, because an https page
   can't post to a plain-http NAS address). If that window can't be reached, the
   library is saved as a file to upload in the app instead. Afterwards it downloads
   the items marked "Download to library" (it has the login, the NAS doesn't) and
   hands each file to that window, which uploads it into the library. */
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
    if (e.data.type === "mmf-queue") {
      finished = true;
      galleries(e.data.gallery || []).then(function () { download(e.data.queue || []); });
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
  function downloads(o) {
    var archives = list(o.archives).filter(function (a) { return a && a.download_url; })
      .map(function (a) { return { url: a.download_url, name: String(a.path || "").split("/").pop() }; });
    if (archives.length) return archives;
    if (o.archive_download_url) return [{ url: o.archive_download_url, name: "" }];
    return list(o.files && (o.files.items || o.files)).filter(function (f) { return f && f.download_url; })
      .map(function (f) { return { url: f.download_url, name: f.filename || "" }; });
  }
  function slim(o, source, collection) {
    var pledge = list(o.pledges && (o.pledges.items || o.pledges))[0];
    return {
      images: pictures(o), downloads: downloads(o),
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
  // The library lists only a small picture per item; its own page has every image in full size.
  // The page also shows other people's prints and other items, so only the item's own image
  // list is used: the objects with an "is_primary" flag (the format the library itself uses),
  // from the image folder the item's library picture or the page's og:image is in.
  // Image links look like https://dl.myminifactory.com/object-assets/<folder>/images/720X720-name.jpg.
  function folderOf(u) { var m = /\/object-assets\/([^\/]+)\//.exec(u || ""); return m && m[1]; }
  function enclosing(text, pos) {  // the {...} around pos, parsed, or null
    for (var a = pos, depth = 0; a >= 0; a--) {
      if (text[a] === "}") depth++;
      else if (text[a] === "{" && !depth--) break;
    }
    for (var b = a, d = 0; a >= 0 && b < text.length; b++) {
      if (text[b] === "{") d++;
      else if (text[b] === "}" && !--d) {
        try { return JSON.parse(text.slice(a, b + 1)); } catch (err) { return null; }
      }
    }
    return null;
  }
  function bestUrl(img) {
    var f = img.large || img.standard || img.original || img.thumbnail || {};
    return typeof f === "string" ? f : f.url || img.url || "";
  }
  async function pageImages(it) {
    if (!/^https:\/\/www\.myminifactory\.com\//.test(it.url)) return [];
    var r = await fetch(it.url, { credentials: "include" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    var text = (await r.text()).replace(/&quot;/g, '"').replace(/&amp;/g, "&").replace(/\\"/g, '"').replace(/\\\//g, "/");
    var og = /<meta[^>]+property=["']og:image["'][^>]+content=["']([^"']+)/i.exec(text) ||
      /<meta[^>]+content=["']([^"']+)["'][^>]+property=["']og:image["']/i.exec(text);
    var byFolder = {}, re = /"is_primary"\s*:/g, m;
    while ((m = re.exec(text))) {
      var img = enclosing(text, m.index), u = img && bestUrl(img), f = folderOf(u);
      if (!f || !/^https:\/\//.test(u)) continue;
      var list = byFolder[f] || (byFolder[f] = []);
      if (list.indexOf(u) < 0) list.push(u);
    }
    var folder = [folderOf(it.image), folderOf(og && og[1])].filter(function (f) { return f && byFolder[f]; })[0];
    return folder ? byFolder[folder] : [];  // not sure which images are the item's: keep the library picture
  }
  async function galleries(items) {
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
  function fileName(r, url, given, fallback) {
    var cd = r.headers.get("Content-Disposition") || "";
    var m = /filename\*=UTF-8''([^;]+)/i.exec(cd) || /filename="?([^";]+)"?/i.exec(cd);
    var name = m ? decodeURIComponent(m[1]) : given || decodeURIComponent(new URL(r.url || url, location.href).pathname.split("/").pop());
    return (name || fallback).replace(/[\\/:*?"<>|]+/g, "-");
  }
  // Downloads each queued item with the user's login and passes the files to the
  // Resin Models window, which uploads them into the library folder it chose.
  async function download(items) {
    for (var i = 0; i < items.length; i++) {
      var it = items[i], links = it.downloads && it.downloads.length ? it.downloads : [{ url: "/download/" + it.id, name: "" }];
      var got = 0, note = "";
      for (var j = 0; j < links.length; j++) {
        try {
          say("Downloading " + it.name + (links.length > 1 ? " (" + (j + 1) + " of " + links.length + ")" : "") + "…");
          var r = await fetch(links[j].url, { credentials: "include" });
          if (/\/login/.test(r.url)) throw new Error("not logged in");
          if (!r.ok) throw new Error("HTTP " + r.status);
          if (/text\/html/.test(r.headers.get("Content-Type") || "")) throw new Error("MyMiniFactory sent a web page instead of a file");
          var blob = await r.blob();
          send({ type: "mmf-file", id: it.id, folder: it.folder, depth: it.depth,
                 name: fileName(r, links[j].url, links[j].name, it.name + ".zip"), blob: blob });
          got++;
        } catch (err) {
          note = (err && err.message) || String(err);
          if (/Failed to fetch|NetworkError|Load failed/.test(note)) note = "MyMiniFactory didn't let the bookmark read the download";
        }
      }
      send({ type: "mmf-item-done", id: it.id, ok: got === links.length, note: got === links.length ? "" : note });
    }
    send({ type: "mmf-downloads-done" });
  }

  (async function () {
    var items = [], complete = [], problems = [];
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
  })();
})();
