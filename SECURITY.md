# Security policy

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting: open the repository's **Security** tab and choose **Report a vulnerability**.
Do not open a public issue for something that could be exploited.

Helpful details: what you found, how to reproduce it, and which version or commit you tested.

## What is in scope

This bridge connects an untrusted radio mesh to a local language model, so the interesting problems are:

- a message from the mesh making the AI do something outside its fixed, read-only menu (prompt injection that reaches a tool,
  a parameter that escapes validation, a way to make the bridge transmit that the operator did not ask for);
- getting past the checks that gate tools (access mode, pinned public key, PKI-encrypted DM, key-changed detection,
  confirmation codes);
- reaching the dashboard from outside the machine, or making it show or change something it should not;
- anything that leaks the radio's keys, Wi-Fi or MQTT credentials, or Bluetooth PIN (these are never shown, saved or written
  by the radio settings page).

## Things to know

- The dashboard binds to `127.0.0.1` by default and then has no login: anyone who can use that computer's loopback is the owner.
  On any other `--web-host` it will not start without a login (`--password-hash-file`, made with `--set-password`; scrypt, never a
  password on the command line or in the environment) and an `--allowed-host` list. Sessions are random 256-bit cookies (`HttpOnly`,
  `SameSite=Strict`, `Secure` over TLS) of which the server keeps only a hash; failed logins back off exponentially (never a lockout);
  POSTs need a matching Origin and a per-session CSRF token; every route is tagged `public`, `viewer` or `admin` in `meshllm/webroutes.py`
  and an untagged one is admin-only; a `viewer` account cannot transmit, change the radio or settings, or fetch backups, and its AI
  questions are log-only. What is **not** covered: without `--tls-cert`/`--tls-key` (or a reverse proxy doing HTTPS) the password and the
  dashboard travel in clear text, which is an owner-accepted risk on a trusted LAN and the login page warns about it; there are two shared accounts, not
  per-person logins, and no two-factor; anyone on the network can slow the sign-in by guessing (a wait of up to a minute from another address, five if they share yours, never a lockout);
  a viewer can read message text; `--trusted-proxy` believes whatever the named proxy sends. The Docker image still runs without a login,
  published on the host's loopback only. See [docs/setup.md](docs/setup.md#use-the-dashboard-from-a-phone-or-another-computer-lan-login).
- A public key proves a device, not a person. A lost or stolen radio or phone carries a valid key; unpin it in the Access page.
- This is hobby software provided as is (see `LICENSE`). There is no formal response-time commitment, but reports are welcome and
  will be looked at.
