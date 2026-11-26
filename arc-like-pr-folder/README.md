# PR Live Folder for Zen

Makes Zen's GitHub **Pull Requests** live folder work like Arc's. Create the folder as usual: right-click the sidebar → **New Live Folder** → **GitHub** → **Pull Requests**.

| | Zen as shipped | With this mod |
|---|---|---|
| Which PRs | the boxes you tick (at first only *Assigned to Me*), however old | also *Created by Me* and *My Review Requests*, and only PRs with activity in the last 14 days |
| PRs by others in repos you look after, when nobody asked for your review | not listed | listed if you name those repos or accounts in `uc.pr-folder.review-in`: open, ready PRs you haven't reviewed |
| When it refreshes | every 30 minutes (15 at the fastest) | when Zen comes to the front, when you switch to the folder's space, when you open a PR page, and every 2 minutes while you use the Mac |
| A PR page you open | stays a separate tab, and the folder adds a second one | moves into the folder, in place of the folder's own tab |
| A PR that merges while you look at it | its tab closes at the next refresh | stays until you leave it |
| A PR that leaves the list and comes back (a box unticked and ticked again, a search that missed it once) | stays hidden when the folder was open, as if you had closed it | shows again |

The rest stays as Zen makes it. The folder's menu still works, and its boxes still decide what the folder lists: a box you untick stays unticked. Closing a PR's tab still keeps that PR out of the folder. Issues and RSS live folders are left alone.

## Install

From the repository root, `./install.sh` installs this mod with the others. For this mod alone (macOS, after `../arc-like-cmdT/install.sh` has put the fx-autoconfig loader in place):

```sh
./install.sh     # script into your default Zen profile, its test into the self-check
./uninstall.sh   # undo it
```

Restart Zen afterwards: open `about:support` and click **Clear startup cache…**.

## Settings

These live in `about:config`.

| Preference | Default | Effect |
|---|---|---|
| `uc.pr-folder.recent-days` | `14` | List only PRs updated in this many days. `0` lists every open PR, as Zen does |
| `uc.pr-folder.poll-seconds` | `120` | Refresh this often while the Mac is in use (no input for 10 minutes pauses it). `0` turns it off |
| `uc.pr-folder.min-gap-seconds` | `10` | The shortest time between two refreshes brought on by Zen coming to the front, a space switch or the timer |
| `uc.pr-folder.adopt-opened-prs` | `true` | Move a PR page you open into the folder |
| `uc.pr-folder.review-in` | empty | Repos (`owner/repo`) and accounts (`owner`, every repo it owns), separated by commas or spaces. In them, the folder also lists open PRs by others that you haven't reviewed, drafts left out. Once you review one, it leaves the folder; when its author asks you to review it again, it comes back as a review request |

The folder menu's **Fetch Interval** is Zen's own timer. It keeps running, but the refreshes above come sooner.

## Testing

`test/run_test.py` starts your installed Zen headless with a throwaway profile, makes a Pull Requests live folder in it and checks the behaviors above. A stand-in for github.com inside the browser answers the folder's searches the way GitHub would, and every other request goes to a dead proxy, so nothing reaches GitHub. Your own profile is not touched. It needs only Python 3:

```sh
python3 test/run_test.py            # headless
python3 test/run_test.py --headful  # watch it in a window
python3 test/run_test.py --via-loader "$HOME/Library/Application Support/zen/Profiles/<profile>/chrome"
```

The self-check runs the `--via-loader` form after each Zen update. Last passing run: Zen 1.24.3b (Firefox 160).

## How it works

- **What Arc does.** Arc 1.174.1 reads the same `github.com/pulls` page Zen reads. Its searches are `is:pr is:open updated:>=<date> sort:updated-desc` with `author:@me` or `review-requested:@me`. Its code refreshes the folder on events (the window coming to the front, a space getting focus, a PR created or reviewed in Arc, a folder item selected), with a throttle. The 14 days come from Arc's own saved folders: open PRs stayed for up to 13 days after their last update and were gone by day 17.
- **Zen's live folder.** `ZenLiveFoldersManager` keeps the folders, and a GitHub provider object fetches each one. The mod wraps three of their methods:
  - the provider's `fetchItems` ticks *Created by Me* and *My Review Requests* when they were never set. With `review-in` set, it also runs a second search, `is:pr is:open draft:false -author:@me -reviewed-by:@me (repo:… OR user:…)`, and adds what it finds;
  - its `parsePullRequests` adds `updated:>=<14 days ago>` to each search;
  - the manager's `onLiveFolderFetch` sees each fetch's results before Zen updates the folder.
  Unloading the script puts all three back.
- **Before each update.** The mod closes the tabs of PRs that left the results itself. Zen would close them too, but in an open folder it animates the close, and the close then counts as you dismissing the PR, so a PR that came back would stay hidden (seen in Zen 1.24.3b; a collapsed folder closes them without animation and is fine). A tab that is on screen stays until you select another tab. For each new PR, an ordinary tab already showing it (same space and container) moves into the folder and becomes that PR's tab.
- **Refreshing.** Each window reports `activate`, Zen's space switches, `TabSelect`, and address changes of its tabs to the module shared by all windows. That module refreshes the folder through the provider's own `refresh()`. Only one refresh per folder runs at a time. A PR page you open asks again within 3 seconds.

## License

MIT. Written with an AI assistant (Claude).
