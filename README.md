# zen-config-monorepo

A [Zen browser](https://zen-browser.app) setup for macOS, as code. One script
turns a fresh Zen into this one:

- **[Tab Autofill](arc-like-cmdT/)**: when the URL bar completes a site
  inline, Tab accepts it, as in Arc. While you type, the URL bar shows the
  favicon of the site Enter would open.
- **[Claude Accounts](multi-cc-switcher/)**: with several Claude accounts,
  each claude.ai link from the terminal opens in the account it belongs to,
  each account in a container of its own; a quick picker asks when the link
  doesn't say which.
- **A self-check** that runs the tests after each Zen update and shows a
  notification only when something broke.

The mods are scripts that run with the browser's own privileges, loaded by
[fx-autoconfig](https://github.com/MrOtherGuy/fx-autoconfig). They are not
extensions, and no mod manager is needed.

**[zen(ius)](https://github.com/zhuconv/zenius)** brings your coding agent
into this Zen: an agent sidebar, and browser tools the agent drives in a
Space of its own while you keep working. It sits here as the `zenius/`
submodule, pinned to the release this setup runs, and installs with its own
installer (see its README), not with `install.sh`.

## Set up a new Mac

1. Install Zen from [zen-browser.app](https://zen-browser.app), open it once
   so it creates your profile, then quit it.
2. Install Apple's Command Line Tools, for git and the python3 the self-check
   runs on:

   ```sh
   xcode-select --install
   ```

3. Let your terminal change Zen.app: System Settings → Privacy & Security →
   App Management → turn on Terminal (or the terminal you use).
4. Install:

   ```sh
   git clone https://github.com/zhuconv/zen-config-monorepo.git
   cd zen-config-monorepo
   ./install.sh
   ```

5. For Claude Accounts, write your account table to
   `~/.config/claude-accounts/accounts.json`
   ([format](multi-cc-switcher/README.md#the-account-table)). Without it,
   Claude links open as usual.
6. Restart Zen: open `about:support` and click **Clear startup cache…**.

To try it, type `red` in the URL bar and press Tab.

This repository holds only the mods. Bookmarks, passwords and add-ons come
from signing in to sync in Zen.

## Day to day

- **Update:** `git pull && ./install.sh`, then restart Zen the same way.
  `git submodule update --init` brings `zenius/` to the release pinned here.
- **After a Zen update** the self-check tests the mods against the new build.
  If it reports a problem, run `./install.sh` again first: an update can
  remove the loader from Zen.app.
- **Remove:** `./uninstall.sh`.

## Layout

| Path | What it holds |
|---|---|
| `install.sh`, `uninstall.sh` | run the mods' installers in order |
| `arc-like-cmdT/` | the URL bar mod, the pinned fx-autoconfig loader (`vendor/`), the self-check (`tools/`) |
| `multi-cc-switcher/` | the Claude account picker and its containers |
| `zenius/` | [zen(ius)](https://github.com/zhuconv/zenius), a submodule pinned to the release this setup runs |

Each folder's README covers its settings and tests. The test suites start
your installed Zen headless with a throwaway profile, so your own profile is
never touched:

```sh
python3 arc-like-cmdT/test/run_test.py
python3 multi-cc-switcher/test/run_test.py
```

The agent sidebar (⌘⇧A) used to live here as `agent-sidebar/`. In November
2026 it moved into zen(ius), now the `zenius/` submodule. Its last version
here is commit 782230a:
`git checkout 782230a -- agent-sidebar` brings it back, uninstall.sh included.

## License

MIT. Vendored: fx-autoconfig (MPL-2.0). Written with an AI
assistant (Claude).
