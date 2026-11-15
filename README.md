# Fleet Control

Phone-first bulk model settings for **Hermes Agent**, by **driptothebone**.

Fleet Control is a cleaner v2 of **Model Board**, my Hermes hackathon entry. The
submitted entry stays frozen exactly as submitted at
[driptothebone/hermes-model-board @ `17de6b8`](https://github.com/driptothebone/hermes-model-board/tree/17de6b85a7e746909d6e524b245d78ccf3187dc7).
This repository starts fresh and does not share its history. Inside the
dashboard the tab is still called **Model Board**.

See every profile's models at a glance, tick several profiles and change their
main model, subagents, auxiliary tasks, reasoning effort, Fast mode, fallback
chain or subagent limit together. Nothing is saved until you review the exact
changes and explicitly confirm.

![Fleet Control on a phone with Colour off by default and with Colour on](docs/phone-demo.png)

*Phone view with synthetic demo profiles. The left side shows the default clean look and the right side shows Colour on.*

## Install

Requires Hermes **0.23.0 or later** with its browser dashboard, the local-file
config backend and **in-process** plugin loading. Separate plugin-host
isolation, remote config and profile-scoped tickets are not supported, so the
plugin refuses them. Do not weaken an existing isolation policy to enable this
plugin.

```sh
hermes plugins install https://github.com/driptothebone/fleet-control
hermes plugins enable fleet-control
```

Restart the Hermes dashboard so it discovers the plugin, then open **Model Board**.
If you have the hackathon `hermes-model-board` installed, you can disable it.
The two use different plugin ids and do not conflict.

The plugin adds its own state, preview and apply API routes inside the existing
authenticated Hermes dashboard. It does not start a separate server or open a
port. On a phone, reach it through the dashboard's existing authenticated
HTTPS or Tailscale access.

## What it does

- **Overview** shows profiles, readable configs, profiles that route some tasks
  to other providers and a main-model breakdown.
- **Change several** works on All shown or Ticked profiles. A scope line always
  names exactly which profiles the next preview will change, including ticked
  profiles hidden by the filter.
- **Settings** cover the main model, main plus subagents together, subagents
  (a specific model or the same as main), all auxiliary tasks (specific, same
  as main or automatic), main reasoning effort, Fast or Normal, max subagents at
  once (1 to 16) and an ordered fallback chain of up to ten entries that you can
  add to, reorder, remove from or clear.
- **Per profile** editing opens any profile to change its settings or
  individual auxiliary tasks.
- **Review** uses plain labels such as "Main model model-a to model-b" with the
  exact config fields and values one click away, then one explicit confirmation
  and a single Apply. Results show what was saved, read back field by field.
- **Looks native** by following the active dashboard theme, dark or light, live.
  A **Colour** switch (off by default) colour-codes models, providers, values
  and status. Your browser remembers the choice.

## Safety and disclosures

- No `.env`, `auth.json`, CLI login or other credential-store reads. Model
  choices come through Hermes's own model-options route. Hermes owns credential
  handling and may refresh or probe provider catalogues. A listed model is not
  proof of login, entitlement or that it will answer.
- Reads host-authorized profile routing configuration through Hermes's config
  backend. It does not return whole configurations or credential or endpoint
  fields.
- Exact preview, explicit confirmation, expiring single-use preview tokens,
  stale-state rejection and saved-field readback. If a profile changed after the
  preview, Apply is refused and nothing is written. Recovery reloads and makes a
  fresh, unconfirmed preview. Apply is never retried or replayed automatically.
- Saves profiles one at a time under the host's same-process mutation lock.
  **Not a fleet transaction, rollback system or cross-process lock.** Partial or
  uncertain outcomes are reported per profile.
- Hermes's file backend rewrites a profile's config when saving, which drops
  YAML comments. Keep comments elsewhere if they matter to you.
- Does not start or restart gateways, switch running chat sessions, launch
  models, execute shell commands, install dependencies, add telemetry or
  self-update. Saved settings apply to future sessions.
- Changing a model within the same provider keeps that route's endpoint and auth
  settings untouched. Changing provider clears only an old endpoint or API mode
  that exactly matches Hermes's own public definition of the previous provider.
  The review masks the endpoint value. Custom endpoints, auth overrides,
  malformed or legacy fallback entries, profiles with external secret sources
  and unsupported task-effort combinations are refused or shown read-only with a
  reason. They are never silently changed or carried across providers.
- Not included are jobs, federation, login or refresh tooling and multi-machine
  control.

## Compatibility and verification

The exact 2.0.0 runtime files were exercised on Hermes v0.23.0 (commit
`2bd378e`) with synthetic profiles and an offline host. Coverage includes
automatic plugin discovery, served UI assets, dashboard authentication and core
config writes with readback through every editor, individually and in bulk.
Stale, unconfirmed and replayed saves were rejected and unticked profiles were
left unchanged.

Some checks were done on earlier pre-release revisions of the same interface
and are not repeated for these exact bytes. Before the Colour switch was added,
theme integration was reviewed in Midnight, Hermes Teal, Mono, Ember and Nous
Blue (light) at 390 px phone and desktop widths with in-browser text contrast of
at least 4.5 to 1. The author also ran a pre-release build on his own Hermes
host, first read-only against real profiles and then for a main-model switch to
another provider and back. Screenshots here use **synthetic profiles**. Whether
a listed model is available to *your* account is not something the plugin can
know.

The local Hermes admission validator passes. **Private and config-backend API
warnings remain.** The adapter uses the host's auth guard, mutation lock and
version-sensitive config-backend, provider-definition and Fast-mode helpers. It
never replaces or patches Hermes core. Review compatibility on future Hermes
releases.

## License

MIT. Copyright (c) 2026 driptothebone.
