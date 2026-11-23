# Claude Accounts for Zen

Open each claude.ai link in the Claude account it belongs to, when you use several. Each account gets a container of its own (`Claude · <alias>`), signed in once, so all of them stay signed in side by side. A link from another app, such as a terminal, opens in the right container. When the link doesn't say which account it belongs to, a small picker asks.

| You click | Zen does |
|---|---|
| `https://claude.ai/artifact/…#acct=gt` | opens it in gt's container at once. The tag is taken off; claude.ai never sees it |
| a claude.ai link with no tag | shows the picker, with the likely account already selected: the one that opened this artifact last, else the one you used last. **Enter** opens it, **1–9** picks another, **Esc** cancels |
| a Claude Code sign-in link (`claude auth login --email <you>`) | opens it in the container of the account with that email, ready for **Authorize** |
| a sign-in link without an email | shows the picker |
| a link for an account the table gives a Chrome profile | hands it to Google Chrome, in that profile, instead of opening a Zen tab |
| any other link | opens it as before |

In the browser:

- **An artifact that doesn't open** (claude.ai's "Page not found": no access, or no such artifact) gets a bar with **Open as …** buttons for the other accounts.
- **Right-click a claude.ai tab** → **Open in Another Claude Account…** reopens it in place, in the account you pick.
- **The toolbar button** does the same for the current claude.ai tab, or opens claude.ai in the account you pick.
- **The picker marks accounts that aren't signed in yet**. Pick one and sign in on the page that opens; the container keeps the login.

The `#acct=<alias>` tags come from the Claude Code side: the [accounts mod in herdr-config](https://github.com/zhuconv/herdr-config/tree/main/claude-mods/accounts) adds one to each artifact link a Claude session publishes, naming the account that session is signed in as. The same mod's `/auth <alias>` switches a machine's Claude login, and reads the same account table.

## Install

From the repository root, `./install.sh` installs this mod with the others. For this mod alone (macOS, after `../zen-tab-autofill/install.sh` has put the fx-autoconfig loader in place):

```sh
./install.sh     # script into your default Zen profile, its test into the self-check
./uninstall.sh   # undo it (your containers and their logins stay)
```

Restart Zen afterwards: open `about:support` and click **Clear startup cache…**.

## The account table

`~/.config/claude-accounts/accounts.json` lists your accounts, in the order the picker numbers them. It stays out of this repository, since it holds your email addresses. `accounts.example.json` shows the format:

| Field | Needed | Meaning |
|---|---|---|
| `alias` | yes | the short name tags and the picker use (`gt`); letters, digits, `.`, `_`, `-` |
| `email` | for sign-in links | the account's email, matched against a sign-in link's `login_hint` |
| `org` | no | the organization uuid Claude Code uses for this account (`cswap list --json`, or `oauthAccount.organizationUuid` in `~/.claude.json`). Artifact links then open in that organization, which matters for an account in several |
| `color` | no | the container's color (`orange`, `cyan`, `green`, `purple`, `pink`, `blue`, `yellow`, `red`); otherwise one by position |
| `container` | no | the container's name; `Claude · <alias>` otherwise |
| `chrome` | no | open this account's links in a Google Chrome profile instead of a Zen container: the profile's folder (`Profile 2`, shown as Profile Path in `chrome://version`) or its name as Chrome shows it. When Chrome has no such profile, a bar says so and offers to open the link in Zen |

The script reads the table again whenever the file changes. A container you rename or recolor in Zen keeps your changes: the script finds it by its id (`uc.claude-accounts.containers`).

## Settings

These live in `about:config`.

| Preference | Default | Effect |
|---|---|---|
| `uc.claude-accounts.enabled` | `true` | Take over Claude links from other apps |
| `uc.claude-accounts.ask-for-tagged` | `false` | Show the picker for tagged and sign-in links too, with their account selected |
| `uc.claude-accounts.config` | empty | Path of the account table, when not the default |

Which account opened which artifact is kept in `claude-accounts-memory.json` in the profile (the latest 500).

## Testing

`test/run_test.py` starts your installed Zen headless with a throwaway profile and a made-up account table, and checks the behaviors above. Every web request goes to a dead proxy, so nothing reaches claude.ai. Your own profile is not touched. It needs only Python 3:

```sh
python3 test/run_test.py            # headless
python3 test/run_test.py --headful  # watch it in a window
python3 test/run_test.py --via-loader "$HOME/Library/Application Support/zen/Profiles/<profile>/chrome"
```

The links go in through Zen's own command-line handler, as links from other apps do. The self-check runs the `--via-loader` form after each Zen update. Last passing run: Zen 1.24.3b (Firefox 160).

## How it works

- **Catching the link.** A link from another app reaches a window as `browserDOMWindow.openURI(uri, …, OPEN_EXTERNAL, …)`. The script hooks `BrowserDOMWindow.prototype.openURI` once and asks the window the call is for whether it takes the link. Anything that isn't a Claude link goes on unchanged. Windows without the script, or with it unloaded, also get the link unchanged.
- **Choosing the container.** The script opens the tab itself with the account's `userContextId`, not marked as external. Zen would otherwise put an external tab in the Space's own container. So one Space holds all accounts' tabs.
- **When Zen wasn't running.** The new window's first argument is the link, and it already has a tab. The script takes the link over and closes that tab.
- **Seeing whether an artifact opened.** An artifact page asks `https://claude.ai/api/frame/<uuid>` for its content. The answer is 200 when this login can open it, and 404 when it can't or the artifact doesn't exist. The script watches those responses (`http-on-examine-response`), remembers successes, and shows the bar on failures.
- **An account in Chrome.** The script runs `open -na "Google Chrome" --args --profile-directory=<folder> <link>`. The Chrome that starts hands the link to the one already running, which opens it in that profile, and quits. The folder comes from Chrome's `Local State` file, which lists each profile's folder and name.
- **Short ids and uuids.** claude.ai's short artifact ids are the uuid in base58, so `/artifact/<id>` and `/code/artifact/<uuid>` count as the same artifact.

## License

MIT. Written with an AI assistant (Claude).
