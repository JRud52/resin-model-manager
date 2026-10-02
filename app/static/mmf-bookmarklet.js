/* Sync bookmarklet: runs on www.myminifactory.com in the user's logged-in browser.
   app.js turns this file into a javascript: link, with __APP__ replaced by the
   app's address. It reads the library the way the site's own Library page does
   and hands it to the app's /mmf-sync window (postMessage, because an https page
   can't post to a plain-http NAS address). If that window can't be reached, the
   library is saved as a file to upload in the app instead. */
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
  function slim(o, source, collection) {
    var pledge = list(o.pledges && (o.pledges.items || o.pledges))[0];
    return {
      id: o.id, name: o.name, source: source,
      collection: collection || (pledge && pledge.name) || "",
      creator: o.user_name || o.username || (o.designer && (o.designer.name || o.designer.username)) || "",
      creator_url: o.user_url || "", url: o.absolute_url || o.url || o.show_url || "", image: picture(o)
    };
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
      var tribes = await pages("/data-library/tribes", "tribes");
      for (var t of tribes) {
        var groups = list(t.groups && (t.groups.items || t.groups));
        var own = groups.filter(function (g) { return !/^all\//.test(String(g.id)); });
        for (var g of (own.length ? own : groups)) {
          var objs = await pages("/data-library/group/" + g.id, (t.name || "tribe") + " · " + (g.name || ""));
          var name = [t.name, own.length > 1 ? g.name : ""].filter(Boolean).join(" · ");
          objs.forEach(function (o) { items.push(slim(o, "tribe", name)); });
        }
      }
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
