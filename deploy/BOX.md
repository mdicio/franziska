# Always-on box + desktop GPU: setup runbook

> **Since 2026-10-28 (DESIGN decision 19) the desktop serves llama-server, not Ollama.** Where this runbook says
> Ollama, read: `deploy/franziska-llm.service` with `--host` set to the desktop's Tailscale address, on the box
> `FRANZISKA_BASE_URL=http://desktop:8080/v1`, and the router vocab exported from the GGUF file
> (`python -m franziska.export_vocab ~/models/gguf/gemma-4-26B-A4B-it-UD-Q4_K_M.gguf ~/.local/share/franziska/router-vocab.gguf`).
> The desktop check uses llama-server's `/health`. The rest is unchanged.

```
                 ┌──────────────── always-on box (~6–10 W) ────────────────┐
 Telegram ◄────► │ bot.py · reminders · brief · calendar sync · mail fetch │
 Google   ◄────► │ life.sqlite · ledger.sqlite · mail · router vocab (16MB)│
 ChatGPT  ◄────► │ pi (Sign in with ChatGPT) ── life MCP                   │
                 └───────┬──────────────────────────────┬──────────────────┘
          Wake-on-LAN    │ magic packet (LAN broadcast)  │ Ollama API over Tailscale (encrypted)
                         ▼                              ▼
                 ┌──────────── desktop (RTX 3060, on when needed) ─────────┐
                 │ Ollama: gemma4:12b → router logprobs, agents, mail triage│
                 └──────────────────────────────────────────────────────────┘
```

What runs where, and what happens when the desktop is off:

| Job | Needs | Desktop off |
|---|---|---|
| Reminders, Done/Snooze buttons, brief, `/today` `/week` `/notes` | box only | works |
| Google Calendar sync, Gmail fetch | box + internet | works |
| Message routing (AnyJev) | desktop Ollama | skipped; policy below decides |
| Secretary / finance / chat agents | desktop Ollama | policy below |
| `/think` | box + ChatGPT | works |
| Email triage | desktop Ollama | queued until the desktop is up (email never goes to the cloud) |

Policy `FRANZISKA_WHEN_OFF` for a message while the desktop is off:
- `ask` (default): buttons "☁️ ChatGPT now" / "🖥️ Wake desktop".
- `cloud`: answer with ChatGPT straight away (schedule, calendar, notes only), wake the desktop in the background.
- `wake`: wake and answer locally when it is up (~1–2 min); if it doesn't come up, offer ChatGPT.
Finance always waits for the desktop: the ledger is never reachable from the cloud.

## 1. The box

Recommended: an Intel N100/N150 mini PC with 16 GB RAM and an NVMe SSD (~6–10 W). It runs the same x86 wheels
as the desktop (llama-cpp-python for the vocabulary, Node for pi). It also has RAM to spare for a small CPU model
later (routing even with the desktop off). A Raspberry Pi 5 (8 GB, NVMe HAT rather than an SD card) also works;
expect llama-cpp-python to compile from source there.

It must be wired to the same LAN as the desktop (192.168.1.0/24): the wake packet is a LAN broadcast.

```bash
# Ubuntu Server 24.04 / Debian 13, user account, then:
curl -LsSf https://astral.sh/uv/install.sh | sh
# Node 24 user-level, same as the desktop (checksum-verified tarball into ~/.local/opt, symlinks in ~/.local/bin)
git clone <your repo> ~/franziska      # or rsync it from the desktop
cd ~/franziska && uv sync && (cd pi && npm ci) && ln -sf "$PWD/pi/node_modules/.bin/pi" ~/.local/bin/pi
sudo tailscale up                                      # join the same tailnet as the desktop
```

Move the data once (bot stopped on the desktop): `rsync -a desktop:.local/share/franziska/ ~/.local/share/franziska/`.

`.env` on the box, in addition to the Telegram settings:

```bash
FRANZISKA_OLLAMA_BASE_URL=http://desktop:11434/v1        # desktop over Tailscale (MagicDNS name)
FRANZISKA_ROUTER_VOCAB=~/.local/share/franziska/router-vocab.gguf          # from step 2
FRANZISKA_DESKTOP_MAC=aa:bb:cc:dd:ee:ff                # the desktop NIC
FRANZISKA_DESKTOP_BROADCAST=192.168.1.255
FRANZISKA_WHEN_OFF=ask
FRANZISKA_GOOGLE=1
```

Then `pi` → `/login openai` (on a headless box pi asks you to paste the final redirect URL), and install
`deploy/franziska-bot.service` as described in that file.

## 2. The desktop

**Wake-on-LAN** (Realtek RTL8111 on `enp39s0`, currently disabled):
1. BIOS: enable "Wake on LAN" / "Power On By PCI-E", and disable "ErP" / deep sleep (otherwise the NIC has no power
   when the PC is off).
2. Linux: in `/etc/netplan/*.yaml`, under `enp39s0:` add `wakeonlan: true`, then `sudo netplan apply`.
   Check: `sudo ethtool enp39s0 | grep Wake-on` should show `Wake-on: g`.
3. Test from the box: `python -c "from franziska.compute import Desktop; Desktop('', 'aa:bb:cc:dd:ee:ff', '192.168.1.255').wake()"`
   with the desktop shut down. If it doesn't wake from full shutdown, it usually does from suspend
   (`systemctl suspend`), which is also faster (~5 s instead of ~40 s).

**Ollama reachable from the box only, over Tailscale:**
```bash
sudo systemctl edit ollama
#   [Service]
#   Environment="OLLAMA_HOST=0.0.0.0:11434"
#   Environment="OLLAMA_KEEP_ALIVE=30m"
sudo systemctl restart ollama
sudo ufw allow in on tailscale0 to any port 11434 proto tcp
sudo ufw deny 11434/tcp
sudo ufw enable        # check `sudo ufw status` first if you already use ufw
```
Ollama has no authentication: never expose 11434 beyond the tailnet.

**Router vocabulary** (16 MB, re-export after `ollama pull` updates the model):
```bash
uv run --with gguf==0.19.0 python -m franziska.export_vocab gemma4:12b ~/.local/share/franziska/router-vocab.gguf
scp ~/.local/share/franziska/router-vocab.gguf box:.local/share/franziska/
```

**Optional, later:** let the desktop suspend itself when Ollama has been idle for a while and nobody is logged in,
so "wake when needed" also means "sleep when not".

## 3. Google (Calendar read/write, Gmail read-only)

1. https://console.cloud.google.com → new project "assistant" → enable **Google Calendar API** and **Gmail API**.
2. OAuth consent screen: External, app name "assistant", your address as user. Scopes: `calendar.events`,
   `gmail.readonly`. Then **Publish app** ("In production"; Google shows an "unverified app" warning you click
   through). In "Testing" status refresh tokens expire after 7 days. Check the console for the current rules on
   restricted scopes (Gmail) for unverified personal apps.
3. Credentials → Create OAuth client ID → **Desktop app** → download the JSON (keep it out of git).
4. Sign in on the web app's Setup page (Google card: upload the JSON, Sign in with Google). Opened from another
   device, the browser ends on a `127.0.0.1` page that doesn't load: paste its address into the page. Or in a
   terminal, on the box through an SSH tunnel so Google's redirect to `localhost:8765` reaches it:
   ```bash
   ssh -L 8765:localhost:8765 box
   cd ~/franziska
   uv run --with google-auth-oauthlib==1.5.0 python -m franziska.google.auth ~/.config/franziska/client_secret.json
   # open the printed URL in your laptop's browser, approve; ~/.local/share/franziska/google_token.json is written (mode 600)
   ```
The token can read your mail and change your calendar: it stays on the box, mode 600, outside the repo, under `~/.local/share/franziska/`.
Revoke it any time at https://myaccount.google.com/permissions.

## 4. Backups

Nightly, on the box: `sqlite3 ~/.local/share/franziska/life.sqlite ".backup ~/.local/share/franziska/backup/life-$(date +%F).sqlite"` (same for
`ledger.sqlite`), then copy `~/.local/share/franziska/backup/` off the box (desktop over Tailscale, or encrypted cloud storage).
