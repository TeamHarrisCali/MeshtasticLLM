// The login page (what the server shows at / until you are signed in). It posts the account and password to /api/login as JSON; the
// browser adds the Origin header itself. The password is cleared from the form as soon as it is sent and never stored or put in a URL.
(function () {
  const $ = id => document.getElementById(id);
  fetch("/api/session").then(r => r.json()).then(s => { $("insecure").hidden = !s.insecure_transport; }).catch(() => {});
  $("loginForm").addEventListener("submit", async e => {
    e.preventDefault();
    const body = JSON.stringify({ account: $("account").value, password: $("pw").value });
    $("pw").value = ""; $("loginBtn").disabled = true; $("loginNote").textContent = "";
    let r, data = {};
    try { r = await fetch("/api/login", { method: "POST", headers: { "Content-Type": "application/json" }, body }); data = await r.json(); }
    catch { $("loginNote").textContent = "Could not reach the bridge."; $("loginBtn").disabled = false; return; }
    if (r.ok) { location.reload(); return; }
    $("loginNote").textContent = data.error || "Could not sign in.";
    $("loginBtn").disabled = false; $("pw").focus();
  });
})();
