# Deploying & running the bot

Three ways to run it, from quickest to most production-ready. **Start in paper
mode every time** — only flip to live once you've watched it behave and funded a
small amount.

> 🔐 **Security first.** Your `POLYMARKET_PRIVATE_KEY` can move all funds in the
> wallet. Keep it only in a local `.env` (chmod 600), never in git, never in a
> chat, never in an image. Use a dedicated trading wallet with limited funds.

---

## 0. First, prove the engine works (no keys, no network)

```bash
pip install -r requirements.txt
python run.py simulate --n 500 --seed 7   # offline backtest over synthetic candles
python -m pytest -q                        # 22 tests
```

---

## 1. Local (laptop / always-on desktop)

```bash
git clone https://github.com/scholarsclick/polymarket-bot.git
cd polymarket-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp config.example.yaml config.yaml         # tune limits (start tiny!)
cp .env.example .env                        # only needed for live

python run.py discover                       # confirm it sees live markets
python run.py run                            # PAPER mode — watch state/paper_journal.jsonl
```

Going live (after paper looks right and USDC is funded — see §4):

```bash
python run.py setup        # verify auth + balance/allowance
python run.py run --live   # REAL orders
```

Keep it alive across logouts with `tmux`/`screen`, or use systemd/Docker below.

---

## 2. Docker (any host)

```bash
cp config.example.yaml config.yaml
cp .env.example .env        # add POLYMARKET_PRIVATE_KEY for live
docker compose up -d        # paper mode, restarts on crash/reboot
docker compose logs -f
```

Go live by setting `dry_run: false` in `config.yaml`, or:

```bash
docker compose run --rm bot python run.py run --live
```

---

## 3. VPS with systemd (recommended for 24/7)

A $5/mo Linux VPS is plenty. The bot is light (REST polling).

```bash
sudo mkdir -p /opt/polymarket-bot && sudo chown $USER /opt/polymarket-bot
git clone https://github.com/scholarsclick/polymarket-bot.git /opt/polymarket-bot
cd /opt/polymarket-bot
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

cp config.example.yaml config.yaml          # tune
cp .env.example .env && chmod 600 .env       # add your key

# create the service user and install the unit
sudo useradd --system --home /opt/polymarket-bot polybot || true
sudo chown -R polybot /opt/polymarket-bot
sudo cp deploy/polybot.service /etc/systemd/system/polybot.service
sudo systemctl daemon-reload
sudo systemctl enable --now polybot
journalctl -u polybot -f
```

To go live, append `--live` to `ExecStart` in the unit file (or set
`dry_run: false` in `config.yaml`), then `sudo systemctl restart polybot`.

---

## 4. Funding & wallet setup (required for live)

1. You need **USDC.e on Polygon** in the account the bot controls.
2. Set the account type in `config.yaml`:
   - `signature_type: 0` — plain EOA wallet; leave `funder` blank.
   - `signature_type: 1` — email/Magic login; `funder` = your Polymarket proxy
     address (holds the USDC).
   - `signature_type: 2` — browser-wallet proxy.
3. Approve the CLOB exchange to spend your USDC. The Polymarket web UI does this
   automatically the first time you place an order; `python run.py setup` will
   show your balance/allowance so you can confirm.

---

## 5. Operating tips

- **Tune before you trust:** raise `min_edge`, lower `max_position_usd`, and keep
  `kelly_fraction` small (0.1–0.25) until you have live evidence.
- **Kill-switches:** `daily_loss_limit_usd`, `max_consecutive_losses`, and the
  exposure caps halt new entries automatically — set them conservatively.
- **Journal:** every paper fill/settle is appended to `state/paper_journal.jsonl`.
- **Stop the bot:** `Ctrl-C` (local), `docker compose down`, or
  `sudo systemctl stop polybot`. It prints a session summary on shutdown.

> ⚠️ No strategy is guaranteed profitable. Short-term crypto markets are fast and
> adversarial. Treat early live runs as paid research with money you can lose.
