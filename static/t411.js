/* Transit411 shared component bundle. Loaded by BOTH the Command Center and the
   public site. Components take an element + API data and render into it, using the
   host page's CSS variables (--ink, --muted, --line, --accent, ...). Build once, use everywhere. */
(function () {
  const RATING = { H: "High", MH: "Medium-High", M: "Medium", ML: "Medium-Low", L: "Low" };
  // Escapes quotes too: values also land inside HTML attributes (titles, data-*).
  function esc(s) { return (s == null ? "" : String(s)).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
  function amt(num, raw) { return num != null ? ("$" + Number(num).toLocaleString(undefined, { maximumFractionDigits: 0 }) + "M") : (raw ? esc(raw) : "-"); }

  function cigRow(p, i) {
    const rt = p.rating ? ('<span title="' + esc(RATING[p.rating] || "") + '">' + esc(p.rating) + '</span>') : '-';
    return '<tr class="t411-row" data-i="' + i + '" title="Show milestone dates"><td style="font-weight:600">' + esc(p.project_name) + '</td><td>' + esc(p.sponsor) + '</td>'
      + '<td>' + esc(p.city || "") + ', ' + esc(p.state || "") + '</td><td>' + esc(p.mode || "-") + '</td><td>' + esc(p.phase) + '</td>'
      + '<td class="num">' + amt(p.cost_musd, p.cost_raw) + '</td><td class="num">' + amt(p.cig_request_musd, p.cig_request_raw) + '</td>'
      + '<td class="num">' + esc(p.cig_share || "-") + '</td><td>' + rt + '</td><td>' + esc(p.est_grant || "-") + '</td></tr>'
      + '<tr class="t411-detail" data-i="' + i + '" hidden><td colspan="10"><div class="t411-ms-title">Milestones</div>'
      + '<div class="t411-ms-box"></div><button type="button" class="t411-btn" data-hist="' + i + '">Show snapshot history</button>'
      + '<div class="t411-tl-box"></div></td></tr>';
  }

  function defaultHistory(p) {
    const q = "?name=" + encodeURIComponent(p.project_name) + "&sponsor=" + encodeURIComponent(p.sponsor || "");
    return fetch("/api/cig/history" + q).then(r => r.ok ? r.json() : Promise.reject(r.status)).then(d => d.history || []);
  }

  // CIG pipeline table. `projects` is the array from /api/cig. Clicking a row shows its milestone
  // dates; "Show snapshot history" loads its month-by-month changes. opts.fetchHistory(project) can
  // supply history from another endpoint (default: /api/cig/history on the same host).
  function renderCigTable(el, projects, opts) {
    opts = opts || {};
    if (!projects || !projects.length) { el.innerHTML = '<div class="t411-empty">' + esc(opts.emptyText || "No projects.") + '</div>'; return; }
    el._t411 = { projects: projects, fetchHistory: opts.fetchHistory || defaultHistory };
    el.innerHTML = '<div class="t411-card"><div class="t411-scroll"><table class="t411-table"><thead><tr>'
      + '<th>Project</th><th>Sponsor</th><th>Location</th><th>Mode</th><th>Phase</th><th>Cost</th><th>CIG</th><th>Share</th><th>Rating</th><th>Est. grant</th>'
      + '</tr></thead><tbody>' + projects.map(cigRow).join("") + '</tbody></table></div></div>';
    if (el._t411Bound) return;
    el._t411Bound = true;
    el.addEventListener("click", function (e) {
      const st = el._t411;
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
      if (det.hidden) renderCigMilestones(det.querySelector(".t411-ms-box"), st.projects[+row.dataset.i]);
      det.hidden = !det.hidden;
    });
  }

  // Per-project milestone dates (from a /api/cig row).
  function renderCigMilestones(el, p) {
    const items = [["PD entry", p.pd_entry], ["NEPA complete", p.nepa], ["Engineering entry", p.eng_entry],
      ["LONP request", p.lonp_req], ["LONP decision", p.lonp_dec], ["LONP action", p.lonp_action],
      ["Rating requested", p.req_rating_date], ["Project rated", p.proj_rating_date],
      ["Overall rating", p.rating ? p.rating + (RATING[p.rating] ? " (" + RATING[p.rating] + ")" : "") : null],
      ["Local match", p.noncig_status], ["Est. grant", p.est_grant]];
    const chips = items.filter(x => x[1]).map(x =>
      '<span class="t411-ms"><span class="t411-ms-k">' + x[0] + '</span><span class="t411-ms-v">' + esc(x[1]) + '</span></span>').join("");
    el.innerHTML = chips || '<div class="t411-empty">No milestone dates recorded.</div>';
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

  window.T411 = { esc, amt, RATING, renderCigTable, renderCigMilestones, renderCigTimeline };
})();
