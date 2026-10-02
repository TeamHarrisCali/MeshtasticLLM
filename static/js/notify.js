// notify: part of the dashboard script (loaded in order by index.html; all files share one global scope)
// ---- what's new: counts beside the sidebar pages, in the tab title, and (if you allow it) a sound or desktop notification ------------------
// Everything here is kept in this browser (localStorage). The server only counts what is newer than the ids this page last showed.
const NT_DEFAULT = { channel: true, dm: true, alerts: true, sound: false, desktop: false, text: false };
const ntGet = k => { const v = lsGet("nt_" + k); return v == null || v === "" ? NT_DEFAULT[k] : v === "1"; };
const ntSet = (k, on) => lsSet("nt_" + k, on ? "1" : "0");
const ALERT_KINDS_SHOWN = ["battery", "quiet", "radio", "ai"];       // a clock hint isn't worth interrupting anyone for
let unreadData = null, lastNotified = { channel: 0, dm: 0 }, seenAlerts = null, unreadBusy = false;

function initNotify() { lastNotified = { channel: 0, dm: 0 }; }

function beep() {
  try {
    const C = window.AudioContext || window.webkitAudioContext; if (!C) return;
    const ctx = beep.ctx || (beep.ctx = new C()), o = ctx.createOscillator(), g = ctx.createGain();
    o.type = "sine"; o.frequency.value = 880; g.gain.setValueAtTime(0.0001, ctx.currentTime); g.gain.exponentialRampToValueAtTime(0.15, ctx.currentTime + 0.02); g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.22);
    o.connect(g); g.connect(ctx.destination); o.start(); o.stop(ctx.currentTime + 0.25);
  } catch {}
}

function desktopNotify(title, body, tag) {
  if (!ntGet("desktop") || !document.hidden || !("Notification" in window) || Notification.permission !== "granted") return;
  try { const n = new Notification(title, { body, tag, silent: true }); n.onclick = () => { window.focus(); n.close(); }; } catch {}
}

function markRead(page) {                      // the page being looked at has nothing unread any more
  const key = page === "dm" ? "dm" : page === "channel" ? "channel" : null;
  if (!key || !unreadData) return;
  lsSet("read_" + key, String(unreadData[key].latest)); lastNotified[key] = 0; unreadData[key].count = 0; unreadData[key].mentions = 0; showBadges();
}

function unreadTotal() {
  const d = unreadData; if (!d) return 0;
  return (ntGet("channel") ? d.channel.count : 0) + (ntGet("dm") ? d.dm.count : 0) + (ntGet("alerts") ? shownAlerts().length : 0);
}
const shownAlerts = () => (unreadData ? unreadData.alerts : []).filter(a => ALERT_KINDS_SHOWN.includes(a.kind));

function showBadges() {
  const d = unreadData; if (!d) return;
  const set = (name, n, text, cls) => { for (const b of document.querySelectorAll(`[data-badge="${name}"]`)) { b.hidden = !n; b.textContent = text != null ? text : n > 99 ? "99+" : String(n); if (cls != null) b.classList.toggle("mention", cls); } };
  const ch = ntGet("channel") ? d.channel.count : 0;
  set("channel", ch, d.channel.mentions && ntGet("channel") ? "@" + (ch > 99 ? "99+" : ch) : null, !!d.channel.mentions);
  set("dm", ntGet("dm") ? d.dm.count : 0);
  set("alerts", ntGet("alerts") ? shownAlerts().length : 0);
  refreshTitle();
}

function refreshTitle() {
  const n = unreadTotal();
  document.title = (n ? `(${n > 99 ? "99+" : n}) ` : "") + (PAGES[view] || "Mesh LLM") + " · Mesh LLM";
}

async function pollUnread() {
  if (unreadBusy) return; unreadBusy = true;
  try {
    const ch = lsGet("read_channel"), dm = lsGet("read_dm"), q = new URLSearchParams();
    if (ch != null) q.set("channel", ch); if (dm != null) q.set("dm", dm);
    const d = await api("/api/unread?" + q);
    if (ch == null) lsSet("read_channel", String(d.channel.latest));       // first visit in this browser: start from now, don't flood
    if (dm == null) lsSet("read_dm", String(d.dm.latest));
    const old = unreadData; unreadData = d;
    if (document.visibilityState === "visible" && (view === "channel" || view === "dm")) markRead(view);
    // announce what arrived since the last poll
    const text = ntGet("text");
    if (ntGet("channel") && d.channel.count > lastNotified.channel && d.channel.newest) {
      const n = d.channel.newest, mention = d.channel.mentions > 0;
      desktopNotify(mention ? `${n.name} mentioned you on the public channel` : "New post on the public channel", text ? `${n.name}: ${n.text}` : `From ${n.name}`, "channel");
      if (ntGet("sound")) beep();
    }
    lastNotified.channel = d.channel.count;
    if (ntGet("dm") && d.dm.count > lastNotified.dm && d.dm.newest) {
      const n = d.dm.newest;
      desktopNotify("New direct message", text ? `${n.name}: ${n.text}` : `From ${n.name}`, "dm");
      if (ntGet("sound")) beep();
    }
    lastNotified.dm = d.dm.count;
    const now = new Set(shownAlerts().map(a => a.text));
    if (seenAlerts && ntGet("alerts")) {
      const fresh = [...now].filter(t => !seenAlerts.has(t));
      if (fresh.length) { desktopNotify("Mesh alert", fresh[0], "alert"); if (ntGet("sound")) beep(); }
    }
    seenAlerts = now;
    showBadges();
  } catch {} finally { unreadBusy = false; }
}
