# Privacy and data boundaries

KakaoTalk Local MCP is designed to run locally under the same Windows user who owns the KakaoTalk session. It has no hosted service and includes no telemetry.

## Data processed locally

Depending on enabled features, the bridge may temporarily process:

- exact titles for explicitly configured rooms;
- a bounded recent transcript used for readback or event detection;
- opaque room IDs and message fingerprints;
- bounded schedule-candidate context;
- encrypted KakaoTalk database pages and keys held only in process memory by the optional backend reader.

## Data persisted locally

The state database stores operational cursors, fingerprints, bounded events, candidate state, and idempotency records. Retention is configurable. Do not sync this state directory to cloud storage.

The project does not intentionally persist raw database keys, plaintext KakaoTalk databases, or a full transcript archive.

## Data exposed through MCP

- Room titles are replaced with user-chosen opaque IDs.
- Approved source aliases are opt-in and should contain only information the user intends to expose to the scheduling owner.
- Health responses are content-free.
- Message text is returned only by explicit allowlisted read/candidate operations.

## Network behavior

Network access is off by default. The optional webhook accepts only loopback destinations and requires an environment-provided signing secret. Calendar integration is a separate optional component with its own credential and authorization boundary.

## Deletion

Stop the watcher, then delete the local `state/` directory to remove bridge state. Delete `config.json` and its backups to remove room titles and local policy. Uninstalling the Python package alone does not delete those files.

## Public bug reports

Never attach production configuration, state databases, logs containing message text, screenshots of conversations, or Scheduled Task XML. Use synthetic fixtures and redact local paths.
