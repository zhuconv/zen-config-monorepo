# Tab Autofill for Zen

When the URL bar completes a site inline, press **Tab** to accept it and keep typing, the way Arc does. While you type, the search glass on the left turns into the favicon of the site Enter would open.

| You do | URL bar shows |
|---|---|
| type `red` | `red[dit.com/]`, with the reddit favicon on the left (`[ ]` is the selected completion) |
| press Tab | `reddit.com/▏`: the completion is accepted and the caret is at the end |
| type `r/zen` | `reddit.com/r/zen[_browser/]`: paths complete one segment at a time |
| press Tab again | `reddit.com/r/zen_browser/▏` |
| type `red panda` | the search glass comes back: Enter will search |

Everything else stays as Zen does it:

- With no completion on screen, Tab moves through the results.
- After accepting, the next Tab moves into the results.
- Shift+Tab, the arrow keys and Enter are unchanged, and Tab in an empty URL bar still opens Zen's commands.
- When you pick a result with the arrow keys, the icon shows that result's favicon. Searches keep the glass.

Which sites get completed is still up to Zen, which uses your history. The favicon comes from the history database, so nothing is fetched while you type. Sites without a stored icon show the default globe.

## Install

From the repository root, `./install.sh` installs this mod together with the agent sidebar. For this mod alone (macOS):

```sh
./install.sh     # loader into Zen.app, script into your default Zen profile, self-check
./uninstall.sh   # undo all of it
```

Then restart Zen: open `about:support` and click **Clear startup cache…**.

`install.sh` copies the [fx-autoconfig](https://github.com/MrOtherGuy/fx-autoconfig) loader (pinned under `vendor/`, MPL-2.0) into `Zen.app`, and copies the loader's profile part and `tab-autofill.uc.js` into your profile. It also installs a LaunchAgent. Whenever the installed Zen build changes, the LaunchAgent runs the end-to-end test through the real loader in a throwaway profile, and shows a notification only if something broke. The log is `~/Library/Logs/zen-tab-autofill.log`. Run `install.sh` again after changing the script.

With your own fx-autoconfig setup, you can instead copy `tab-autofill.uc.js` into your profile's `chrome/JS/` folder and restart Zen.

`theme.json` and `preferences.json` describe the mod in Sine's format, but Sine cannot install it from this repository yet.

## Settings

These live in `about:config`.

| Preference | Default | Effect |
|---|---|---|
| `uc.tab-autofill.tab-accepts` | `true` | Tab accepts the inline completion |
| `uc.tab-autofill.favicon` | `true` | Show the favicon instead of the search glass |
| `uc.tab-autofill.favicon-after-accept` | `false` | Keep the glass until the completion is accepted |

## Testing

`test/run_test.py` starts your installed Zen headless with a throwaway profile, seeds some fake history, loads the script and checks the behaviors above. Your own profile is not touched. It needs only Python 3:

```sh
python3 test/run_test.py            # headless
python3 test/run_test.py --headful  # watch it in a window
```

Run it after a Zen update to check that the mod still works. Last passing run: Zen 1.24.2b (Firefox 160).

## How it works

Pressing Right or End already accepts a completion in Zen: collapsing the selection makes the URL bar confirm the completed text as typed. On Tab, the script collapses the selection the same way, and only while an untouched completion is on screen. For the icon, it watches which result the URL bar would open (`setResultForCurrentValue`) and overrides `#identity-icon` with a `page-icon:` URL. Disabling the mod in Sine undoes both.

## License

MIT. Written with an AI assistant (Claude).
