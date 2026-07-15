# Shared browser profile broker

Hermes can coordinate several agents against one live, agent-owned browser
profile without opening the profile directory more than once.

The profile topology is:

```text
one browser profile directory
        |
one browser process / CDP endpoint
        |
one BrowserProfileBroker
        |
several exact CDP page targets
```

`tools/browser_profile_broker.py` is an internal protocol layer, not a
model-facing tool. It provides:

- one process-local broker owner per profile id, plus an optional filesystem
  owner lock;
- exact CDP `targetId` and attached `sessionId` routing;
- independent page-context locks, allowing calls to different pages to overlap;
- a wider browser/profile lock for `Browser.*` and `Target.*` mutations;
- explicit one-to-one bindings between CDP targets and incarnation-qualified
  CUA compositor surface tokens;
- bounded nonce proof for the initial CDP-target/surface join. The temporary page
  title is restored in `finally`; title is never used after the exact binding is
  established.

`CDPSupervisor.call_cdp()` is the transport seam. A broker can therefore reuse
the supervisor's one persistent WebSocket rather than creating a connection per
agent action.

## Safety constraints

- The profile must be agent-owned. Do not point this at the default profile of a
  separately running human browser.
- Never bypass Firefox/Chromium profile locks or share a live profile directory
  across users, containers, or compositor instances.
- Page-local protocol calls may overlap only across distinct exact target ids.
  Browser chrome, permissions, extensions, downloads, history/bookmark writes,
  and profile settings remain profile-global.
- CUA/raw browser UI input is coordinated separately by the compositor target;
  CDP concurrency does not imply several independent standard Wayland keyboards
  inside one browser process.
- A changed compositor epoch is a new surface identity and requires a new join.

The private CUA session supervisor exports `CUA_BROWSER_PROFILE_DIR` for the one
browser owner. It does not launch a browser automatically and does not attach to
the human desktop.
