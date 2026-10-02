// data: the Data page. /api/data/overview lists each stored dataset with row count, date range and retention, plus database size,
// saved map tiles and the hop breakdown. CSV buttons link to /api/data/export/<name>.csv, /api/telemetry/export.csv and /api/export.csv.
// Uses barsInto from mesh-common.js and fmtTime from core.js.
// ---- the Data page: everything this bridge collects, how much, how long it is kept ----------------------------------------------------
// Time of the last fetch; refreshes are throttled to once per 10 s.
let dataAt = 0;
// Bytes as GB, MB or KB.
const fmtBytes = n => n >= 1073741824 ? (n / 1073741824).toFixed(2) + " GB" : n >= 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB";

// Fetches the overview and redraws the dataset table, totals, hop breakdown and storage sizes. Throttled to once per 10 s unless forced.
async function refreshData(force) {
  if (!force && Date.now() - dataAt < 10000) return; dataAt = Date.now();
  let d; try { d = await api("/api/data/overview"); } catch { return; }
  const rows = $("dataRows"); rows.replaceChildren();
  let total = 0;
  for (const s of d.datasets) {
    total += s.rows;
    const r = el("div", "drow"), name = el("div"), range = s.oldest == null ? "nothing yet" : `${fmtTime(s.oldest)} to ${fmtTime(s.newest)}`;
    name.append(el("b", null, s.label), el("small", null, s.what));
    const act = el("div");
    // Most datasets export through /api/data/export; telemetry and AI requests have their own export endpoints.
    if (s.csv) { const a = el("a", "btn", "CSV"); a.href = `/api/data/export/${s.name}.csv`; a.download = ""; a.style.padding = "2px 10px"; act.append(a); }
    else if (s.name === "telemetry") { const a = el("a", "btn", "CSV"); a.href = "/api/telemetry/export.csv"; a.download = ""; a.style.padding = "2px 10px"; act.append(a); }
    else if (s.name === "requests") { const a = el("a", "btn", "CSV"); a.href = "/api/export.csv"; a.download = ""; a.style.padding = "2px 10px"; act.append(a); }
    r.append(name, el("div", "num", s.rows.toLocaleString()), el("div", "time", range), el("div", null, s.kept), act); rows.append(r);
  }
  $("dataTotals").textContent = `${total.toLocaleString()} rows in ${d.datasets.length} datasets`;
  // The server keys hop counts by relay number as a string; relabel them for the bars.
  const hops = {}; for (const [k, v] of Object.entries(d.hops)) hops[k === "0" ? "direct" : `${k} relay${k === "1" ? "" : "s"}`] = v;
  barsInto($("dataHops"), hops);
  const st = $("dataStore"); st.replaceChildren();
  for (const [k, v] of [["Database file", d.db_bytes != null ? fmtBytes(d.db_bytes) : "unknown"], ["Saved map tiles", `${d.tiles.tiles} (${fmtBytes(d.tiles.bytes)} of ${fmtBytes(d.tiles.max_bytes)} allowed)`]]) {
    const row = el("div", "irow static"); row.append(el("span", null, k), el("small", null, v)); st.append(row);
  }
}
