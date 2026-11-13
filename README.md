# zen-config-monorepo

A [Zen browser](https://zen-browser.app) setup for macOS, as code. One script
turns a fresh Zen into this one:

- **[Tab Autofill](zen-tab-autofill/)**: when the URL bar completes a site
  inline, Tab accepts it, as in Arc. While you type, the URL bar shows the
  favicon of the site Enter would open.
- **[Agent Sidebar](zen-agent-sidebar/)**: select text, press ⌘⇧A, and ask
  your local Codex or Claude Code about the page or PDF in a sidebar. It runs
  on your ChatGPT or Claude login, with no API key.
- **A self-check** that runs the tests after each Zen update and shows a
  notification only when something broke.

Both mods are scripts that run with the browser's own privileges, loaded by
[fx-autoconfig](https://github.com/MrOtherGuy/fx-autoconfig). They are not
extensions, and no mod manager is needed.

## Set up a new Mac

1. Install Zen from [zen-browser.app](https://zen-browser.app), open it once
   so it creates your profile, then quit it.
2. Install the tools. The sidebar needs Node and at least one of the two
   agent CLIs, logged in; Tab Autofill needs none of them.

   ```sh
   xcode-select --install                 # git and python3
   brew install node                      # Homebrew: https://brew.sh
   brew install --cask codex && codex login
   curl -fsSL https://claude.ai/install.sh | bash    # Claude Code; log in with `claude`, then /login
   ```

3. Let your terminal change Zen.app: System Settings → Privacy & Security →
   App Management → turn on Terminal (or the terminal you use).
4. Install:

   ```sh
   git clone https://github.com/zhuconv/zen-config-monorepo.git
   cd zen-config-monorepo
   ./install.sh
   ```

5. Restart Zen: open `about:support` and click **Clear startup cache…**.

To try it, type `red` in the URL bar and press Tab, or select text on a page
and press ⌘⇧A.

This repository holds only the mods. Bookmarks, passwords and add-ons come
from signing in to sync in Zen.

## Day to day

- **Update:** `git pull && ./install.sh`, then restart Zen the same way.
- **After a Zen update** the self-check tests the mods against the new build.
  If it reports a problem, run `./install.sh` again first: an update can
  remove the loader from Zen.app.
- **Remove:** `./uninstall.sh`.

## Layout

| Path | What it holds |
|---|---|
| `install.sh`, `uninstall.sh` | run both mods' installers in order |
| `zen-tab-autofill/` | the URL bar mod, the pinned fx-autoconfig loader (`vendor/`), the self-check (`tools/`) |
| `zen-agent-sidebar/` | the sidebar, the pinned agent adapters (`adapter/`) |

Each folder's README covers its settings and tests. Both test suites start
your installed Zen headless with a throwaway profile, so your own profile is
never touched:

```sh
python3 zen-tab-autofill/test/run_test.py
python3 zen-agent-sidebar/test/run_test.py
```

## License

MIT. Vendored: fx-autoconfig (MPL-2.0), marked (MIT). Written with an AI
assistant (Claude).
