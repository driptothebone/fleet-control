# Changelog

## 2.2.1

- Main-model previews now distinguish the model window from the saved compaction ceiling. When a switch clears the old window override, the preview shows **Auto-detect** for the model window and reports the saved ceiling separately. The preview does not change the ceiling.

## 2.2.0

- Optional other machines. Fill in up to four machine slots in the plugin's
  settings (a name, the address of that machine's own Hermes dashboard and its
  dashboard token) and one board shows and changes profiles on all of them.
  Saves on another machine go through that machine's Hermes dashboard.
- With no machine configured nothing changes. There are no outbound calls or
  machine picker. The board's responses match 2.1.0.
- Profiles are named by machine and name, so the same profile name on two
  machines stays two rows. A machine filter and machine labels appear once
  another machine is configured. The scope line and review name the
  machine.
- Each preview and Apply stays on one machine. Selections across machines are
  refused before any write.
- A machine that is offline, refuses its token or redirects is shown with that
  status and its profiles are hidden. The other machines keep working.
- Addresses must be https, or http on loopback and Tailscale only. Tokens are
  read through Hermes's secret API and never reach the browser.
- Still **not a fleet transaction**. Saves are per profile and per machine.

## 2.1.0

- Every read and save now goes through Hermes's public dashboard routes instead
  of Hermes internals, as the plugin catalog review asked. No private imports,
  host locks or config-backend access remain.
- Main model changes use Hermes's own model switch. It validates the choice and
  may ask for a cost confirmation, which the board reports instead of skipping.
- Fast availability now comes from Hermes's own model catalog. A profile on a
  custom endpoint shows why Fast can't be verified.
- Each profile is rechecked right before it is saved. Hermes has no conditional
  save yet, which is requested in
  [hermes-agent#156100](https://github.com/NousResearch/hermes-agent/issues/156100).
- The dashboard tab is now called **Fleet Control**. The interface is otherwise
  unchanged.
- This repository now holds the hackathon entry and Fleet Control together.
  The entry as submitted is tagged `hackathon-submission`.

## 2.0.0

First release of **Fleet Control**, a cleaner v2 of the hackathon Model Board
([v1 as submitted](https://github.com/driptothebone/fleet-control/tree/hackathon-submission)).
It shipped in its own repository with a new plugin id (`fleet-control`).

- Restores the full Model Board interface with the overview, profile table and
  phone cards, filter, All or Ticked selection, per-profile sheets and bulk
  editors.
- New controls cover all auxiliary tasks (specific, same as main or automatic,
  per task or all at once), Fast or Normal, ordered fallback chains and max
  subagents.
- Switching provider works on profiles pinned to a first-party endpoint. Only
  pins matching the old provider's public definition are cleared. Custom
  endpoints and auth are still refused. Same-provider model changes keep them.
- Native look that follows the active Hermes dashboard theme, dark or light,
  live. An optional **Colour** switch is off by default.
- Clearer review with plain-language before and after values, exact fields one
  click away, a scope line naming exactly which profiles change and saved values
  in results.
- Stale saves explain that nothing was written and offer a reload with a fresh
  preview. Transport failures are reported as uncertain, never as "not saved".
- One broken profile config or catalogue no longer blocks the others. Profiles
  using external secret sources are shown read-only.
- Accessibility improvements include keyboard-operable rows, a focus-trapped
  dialog, focus return and 44 px phone targets.
