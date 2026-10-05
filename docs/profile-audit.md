# Optional profile audit and site environments

The normal one-host setup is unchanged. No profile name, process metadata, or
additional file is required. This page is for users who explicitly need an
environment selector or an inventory of several existing profiles.

## Site environment selector

Keep `VB_CADENCE_CSHRC` for the script path. Its default shell is `csh`; existing
configurations need no changes. A site with a POSIX sh selector can opt in:

```dotenv
VB_CADENCE_CSHRC=/shared/project/tools.sh
VB_CADENCE_ENV_SHELL=sh
```

Both settings accept case-sensitive profile suffixes such as
`VB_CADENCE_ENV_SHELL_worker=sh`. A nonempty profile value overrides the global
value; otherwise the global value is inherited. Supported shells are exactly
`csh` and `sh`, selected explicitly, not guessed from a filename or shebang.

Spectre execution/probing, remote SKILL Finder discovery, and remote document
discovery share one loader. Exported variables, including licensing and library
paths, pass directly to the tool process; environment output is never evaluated
as shell source. Source banners go to stderr so they cannot masquerade as tool
paths or document records. A configured selector is used even if another tool
installation is already on PATH. Without a selector, existing PATH-based use
remains available.

The selector is trusted executable site configuration. It must report failure
with a nonzero return/exit status. Normal csh source semantics are preserved,
including unsuccessful optional probes followed by successful initialization;
the loader does not impose `csh -e`. It supplies HOSTNAME and an initially empty
LD_LIBRARY_PATH when necessary. Scripts should export environment variables;
shell-local aliases/functions are not exported to the POSIX command process.

## Read-only inventory

```bash
virtuoso-bridge profile list --json --env /path/to/bridge.env
```

This explicit command reads a configuration snapshot, without modifying the
process environment, connecting to SSH/TCP, starting a daemon, or running the
selector. Selected `.env` values override process environment values, matching
the existing loading policy. Names are case-sensitive. Default and named
profiles are enumerated from recognized connection/metadata keys, including
incomplete named profiles, so invalid entries are visible rather than dropped.

Each row includes resolved host roles, daemon `host` and `port`, `local_port`,
user/jump settings, selector path/shell, and optional opaque metadata:

```dotenv
# Optional; these fields are never required for ordinary bridge use.
VB_PROCESS_worker=process-family
VB_PROCESS_VERSION_worker=release-id
VB_PROCESS_VARIANT_worker=validated
```

`sources` records each output field's defining variable, environment or `.env`
origin, path, and global inheritance. It reports the direct definition, not a
full chain of dotenv interpolation references. Role fallbacks use the same
resolver as connection setup. Named hosts/users/ports do not inherit global
connection settings. Missing ports use the existing per-user default, with the
local port defaulting to the remote port. Legacy port derivation also consults
the global remote username when a named username is absent; the inventory uses
the selected snapshot for that computation. Live port deconfliction may later
choose a different value; the inventory is not a live tunnel-state report.

`resolved` describes configuration validation only. Missing hosts, invalid
ports, and unsupported selector shells produce per-row `errors` and exit 1;
successful inventories exit 0. Unknown variables and credentials are not
included in the output. Hostnames, usernames and script paths can still be
sensitive; redact the inventory before sharing it.

`runtime_verified` is always false: declared process/revision/variant values
do not prove what an already-running CIW loaded. Matrix composition, forbidden
combinations, and runtime process-version identity are not implemented by this
increment. A future optional identity check needs trustworthy per-session
evidence, not directory names, modification times, or client-side declarations.
