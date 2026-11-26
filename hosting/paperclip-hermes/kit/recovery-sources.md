# Recovery implementation evidence

Checked against the kit's pinned releases on 25 November 2026. These are the
contracts the scripts rely on; recheck them before changing image pins.

## Hermes v0.23.4

- [Full backup implementation](https://github.com/NousResearch/hermes-agent/blob/v0.23.4/hermes_cli/backup.py)
  and [backup parser](https://github.com/NousResearch/hermes-agent/blob/v0.23.4/hermes_cli/subcommands/backup.py):
  full backup writes a ZIP and snapshots SQLite safely. A partial backup fails;
  the kit also requires the requested ZIP to exist. It uses an explicit output
  outside the home and disables archive pruning for the one-shot invocation.
- [Import parser](https://github.com/NousResearch/hermes-agent/blob/v0.23.4/hermes_cli/subcommands/import_cmd.py)
  and [keep-host implementation](https://github.com/NousResearch/hermes-agent/blob/v0.23.4/hermes_cli/backup_keep_host.py):
  --messaging-off takes effect with --keep-host. It writes
  platforms.<id>.enabled: false only for bots it sees as on: a block with
  enabled true, or none with a token in that home's own .env (messaging_off,
  _publish_profile_config's _archive_env). The kit's regular startup rewrites
  the root config.yaml from the kit's own hermes-config.yaml (never the
  archive's), which drops that switch; the gateway then enables any bot whose
  credential is in the environment, such as a token in the restored root
  .op.env, which load_hermes_dotenv in hermes_cli/env_loader.py loads to fill
  gaps (gateway/config_env.py _enable_from_env: env credentials enable a
  platform unless config.yaml explicitly disables it). Checked in the pinned
  image: a root .op.env Telegram token started the adapter under the kit
  policy. Restore therefore sets KIT_MESSAGING_OFF=1 in .secrets/compose.env,
  and container/configure-hermes.py then writes enabled: false for every
  platform _bot_platforms lists (30 in v0.23.4), clearing enabled in every
  block _platform_blocks names, in the root and in each named profile, at every
  start; it refuses to start if either helper is missing. A profile's own
  plugins (profiles/<name>/plugins, enabled in its config) register platforms
  only in that profile's plugin scope (hermes_cli/profile_channels.py
  _plugin_scope, platform_ids), so each profile is listed again in a child with
  HERMES_HOME set to it, and any listing failure stops the start; every id
  with a block under platforms or gateway.platforms there is switched off too.
  Checked in the pinned image: a profile-only platform plugin enabled in a
  profile is off after configure, and load_gateway_config under that profile
  enables neither it nor a Telegram token in the profile's .env. Checked in the pinned
  image: no bot adapter is enabled for the root or a profile with root .op.env
  and profile .env tokens.
- The full backup implementation excludes browser profiles, symlinks, runtime
  downloads, source checkout, caches and checkpoints; quick snapshots are
  narrower. Neither mode captures the separate SSH sandbox workspace.
- Import calls _revive_gateway_after_import after writing state. The kit's
  container/recovery-hermes.py suppresses that one best-effort hook, then invokes the
  upstream CLI normally. It refuses an implementation missing that callable.
  The one-shot container overrides s6/the application entrypoint, against a
  stopped home; Compose starts applications after database validation.
- [Home resolution](https://github.com/NousResearch/hermes-agent/blob/v0.23.4/hermes_constants.py)
  honors explicit HERMES_HOME. The one-shot CLI uses /opt/data, matching the kit
  image's mounted home, and uid/gid 10000.

## Paperclip v2026.1117.0

- [Storage guide](https://github.com/paperclipai/paperclip/blob/v2026.1117.0/docs/deploy/storage.md)
  locates the default instance config below the instance directory and explains
  external storage. The kit sets PAPERCLIP_HOME=/paperclip and instance default.
  Backing up the entire /paperclip volume captures instructions and agent homes
  as well as local storage; a database dump alone cannot do that.
- [Built-in db:backup command](https://github.com/paperclipai/paperclip/blob/v2026.1117.0/cli/src/commands/db-backup.ts)
  accepts DATABASE_URL, configures a database backup directory/retention and
  invokes the database package. It does not bundle the kit secret settings,
  instance files or Caddy CA. This kit uses PostgreSQL 17 pg_dump/pg_restore
  explicitly to pair that logical dump with all of those files.
- [Migration discovery](https://github.com/paperclipai/paperclip/blob/v2026.1117.0/packages/db/src/client.ts)
  uses __drizzle_migrations in drizzle or public. The manifest records the actual
  full journal rows (IDs, hashes, timestamps and any additional columns), not
  an invented application schema-version setting. The pinned
  [journal](https://github.com/paperclipai/paperclip/blob/v2026.1117.0/packages/db/src/migrations/meta/_journal.json)
  ends at 0451_budget_resume_claims. Its journal format version is distinct from
  the applied database schema. Restore compares applied rows before starting
  Paperclip, so it cannot silently claim an unverified migration state.

## Upload and host scheduling

- Azure [service SAS fields](https://learn.microsoft.com/en-us/rest/api/storageservices/create-service-sas)
  and [stored access policies](https://learn.microsoft.com/en-us/rest/api/storageservices/define-stored-access-policy):
  policy-bound SAS permissions may be supplied by the policy instead of the
  query. Local validation cannot prove a remote policy's effective permissions.
  The operator must verify Create/Write only. The host file is never exported,
  archived or mounted, including into a one-shot container.
- Azure [Put Blob](https://learn.microsoft.com/en-us/rest/api/storageservices/put-blob),
  [Put Block](https://learn.microsoft.com/en-us/rest/api/storageservices/put-block)
  and [Put Block List](https://learn.microsoft.com/en-us/rest/api/storageservices/put-block-list):
  small ciphertext uses one conditional PUT; larger ciphertext uses 16 MiB blocks
  and a conditional final commit. The implementation caps the block count at
  50,000, rejects redirects and never reads, lists or deletes destination data.
  Failed large uploads can leave uncommitted ciphertext blocks for the storage
  service to expire. They never leave a local plaintext payload after cleanup.
- Docker [start](https://docs.docker.com/reference/cli/docker/compose/start/)
  supports waiting for service health. The job waits up to five minutes for an
  existing stack to become healthy on boot, captures stopped application state,
  and restarts the original services with a health wait even on capture failure.
- The systemd timer uses 00:15 host time and Persistent=true. Its service invokes
  the host script as root; the settings are read by Python rather than systemd
  EnvironmentFile, so the SAS is absent from child-process environments.

## Verification boundaries

Unit tests cover arguments, fencing before side effects, manifest/checksums,
archive path validation, SAS redaction and permissions, conditional publication,
and plaintext cleanup on failure. They do not prove a live remote policy or a
Docker restore. Run tests/e2e-backup-restore.sh on disposable Linux Docker, then
complete the application-level pending approval, attachment and exactly-one-PR
checks with the operator. Neither a parsed script nor a healthy container is
reported as the full restore drill passing.
