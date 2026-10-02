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

- The dashboard has no login and binds to `127.0.0.1` by default. `--web-host 0.0.0.0` exposes it, including the send box, to
  your network; only do that on a network you trust.
- A public key proves a device, not a person. A lost or stolen radio or phone carries a valid key; unpin it in the Access page.
- This is hobby software provided as is (see `LICENSE`). There is no formal response-time commitment, but reports are welcome and
  will be looked at.
