# Notes

- **Delivery.** Replies are split into balanced parts of at most `--chunk-bytes` (160 by default; PKI DMs
  have less room than plain ones and bigger packets are lost more often). Every part is sent with an ack
  request. If the radio gives up on a part (a NAK such as MAX_RETRANSMIT), the bridge resends it up to
  `--send-retries` times and records the reason in the audit ("Delivery note"). A part that never gets
  through shows as a red "⚠ part NOT delivered" message in the Direct messages page. The numbering
  "(1/2)" means a reader can tell when a piece is missing.
- Opening the serial port reboots most ESP32 nodes; "node rebooted, reconnected" at startup is normal.
- Memory is keyed by the sender's node ID as reported by the radio. Treat it as convenience, not
  authentication.
- `audit.db` holds every prompt and reply. Delete it to clear the history.
