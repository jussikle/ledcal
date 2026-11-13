# ledcal: pixel desk dashboard

A clock, the weather, and today's Apple Calendar agenda on a 64×32 RGB LED matrix
(Raspberry Pi 4 + Adafruit RGB Matrix Bonnet). It rotates through these screens:

- **Clock**: date, big time, the current temperature, and your next event
- **Agenda**: today's remaining events in your iPhone calendar colors, with `NOW` on what's happening
- **Weather**: animated icon, temperature, conditions, and today's high and low (Open-Meteo, no API key needed)
- **Games**: one compact page with the last 24 hours of results and the next game for each of your teams.
  Finished results stay hidden as `?-?` until noon
- **Live** (only while one of your games is on): a big scoreboard with the running score and period/clock

Events you add on your phone show up within about 5 minutes. Finnish letters (ä, ö, å) display properly.

## 1. Get an app-specific password for iCloud

Apple doesn't let other devices sign in with your normal password, so make a separate password just for this:

1. Go to **account.apple.com** and sign in
2. Open **Sign-In and Security → App-Specific Passwords**
3. Click **+** and name it `ledcal`
4. Copy the password (`xxxx-xxxx-xxxx-xxxx`)

You can revoke it there at any time. If you ever change your Apple ID password, all app-specific
passwords stop working and you'll need to make a new one.

## 2. Copy the folder to the Pi

From your computer (replace `pi` and `raspberrypi.local` with your username and Pi hostname):

```bash
scp -r ledcal pi@raspberrypi.local:~/
ssh pi@raspberrypi.local
cd ~/ledcal
```

## 3. Install

```bash
bash install.sh
```

This sets up a Python environment, reuses your LED matrix library from the earlier project
(or builds it if it's missing), turns off the Pi's onboard audio (it causes flicker on the matrix),
and installs a background service that starts on boot. If it asks you to reboot, do that.

## 4. Configure

```bash
nano config.toml
```

Fill in `apple_id` and `app_password`. If you don't live in central Helsinki, set your `latitude`
and `longitude` too. Everything else has sensible defaults.

## 5. Test it, then turn it on

```bash
sudo venv/bin/python ledcal.py --demo     # fake data: checks the wiring and display
sudo venv/bin/python ledcal.py            # real data, in the foreground (Ctrl+C to stop)
sudo systemctl start ledcal               # run in the background, and on every boot
journalctl -u ledcal -f                   # watch the log
```

After you change `config.toml`, run `sudo systemctl restart ledcal`.

## Sports teams

Add one `[[sports.teams]]` block per team at the end of `config.toml`.

**NHL, NBA, NFL, MLB, MLS and the big European football leagues** come from ESPN's free data.
Find your team's id with:

```bash
venv/bin/python ledcal.py --find-team panthers --league hockey/nhl
venv/bin/python ledcal.py --find-team arsenal --league soccer/eng.1
```

Then paste the printed `team = "..."` line into a block:

```toml
[[sports.teams]]
source = "espn"
league = "hockey/nhl"
team = "26"
```

**Other leagues (for example Finnish Liiga)** can come from TheSportsDB: open your team's page on
thesportsdb.com, take the number from the page address, and use `source = "thesportsdb"`. The free
key is limited, so if games are missing, a supporter key (about $9/month) fixes that.

How the spoiler protection works: while one of your games is live, it's shown first with the
live score and period/clock, refreshed every minute. Once it ends, the result is hidden
until the first 12:00 after the game *started*.
A 02:00 NHL game shows its score at noon the same day; a 18:30 Liiga game stays hidden until noon the
next day. Change the time with `hide_scores_until`, or set it to `""` to always show scores.

## Preview without the Pi

On any computer with Python 3.11+, you can render the screens to images:

```bash
pip install -r requirements.txt
python ledcal.py --preview --demo        # writes preview/*.png and preview/preview.gif
python ledcal.py --preview               # same, but with your real calendar and weather
```

The second command is also a handy way to check your iCloud login before touching the Pi.

## Troubleshooting

| Problem | Fix |
|---|---|
| Flickering or garbled pixels | Raise `gpio_slowdown` to 5 in `config.toml` |
| Error mentioning `snd_bcm2835` | Reboot after `install.sh`, or set `disable_hardware_pulsing = true` |
| Colors swapped or only half the panel lights up | Check `hardware_mapping`: `adafruit-hat`, or `adafruit-hat-pwm` if you soldered the PWM jumper |
| `401 Unauthorized` in the log | You used your normal Apple password. Use an app-specific password |
| Some calendars are missing | Remove the `calendars = [...]` filter, or check the spelling matches your phone |
| A small red dot or "LOADING" stays on screen | The Pi has no internet. Check Wi-Fi and `journalctl -u ledcal` |
| Too bright at night | Lower `night_brightness` (0 turns the display off) |
| Sports screen never appears | It's skipped when there are no games in range. Check `journalctl -u ledcal` for "sports" errors and the team id |

## Building the box

The goal: a clean wooden or printed box, a dark front where the pixels glow through, and one power
cable out the back.

**Parts**
- A **5 V 4 A power supply** with a 5.5×2.1 mm barrel plug. The bonnet powers both the panel
  *and* the Pi from its barrel jack, so this is the only cable. Don't plug in the Pi's USB-C as well.
- A **panel-mount 5.5×2.1 mm DC socket** plus a short barrel cable, so the plug sits flush in the back
- A **smoked/tinted acrylic sheet** (2–3 mm, about 50% light transmission) for the front
- Optionally, **diffuser film or tracing paper** and a 3D-printed **grid** (one square cell per LED)
- M3 screws/standoffs (the panel has M3 holes on its back), rubber feet, black paint or tape

**Size it to your panel.** Check the pitch printed on the back (P2.5, P3, P4...). A 64×32 panel at
P3 is 192×96 mm, at P4 256×128 mm, at P5 320×160 mm. Inside depth ~55–60 mm: panel ~15 mm,
Pi + bonnet ~35 mm, plus air.

**Layering, from the front:**
1. Tinted acrylic, set in a rabbet or groove in the front frame. Black when off, LEDs shine through
   crisply when on. This one sheet is what makes it look like a product rather than a project.
2. (Optional) Diffuser film, then the grid pressed against the LEDs. This gives soft, square
   "big pixel" look instead of sharp dots. Search Printables or Thingiverse for "64x32 LED matrix grid"
   with your pitch.
3. The panel, screwed to a frame or cleats inside the box.
4. The Pi with the bonnet, mounted on standoffs on the panel's back or on the back cover.

**Build options**
- **Wooden box (most appliance-like):** four sides from 12–15 mm oak or walnut with mitred or box
  joints, a routed groove for the acrylic, and a removable back panel held with magnets or screws.
- **3D printed:** search Printables for "64x32 LED matrix case" for your pitch, or ask me to
  design a parametric box with your exact measurements.
- **Laser-cut:** plywood or acrylic finger-joint box from a generator like boxes.py, in black
  acrylic or birch ply.

**Details that make it feel finished**
- Paint the inside black so no light leaks around the edges.
- Ventilation: a row of slots low on the back and high on the back. Warm air rises out and the Pi stays
  cool with no fan. Add stick-on heatsinks to the Pi.
- Use a wooden or plastic box, not metal, so Wi-Fi still works.
- Strain relief on the power cable (a cable tie inside, or the panel-mount socket).
- Lean the box back 5–10° with angled feet; it reads better on a desk.
- A removable back so you can reach the SD card. Everything else is done over SSH.

## Customizing

- Screen order and timing: the `[screens]` section of `config.toml`
- Colors, layouts, and new screens: `ledcal.py`. Each screen is a small class with a `draw()` method.
  Add one to `SCREENS` and to `order` in the config.
