// notify: unread counts and notifications. pollUnread asks /api/unread how many public-channel posts, direct messages and alerts are
// newer than what this browser last saw, shows them as sidebar badges and in the tab title, and can play a sound or show a desktop
// notification. Read positions and options are kept in localStorage, so they are per browser.
// ---- what's new: counts beside the sidebar pages, in the tab title, and (if you allow it) a sound or desktop notification ------------------
// Everything here is kept in this browser (localStorage). The server only counts what is newer than the ids this page last showed.
// Notification options and their defaults. Sound, desktop notifications and including the message text are off until switched on in Settings.
const NT_DEFAULT = { channel: true, dm: true, alerts: true, sound: false, desktop: false, text: false };
// Reads a notification option from localStorage (stored as '1' or '0'), falling back to its default.
const ntGet = k => { const v = lsGet("nt_" + k); return v == null || v === "" ? NT_DEFAULT[k] : v === "1"; };
// Stores a notification option.
const ntSet = (k, on) => lsSet("nt_" + k, on ? "1" : "0");
const ALERT_KINDS_SHOWN = ["battery", "quiet", "radio", "ai"];       // a clock hint isn't worth interrupting anyone for
// unreadData: last /api/unread reply. lastNotified: counts already announced, so each arrival is announced once. seenAlerts: alert texts already seen (null until the first poll). unreadBusy stops polls overlapping.
let unreadData = null, lastNotified = { channel: 0, dm: 0 }, seenAlerts = null, unreadBusy = false;

// Resets the announced counts at startup.
function initNotify() { lastNotified = { channel: 0, dm: 0 }; }

// Plays a short 880 Hz tone with Web Audio. The AudioContext is created once and reused; failures such as blocked autoplay are ignored.
function beep() {
  try {
    const C = window.AudioContext || window.webkitAudioContext; if (!C) return;
    const ctx = beep.ctx || (beep.ctx = new C()), o = ctx.createOscillator(), g = ctx.createGain();
    o.type = "sine"; o.frequency.value = 880; g.gain.setValueAtTime(0.0001, ctx.currentTime); g.gain.exponentialRampToValueAtTime(0.15, ctx.currentTime + 0.02); g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.22);
    o.connect(g); g.connect(ctx.destination); o.start(); o.stop(ctx.currentTime + 0.25);
  } catch {}
}

// Shows a silent desktop notification, only when enabled, permission is granted and the tab is in the background. Clicking it focuses the tab.
function desktopNotify(title, body, tag) {
  if (!ntGet("desktop") || !document.hidden || !("Notification" in window) || Notification.permission !== "granted") return;
  try { const n = new Notification(title, { body, tag, silent: true }); n.onclick = () => { window.focus(); n.close(); }; } catch {}
}

function markRead(page) {                      // the page being looked at has nothing unread any more
  const key = page === "dm" ? "dm" : page === "channel" ? "channel" : null;
  if (!key || !unreadData) return;
  lsSet("read_" + key, String(unreadData[key].latest)); lastNotified[key] = 0; unreadData[key].count = 0; unreadData[key].mentions = 0; showBadges();
}

// Total unread items for the categories the user has switched on.
function unreadTotal() {
  const d = unreadData; if (!d) return 0;
  return (ntGet("channel") ? d.channel.count : 0) + (ntGet("dm") ? d.dm.count : 0) + (ntGet("alerts") ? shownAlerts().length : 0);
}
// The alerts of the kinds worth showing.
const shownAlerts = () => (unreadData ? unreadData.alerts : []).filter(a => ALERT_KINDS_SHOWN.includes(a.kind));

// Updates the sidebar badges (capped at 99+, with an @ for channel mentions) and the tab title.
function showBadges() {
  const d = unreadData; if (!d) return;
  const set = (name, n, text, cls) => { for (const b of document.querySelectorAll(`[data-badge="${name}"]`)) { b.hidden = !n; b.textContent = text != null ? text : n > 99 ? "99+" : String(n); if (cls != null) b.classList.toggle("mention", cls); } };
  const ch = ntGet("channel") ? d.channel.count : 0;
  set("channel", ch, d.channel.mentions && ntGet("channel") ? "@" + (ch > 99 ? "99+" : ch) : null, !!d.channel.mentions);
  set("dm", ntGet("dm") ? d.dm.count : 0);
  set("alerts", ntGet("alerts") ? shownAlerts().length : 0);
  refreshTitle();
}

// Sets the tab title from the unread count, the current page name and the app name.
function refreshTitle() {
  const n = unreadTotal();
  document.title = (n ? `(${n > 99 ? "99+" : n}) ` : "") + (PAGES[view] || "Mesh LLM") + " · Mesh LLM";
}

// Called on every page tick from main.js. Sends the last-read ids kept in localStorage, gets back counts of newer items, announces new arrivals once and updates the badges.
async function pollUnread() {
  if (unreadBusy) return; unreadBusy = true;
  try {
    const ch = lsGet("read_channel"), dm = lsGet("read_dm"), q = new URLSearchParams();
    if (ch != null) q.set("channel", ch); if (dm != null) q.set("dm", dm);
    const d = await api("/api/unread?" + q);
    if (ch == null) lsSet("read_channel", String(d.channel.latest));       // first visit in this browser: start from now, don't flood
    if (dm == null) lsSet("read_dm", String(d.dm.latest));
    const old = unreadData; unreadData = d;
    // Anything that arrives while the user is looking at the channel or direct message page counts as read at once.
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
    // Alerts are compared by text so each is announced once; the first poll only records them.
    const now = new Set(shownAlerts().map(a => a.text));
    if (seenAlerts && ntGet("alerts")) {
      const fresh = [...now].filter(t => !seenAlerts.has(t));
      if (fresh.length) { desktopNotify("Mesh alert", fresh[0], "alert"); if (ntGet("sound")) beep(); }
    }
    seenAlerts = now;
    showBadges();
  } catch {} finally { unreadBusy = false; }
}
