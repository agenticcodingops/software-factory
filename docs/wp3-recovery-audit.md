# WP3 recovery and credential audit

This audit covers `kit-control.py`, `backup-restore.py`, `sfctl`, `upgrade.sh`,
`health.sh`, and their unit and container tests. The shell entrypoints resolve
and quote their own checkout path and delegate to the locked Python commands.

## Findings fixed beyond the supplied review

1. The rollback writer barrier was treated as a completed upgrade. It now blocks
   another upgrade until recovery finishes startup and health checks.
2. Interrupted preparation needs a durable no-replay decision before restarting
   writers, including when failed cleanup leaves a manifest deletion uncertain.
   `preparation-restarting` is resumable and never reads snapshot payloads.
3. Snapshot directory creation and newly created secret/state directories need
   their parent entries synced. Restored volume files need a filesystem flush
   before the writer barrier; PostgreSQL restore needs a checked checkpoint.
4. A database started only for restore must be stopped again when it was absent
   from the original running inventory. Empty restart lists must do nothing.
5. Full-profile health checks incorrectly reject recovery of an intentionally
   stopped service. Recovery checks the recorded inventory and its routes.
6. Run IDs alone are reusable after failed preparation. Every upgrade now has a
   random operation identity, also stored in the rollback manifest. Recovery
   validates journal fields and snapshot inventories before activation and again
   in the locked child. Physical volumes include name, creation time, device and
   inode; all are resolved before fencing or wiping any data.
7. SAS IDs, signatures, versions, optional start times and timestamp syntax were
   incompletely validated. The supported signing/key version is explicitly
   `2023-11-03`; unknown fields and versions fail before credential replacement.
   GitHub expiration headers must be valid future timestamps. Public status
   exposes expiry only when its receipt fingerprint matches the installed token.
8. Recovery bundle timestamps accepted impossible calendar dates and reversed
   intervals. Backup target, VM and stop-operation IDs also needed bounded syntax
   before entering object metadata and receipts.
9. Backup/restore could die after stopping or creating services without enough
   durable state for recovery. `service-op.json` records intent before downtime.
   Backup recovery checks the original container IDs and removes only its
   generation's one-off CLI containers before restarting; persistent-container
   lookup explicitly excludes one-offs left by killed clients. Restore
   recovery removes only containers labeled with that restore's unique run ID.
   Plaintext cleanup failure still attempts restart after one-offs are removed.
   This journal is independent of a pending upgrade journal. An interrupted
   restore is fenced for deliberate inspection and a new restore project.
10. Shell fixtures also embedded an unquoted executable under `TMPDIR`. Both
    loader and executable fixtures now use quoted variables. Host state and lock
    paths in the upgrade tests are isolated, including the CLI subprocess.
    The unsafe-parent test now creates a world-writable ancestor explicitly,
    instead of assuming the caller's temporary directory lives below `/tmp`.
    Malformed health status shapes also return bounded JSON instead of crashing.

11. Other service-starting commands could bypass a pending recovery barrier.
    Hostname and messaging changes now require resolved recovery state.
    Operation-bound points also refuse the standalone restore path.
12. Checkout handoff copied secret files in place, allowing a torn credential on
    interruption. Each transferred secret is now atomically replaced and synced.

13. A modified previous checkout could claim its Git commit while snapshotting
    different configuration. Upgrade now refuses modified or unknown source
    provenance before publishing a recovery point or stopping writers.

14. A private staging child could still be renamed through an unsafe parent.
    Staging creation and stale cleanup now require root-owned ancestors protected
    against renames; standard root-owned sticky temporary directories remain valid.

15. Inherited Git context could redirect checkout or fixture operations to the
    caller's repository/index. Host Git commands now clear inherited Git context;
    fixture Git commands also isolate global configuration while retaining the
    explicitly supplied fixture commit identity. No Git identity is changed.

16. The age recipient check accepted a matching shape without checking its
    checksum or X25519 usability. Credential replacement now requires successful
    encryption of an empty payload with age before replacing the working file.

17. Snapshot-bound application secrets could be changed while recovery was
    pending, making restored database/encrypted data unusable with a new key.
    Application-secret delivery now requires resolved operation state; external
    GitHub/OpenAI provider-token renewal remains available.

Each item has regression coverage in the kit tests. The profile matrix removes
inherited `COMPOSE_PROFILES`; temporary secret paths explicitly contain a space.

## Crash boundaries

The lock serializes all mutations. Each journal publication uses file fsync,
atomic replacement and directory fsync. Failure to persist a barrier prevents
writer startup whenever an archive could still be replayed. A retried barrier is persisted again before starting services.

| Durable phase | Side effects and interruption behavior |
| --- | --- |
| `creating-rollback-point` | The previous inventory and operation identity are durable before stopping writers. Death before/after stop, dump, database stop or archive writes leaves either an incomplete point or a complete, hashed manifest. Without a manifest recovery records `preparation-restarting` before startup. With a manifest it validates the run and physical resources before restore. |
| `preparation-restarting` | Archive replay is forbidden even if an old manifest reappears. Death before/after startup or its health wait retries only the recorded inventory; successful startup records `prepare-failed`. Cleanup errors alert separately from restart errors. |
| `installing` | The previous checkout's `recover-locked` capability is verified before capture; pre-WP3 kits are refused (see the kit README's clean-install migration). The point is durable before candidate checkout, pull, startup, health and symlink activation. Any interruption can restore it after checking its operation identity, hashes, kit pins and every physical volume generation. Backups refuse this and every unresolved upgrade phase. |
| `healthy-awaiting-handoff` | Candidate writers may be active. Explicit rollback fences every project container, including candidate-only writers, before restoring the retained point. This phase continues to block another upgrade. |
| `rollback-failed` | Any partial fence, wipe, extraction or database restore is retried while application writers remain absent. A failure after the writer barrier cannot replace that barrier with this replayable phase. |
| `restored-services-started` | Volume files are flushed and database restore checkpointed before this durable no-replay barrier. Death before/after service startup, health checks or the parent's completion write retries startup and health only. No archive is opened and acknowledged writes survive. |
| `rolled-back`, `prepare-failed`, `complete` | Upgrade recovery refuses replay. A new upgrade must use a new retained-point path and operation identity. |
| Backup `service-op: running` | Intent includes exact container IDs before stopping writers. Death before/after stop, dump, plaintext cleanup or restart leaves a safe restart path. After a successful restart this journal is complete before encryption/upload. Pre-stop/final backups keep it pending until successful publication and final stop, or successful failure-path restart. |
| Restore `service-op: running` | Intent and the unique generation label are installed before Compose creates containers. Death before/after create, extraction, import or startup is recovered by fencing only this generation. A completion-write failure also fences the project; the retained running record follows the same abandon-only recovery, including when no containers remain. Archives are never replayed by `sfctl recover`. |
| `service-op: complete` | No pending service action remains. Death during final bookkeeping cannot cause snapshot replay. The separate `last-op` backup/restore running/failed/complete record is informational. |

A failed snapshot publication must be durably invalidated before that invocation
restarts writers. If invalidation fails it alerts and leaves writers stopped.
A later recovery can durably choose `preparation-restarting` before restarting.
Failure before a snapshot manifest could exist cannot let cleanup skip restart.

## External input contract

The SAS validator accepts one nonempty value per supported query field, a
container HTTPS URL, canonical UUID-shaped delegation IDs, a 32-byte Base64
signature, `sr=c`, `sp=cw`, `spr=https`, `sks=b`, and the qualified API versions.
`st` remains optional; supplied start times must already be active. Expiry and
key lifetime are checked together using timezone-aware UTC calendar values.
The SAS signed interval `se - st` is at most seven days, or `se - now` when
`st` is absent.
The validator rejects unsupported query constraints instead of silently ignoring
fields that could make a replacement unusable. This is local validation, not a
proof of provider-side authorization; the existing canary remains the live test.
See [Azure's user delegation SAS specification](https://learn.microsoft.com/en-us/rest/api/storageservices/create-user-delegation-sas).

The rollback manifest schema now requires physical-volume identities and a bound
operation nonce. Legacy unbound upgrade points fail closed; they cannot be
substituted for a current operation's point. Archive relocation is supported
before the no-replay decision when the operation identity matches. Once a
no-replay phase is durable, recovery uses only its recorded path and inventory.
