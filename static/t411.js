/* Transit411 shared component bundle. Loaded by BOTH the Command Center and the
   public site. Components take an element + API data and render into it, using the
   host page's CSS variables (--ink, --muted, --line, --accent, ...). Build once, use everywhere. */
(function () {
  const RATING = { H: "High", MH: "Medium-High", M: "Medium", ML: "Medium-Low", L: "Low" };
  // Escapes quotes too: values also land inside HTML attributes (titles, data-*).
  function esc(s) { return (s == null ? "" : String(s)).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
  function amt(num, raw) { return num != null ? ("$" + Number(num).toLocaleString(undefined, { maximumFractionDigits: 0 }) + "M") : (raw ? esc(raw) : "-"); }

  function cigRow(p, i, opts) {
    const rt = p.rating ? ('<span title="' + esc(RATING[p.rating] || "") + '">' + esc(p.rating) + '</span>') : '-';
    const pv = !!(opts && opts.profileVersions);
    const upd = pv && p.profile_changed_at
      ? ' <span class="t411-badge t411-k-date" title="FTA revised this project\'s profile (archived ' + esc(p.profile_changed_at) + ')">profile updated</span>' : '';
    return '<tr class="t411-row" data-i="' + i + '" title="Show milestone dates"><td style="font-weight:600">' + esc(p.project_name) + upd + '</td><td>' + esc(p.sponsor) + '</td>'
      + '<td>' + esc(p.city || "") + ', ' + esc(p.state || "") + '</td>' + (p.mode ? '<td title="' + esc(p.mode_source ? "Source: " + p.mode_source : "") + '">' + esc(p.mode) + '</td>'
        : '<td class="t411-unspec" title="' + esc(p.mode_source ? "Not stated: " + p.mode_source : "Not stated by FTA sources") + '">Unspecified</td>') + '<td>' + esc(p.phase) + '</td>'
      + '<td class="num">' + amt(p.cost_musd, p.cost_raw) + '</td><td class="num">' + amt(p.cig_request_musd, p.cig_request_raw) + '</td>'
      + '<td class="num">' + esc(p.cig_share || "-") + '</td><td>' + rt + '</td><td>' + esc(p.est_grant || "-") + '</td></tr>'
      + '<tr class="t411-detail" data-i="' + i + '" hidden><td colspan="10"><div class="t411-ms-title">Milestones</div>'
      + '<div class="t411-ms-box"></div><button type="button" class="t411-btn" data-hist="' + i + '">Show snapshot history</button>'
      + (pv ? ' <button type="button" class="t411-btn" data-pv="' + i + '">Profile versions' + (p.profile_versions ? ' (' + p.profile_versions + ')' : '') + '</button>' : '')
      + '<div class="t411-tl-box"></div>' + (pv ? '<div class="t411-pv-box"></div>' : '') + '</td></tr>';
  }

  function defaultHistory(p) {
    const q = "?name=" + encodeURIComponent(p.project_name) + "&sponsor=" + encodeURIComponent(p.sponsor || "");
    return fetch("/api/cig/history" + q).then(r => r.ok ? r.json() : Promise.reject(r.status)).then(d => d.history || []);
  }

  // CIG pipeline table. `projects` is the array from /api/cig. Clicking a row shows its milestone
  // dates; "Show snapshot history" loads its month-by-month changes. opts.fetchHistory(project) can
  // supply history from another endpoint (default: /api/cig/history on the same host); opts.apiBase is
  // the host serving the FTA profile PDFs (default: same host).
  // Mode comes only from FTA: the dashboard's exclusive-BRT column or the project's FTA profile
  // (cig.py); where neither states it the table says "Unspecified" rather than guessing.
  const MODE_NOTE = "Mode from FTA sources (the dashboard's exclusive-BRT column or the project's FTA profile); "
    + "Unspecified where they don't state it.";

  // Sortable columns: [header, key, value(project) for sorting, first-click direction]. Blank values
  // (TBD, unrated, Unspecified) always sort last. Default order is the API's (largest CIG request first).
  const RATING_RANK = { H: 5, MH: 4, M: 3, ML: 2, L: 1 };
  const PHASE_RANK = { PD: 1, Eng: 2, Const: 3, FFGA: 4, CGA: 5 };
  const SEASON = { early: 0.15, winter: 0.1, spring: 0.3, mid: 0.5, summer: 0.55, fall: 0.75, autumn: 0.75, late: 0.9 };
  const MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"];
  function grantWhen(s) {  // "Spring 2027" -> 2027.3, "August 2026" -> 2026.6, "2028" -> 2028.5, TBD -> null
    const m = /(20\d\d)/.exec(s || "");
    if (!m) return null;
    const w = (s || "").toLowerCase(), mi = MONTHS.findIndex(x => w.includes(x));
    const season = Object.keys(SEASON).find(k => w.includes(k));
    return +m[1] + (mi >= 0 ? (mi + 0.5) / 12 : season ? SEASON[season] : 0.5);
  }
  function pct(s) { const m = /(\d+(\.\d+)?)\s*%/.exec(s || ""); return m ? +m[1] : null; }
  const CIG_COLS = [
    ["Project", "project_name", p => (p.project_name || "").toLowerCase(), "asc"],
    ["Sponsor", "sponsor", p => (p.sponsor || "").toLowerCase() || null, "asc"],
    ["Location", "location", p => ((p.state || "") + " " + (p.city || "")).toLowerCase().trim() || null, "asc"],
    ["Mode", "mode", p => p.mode ? p.mode.toLowerCase() : null, "asc"],
    ["Phase", "phase", p => PHASE_RANK[p.phase] || null, "asc"],
    ["Cost", "cost_musd", p => p.cost_musd, "desc"],
    ["CIG", "cig_request_musd", p => p.cig_request_musd, "desc"],
    ["Share", "cig_share", p => pct(p.cig_share), "desc"],
    ["Rating", "rating", p => RATING_RANK[p.rating] || null, "desc"],
    ["Est. grant", "est_grant", p => grantWhen(p.est_grant), "asc"],
  ];
  function sortProjects(list, key, dir) {
    const col = CIG_COLS.find(c => c[1] === key);
    if (!col) return list.slice();
    const f = col[2], s = dir === "desc" ? -1 : 1;
    return list.map((p, i) => [p, f(p), i]).sort((a, b) => {
      const x = a[1], y = b[1];
      if (x == null || x === "") return (y == null || y === "") ? a[2] - b[2] : 1;
      if (y == null || y === "") return -1;
      return (x < y ? -1 : x > y ? 1 : a[2] - b[2]) * s;  // ties keep the default order
    }).map(t => t[0]);
  }

  function renderCigTable(el, projects, opts) {
    opts = opts || {};
    if (!projects || !projects.length) { el.innerHTML = '<div class="t411-empty">' + esc(opts.emptyText || "No projects.") + '</div>'; return; }
    // Keep the chosen sort across re-renders (e.g. a phase filter or a live refresh).
    const sort = (el._t411 && el._t411.sort) || null;
    const original = projects;
    if (sort) projects = sortProjects(projects, sort.key, sort.dir);
    // opts.profileVersions(project) -> Promise of /api/cig/profile-versions data (Command Center only):
    // adds a "Profile versions" button to each project and a "profile updated" badge.
    el._t411 = { projects: projects, original: original, opts: opts, sort: sort,
                 fetchHistory: opts.fetchHistory || defaultHistory, apiBase: opts.apiBase || "",
                 profileVersions: opts.profileVersions, profileOpts: opts.profileOpts || {} };
    const heads = CIG_COLS.map(c => {
      const on = sort && sort.key === c[1];
      const label = c[1] === "mode" ? 'Mode<sup class="t411-fn">*</sup>' : esc(c[0]);
      return '<th aria-sort="' + (on ? (sort.dir === "asc" ? "ascending" : "descending") : "none") + '"'
        + (c[1] === "mode" ? ' title="' + esc(MODE_NOTE) + '"' : '')
        + '><button type="button" class="t411-sort' + (on ? " on" : "") + '" data-sort="' + c[1] + '">' + label
        + '<span class="t411-sort-ic" aria-hidden="true">' + (on ? (sort.dir === "asc" ? "▲" : "▼") : "↕") + '</span></button></th>';
    }).join("");
    el.innerHTML = '<div class="t411-card"><div class="t411-scroll"><table class="t411-table"><thead><tr>'
      + heads + '</tr></thead><tbody>' + projects.map((p, i) => cigRow(p, i, opts)).join("") + '</tbody></table></div>'
      + '<div class="t411-footnote"><sup class="t411-fn">*</sup> ' + esc(MODE_NOTE) + '</div>'
      + '</div>';
    if (el._t411Bound) return;
    el._t411Bound = true;
    el.addEventListener("click", function (e) {
      const st = el._t411;
      const sb = e.target.closest("[data-sort]");
      if (sb) {  // first click: the column's natural direction; again: reverse; third: back to default
        const col = CIG_COLS.find(c => c[1] === sb.dataset.sort), cur = st.sort;
        let next = { key: col[1], dir: col[3] };
        if (cur && cur.key === col[1]) next = cur.dir === col[3] ? { key: col[1], dir: col[3] === "asc" ? "desc" : "asc" } : null;
        st.sort = next;
        renderCigTable(el, st.original, st.opts);
        const b = el.querySelector('[data-sort="' + col[1] + '"]');
        if (b) b.focus();
        return;
      }
      const vb = e.target.closest("[data-pv]");
      if (vb && st.profileVersions) {
        const p = st.projects[+vb.dataset.pv], box = vb.parentElement.querySelector(".t411-pv-box");
        box.innerHTML = '<div class="t411-empty">Loading...</div>';
        st.profileVersions(p).then(d => renderCigProfileVersions(box, d, Object.assign({ project: p }, st.profileOpts)))
          .catch(() => { box.innerHTML = '<div class="t411-empty">Could not load profile versions.</div>'; });
        return;
      }
      if (e.target.closest(".t411-pv-box")) return;  // clicks inside the versions panel are its own
      const hb = e.target.closest("[data-hist]");
      if (hb) {
        const p = st.projects[+hb.dataset.hist], box = hb.parentElement.querySelector(".t411-tl-box");
        box.innerHTML = '<div class="t411-empty">Loading...</div>';
        st.fetchHistory(p).then(h => renderCigTimeline(box, h))
          .catch(() => { box.innerHTML = '<div class="t411-empty">Could not load history.</div>'; });
        return;
      }
      const row = e.target.closest(".t411-row");
      if (!row) return;
      const det = el.querySelector('.t411-detail[data-i="' + row.dataset.i + '"]');
      if (!det) return;
      if (det.hidden) renderCigMilestones(det.querySelector(".t411-ms-box"), st.projects[+row.dataset.i], st);
      det.hidden = !det.hidden;
    });
  }

  // Per-project milestone dates (from a /api/cig row), plus a link to its FTA project profile PDF.
  function renderCigMilestones(el, p, opts) {
    const base = (opts && opts.apiBase) || "";
    const links = [];
    if (p.profile_file) links.push('<a class="t411-prof" href="' + esc(base + "/api/cig/profile?file=" + encodeURIComponent(p.profile_file))
      + '" target="_blank" rel="noopener noreferrer">FTA project profile (PDF)</a>');
    if (/^https:\/\/(www\.)?transit\.dot\.gov\//.test(p.profile_url || ""))
      links.push('<a class="t411-prof" href="' + esc(p.profile_url) + '" target="_blank" rel="noopener noreferrer">Profile page on transit.dot.gov</a>');
    const prof = links.length ? '<div class="t411-prof-links">' + links.join(" ") + '</div>' : '';
    const items = [["PD entry", p.pd_entry], ["NEPA complete", p.nepa], ["Engineering entry", p.eng_entry],
      ["LONP request", p.lonp_req], ["LONP decision", p.lonp_dec], ["LONP action", p.lonp_action],
      ["Rating requested", p.req_rating_date], ["Project rated", p.proj_rating_date],
      ["Overall rating", p.rating ? p.rating + (RATING[p.rating] ? " (" + RATING[p.rating] + ")" : "") : null],
      ["Local match", p.noncig_status], ["Est. grant", p.est_grant]];
    const chips = items.filter(x => x[1]).map(x =>
      '<span class="t411-ms"><span class="t411-ms-k">' + x[0] + '</span><span class="t411-ms-v">' + esc(x[1]) + '</span></span>').join("");
    el.innerHTML = (chips || '<div class="t411-empty">No milestone dates recorded.</div>') + prof;
  }

  // Archived versions of a project's FTA profile (from /api/cig/profile-versions), newest first, each
  // with its PDF and, when FTA revised it, a diff against the version before. opts.fileUrl(id),
  // opts.fetchDiff(id) -> Promise of {diff}, and optionally opts.onUpload(project, file) -> Promise
  // (an "Upload a newer version" button) come from the host page.
  const PV_SRC = { seed: "first download", upload: "upload", inbox: "inbox", weekly: "inbox (weekly)", manual: "inbox",
                   fta: "fetched from FTA", "command line": "command line" };
  function renderCigProfileVersions(el, data, opts) {
    opts = opts || {};
    const v = (data && data.versions) || [];
    const rows = v.map((x, i) => {
      const badge = !x.prev_version_id ? '<span class="t411-badge">' + (i === v.length - 1 ? "baseline" : "first") + '</span>'
        : '<span class="t411-badge t411-k-phase">changed</span> <span class="t411-pv-n">+' + (x.lines_added || 0) + ' / −' + (x.lines_removed || 0) + ' lines</span>';
      return '<div class="t411-pv-row"><span class="t411-tl-date">' + esc(x.captured_at) + '</span>' + badge
        + (x.fta_date ? ' <span class="t411-pv-n" title="Date in FTA\'s file name">FTA dated ' + esc(x.fta_date) + '</span>' : '')
        + ' <span class="t411-pv-n">' + esc(PV_SRC[x.source] || x.source || "") + '</span>'
        + (x.has_file && opts.fileUrl ? ' <a href="' + esc(opts.fileUrl(x.id)) + '" target="_blank" rel="noopener" title="' + esc(x.file_name || "") + '">PDF</a>' : '')
        + (x.prev_version_id && opts.fetchDiff ? ' <button type="button" class="t411-linkbtn" data-diff="' + x.id + '">Show changes</button>' : '')
        + '<div class="t411-diff-box" data-for="' + x.id + '"></div></div>';
    }).join("");
    const head = '<div class="t411-ms-title">FTA profile versions</div>';
    const up = opts.onUpload ? '<button type="button" class="t411-btn" data-pvup="1">Upload a newer version</button><input type="file" accept="application/pdf,.pdf" hidden>' : '';
    el.innerHTML = '<div class="t411-pv">' + head + (rows || '<div class="t411-empty">No archived profile yet.</div>') + up + '<div class="t411-pv-msg"></div></div>';
    el.onclick = function (e) {
      const db = e.target.closest("[data-diff]");
      if (db) {
        const box = el.querySelector('.t411-diff-box[data-for="' + db.dataset.diff + '"]');
        if (box.innerHTML) { box.innerHTML = ""; db.textContent = "Show changes"; return; }
        box.innerHTML = '<div class="t411-empty">Loading...</div>';
        opts.fetchDiff(+db.dataset.diff).then(d => { box.innerHTML = diffHtml(d.diff); db.textContent = "Hide changes"; })
          .catch(() => { box.innerHTML = '<div class="t411-empty">Could not load the changes.</div>'; });
        return;
      }
      if (e.target.closest("[data-pvup]")) el.querySelector('input[type="file"]').click();
    };
    const inp = el.querySelector('input[type="file"]');
    if (inp) inp.onchange = function () {
      const f = inp.files[0]; inp.value = ""; if (!f) return;
      const msg = el.querySelector(".t411-pv-msg"); msg.textContent = "Reading " + f.name + "...";
      opts.onUpload(opts.project, f).then(r => { msg.textContent = r.text || ""; if (r.reload) r.reload(); })
        .catch(err => { msg.textContent = String(err && err.message || err); });
    };
  }
  function diffHtml(diff) {
    if (!diff) return '<div class="t411-empty">No text changes recorded.</div>';
    return '<pre class="t411-diff">' + diff.split("\n").filter(l => !/^(---|\+\+\+) /.test(l)).map(l =>
      '<span class="' + (l.startsWith("+") ? "t411-d-add" : l.startsWith("-") ? "t411-d-del" : l.startsWith("@@") ? "t411-d-hunk" : "") + '">'
      + esc(l.startsWith("@@") ? "…" : l) + '</span>').join("\n") + '</pre>';
  }

  // Snapshot history / change log (from /api/cig/history). Shows what moved each month.
  function renderCigTimeline(el, history) {
    if (!history || history.length < 2) {
      el.innerHTML = '<div class="t411-empty">Only one snapshot so far — history builds as monthly dashboards load.</div>'; return;
    }
    const rows = history.map((h, i) => {
      const prev = history[i - 1]; let ch = [];
      if (prev) {
        if (h.phase !== prev.phase) ch.push("phase " + esc(prev.phase || "?") + " → " + esc(h.phase || "?"));
        if (h.rating !== prev.rating) ch.push("rating " + esc(prev.rating || "?") + " → " + esc(h.rating || "?"));
        if (h.est_grant !== prev.est_grant) ch.push("est. grant " + esc(prev.est_grant || "?") + " → " + esc(h.est_grant || "?"));
        if (h.cig_request_musd !== prev.cig_request_musd) ch.push("CIG " + amt(prev.cig_request_musd) + " → " + amt(h.cig_request_musd));
        if (h.cost_musd !== prev.cost_musd) ch.push("cost " + amt(prev.cost_musd) + " → " + amt(h.cost_musd));
      }
      const badge = i === 0 ? "first seen" : (ch.length ? ch.join(" · ") : "no change");
      return '<div class="t411-tl-row"><span class="t411-tl-dot"></span><span class="t411-tl-date">' + esc(h.snapshot_date) + '</span><span>' + badge + '</span></div>';
    }).join("");
    el.innerHTML = '<div class="t411-tl">' + rows + '</div>';
  }

  // "What changed" between two dashboard snapshots (from /api/cig/changes). opts.onProject(name, sponsor)
  // is called when a project name is clicked; opts.headerExtra is HTML placed in the header (e.g. a
  // compare-with selector).
  const KIND = {
    new: ["New", "t411-k-new"], dropped: ["Dropped", "t411-k-drop"], phase: ["Phase", "t411-k-phase"],
    rating: ["Rating", "t411-k-rating"], est_grant: ["Grant date", "t411-k-date"], cig_request_musd: ["CIG request", "t411-k-money"],
    cost_musd: ["Cost", "t411-k-money"], cig_share: ["CIG share", "t411-k-money"], noncig_status: ["Local match", "t411-k-date"]
  };
  function day(iso) {
    if (!iso) return "?";
    const d = new Date(iso + "T12:00:00");
    return isNaN(d) ? esc(iso) : d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
  }
  function val(v, money) { return v == null || v === "" ? "—" : money ? amt(v) : esc(v); }
  function changeText(c) {
    const d = c.detail || {};
    if (c.type === "new") return "entered the pipeline" + (d.phase ? " in " + esc(d.phase) : "") + (d.cig_request_musd != null ? " · CIG " + amt(d.cig_request_musd) : "");
    if (c.type === "dropped") return "no longer listed" + (d.phase ? " (was " + esc(d.phase) + (d.cig_request_musd != null ? ", CIG " + amt(d.cig_request_musd) : "") + ")" : "");
    const money = c.type.endsWith("_musd");
    let t = val(c.before, money) + " → " + val(c.after, money);
    if (c.type === "phase" && c.direction) t += c.direction === "advanced" ? " (advanced)" : " (moved back)";
    if (money && c.delta != null) t += " (" + (c.delta > 0 ? "+" : c.delta < 0 ? "-" : "") + amt(Math.abs(c.delta)) + (c.pct != null ? ", " + (c.pct > 0 ? "+" : "") + c.pct + "%" : "") + ")";
    return t;
  }
  function renderCigChanges(el, data, opts) {
    opts = opts || {};
    data = data || {};
    const head = '<div class="t411-ch-head"><div class="t411-ch-title">What changed'
      + (data.from ? ' <span class="t411-ch-range">' + day(data.from) + ' → ' + day(data.to) + '</span>' : '') + '</div>'
      + (opts.headerExtra || "") + '</div>';
    if (!data.from) {
      el.innerHTML = '<div class="t411-card t411-ch">' + head + '<div class="t411-empty">'
        + ((data.snapshots || []).length ? "Only one dashboard loaded so far — changes appear once a second month is loaded."
                                         : "No dashboards loaded yet.") + '</div></div>';
      return;
    }
    const list = data.changes || [];
    const counts = {};
    list.forEach(c => { counts[c.type] = (counts[c.type] || 0) + 1; });
    const summary = Object.keys(KIND).filter(k => counts[k]).map(k =>
      '<span class="t411-badge ' + KIND[k][1] + '">' + counts[k] + ' ' + KIND[k][0].toLowerCase() + '</span>').join(" ");
    const rows = list.map(c => {
      const k = KIND[c.type] || [c.label || c.type, ""];
      const dirCls = c.direction === "back" ? " t411-k-back" : "";
      return '<div class="t411-ch-row"><span class="t411-badge ' + k[1] + dirCls + '">' + esc(k[0]) + '</span>'
        + '<span class="t411-ch-main"><button type="button" class="t411-ch-proj" data-name="' + esc(c.project_name) + '" data-sponsor="' + esc(c.sponsor || "") + '">'
        + esc(c.project_name) + '</button> <span class="t411-ch-sub">' + esc(c.sponsor || "") + (c.state ? ", " + esc(c.state) : "") + '</span>'
        + '<span class="t411-ch-what">' + changeText(c) + '</span></span></div>';
    }).join("");
    el.innerHTML = '<div class="t411-card t411-ch">' + head
      + (list.length ? '<div class="t411-ch-sum">' + summary + '</div>' + rows
                     : '<div class="t411-empty">No changes between these two dashboards.</div>') + '</div>';
    if (!el._t411ChBound) {
      el._t411ChBound = true;
      el.addEventListener("click", e => {
        const b = e.target.closest(".t411-ch-proj");
        if (b && el._t411ChOpts && el._t411ChOpts.onProject) el._t411ChOpts.onProject(b.dataset.name, b.dataset.sponsor);
      });
    }
    el._t411ChOpts = opts;
  }

  // Open (and scroll to) a project's row in a table drawn by renderCigTable. Returns false if it
  // isn't in the table (e.g. filtered out, or a dropped project).
  function openCigProject(tableEl, name, sponsor) {
    const st = tableEl._t411;
    if (!st) return false;
    const i = st.projects.findIndex(p => p.project_name === name && (p.sponsor || "") === (sponsor || ""));
    if (i < 0) return false;
    const row = tableEl.querySelector('.t411-row[data-i="' + i + '"]');
    const det = tableEl.querySelector('.t411-detail[data-i="' + i + '"]');
    if (det && det.hidden) row.click();
    row.scrollIntoView({ behavior: "smooth", block: "center" });
    row.classList.add("t411-flash");
    setTimeout(() => row.classList.remove("t411-flash"), 1600);
    return true;
  }

  // ---- Ask NTD / Ask CIG answers ---------------------------------------------------------------
  // Column-type formatting (same rules as the Command Center): money by name, *_musd = $ millions,
  // percent/ratio/year columns, big numbers shortened to M/B.
  const LABELS = {
    upt: "Trips", agency: "Agency", mode: "Mode", mode_code: "Mode code", state: "State", city: "City",
    ntd_id: "NTD ID", report_year: "Year", operating_expense: "Operating expense", cost_per_rider: "Cost per rider",
    fare_recovery: "Fare recovery", fares: "Fares", voms: "Peak vehicles", passenger_miles: "Passenger miles",
    vehicle_revenue_miles: "Revenue miles", vehicle_revenue_hours: "Revenue hours",
    cost_per_revenue_hour: "Cost per revenue hour", avg_trip_miles: "Avg trip (miles)", project_name: "Project",
    sponsor: "Sponsor", phase: "Phase", rating: "Rating", cig_share: "CIG share", est_grant: "Est. grant",
    noncig_status: "Local match", snapshot_date: "Snapshot", cost_musd: "Cost", cig_request_musd: "CIG request",
  };
  function colLabel(c) {
    const real = /_real(_|$)/.test(c);
    const base = c.replace(/_real(_|$)/, "$1").replace(/_$/, "");
    let s = LABELS[base] || base.replace(/_musd$/, "").replace(/_/g, " ");
    s = s.charAt(0).toUpperCase() + s.slice(1);
    return real ? s + " (inflation-adj.)" : s;
  }
  function colKind(c, rows) {
    const n = c.toLowerCase(), vals = rows.map(r => r[c]).filter(v => v != null);
    if (!vals.length || !vals.every(v => typeof v === "number" && isFinite(v))) return "text";
    if (/(^|_)(year|yr)$|^year/.test(n)) return "year";
    if (/(^|_)id$/.test(n)) return "text";
    if (/musd/.test(n)) return "musd";
    if (/percent|pct/.test(n)) return "pct";
    if (/recovery|ratio/.test(n)) return "ratio";
    if (/factor|cpi/.test(n)) return "num";
    if (/cost|expense|opex|fare|dollar|spend|price|cpr|_real/.test(n)) return "money";
    return "num";
  }
  function fmtCell(v, k) {
    if (v == null) return "—";
    if (k === "text") return String(v);
    if (k === "year") return String(Math.round(v));
    if (k === "pct") return v.toFixed(1) + "%";
    if (k === "ratio") return (v * 100).toFixed(1) + "%";
    if (k === "musd") return (v < 0 ? "-" : "") + "$" + Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 1 }) + "M";
    const a = Math.abs(v); let s;
    if (a >= 1e9) s = (a / 1e9).toFixed(2) + "B";
    else if (a >= 1e6) s = (a / 1e6).toFixed(1) + "M";
    else if (Number.isInteger(v) || a >= 1000) s = Math.round(a).toLocaleString();
    else s = a.toLocaleString(undefined, { minimumFractionDigits: k === "money" ? 2 : 0, maximumFractionDigits: 2 });
    return (v < 0 ? "-" : "") + (k === "money" ? "$" : "") + s;
  }

  // Readable message from an API error body ({"detail": "..."} or {"detail": {"error": ...}}).
  function errorText(body, status) {
    let t = body;
    for (let i = 0; i < 2; i++) {
      try { const d = JSON.parse(t).detail; t = typeof d === "string" ? d : (d && d.error) || JSON.stringify(d); } catch (_) { break; }
    }
    return t || ("Something went wrong (HTTP " + status + ").");
  }

  // One answer card: heading, table and the SQL that produced it. `res` is the /api/ask or
  // /api/cig/ask response ({question, sql, columns, rows}; rows may be objects or arrays).
  function askCardHtml(res, opts) {
    opts = opts || {};
    const cols = res.columns || [];
    const rows = (res.rows || []).map(r => Array.isArray(r) ? Object.fromEntries(cols.map((c, i) => [c, r[i]])) : r);
    const kinds = Object.fromEntries(cols.map(c => [c, colKind(c, rows)]));
    const head = '<div class="t411-ask-head">' + (opts.followUp ? '<span class="t411-ask-fu">Follow-up</span>' : "")
      + esc(res.question) + ' <span class="t411-ask-n">' + rows.length + (rows.length === 1 ? " row" : " rows") + '</span></div>';
    const table = rows.length
      ? '<div class="t411-scroll"><table class="t411-table"><thead><tr>' + cols.map(c => '<th class="' + (kinds[c] === "text" ? "" : "num") + '" title="' + esc(c) + '">' + esc(colLabel(c)) + '</th>').join("")
        + '</tr></thead><tbody>' + rows.map(r => '<tr>' + cols.map(c => '<td class="' + (kinds[c] === "text" ? "" : "num") + '">' + esc(fmtCell(r[c], kinds[c])) + '</td>').join("") + '</tr>').join("")
        + '</tbody></table></div>'
      : '<div class="t411-empty">No matching rows. Try different years, modes, agencies or wording.</div>';
    const sql = res.sql ? '<details class="t411-sql"><summary>Show the query used to answer this</summary><pre>' + esc(res.sql) + '</pre></details>' : "";
    return '<div class="t411-card t411-ask">' + head + table + sql + '</div>';
  }


  // ---- Data cards: turn an Ask answer into a branded, exportable card -------------------------
  // The card TYPE is decided from the shape of the result, never by a model: same answer, same
  // card, every time, at no cost. pickCardType() is pure and testable.
  const CARD_MONEY = /(cost|expense|fare|revenue|musd|budget|funding|request|\$)/i;
  const CARD_YEAR = /^(report_)?year$|_year$|^snapshot_date$|_date$/i;
  const CARD_NAME = /(agency|name|project|sponsor|city|state|mode|system|pillar)/i;
  const CARD_NOT_MEASURE = /(^|_)id$|^ntd_id$|_code$/i;

  function cardNumericCols(columns, rows) {
    return columns.filter(c => {
      const vals = rows.map(r => cellOf(r, c, columns)).filter(v => v != null && v !== "");
      return vals.length > 0 && vals.every(v => typeof v === "number" && isFinite(v));
    });
  }
  // Ask NTD returns rows as objects, Ask CIG as arrays; one accessor covers both.
  function cellOf(row, col, columns) {
    return Array.isArray(row) ? row[columns.indexOf(col)] : row[col];
  }
  function cardRowObjects(columns, rows) {
    return rows.map(r => Object.fromEntries(columns.map(c => [c, cellOf(r, c, columns)])));
  }

  function pickCardType(columns, rows) {
    columns = columns || []; rows = rows || [];
    const years = columns.filter(c => CARD_YEAR.test(c));
    // A year is an axis, never the measure - otherwise "2024" becomes the headline number.
    const nums = cardNumericCols(columns, rows).filter(c => !years.includes(c) && !CARD_NOT_MEASURE.test(c));
    const names = columns.filter(c => CARD_NAME.test(c) && !nums.includes(c));
    const why = [];
    let type = "table";
    if (rows.length === 1 && nums.length >= 1) {
      type = "stat"; why.push("one row with a number");
    } else if (rows.length >= 3 && years.length >= 1 && nums.length >= 1) {
      type = "trend"; why.push("a year/date column across " + rows.length + " rows");
    } else if (rows.length >= 2 && rows.length <= 5 && names.length >= 1 && nums.length >= 1) {
      type = "comparison"; why.push(rows.length + " named things on one measure");
    } else if (rows.length >= 2 && names.length >= 1 && nums.length >= 1) {
      type = "ranked"; why.push(rows.length + " rows ranked by a number");
    } else {
      why.push("no numeric/name shape to key off");
    }
    // Ask sorts by whatever the question asked for, so the ordered column is the measure -
    // better than "first numeric", which picks ridership when the question was about cost.
    const ordered = nums.find(c => {
      const vals = rows.map(r => cellOf(r, c, columns));
      if (vals.length < 3 || vals.some(v => typeof v !== "number")) return false;
      let up = true, down = true;
      for (let i = 1; i < vals.length; i++) { if (vals[i] > vals[i - 1]) down = false; if (vals[i] < vals[i - 1]) up = false; }
      return (up || down) && vals[0] !== vals[vals.length - 1];
    });
    return {
      type,
      reason: why.join("; "),
      valueCol: (type === "trend" ? (nums[0] || null) : (ordered || nums[0] || null)),
      labelCol: names[0] || (columns.find(c => !nums.includes(c)) || null),
      yearCol: years[0] || null,
      numericCols: nums,
      money: CARD_MONEY.test((type === "trend" ? nums[0] : (ordered || nums[0])) || ""),
    };
  }


  // ---- Card renderer. Cards are SVG so the export is the same artwork as the screen, not a
  // second layout that drifts. Portrait (social) and landscape (slide) share one frame.
  const CARD = {
    ink: "#17140F", paper: "#F7F4ED", red: "#C0341F", muted: "#6A6458", line: "#D8D2C4",
    soft: "#E7E1D4", dim: "#8A8376", gen: "#9A9384", good: "#2E7D52",
    sans: "Archivo, 'Helvetica Neue', Helvetica, Arial, sans-serif",
    serif: "Spectral, Georgia, 'Times New Roman', serif",
    mono: "'JetBrains Mono', ui-monospace, 'Courier New', monospace",
  };
  const CARD_SIZES = { portrait: { w: 680, h: 850 }, landscape: { w: 1000, h: 563 } };

  function sv(tag, attrs, inner) {
    const a = Object.entries(attrs || {}).map(([k, v]) => k + '="' + String(v).replace(/"/g, "&quot;") + '"').join(" ");
    return "<" + tag + (a ? " " + a : "") + (inner != null ? ">" + inner + "</" + tag + ">" : "/>");
  }
  function svText(x, y, s, o) {
    o = o || {};
    return sv("text", {
      x: x, y: y, fill: o.fill || CARD.ink, "font-family": o.font || CARD.sans,
      "font-size": o.size || 14, "font-weight": o.weight || 400,
      "letter-spacing": o.track != null ? o.track : 0,
      "text-anchor": o.anchor || "start",
    }, esc(s));
  }
  // Wrap on width, measured by an average glyph width for the size - close enough for a fixed card
  // and it keeps the renderer dependency-free.
  function svWrap(text, size, width, max) {
    const per = size * 0.54, perLine = Math.max(8, Math.floor(width / per));
    const words = String(text || "").split(/\s+/), lines = [];
    let line = "";
    for (const w of words) {
      const next = line ? line + " " + w : w;
      if (next.length > perLine && line) { lines.push(line); line = w; } else { line = next; }
      if (max && lines.length >= max) break;
    }
    if (line && (!max || lines.length < max)) lines.push(line);
    if (max && lines.length === max && words.join(" ").length > lines.join(" ").length) {
      lines[max - 1] = lines[max - 1].replace(/[\s,.;:]+$/, "") + "…";
    }
    return lines;
  }
  function cardNum(v, money) {
    if (v == null || v === "") return "—";
    if (typeof v !== "number") return String(v);
    const abs = Math.abs(v);
    const unit = abs >= 1e9 ? [1e9, "B"] : abs >= 1e6 ? [1e6, "M"] : abs >= 1e3 && !money ? [1e3, "K"] : [1, ""];
    const n = v / unit[0];
    // A small dollar figure keeps its cents - $15.80, never $15.8, which reads like a typo in a
    // column of four-character prices. Everything else drops trailing zeros.
    const cents = money && abs < 1e3;
    const s = n.toLocaleString(undefined, {
      minimumFractionDigits: cents ? 2 : 0,
      maximumFractionDigits: cents || !Number.isInteger(n) ? 2 : 0,
    });
    return (money ? "$" : "") + s + unit[1];
  }

  // One line clipped to a pixel width, with an ellipsis. Character widths are approximated (narrow
  // letters really are narrower) rather than measured, so the same string fits the same way whether
  // the card is drawn in a browser or in Node - and a long agency name stops at the label column
  // instead of running under the bars.
  const CARD_NARROW = "ijltfrI.,:;'\"!|()[]{} ";
  const CARD_WIDE = "mwMW@%";
  function textW(s, size) {
    let u = 0;
    for (const ch of String(s == null ? "" : s)) {
      u += CARD_NARROW.indexOf(ch) >= 0 ? 0.34 : CARD_WIDE.indexOf(ch) >= 0 ? 0.92 : 0.58;
    }
    return u * size;
  }
  function svFit(s, maxPx, size) {
    s = String(s == null ? "" : s);
    if (textW(s, size) <= maxPx) return s;
    let out = "";
    for (const ch of s) {
      if (textW(out + ch + "…", size) > maxPx) break;
      out += ch;
    }
    while (out && " ,.;:-–—".indexOf(out[out.length - 1]) >= 0) out = out.slice(0, -1);
    return out + "…";
  }

  // The permanent frame: wordmark, badge, kicker, headline, and the source footer. The source line
  // is NOT optional - it is the credibility of the card and it always renders.
  function cardFrame(W, H, spec, opts, bodyFn) {
    const pad = 30, headH = 62;
    let y = headH;
    const parts = [
      sv("rect", { x: 0, y: 0, width: W, height: H, fill: CARD.paper }),
      sv("rect", { x: 0, y: headH - 3, width: W, height: 3, fill: CARD.ink }),
      svText(pad, 40, "TRANSIT", { size: 21, weight: 900, track: -1 }),
      svText(pad + 92, 40, "411", { size: 21, weight: 900, track: -1, fill: CARD.red }),
      svText(W - pad, 39, (spec.tool || "Ask NTD") + " · Data Card",
        { size: 10, weight: 800, track: 2, fill: CARD.muted, anchor: "end" }),
    ];
    // Title block
    y += 34;
    if (spec.kicker) {
      parts.push(svText(pad, y, spec.kicker.toUpperCase(), { size: 11, weight: 800, track: 1.5, fill: CARD.red }));
      y += 22;
    }
    const titleSize = W > 800 ? 30 : 27;
    svWrap(spec.title || "", titleSize, W - pad * 2, 3).forEach(l => {
      parts.push(svText(pad, y + titleSize * 0.82, l, { size: titleSize, weight: 800, track: -0.5 }));
      y += titleSize * 1.12;
    });
    if (spec.deck && opts.methodology !== "only") {
      svWrap(spec.deck, 15, W - pad * 2, 2).forEach(l => {
        y += 20; parts.push(svText(pad, y, l, { size: 15, font: CARD.serif, fill: CARD.muted }));
      });
    }
    // Footer first (fixed to the bottom), so the body knows the space it has
    const footTop = H - (spec.methodology && opts.methodology ? 96 : 78);
    parts.push(sv("rect", { x: 0, y: footTop, width: W, height: 2, fill: CARD.ink }));
    let fy = footTop + 26;
    const srcLines = svWrap("Source: " + (spec.source || "National Transit Database"), 12, W - pad * 2, 2);
    srcLines.forEach(l => { parts.push(svText(pad, fy, l, { size: 12, font: CARD.serif, fill: CARD.muted })); fy += 17; });
    if (spec.methodology && opts.methodology) {
      svWrap(spec.methodology, 12, W - pad * 2, 2).forEach(l => {
        parts.push(svText(pad, fy, l, { size: 12, font: CARD.serif, fill: CARD.muted })); fy += 17;
      });
    }
    parts.push(svText(pad, H - 22, "transit411", { size: 13, weight: 800 }));
    parts.push(svText(pad + 74, H - 22, ".net", { size: 13, weight: 800, fill: CARD.red }));
    parts.push(svText(W - pad, H - 22, "Generated with " + (spec.tool || "Ask NTD"),
      { size: 10, track: 1, fill: CARD.gen, anchor: "end" }));
    // Body, between the title and the footer
    parts.push(bodyFn(y + 10, footTop - 16, pad, W));
    return parts.join("");
  }

  // ---- the four bodies ---------------------------------------------------------------------

  // The benchmark strip - what the headline number should be read against. Every body can show it,
  // so the "Context stats" toggle means the same thing on all four card types.
  function cardCtx(spec, opts) {
    return opts.context ? (spec.context || []).slice(0, 3) : [];
  }
  const CTX_H = 62;

  // The "so what" line, wrapped to the card - a sentence naming an agency is easily wider than the
  // card, and a single unwrapped line runs straight off the edge. Bodies size their space from
  // takeLines() first, then draw it last.
  function takeLines(spec, opts, W, pad) {
    return (opts.takeaway && spec.takeaway) ? svWrap(spec.takeaway, 15, W - pad * 2, 2) : [];
  }
  function takeHeight(lines) { return lines.length ? lines.length * 20 + 8 : 0; }
  function takeParts(lines, pad, W, bottom) {
    return lines.map((l, i) => svText(pad, bottom - 2 - (lines.length - 1 - i) * 20, l,
      { size: 15, weight: 600, font: CARD.serif })).join("");
  }

  function ctxStrip(ctx, pad, W, cy) {
    if (!ctx.length) return "";
    const cw = (W - pad * 2) / ctx.length, parts = [];
    ctx.forEach((c, i) => {
      const cx = pad + cw * i + cw / 2;
      if (i) parts.push(sv("rect", { x: pad + cw * i, y: cy - 24, width: 1, height: 44, fill: CARD.soft }));
      parts.push(svText(cx, cy, String(c.label).toUpperCase(), { size: 10, weight: 800, track: 1, fill: CARD.dim, anchor: "middle" }));
      parts.push(svText(cx, cy + 24, c.value, { size: 20, weight: 700, font: CARD.mono, anchor: "middle", fill: c.good ? CARD.good : CARD.ink }));
    });
    return parts.join("");
  }

  function bodyStat(spec, opts) {
    return (top, bottom, pad, W) => {
      const mid = (top + bottom) / 2, parts = [];
      const big = W > 800 ? 96 : 88;
      parts.push(svText(W / 2, mid - 10, spec.value, { size: big, weight: 900, track: -3, fill: CARD.red, anchor: "middle" }));
      if (spec.unit) parts.push(svText(W / 2, mid + 24, spec.unit, { size: 16, weight: 700, fill: CARD.muted, anchor: "middle" }));
      parts.push(ctxStrip(cardCtx(spec, opts), pad, W, mid + 76));
      parts.push(takeParts(takeLines(spec, opts, W, pad), pad, W, bottom - 2));
      return parts.join("");
    };
  }

  function bodyBars(spec, opts, ranked) {
    return (top, bottom, pad, W) => {
      const rows = (spec.rows || []).slice(0, ranked ? 8 : 5);
      if (!rows.length) return "";
      const max = Math.max(...rows.map(r => Math.abs(r.value || 0)), 1);
      const nameW = ranked ? 210 : 165, valW = 70;
      const trackX = pad + nameW + 14, trackW = W - pad * 2 - nameW - valW - 28;
      const ctx = cardCtx(spec, opts);
      const tl = takeLines(spec, opts, W, pad), takeH = takeHeight(tl);
      const avail = bottom - top - (ctx.length ? CTX_H : 0) - takeH;
      const gap = Math.min(52, avail / rows.length);
      const barH = Math.max(9, Math.min(26, gap - 14));
      // A landscape card is short: when the rows are tight the city/state line is the first thing
      // to go, because two lines per row would collide before the bars did.
      const showSub = gap >= 34;
      // Few rows shouldn't leave a hole above the footer: centre what there is in the space it has.
      const y0 = top + Math.max(0, (avail - gap * rows.length) / 2);
      const parts = [];
      rows.forEach((r, i) => {
        const y = y0 + gap * i + gap / 2;
        const sub = showSub ? r.sub : null;
        parts.push(svText(pad, y + (sub ? -3 : 4), svFit(r.label, nameW, 15), { size: 15, weight: 700 }));
        if (sub) parts.push(svText(pad, y + 13, svFit(sub, nameW, 11), { size: 11, font: CARD.serif, fill: CARD.dim }));
        if (opts.chart) {
          parts.push(sv("rect", { x: trackX, y: y - barH / 2, width: trackW, height: barH, rx: 5, fill: CARD.soft }));
          parts.push(sv("rect", { x: trackX, y: y - barH / 2, width: Math.max(3, trackW * (Math.abs(r.value || 0) / max)),
                                  height: barH, rx: 5, fill: i === 0 ? CARD.red : CARD.ink }));
        }
        parts.push(svText(W - pad, y + 6, r.display, { size: 16, weight: 700, font: CARD.mono, anchor: "end" }));
      });
      parts.push(ctxStrip(ctx, pad, W, top + avail + 26));
      parts.push(takeParts(tl, pad, W, bottom));
      return parts.join("");
    };
  }

  function bodyTrend(spec, opts) {
    return (top, bottom, pad, W) => {
      const pts = spec.points || [];
      if (pts.length < 2) return bodyBars(spec, opts, true)(top, bottom, pad, W);
      const tl = takeLines(spec, opts, W, pad), takeH = takeHeight(tl);
      const ctx = cardCtx(spec, opts), ctxH = ctx.length ? CTX_H : 0;
      const x0 = pad + 34, x1 = W - pad, y0 = top + 18, y1 = bottom - 34 - takeH - ctxH;
      const vals = pts.map(p => p.value);
      const lo = Math.min(...vals), hi = Math.max(...vals), span = (hi - lo) || 1;
      const px = i => x0 + (x1 - x0) * (pts.length === 1 ? 0.5 : i / (pts.length - 1));
      const py = v => y1 - (y1 - y0) * ((v - lo) / span);
      const parts = [
        sv("line", { x1: x0, y1: y1, x2: x1, y2: y1, stroke: CARD.line, "stroke-width": 1 }),
        sv("line", { x1: x0, y1: y0, x2: x0, y2: y1, stroke: CARD.line, "stroke-width": 1 }),
      ];
      if (opts.chart) {
        parts.push(sv("polyline", { points: pts.map((p, i) => px(i) + "," + py(p.value)).join(" "),
                                    fill: "none", stroke: CARD.red, "stroke-width": 3 }));
        pts.forEach((p, i) => parts.push(sv("circle", { cx: px(i), cy: py(p.value), r: 4.5, fill: CARD.ink })));
      }
      pts.forEach((p, i) => {
        if (pts.length > 8 && i % 2) return;
        parts.push(svText(px(i), y1 + 20, p.label, { size: 12, font: CARD.mono, fill: CARD.muted, anchor: "middle" }));
      });
      const first = pts[0], last = pts[pts.length - 1];
      parts.push(svText(px(0), py(first.value) - 12, first.display, { size: 12, weight: 700, font: CARD.mono, anchor: "middle" }));
      parts.push(svText(px(pts.length - 1), py(last.value) - 12, last.display, { size: 12, weight: 700, font: CARD.mono, fill: CARD.red, anchor: "middle" }));
      parts.push(ctxStrip(ctx, pad, W, y1 + 46));
      parts.push(takeParts(tl, pad, W, bottom));
      return parts.join("");
    };
  }

  function bodyTable(spec, opts) {
    return (top, bottom, pad, W) => {
      const cols = (spec.columns || []).slice(0, 4), rows = (spec.tableRows || []).slice(0, 9);
      const colW = (W - pad * 2) / Math.max(cols.length, 1);
      const ctx = cardCtx(spec, opts), rowsBottom = bottom - 8 - (ctx.length ? CTX_H : 0);
      const parts = [sv("rect", { x: pad, y: top + 14, width: W - pad * 2, height: 1, fill: CARD.ink })];
      cols.forEach((c, i) => parts.push(svText(pad + colW * i, top + 6, svFit(String(c).toUpperCase(), colW - 10, 11),
        { size: 10, weight: 800, track: 1, fill: CARD.dim })));
      rows.forEach((r, ri) => {
        const y = top + 36 + ri * 24;
        if (y > rowsBottom) return;
        cols.forEach((c, ci) => {
          const v = r[ci];
          const isNum = typeof v === "number";
          const s = isNum ? cardNum(v, spec.money) : String(v == null ? "—" : v);
          parts.push(svText(pad + colW * ci, y, svFit(s, colW - 10, 13),
            { size: 13, font: isNum ? CARD.mono : CARD.serif, weight: isNum ? 700 : 400 }));
        });
      });
      parts.push(ctxStrip(ctx, pad, W, bottom - CTX_H + 30));
      return parts.join("");
    };
  }

  // ---- answer -> card spec. Everything here is computed from the returned rows: no model call,
  // no second query. The benchmark (median of the answer's own population) drives the context
  // toggle, and the toggle hides itself when there is nothing to compare against.
  const CARD_DEFAULTS = { context: true, chart: true, takeaway: false, methodology: false };

  function median(nums) {
    const s = nums.filter(n => typeof n === "number" && isFinite(n)).sort((a, b) => a - b);
    if (!s.length) return null;
    const m = Math.floor(s.length / 2);
    return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
  }
  function prettyCol(c) { return String(c || "").replace(/_musd$/, " ($M)").replace(/_/g, " ").replace(/\b\w/g, m => m.toUpperCase()); }

  function cardSpecFromAnswer(answer, opts) {
    opts = opts || {};
    const columns = answer.columns || [];
    const rows = answer.rows || [];
    const pick = pickCardType(columns, rows);
    const objs = cardRowObjects(columns, rows);
    const money = pick.money;
    const val = r => (pick.valueCol ? r[pick.valueCol] : null);
    const label = r => (pick.labelCol ? r[pick.labelCol] : "");
    const tool = opts.tool || "Ask NTD";
    const spec = {
      type: pick.type, tool, columns,
      title: opts.title || answer.question || "Transit411 data card",
      kicker: opts.kicker || (tool === "Ask CIG" ? "FTA Capital Investment Grants" : "National Transit Database"),
      deck: opts.deck || null,
      source: opts.source || (tool === "Ask CIG"
        ? "FTA Capital Investment Grants dashboard, latest monthly snapshot."
        : "National Transit Database, latest reported year."),
      methodology: opts.methodology || (answer.sql ? "Figures are the query's own output; the generated SQL is shown with the answer on transit411.net." : null),
      money, rowCount: rows.length,
      tableRows: rows.map(r => columns.map(c => cellOf(r, c, columns))),
      reason: pick.reason,
    };
    const med = median(objs.map(val));
    if (pick.type === "stat") {
      const r = objs[0] || {};
      spec.value = cardNum(val(r), money);
      spec.unit = prettyCol(pick.valueCol);
      spec.context = [];
      // Other numbers on the same row make honest context (e.g. trips alongside cost per rider).
      pick.numericCols.slice(1, 3).forEach(c => spec.context.push({ label: prettyCol(c), value: cardNum(r[c], CARD_MONEY.test(c)) }));
      if (label(r)) spec.title = opts.title || (label(r) + " — " + prettyCol(pick.valueCol));
    } else if (pick.type === "trend") {
      const sorted = objs.slice().sort((a, b) => String(a[pick.yearCol]).localeCompare(String(b[pick.yearCol])));
      spec.points = sorted.map(r => ({ label: String(r[pick.yearCol]).slice(0, 10), value: val(r), display: cardNum(val(r), money) }));
      const a = spec.points[0], b = spec.points[spec.points.length - 1];
      if (a && b && a.value) {
        const change = Math.round(((b.value - a.value) / Math.abs(a.value)) * 100);
        spec.takeaway = (change >= 0 ? "Up " : "Down ") + Math.abs(change) + "% since " + a.label + ".";
      }
    } else if (pick.type === "table") {
      spec.tableRows = spec.tableRows.slice(0, 9);
    } else {
      const sorted = objs.slice().sort((a, b) => (val(b) || 0) - (val(a) || 0));
      spec.rows = sorted.map(r => ({
        label: String(label(r) || "—").slice(0, 80),   // the draw step fits it to the label column
        sub: [r.city, r.state].filter(Boolean).join(", ") || null,
        value: val(r), display: cardNum(val(r), money),
      }));
      if (med != null && spec.rows.length > 2) {
        const top = spec.rows[0];
        if (top && typeof top.value === "number" && med) {
          const ratio = top.value / Math.abs(med);
          const diff = Math.round((ratio - 1) * 100);
          spec.takeaway = ratio >= 3
            ? top.label + " is " + (Math.round(ratio * 10) / 10) + "× the median of this set (" + cardNum(med, money) + ")."
            : top.label + " is " + Math.abs(diff) + "% " + (diff >= 0 ? "above" : "below") + " the median of this set (" + cardNum(med, money) + ").";
        }
      }
    }
    // Context is only offered when there is a real benchmark to show.
    if (pick.type !== "stat") {
      spec.context = med != null && objs.length > 2
        ? [{ label: "Rows", value: String(rows.length) }, { label: "Median", value: cardNum(med, money) }]
        : [];
    }
    spec.hasContext = !!(spec.context && spec.context.length);
    spec.hasChart = pick.type !== "table";
    spec.hasTakeaway = !!spec.takeaway;
    return spec;
  }

  function renderCardSvg(spec, opts) {
    opts = Object.assign({}, CARD_DEFAULTS, opts || {});
    const size = CARD_SIZES[opts.aspect === "landscape" ? "landscape" : "portrait"];
    const body = spec.type === "stat" ? bodyStat(spec, opts)
      : spec.type === "trend" ? bodyTrend(spec, opts)
      : spec.type === "table" ? bodyTable(spec, opts)
      : bodyBars(spec, opts, spec.type === "ranked");
    const inner = cardFrame(size.w, size.h, spec, opts, body);
    return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ' + size.w + " " + size.h + '" width="' + size.w
      + '" height="' + size.h + '" font-family="' + CARD.sans + '">' + inner + "</svg>";
  }


  // ---- exports. The PNG is rasterised from the very SVG on screen, so it cannot drift from what
  // the user approved. The CSV is always the full result set, whatever the toggles say.
  function cardDownload(blob, filename) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = filename;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
  }
  function cardSlug(spec) {
    return (spec.title || "transit411-data")
      .toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 60) || "transit411-data";
  }
  function cardCsv(spec) {
    const q = v => {
      const s = v == null ? "" : String(v);
      return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
    };
    const lines = [
      "# " + (spec.title || "Transit411 data"),
      "# Source: " + (spec.source || ""),
      spec.methodology ? "# " + spec.methodology : "# Generated with " + (spec.tool || "Ask NTD") + " - transit411.net",
      "",
      (spec.columns || []).map(q).join(","),
    ];
    (spec.tableRows || []).forEach(r => lines.push(r.map(q).join(",")));
    return lines.join("\n");
  }
  function exportCardCsv(spec) {
    // BOM so Excel opens UTF-8 cleanly on Windows.
    cardDownload(new Blob(["﻿" + cardCsv(spec)], { type: "text/csv;charset=utf-8" }), cardSlug(spec) + ".csv");
  }
  // Fonts have to travel inside the SVG or the raster falls back to system faces. The bundle's host
  // page tells us where the woff2 files live; without them we still export, just in the fallbacks.
  let cardFontCss = null;
  async function cardFonts(base) {
    if (cardFontCss !== null) return cardFontCss;
    const want = [["Archivo", "archivo-800.woff2", 800], ["Archivo", "archivo-900.woff2", 900],
                  ["JetBrains Mono", "jetbrains-mono-700.woff2", 700]];
    try {
      const parts = [];
      for (const [family, file, weight] of want) {
        const r = await fetch(base.replace(/\/$/, "") + "/" + file);
        if (!r.ok) throw new Error("missing " + file);
        const buf = await r.arrayBuffer();
        let bin = ""; new Uint8Array(buf).forEach(b => bin += String.fromCharCode(b));
        parts.push("@font-face{font-family:'" + family + "';font-weight:" + weight
          + ";src:url(data:font/woff2;base64," + btoa(bin) + ") format('woff2');}");
      }
      cardFontCss = parts.join("");
    } catch (e) {
      cardFontCss = "";   // export still works, just with fallback faces
    }
    return cardFontCss;
  }
  async function exportCardPng(spec, opts, fontBase) {
    const scale = (opts && opts.scale) || 2;
    const size = CARD_SIZES[(opts && opts.aspect) === "landscape" ? "landscape" : "portrait"];
    let svg = renderCardSvg(spec, opts);
    const css = fontBase ? await cardFonts(fontBase) : "";
    if (css) svg = svg.replace("><", "><style>" + css + "</style><");
    const img = new Image();
    img.crossOrigin = "anonymous";
    const blobUrl = URL.createObjectURL(new Blob([svg], { type: "image/svg+xml;charset=utf-8" }));
    try {
      await new Promise((res, rej) => { img.onload = res; img.onerror = () => rej(new Error("render failed")); img.src = blobUrl; });
      const c = document.createElement("canvas");
      c.width = size.w * scale; c.height = size.h * scale;
      const ctx = c.getContext("2d");
      ctx.fillStyle = CARD.paper; ctx.fillRect(0, 0, c.width, c.height);
      ctx.drawImage(img, 0, 0, c.width, c.height);
      const blob = await new Promise(r => c.toBlob(r, "image/png"));
      cardDownload(blob, cardSlug(spec) + ".png");
    } finally {
      URL.revokeObjectURL(blobUrl);
    }
  }
  // PDF: the same artwork, page-sized, handed to the browser's own print-to-PDF. No extra library,
  // and it embeds the fonts the page already has.
  function exportCardPdf(spec, opts) {
    const size = CARD_SIZES[(opts && opts.aspect) === "landscape" ? "landscape" : "portrait"];
    const w = window.open("", "_blank");
    if (!w) return alert("Allow pop-ups to export a PDF.");
    w.document.write('<!doctype html><html><head><meta charset="utf-8"><title>' + esc(spec.title || "Transit411 data card")
      + '</title><link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@600;700;800;900&family=Spectral:wght@400;500;600&family=JetBrains+Mono:wght@600;700&display=swap">'
      + '<style>@page{size:' + (size.w > size.h ? "landscape" : "portrait") + ';margin:14mm}'
      + 'body{margin:0;display:flex;align-items:center;justify-content:center}svg{width:100%;height:auto}</style></head><body>'
      + renderCardSvg(spec, opts) + '<script>window.onload=()=>{setTimeout(()=>window.print(),350)}<\/script></body></html>');
    w.document.close();
  }

  // ---- the panel: card + toggles + exports. Smart defaults mean most answers need no fiddling:
  // source always on, one context stat on, chart on, "so what" and methodology off.
  function renderDataCard(el, answer, opts) {
    opts = opts || {};
    const spec = cardSpecFromAnswer(answer, opts);
    const state = Object.assign({}, CARD_DEFAULTS, opts.state || {}, { aspect: opts.aspect || "portrait" });
    el._card = { spec, state, fontBase: opts.fontBase || null };
    const toggles = [
      ["takeaway", "“So what” line", spec.hasTakeaway],
      ["context", "Context stats", spec.hasContext],
      ["chart", "Chart", spec.hasChart],
      ["methodology", "Methodology", !!spec.methodology],
    ].filter(t => t[2]);
    el.innerHTML = '<div class="t411-dc">'
      + '<div class="t411-dc-art" id="' + (opts.id || "t411-card") + '-art"></div>'
      + '<div class="t411-dc-ctl">'
      + '<div class="t411-dc-toggles">'
      + toggles.map(t => '<label class="t411-dc-tog"><input type="checkbox" data-tog="' + t[0] + '"'
        + (state[t[0]] ? " checked" : "") + '> ' + esc(t[1]) + "</label>").join("")
      + '<label class="t411-dc-tog"><input type="checkbox" data-tog="aspect"' + (state.aspect === "landscape" ? " checked" : "") + '> Landscape</label>'
      + '<span class="t411-dc-fixed" title="The citation is never optional - it is what makes the card trustworthy">Source: always shown</span>'
      + "</div>"
      + '<div class="t411-dc-exports">'
      + '<button type="button" class="t411-btn" data-exp="png">Download PNG</button>'
      + '<button type="button" class="t411-btn" data-exp="pdf">PDF</button>'
      + '<button type="button" class="t411-btn" data-exp="csv">Excel / CSV</button>'
      + '<span class="t411-dc-kind">' + esc(spec.type) + " card · " + esc(spec.reason) + "</span>"
      + "</div></div></div>";
    const draw = () => {
      el.querySelector(".t411-dc-art").innerHTML = renderCardSvg(el._card.spec, el._card.state);
    };
    draw();
    el.addEventListener("change", e => {
      const t = e.target.closest("[data-tog]");
      if (!t) return;
      const key = t.dataset.tog;
      el._card.state[key] = key === "aspect" ? (t.checked ? "landscape" : "portrait") : t.checked;
      draw();
    });
    el.addEventListener("click", async e => {
      const b = e.target.closest("[data-exp]");
      if (!b) return;
      const { spec, state, fontBase } = el._card;
      b.disabled = true;
      try {
        if (b.dataset.exp === "csv") exportCardCsv(spec);
        else if (b.dataset.exp === "pdf") exportCardPdf(spec, state);
        else await exportCardPng(spec, state, fontBase);
      } catch (err) {
        alert("Couldn't export that card: " + (err && err.message ? err.message : err));
      } finally {
        b.disabled = false;
      }
    });
    return spec;
  }

  window.T411 = { esc, amt, RATING, pickCardType, cardSpecFromAnswer, renderCardSvg, renderDataCard, exportCardCsv, exportCardPng, exportCardPdf, CARD_DEFAULTS, renderCigTable, renderCigMilestones, renderCigTimeline, renderCigChanges, openCigProject,
                  renderCigProfileVersions, sortCigProjects: sortProjects,
                  colLabel, colKind, fmtCell, errorText, askCardHtml };
})();
