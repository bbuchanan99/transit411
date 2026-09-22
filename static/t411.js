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

  window.T411 = { esc, amt, RATING, renderCigTable, renderCigMilestones, renderCigTimeline, renderCigChanges, openCigProject,
                  renderCigProfileVersions, sortCigProjects: sortProjects,
                  colLabel, colKind, fmtCell, errorText, askCardHtml };
})();
