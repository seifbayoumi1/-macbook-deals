# MacBook Air M5 — UAE price monitor

Watches the price of the **MacBook Air M5 13" (Midnight, Arabic/English keyboard)**
across trusted UAE stores (Amazon.ae, Noon, Sharaf DG, Virgin, Lulu, Jumbo, Emax)
and **emails you when any listing drops to 4,200 AED or below**.

Runs in **discovery mode**: you give each store a *search URL*, and the script
finds **every** matching listing — so when any store uploads a **new** listing,
it gets monitored automatically without you adding it.

Runs for free on GitHub Actions every 2 hours — no need to keep your PC on.

---

## How it works

1. You give each store a **search URL** in `config.yaml` (not a product link).
2. Every 2 hours GitHub Actions runs `price_monitor.py`.
3. For each store it opens the search results and discovers every listing
   (link + title + price), using structured data first, then a generic fallback.
4. It keeps only listings whose title contains all of `require_keywords` and none
   of `exclude_keywords` (this filters out wrong colors/models/accessories).
5. If a kept listing's price is `<= threshold_aed`, you get an email with price + link.
6. `state.json` remembers the last alerted price so you aren't spammed every run
   (you get re-alerted only if the price drops further).

---

## Setup (one time, ~10 minutes)

### 1. Check the search URLs
`config.yaml` already has a search URL per store. To get/replace one: open the
store, type your search (e.g. `macbook air m5 midnight`) in its search box, press
enter, and copy the address bar into `search_url`. Delete any store you don't want.

> Tweak `require_keywords` if too few listings match (e.g. remove `midnight`), or
> add to `exclude_keywords` if junk slips through.

### 2. Set up alerts — Telegram (recommended) or Email

**Telegram (simplest, no password needed):**
1. In Telegram, open **@BotFather** → send `/newbot` → follow prompts → copy the
   **bot token** it gives you.
2. Open **@userinfobot** → it replies with your numeric **chat id**.
3. Send any message to your new bot once (so it's allowed to message you).
4. You'll add `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID` as secrets in step 3.

**Email (optional alternative):** create a Gmail **App Password**
(enable 2-Step Verification → <https://myaccount.google.com/apppasswords>),
and use `SMTP_USER` / `SMTP_PASSWORD` / `ALERT_TO` secrets instead.

You can set up either one, or both.

### 3. Put it on GitHub
1. Create a new GitHub repo and push this folder to it:
   ```bash
   git init
   git add .
   git commit -m "MacBook price monitor"
   git branch -M main
   git remote add origin https://github.com/<you>/<repo>.git
   git push -u origin main
   ```
2. In the repo: **Settings → Secrets and variables → Actions → New repository secret**.
   For **Telegram** add:
   | Secret name        | Value                          |
   |--------------------|--------------------------------|
   | `TELEGRAM_TOKEN`   | the bot token from @BotFather  |
   | `TELEGRAM_CHAT_ID` | your chat id from @userinfobot |

   Or for **Email** add `SMTP_USER`, `SMTP_PASSWORD`, `ALERT_TO` instead.

3. Go to the **Actions** tab → "MacBook price monitor" → **Run workflow** to test it now.

That's it. It will now check every 2 hours automatically.

### 4. Turn on the website (GitHub Pages)
The repo includes a deal-dashboard webpage (like bestlaptop.deals, but for UAE).
To publish it: **Settings → Pages → Build and deployment → Source: "Deploy from a
branch" → Branch: `main` / folder: `/docs` → Save.**

Your site will be live at `https://<you>.github.io/<repo>/` within a minute. It shows
every tracked MacBook M5 listing with price, savings, a deal badge
(🔥 Target hit / Best price ever / Great deal…), a price-history sparkline, and a
"View deal" button. It refreshes automatically every time the monitor runs.

---

## Run it locally (to test)

```bash
pip install -r requirements.txt

# Windows PowerShell (Telegram):
$env:TELEGRAM_TOKEN="your-bot-token"
$env:TELEGRAM_CHAT_ID="your-chat-id"
python price_monitor.py
```

Without any notifier vars it still runs, prints prices, and writes the dashboard
data — it just won't send an alert.

---

## Tuning

- **Change the price threshold:** edit `threshold_aed` in `config.yaml`.
- **Check more/less often:** edit the `cron:` line in `.github/workflows/monitor.yml`.
- **Add a store:** add its domain to `trusted_domains` and a product entry.

---

## Known limitation (important)

Amazon.ae, Noon, and Sharaf DG use anti-bot protection. From GitHub's datacenter
IPs a page may occasionally return a captcha/error instead of the price. When that
happens the log prints `fetch failed / likely blocked` or `could not read a price`
for that store, and the others still work. If one store is blocked often:

- Add a `selector:` line for it in `config.yaml`, or
- Rely on the stores that do work (Lulu, Jumbo, Virgin tend to be friendlier).

The price sanity window is 1,000–20,000 AED to avoid picking up an unrelated number.
