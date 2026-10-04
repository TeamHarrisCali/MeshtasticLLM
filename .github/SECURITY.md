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
  `SameSite=Strict`, `Secure` over TLS) of which the server keeps only a hash; failed logins back off exponentially (a source is never locked out for good, and a browser the admin has signed in from before is not held up by other people's failures);
  POSTs need a matching Origin and a per-session CSRF token; every route is tagged `public`, `viewer` or `admin` in `meshllm/webroutes.py`
  and an untagged one is admin-only; a `viewer` account cannot transmit, change the radio or settings, or fetch backups, and its AI
  questions are log-only. What is **not** covered: without `--tls-cert`/`--tls-key` (or a reverse proxy doing HTTPS) the password and the
  dashboard travel in clear text, which is an owner-accepted risk on a trusted LAN and the login page warns about it; there are two shared accounts, not
  per-person logins, and no two-factor; anyone on the network can slow the sign-in by guessing (a wait of up to a minute for a new browser from another address, five if they share yours, and a determined guesser can renew that for as long as they keep guessing; a browser that has signed in as admin before is not affected by the one-minute wait);
  a viewer account sees the kind of connection (not its address) but also each node's access rule; `MESHLLM_CONTAINER=1` (set by the Docker image) is the only thing that allows a wildcard bind with no login, so do not set it elsewhere, and
  it stops allowing it when `MESHLLM_PUBLISH_LAN=1` (set by the image's entrypoint when Compose publishes the port beyond the host's loopback);
  a viewer can read message text; `--trusted-proxy` believes whatever the named proxy sends.
- Docker: by default the dashboard runs without a login, published on the host's loopback only (`127.0.0.1`). With `docker/docker-compose.login.yml` the password hash is a Compose secret file,
  never an environment variable (`docker inspect` shows those). Because Compose keeps the host file's owner and mode, the bridge container starts as root for a moment
  with `DAC_OVERRIDE`, `SETUID` and `SETGID` only, copies the hash into a 0400 file for uid 10001 on a memory-only tmpfs and drops to 10001 with no effective, permitted or inheritable capabilities (the container's bounding set keeps those three, which grants nothing under `no-new-privileges`); the bridge never runs as root.
  `docker exec` and the healthcheck still run as that configured root user. Publishing beyond loopback (`MESHLLM_WEB_BIND`) makes the container refuse to start without the login and `MESHLLM_ALLOWED_HOSTS`;
  a hand-edited `ports:` line or `docker run -p 0.0.0.0:...` is invisible to it and unprotected. The traffic is clear text (no TLS in Compose), and every connection through Docker's proxy looks like it comes from the Docker gateway.
  See [docs/setup.md](../docs/setup.md#run-with-docker).
- Updates: the dashboard can look for a newer release and, on a git checkout, update itself. What that trusts is this repository's GitHub account (the release list and the tags on `main`) and the
  admin's confirmation click; it follows only tags of the form `v1.2.3` that are on `main` of the project's own `origin`, never another address or ref. The check is **off by default** and sends one
  HTTPS GET (no identifier; GitHub sees the IP address and `meshllm/<version>`). The update runs `pip install -r requirements.txt` from the pulled release, so it runs code from that release: it needs the same trust as
  installing it by hand. The packaged program and Docker never replace themselves. With no dashboard login (loopback) any program on that computer can start an update, as it can change other settings. Details: [docs/setup.md](../docs/setup.md#updating).
- A public key proves a device, not a person. A lost or stolen radio or phone carries a valid key; unpin it in the Access page.
- This is hobby software provided as is (see `LICENSE`). There is no formal response-time commitment, but reports are welcome and
  will be looked at.
