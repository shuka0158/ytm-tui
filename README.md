# ytm-tui

A terminal music player for **your own YouTube Music account**. Browse your
playlists, liked songs and library, search, start radios, and play — all inside
your terminal, with real album art via the kitty graphics protocol.

![now playing](docs/screenshots/03-playing.png)

<p align="center">
  <img src="docs/screenshots/04-search.png" width="49%" alt="search" />
  <img src="docs/screenshots/01-library.png" width="49%" alt="library" />
</p>

## Features

- Loads **your** YouTube Music playlists, Liked Music and Library Songs
- Full-text search across YouTube Music
- Start a radio from any track
- Gapless streaming — nothing is downloaded to disk, audio is piped into `mpv`
- Real album art in the terminal (kitty graphics protocol; unicode-block fallback
  elsewhere)
- Queue, shuffle, repeat, seek, volume — remembered between runs

## How it works

| Layer | Component |
|-------|-----------|
| Account, playlists, search | [`ytmusicapi`](https://github.com/sigma67/ytmusicapi) |
| Audio stream URLs | [`yt-dlp`](https://github.com/yt-dlp/yt-dlp) (cached, next track prefetched) |
| Playback | `mpv`, driven over its JSON IPC socket |
| Interface | [`textual`](https://github.com/Textualize/textual) + `textual-image` |

## Requirements

- Python 3.11+
- `mpv` and `ffmpeg` on your `PATH`
- A terminal for best results **kitty** (album art). Works in others with
  unicode-block art.

## Install

```sh
git clone https://github.com/shuka0158/ytm-tui.git
cd ytm-tui
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Then launch:

```sh
./ytm
```

The `ytm` launcher uses the project-local `.venv` and can be run from anywhere
(a shell, a `.desktop` shortcut, or inside kitty):

```sh
kitty --title ytm-tui -e /path/to/ytm-tui/ytm
```

## Connecting your account

With no credentials stored, `./ytm` drops straight into setup. You can also run
the steps explicitly:

```sh
./ytm setup     # connect a Google account
./ytm verify    # check the stored credentials still work
./ytm logout    # delete the stored credentials
```

### Sign in with browser headers

This app signs in by reusing your browser's YouTube Music session. It does **not**
ask for your password, and nothing is sent anywhere except YouTube's own API.

1. Open <https://music.youtube.com> signed in to the account you want.
2. Press `F12` → **Network** tab → type `browse` in the filter box, then click
   anything in the page so a request appears.
3. Click a **POST** request to `music.youtube.com` and copy its **request
   headers** as raw text:
   - **Chrome/Edge:** Headers tab → *Request Headers* → toggle **Raw** → select
     all → copy
   - **Firefox:** right-click the request → *Copy Value* → *Copy Request Headers*
4. Run `./ytm setup`, paste the whole block, and press `Ctrl-D`.

Credentials are stored in `config/browser.json` (git-ignored). The file is a
snapshot of your login, so it **survives reboots and your browser being closed or
killed** — it does not need the browser running. It ends only when the underlying
**Google session** is revoked or rotated.

### Make it survive browser logouts (recommended)

Cookies copied from your everyday browser share that browser's session
credential. If you later sign out of that browser, the session is revoked
server-side and the copy dies with it. To make the player independent of your
normal browsing, grab its cookies from a **throwaway session you never sign out
of**:

1. Open an **Incognito / Private** window — a fresh, isolated session.
2. Sign in to <https://music.youtube.com> and confirm your playlists load.
3. Copy the raw `browse` request headers as above.
4. Run `./ytm setup` and paste them.
5. **Close the Incognito window — do _not_ click "sign out" inside it.** Closing
   discards the local cookies but leaves the session alive on Google's side; the
   player keeps using it.

Now logging out of, closing, or rebooting your main browser does not affect the
player. Only a **password change**, **"sign out of all devices"**, or a
Google-forced re-auth will end it. When the library shows up empty, redo the
steps above.

> **Why not OAuth?** A Google "TVs and Limited Input devices" OAuth client
> authenticates and refreshes forever, but Google blocks device-OAuth tokens from
> the internal `youtubei` API that `ytmusicapi` uses — *every* call (search, home,
> library) returns HTTP 400. There is no forever-token for a third-party YouTube
> Music client that actually works; browser headers are the only functioning
> method.

## Keys

| Key | Action |
|-----|--------|
| `enter` | play the highlighted track |
| `space` | play / pause |
| `n` / `p` | next / previous track |
| `←` / `→` | seek 5s (`shift` for 30s) |
| `+` / `-` | volume |
| `s` | shuffle |
| `r` | repeat: off → all → one |
| `x` | stop |
| `/` | search YouTube Music |
| `R` | start a radio from the highlighted track |
| `a` | append the highlighted track to the queue |
| `F5` | reload playlists |
| `tab` | move between panes |
| `?` | help |
| `q` | quit |

Volume, shuffle and repeat are remembered in `config/settings.json`.

## Maintenance

`yt-dlp` breaks whenever YouTube changes something, so if playback fails while
playlists still load, update it first:

```sh
.venv/bin/pip install -U yt-dlp
```

To rebuild the environment from scratch:

```sh
rm -rf .venv && python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Notes & disclaimer

This is an unofficial client that uses private YouTube Music endpoints via
`ytmusicapi`. It is not affiliated with, endorsed by, or supported by Google or
YouTube. Use it with your own account, at your own risk.

## License

[MIT](LICENSE)
