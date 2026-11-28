# Public-tree deny-list

Run `python3 hosting/paperclip-hermes/tools/deny-list.py` at the repository root.
It scans all tracked and non-ignored untracked files, including dotfiles, without reading Git history. Findings print the file, line and category, never the matched value.

Files must decode as UTF-8 (with or without a BOM), BOM-marked UTF-16/UTF-32, or NUL-interleaved UTF-16/UTF-32 detected from byte patterns. Decoding is strict: unsupported, malformed or remaining NUL-containing content fails closed with a file-level finding. Convert such files to UTF-8 before committing them.

The patterns reject GUIDs, e-mail addresses and hostnames ending in `.ts.net`, including wildcard-prefixed names.
These public forms pass:

- The organization name `agenticcodingops` and GitHub handles in CODEOWNERS.
- Addresses whose domain is exactly `users.noreply.github.com` (case insensitive).
- Hostnames beneath `example.ts.net` and `test.ts.net`, including nested names. A suffix such as `example.ts.net.evil` is not an allowed tailnet hostname.
- The single GUID `00000000-0000-0000-0000-000000000000`. Use it for every subscription, tenant and client ID in public examples. OIDC subjects use `<owner>`, `<id>`, `<repo>` and `<env>` as in ADR 0001; never copy an actual instance subject.
- Exact, reviewed fixture strings and public upstream dependency metadata in [allow.txt](allow.txt). This is not a file exclusion list; another disallowed value on the same line still fails.

Instance operators may supply `--private-hashes /path/outside/the/checkout` (or `SF_PRIVATE_NAMES_HASH_FILE`). The file contains one SHA-256 per line, with blank lines and `#` comments permitted. Each digest is the hash of one UTF-8, case-folded token matching `[A-Za-z0-9_@.+-]+` (for example a private organization, login, full e-mail address or hostname). These hashes reject exact tokens in addition to the public patterns, including otherwise allowed placeholders. Split multi-word names into tokens before hashing. Missing or malformed requested files fail closed. The list is optional for this public scaffold and is never fetched from a fork or committed by CI. Hashes are not encryption: keep even the hash list private when names can be guessed.

To test an instance list locally, create `.private-deny-hashes` (ignored by Git) and pass its path explicitly. Runtime tests construct disallowed strings in temporary repositories; no personal identifiers are stored in test fixtures.
