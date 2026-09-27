# 🛍️ Stock Watcher (Hlídač skladu)

Checks product pages every 30 minutes and sends you a **WhatsApp message** the moment a size you want is back in stock.

- **Runs for free on GitHub Actions**, even when your computer is off
- **Web dashboard** (works on your phone too) for adding and editing products and choosing the rule for each one
- Supports **Reserved** (and the other LPP shops: Mohito, Sinsay, House, Cropp) and **Decathlon**. Many other shops work through a generic reader
- You only get a message when something changes, so it won't send the same alert every 30 minutes

```
GitHub Actions (every 30 min) ──► downloads each product page ──► compares with last check (state.json)
        ▲                                                              │ new size in stock?
        │ items.json                                                   ▼
  Dashboard (GitHub Pages)                                  WhatsApp via CallMeBot
```

---

## Setup (one time, about 20 minutes)

### Step 1: Create a GitHub account and a repository

1. Sign up at <https://github.com> if you don't have an account yet.
2. Click **+** (top right), then **New repository**.
   - Name: `stock-watcher`
   - Visibility: **Public** (recommended). Public repos get unlimited free Actions minutes and a free dashboard page. Anyone who finds the repo can see which product links you're watching. Your phone number and keys stay secret either way.
   - Click **Create repository**.

### Step 2: Upload the files

The zip contains no folders, only plain files, so everything uploads in one go.

1. Unzip `stock-watcher.zip` on your computer.
2. On the new repo page, click **uploading an existing file**.
3. Select or drag in **all the files except `check.yml`**:
   `watcher.py`, `index.html`, `items.json`, `state.json`, `requirements.txt`, `README.md`, `test_watcher.py`
4. Click **Commit changes**.

### Step 2b: Create the schedule file (the only file that has to be in a folder)

GitHub only runs schedules from the `.github/workflows` folder. You can create it right in the browser:

1. In the repo, click **Add file → Create new file**.
2. In the file name box, type exactly: `.github/workflows/check.yml`
   Each time you type `/`, GitHub turns the text before it into a folder. That's expected.
3. Open `check.yml` from the zip in a text editor (TextEdit / Notepad), copy **all** of its text and paste it into the big box on GitHub.
4. Click **Commit changes…** → **Commit changes**.
5. Check that the **Actions** tab now shows a workflow called **Stock check**.

### Step 3: Activate WhatsApp (CallMeBot)

1. Save the number **+34 644 95 42 75** in your phone contacts, e.g. as "CallMeBot".
   (This number sometimes changes. Check the current one at <https://www.callmebot.com/blog/free-api-whatsapp-messages/>.)
2. Send it this WhatsApp message: `I allow callmebot to send me messages`
3. Within about 2 minutes you'll get a reply with your **APIKEY** (a number). Keep it.

### Step 4: Add the secrets to GitHub

In the repo, go to **Settings → Secrets and variables → Actions → New repository secret** and add:

| Name | Value |
|---|---|
| `CALLMEBOT_PHONE` | your number with country code, e.g. `+420777123456` |
| `CALLMEBOT_APIKEY` | the key from step 3 |

### Step 5: Turn on the dashboard (GitHub Pages)

1. Go to **Settings → Pages**.
2. Under *Build and deployment*, pick **Deploy from a branch**, branch **main**, folder **/ (root)**, then **Save**.
3. After about a minute, the page shows your dashboard address, something like `https://YOUR-NAME.github.io/stock-watcher/`. Bookmark it, or add it to your phone's home screen.

### Step 6: Create a token for the dashboard

The dashboard needs a key that lets it save changes to your repo.

1. Open <https://github.com/settings/personal-access-tokens/new> (Fine-grained token).
2. Token name: `stock-watcher dashboard`. Expiration: 1 year (or longer).
3. **Repository access → Only select repositories →** `stock-watcher`
4. **Permissions → Repository permissions**:
   - **Contents**: *Read and write*
   - **Actions**: *Read and write*
5. Click **Generate token** and copy it. It starts with `github_pat_…`.

### Step 7: First run

1. Open the dashboard and click ⚙︎. Fill in the repository (`YOUR-NAME/stock-watcher`) and paste the token. **Save**.
   The token is stored only in that browser. Enter it once on each device you use.
2. In settings, click **📱 Poslat testovací zprávu** (send test message). You should get a WhatsApp message within 1–2 minutes.
3. Click **↻ Zkontrolovat teď** (check now). After 1–2 minutes each product shows its sizes: green = in stock, purple outline = sizes you're watching.

That's it. From now on the check runs automatically every 30 minutes.

---

## Using the dashboard

- **+ Přidat** (add): paste the product link (for the exact colour you want), then pick a rule:
  - **Naskladní se konkrétní velikost** (a specific size comes back in stock), e.g. `S, M` for the Reserved thong
  - **Objeví se jakákoliv velikost kromě…** (any size except… appears), e.g. `36` for the Decathlon shoes
  - **Produkt je vůbec skladem** (the product is in stock at all): for shops without sizes, or when any size is fine
- After the first check, **Upravit** (edit) shows the sizes the shop offers as buttons you can click.
- The **toggle** pauses watching an item without deleting it.
- "Napsat mi i když se hlídaná velikost zase vyprodá" (also notify me when it sells out again) is optional.
- If a check fails 6 times in a row (about 3 hours), you get a ⚠️ WhatsApp message.

## Adding more shops

Reserved, Mohito, Sinsay, House, Cropp and Decathlon each have their own reader. For any other shop the watcher tries the standard product data that most shops publish for Google. After adding a product from a new shop, click **↻ Zkontrolovat teď**:

- **Sizes appear:** it works.
- **"e-shop neukazuje velikosti":** only whole-product availability can be tracked there.
- **"tento e-shop zatím neumím přečíst" (can't read this shop yet):** that shop needs its own reader. The shop readers are in `watcher.py`, one short section per shop.

## Known limitations

- **Anti-bot protection.** Some shops block traffic from data-centre servers like GitHub's. The watcher imitates a real Chrome browser, which usually gets through, but if the dashboard keeps showing *"e-shop zablokoval přístup (HTTP 403)"* (the shop blocked access) for a shop, GitHub's servers are being blocked. Fixes for that are running the watcher on a home computer or Raspberry Pi (`python watcher.py` every 30 min via cron), or a paid proxy.
- **Timing.** GitHub runs scheduled jobs "roughly" every 30 minutes, and delays of 5–15 minutes are normal at busy times.
- **CallMeBot** is a free hobby service. It occasionally lags or goes down. To add Telegram as a backup, create a bot with @BotFather and add the `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` secrets. Messages then go to both.
- **Paused schedules.** GitHub pauses schedules in repos with no activity for 60 days. The watcher saves a small daily "heartbeat" so this shouldn't happen. If it ever does, *Actions → Stock check → Enable workflow*.
- **Shop website changes.** If a shop redesigns its site, its reader may stop working. You'll get the ⚠️ message, and the reader then needs a small update.

## For the technically curious

```
items.json               what to watch (edited by the dashboard)
state.json               last seen stock (written by the bot, don't edit)
watcher.py               everything: shop readers (Reserved/LPP, Decathlon, generic JSON-LD),
                         check loop, rules, WhatsApp/Telegram notifications
index.html               dashboard (static page, talks to GitHub API)
test_watcher.py          tests
.github/workflows/check.yml  schedule (created in step 2b)
```

Run locally: `pip install -r requirements.txt`, then

- `python watcher.py --inspect "<product url>"` shows what the watcher sees
- `python watcher.py --dry-run` runs a full check without sending or saving
- `pytest` runs the tests
