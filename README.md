# Twitch Drops Miner

This application allows you to AFK mine timed Twitch drops, without having to worry about switching channels when the one you were watching goes offline, claiming the drops, or even receiving the stream data itself. This helps you save on bandwidth and hassle.

### How It Works:

Every several seconds, the application pretends to watch a particular stream by fetching stream metadata - this is enough to advance the drops. Note that this completely bypasses the need to download any actual stream video and sound. To keep the status (ONLINE or OFFLINE) of the channels up-to-date, there's a websocket connection established that receives events about streams going up or down, or updates regarding the current amount of viewers.

### Features:

- Stream-less drop mining - save on bandwidth.
- Game priority and exclusion lists, allowing you to focus on mining what you want, in the order you want, and ignore what you don't want.
- Sharded websocket connection, allowing for tracking up to `199` channels at the same time.
- Automatic drop campaigns discovery based on linked accounts (requires you to do [account linking](https://www.twitch.tv/drops/campaigns) yourself though).
- Stream tags and drop campaign validation, to ensure you won't end up mining a stream that can't earn you the drop.
- Automatic channel stream switching, when the one you were currently watching goes offline, as well as when a channel streaming a higher priority game goes online.
- Login session is saved in a cookies file, so you don't need to login every time.
- Mining is automatically started as new campaigns appear, and stopped when the last available drops have been mined.

### Usage:

- Download and unzip [the latest release](https://github.com/DevilXD/TwitchDropsMiner/releases) - it's recommended to keep it in the folder it comes in.
- Run it and login/connect the miner to your Twitch account by using the in-app login form.
- After a successful login, the app should fetch a list of all available campaigns and games you can mine drops for - you can then select and add games of choice to the Priority List available on the Settings tab, and then press on the `Reload` button to start processing. It will fetch a list of all applicable streams it can watch, and start mining right away. You can also manually switch to a different channel as needed.
- If you wish to keep the miner occupied with mining anything it can, beyond what you've selected via the Priority List, you can use the Priority Mode setting to specify the mining order for the rest of the games.
- Make sure to link your Twitch account to game accounts on the [campaigns page](https://www.twitch.tv/drops/campaigns), to enable more games to be mined.

### CLI mode (this fork)

This fork can run the miner without Tk, a display server, or the tray UI. From a source checkout:

```sh
TDM_DATA_DIR=/var/lib/twitch-drops .venv/bin/python main.py cli run
```

`TDM_DATA_DIR` selects the directory for `settings.json`, `cookies.jar`, logs, and the lock file.
To reuse an existing login, create the directory with restricted permissions, copy the existing
`cookies.jar` into it, and protect it: `chmod 600 /var/lib/twitch-drops/cookies.jar`.

When stdin is a TTY, `cli run` accepts these console commands:

| Command | Action |
| --- | --- |
| `help`, `status`, `channels`, `games`, `inventory [all]` | Show CLI state. |
| `switch <channel>`, `reload` | Switch the watched channel or reload inventory. |
| `priority list\|add\|remove\|move ...`, `exclude list\|add\|remove ...` | Edit game filters. |
| `get [key]`, `set <key> <value>` | View or edit settings. |
| `logout`, `quit` / `exit` | Remove the login after confirmation, or stop cleanly. |

Offline commands do not run the miner: `cli settings show|get|set ...`, `cli priority
list|add|remove|move ...`, `cli exclude list|add|remove ...`, and `cli logout --yes`.
Exit codes are `0` success, `1` fatal error, `2` argument or command error, `3` miner already
running, and `4` settings error.

Use `systemd` or `nohup` with `cli run` for unattended execution. They have no TTY, so the
interactive console is disabled; edit settings through the offline commands and control the
process with signals. The packaged Windows executable is windowed and has no console output;
run this fork from source on Windows when using CLI mode.

#### Dashboard (CLI mode)

Enable the optional browser panel with `main.py cli run --dashboard`. It listens on
`127.0.0.1` on the first free port from `23450` to `23500` by default; open the URL printed at startup.

| Flag | Environment variable | Default |
| --- | --- | --- |
| `--dashboard` | `TDM_DASHBOARD=1` | Off |
| `--dashboard-host HOST` | `TDM_DASHBOARD_HOST` | `127.0.0.1` |
| `--dashboard-port N\|A-B` | `TDM_DASHBOARD_PORT` | First free in `23450-23500` |
| `--dashboard-readonly` | `TDM_DASHBOARD_READONLY=1` | Off |
| `--dashboard-token-file [PATH]` | `TDM_DASHBOARD_TOKEN_FILE` | Off (default path when enabled: `TDM_DATA_DIR/dashboard.token`) |
| — | `TDM_DASHBOARD_TOKEN` | Off |
| — | `TDM_DASHBOARD_ORIGINS` | No additional origins |

Authentication is off by default, even outside loopback. A non-loopback bind without a token
warns that anyone on the network can control the miner. Set `TDM_DASHBOARD_TOKEN` or enable
`--dashboard-token-file [PATH]` to require a token; the latter creates or reads a restricted
`0600` file. The env token takes precedence over the file. Startup prints only the token
file path, never the token. `GET /api/meta` exposes the auth and readonly settings without
authentication. The browser keeps a supplied token in session storage. Read-only
mode hides controls and rejects changes. Host and Origin checks protect the API; set
`TDM_DASHBOARD_ORIGINS` to comma-separated exact origins when a reverse proxy rewrites Host.
The panel offers Overview (live drop, KPIs and charts), Inventory (campaigns grouped by game,
filter toggles and expandable drops), Games (priority, exclusions and channel selection),
Channels (watch controls), Settings and Logs. Search filters the current page. Inventory filters
and the light/dark theme persist in browser local storage. The layout adapts to a bottom tab bar
on mobile. Priority and exclusion controls use the matching `GET` and `POST` routes.
From another terminal, `main.py cli ctl state` queries the running miner through its local
control channel; `main.py cli ctl help` lists the available remote console commands.
Authentication failures are limited per client IP, so clients behind one reverse proxy share
the limit for invalid tokens while valid tokens remain usable. Use TLS at the proxy for remote access.

For nginx, set `TDM_DASHBOARD_ORIGINS=https://your.host` and use
`location / { proxy_pass http://127.0.0.1:23450; proxy_http_version 1.1; proxy_set_header Upgrade $http_upgrade; proxy_set_header Connection "upgrade"; }`.

### Pictures:

![Main](https://user-images.githubusercontent.com/4180725/164298155-c0880ad7-6423-4419-8d73-f3c053730a1b.png)
![Inventory](https://user-images.githubusercontent.com/4180725/164298315-81cae0d2-24a4-4822-a056-154fd763c284.png)
![Settings](https://user-images.githubusercontent.com/4180725/164298391-b13ad40d-3881-436c-8d4c-34e2bbe33a78.png)

### Notes:

> [!WARNING]  
> Due to how Twitch handles the drop progression on their side, watching a stream in the browser (or by any other means) on the same account that is actively being used by the miner, will usually cause the miner to misbehave, reporting false progress and getting stuck mining the current drop.  
> 
> Using the same account to watch other streams during mining is thus discouraged, in order to avoid any problems arising from it.

> [!CAUTION]  
> Persistent cookies will be stored in the `cookies.jar` file, from which the authorization (login) information will be restored on each subsequent run. Make sure to keep your cookies file safe, as the authorization information it stores can give another person access to your Twitch account, even without them knowing your password!

> [!IMPORTANT]  
> Successfully logging into your Twitch account in the application may cause Twitch to send you a "New Login" notification email. This is normal - you can verify that it comes from your own IP address. The detected browser during the login will be "Chrome", as that's what the miner currently presents itself to the Twitch server.

> [!NOTE]  
> The time remaining timer always countdowns a single minute and then stops - it is then restarted only after the application redetermines the remaining time. This "redetermination" can happen at any time Twitch decides to report on the drop's progress, but not later than 20 seconds after the timer reaches zero. The seconds timer is only an approximation and does not represent nor affect actual mining speed. The time variations are due to Twitch sometimes not reporting drop progress at all, or reporting progress for the wrong drop - these cases have all been accounted for in the application though.

> [!NOTE]  
> The source code requires Python 3.10 or higher to run.

### Notes about the Windows build:

- To achieve a portable-executable format, the application is packaged with PyInstaller into an `EXE`. Some antivirus engines (including Windows Defender) might report the packaged executable as a trojan, because PyInstaller has been used by others to package malicious Python code in the past. These reports can be safely ignored. If you absolutely do not trust the executable, you'll have to install Python yourself and run everything from source.
- The executable uses the `%TEMP%` directory for temporary runtime storage of files, that don't need to be exposed to the user (like compiled code and translation files). For persistent storage, the directory the executable resides in is used instead.
- The autostart feature is implemented as a registry entry to the current user's (`HKCU`) autostart key. It is only altered when toggling the respective option. If you relocate the app to a different directory, the autostart feature will stop working, until you toggle the option off and back on again

### Notes about the Linux build:

- The Linux app is built and distributed using two distinct portable-executable formats: [AppImage](https://appimage.org/) and [PyInstaller](https://pyinstaller.org/).
- There are no major differences between the two formats, but if you're looking for a recommendation, use the AppImage.
- The Linux app should work out of the box on any modern distribution, as long as it has `glibc>=2.35`, plus a working display server.
- Every feature of the app is expected to work on Linux just as well as it does on Windows. If you find something that's broken, please [open a new issue](https://github.com/DevilXD/TwitchDropsMiner/issues/new).
- The size of the Linux app is significantly larger than the Windows app due to the inclusion of the `gtk3` library (and its dependencies), which is required for proper system tray/notifications support.
- As an alternative to the native Linux app, you can run the Windows app via [Wine](https://www.winehq.org/) instead. It works really well!

### Notes about the macOS build:

- The macOS version is packaged using PyInstaller into a standalone `.app` bundle, distributed as a ZIP archive.
- Since this application is not signed with a paid Apple Developer Certificate, **macOS Gatekeeper will block it** on the first run (saying it "The application is damaged and can't be opened").
  - **To fix this**: Either open the Terminal in the folder the app is in (or navigating with `cd path/to/folder`) and enter `xattr -cr Twitch Drops Miner (by DevilXD).app` or just type `xattr -cr ` (make sure to put a space at the end), drag and drop the `Twitch Drops Miner (by DevilXD).app` file into the terminal window (this will auto-fill the path) and enter
- Persistent files (like `cookies.jar`, `settings.json`, `lock.file` and the `cache` folder) are stored inside the application bundle in `Twitch Drops Miner (by DevilXD).app/Contents/MacOS` (to access them Right-click the application and select `Show Package Contents`)

### Advanced Usage:

If you'd be interested in running the latest master from source or building your own executable, see the wiki page explaining how to do so: https://github.com/DevilXD/TwitchDropsMiner/wiki/Setting-up-the-environment,-building-and-running

### Support

If you'd encounter any issues with the miner:

- Please see the [troubleshooting page](https://github.com/DevilXD/TwitchDropsMiner/wiki/Troubleshooting) for some common issues and their explanation.  
- Please [search the issues page](https://github.com/DevilXD/TwitchDropsMiner/issues?q=sort%3Aupdated-desc%20is%3Aissue) to see if your issue hasn't been reported yet.  
- If it's not been reported yet, feel free to open a new issue, describing your problem.

If you like the application and found it useful, please consider donating a small amount of money to support me. Thank you!

<div align="center">

[![Buy me a coffee](https://i.imgur.com/cL95gzE.png)](
    https://www.buymeacoffee.com/DevilXD
)
[![Support me on Patreon](https://i.imgur.com/Mdkb9jq.png)](
    https://www.patreon.com/bePatron?u=26937862
)

</div>

### Project goals:

Twitch Drops Miner (TDM for short) has been designed with a couple of simple goals in mind. These are, specifically:

- Twitch Drops oriented - it's in the name. That's what I made it for.
- Easy to use for an average person. Includes a nice looking GUI and is packaged as a ready-to-go executable, without requiring an existing Python installation to work.
- Intended as a helper tool that starts together with your PC, runs in the background through out the day, and then closes together with your PC shutting down at the end of the day. If it can run continuously for 24 hours at minimum, and not run into any errors, I'd call that good enough already.
- Requiring a minimum amount of attention during operation - check it once or twice through out the day to see if everything's fine with it.
- Underlying service friendly - the amount of interactions done with the Twitch site is kept to the minimum required for reliable operation, at a level achievable by a diligent site user.

TDM is not intended for/as:

- Mining channel points - again, it's about the drops: only.
- Mining anything else besides Twitch drops - no, I won't be adding support for a random 3rd party site that also happens to rely on watching Twitch streams.
- Unattended operation: worst case scenario, it'll stop working and you'll hopefully notice that at some point. Hopefully.
- 100% uptime application, due to the underlying nature of it, expect fatal errors to happen every so often.
- Being hosted on a remote server as a 24/7 miner.
- Being used with more than one managed account.
- Mining campaigns the managed account isn't linked to.

This means that features such as:

- It being possible to run it without a GUI, or with only a console attached.
- Any form of automatic restart when an error happens.
- Docker or any other form of remote deployment.
- Using it with more than one managed account.
- Making it possible to mine campaigns that the managed account isn't linked to.
- Anything that increases the site processing load caused by the application.
- Any form of additional notifications system (email, webhook, etc.), beyond what's already implemented.

..., are most likely not going to be a feature, ever. You're welcome to search through the existing issues to comment on your point of view on the relevant matters, where applicable. Otherwise, most of the new issues that go against these goals will be closed and the user will be pointed to this paragraph.

For more context about these goals, please check out these issues: [#161](https://github.com/DevilXD/TwitchDropsMiner/issues/161), [#105](https://github.com/DevilXD/TwitchDropsMiner/issues/105), [#84](https://github.com/DevilXD/TwitchDropsMiner/issues/84)

### Credits:

<!---
Note: The translations credits are sorted alphabetically, based on their English language name.
When adding a new entry, please ensure to insert it in the correct place in the second section.
Non-translations related credits should be added to the first section instead.

Note: When adding a new credits line below, please add two trailing spaces at the end
of the previous line, if they aren't already there. Doing so ensures proper markdown
rendering on Github. In short: Each credits line should end with two trailing spaces,
placed past the period character at the end.

• Last line can have the two trailing spaces omitted.
• Please ensure your editor won't trim the trailing spaces upon saving the file.
• Please ensure to leave a single empty new line at the end of the file.
-->

@guihkx - For the CI script, CI maintenance, and everything related to Linux builds.  
@kWAYTV - For the implementation of the dark mode theme.  
@crocchetto - For the macOS port.  

@Bamboozul - For the entirety of the Arabic (العربية) translation.  
@Suz1e - For the entirety of the Chinese (简体中文) translation and revisions.  
@wwj010, @zhangminghao1989, @Self4215 - For the Chinese (简体中文) translation corrections and revisions.  
@Ricky103403 - For the entirety of the Traditional Chinese (繁體中文) translation.  
@LusTerCsI - For the Traditional Chinese (繁體中文) translation corrections and revisions.  
@nwvh - For the entirety of the Czech (Čeština) translation.  
@Kjerne - For the entirety of the Danish (Dansk) translation.  
@lmdpocus - For the entirety of the Dutch (Nederlandse) translation.  
@Rensoraa - For the Traditional Dutch (Nederlandse) translation corrections and revisions.  
@roobini-gamer - For the entirety of the French (Français) translation.  
@Calvineries - For the French (Français) translation revisions.  
@ThisIsCyreX - For the entirety of the German (Deutsch) translation.  
@Nagyhoho1234 - For the entirety of the Hungarian (Magyar) translation.  
@Eriza-Z - For the entirety of the Indonesian translation.  
@casungo - For the entirety of the Italian (Italiano) translation.  
@ShimadaNanaki - For the entirety of the Japanese (日本語) translation.  
@biroman -  For the entirety of the Norwegian (Norsk) translation.  
@Patriot99 - For the Polish (Polski) translation and revisions (co-authored with @DevilXD).  
@zarigata - For the entirety of the Portuguese (Português) translation.  
@Sergo1217 - For the entirety of the Russian (Русский) translation.  
@kilroy98, @flamesv - For the Russian (Русский) translation corrections and revisions.  
@Shofuu - For the entirety of the Spanish (Español) translation and revisions.  
@Forero-0 - For the Spanish (Español) translation revisions.  
@alikdb - For the entirety of the Turkish (Türkçe) translation.  
@DogancanYr, @Elderly-Emre, @Hweord - For the Turkish (Türkçe) translation corrections and revisions.  
@Nollasko - For the entirety of the Ukrainian (Українська) translation and revisions.  
@kilroy98 - For the Ukrainian (Українська) translation corrections and revisions.  

# Login in 2026

Since September 18, 2026, Twitch has blocked new device logins for the ANDROID_APP client. Existing Android sessions may still work: preserve `cookies.jar`; a new device login cannot recreate one. New logins require a browser/integrity-based method, which this project does not yet implement.

The miner saves `cookies.jar` atomically with private permissions and backs up a validated session to `cookies.jar.bak`. When the token changes, the prior backup becomes a dated `cookies.jar.bak.YYYYmmdd-HHMMSS` archive; archives are deduplicated by token and never deleted automatically (each one is a distinct session). A jar without a token is never saved over a saved session without backing it up first, and an unreadable jar is never overwritten. Use `cli auth status` to check the saved session and archive count, `cli auth backup` for an immediate backup, `cli auth restore` to recover the backup (the replaced session goes to `cookies.jar.bak.prev`), or `cli auth import --from-jar PATH` to import another saved jar. `cli auth import` also accepts `TDM_AUTH_TOKEN` or a token from stdin and only accepts ANDROID_APP tokens. Import backs up both the previous and newly imported sessions. A failed backup blocks destructive session changes.

Logout is disabled by default. Set `TDM_ALLOW_LOGOUT=1` to allow `cli logout --yes` or online logout; the offline command backs up the jar before removing it, preserving any previous backup as an archive. **The auth token grants full account access.** Keep the jar and its backups private with permissions `0600`; never share a jar or token.
