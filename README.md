# inventory_manager – Grocy Kitchen Kiosk

A touch-first, installable PWA for a kitchen inventory kiosk. It uses
[Grocy](https://grocy.info) as the database, a **Zebra DS2278** barcode scanner
(USB HID keyboard mode), and a **Brother QL-800** USB thermal label printer.

* **Silent printing.** Labels are rendered on the server (Pillow + python-barcode
  **Code 128**) and sent as raw raster commands to the QL-800 with `brother_ql`.
  The browser never calls `window.print()`, so you never see a print dialog.
* **Scan anywhere.** A global key listener plus an off-screen input that keeps focus
  means scans work as soon as the page is open, in every mode, without tapping a field.
  The input uses `inputmode="none"`, so tablets don't open the on-screen keyboard.
* **Big touch targets.** Dark, high-contrast UI. You only need the keyboard to name
  a brand-new product.

```
┌──────────────┬──────────────┬──────────────┐   ● Grocy ● Printer
│   CONSUME    │    SEARCH    │     ADD      │
└──────────────┴──────────────┴──────────────┘
```

## Modes

### CONSUME (default)
Scan a label (`grcy:s:<stock_id>`), a raw `<stock_id>`, or a manufacturer barcode.
The kiosk shows the item (name, expiry, location, stock ID), an **"Item consumed"**
banner, a `5… 4… 3… 2… 1…` countdown and a large **UNDO / CANCEL** button.

* Nothing is removed from Grocy until the countdown reaches 0. The kiosk then calls
  `POST /api/stock/products/{id}/consume` with `amount: 1`. If you scanned a UID label,
  the call also sends that exact stock entry. Grocy's API calls this field
  `stock_entry_id`. The banner then changes to **"Removed from Grocy"**.
* Tap **UNDO** before 0 and you get **"Cancelled — Item kept in stock"**.
* If you scan another item during a countdown, the pending item is committed
  immediately and a new countdown starts.
* Pending consumptions are tracked on the server. If the tablet reloads mid-countdown,
  a safety timer commits the item after the countdown plus a 3 s grace period.
  The server never consumes early.
* With a manufacturer barcode, Grocy picks the entry using its normal order (opened
  items first, then the soonest due date). The card shows the entry that will be used.
* **Long-life items** (major category `Long-Life`): scanning an *unopened* item first
  asks **Consume 1** or **Open 1**. Opening marks the unit as opened, sets its use-by
  date to today plus the product's *use by days after opened* (3–14, default 7) and
  prints a new `USE BY: YYYY-MM-DD` label. Already-opened units are consumed without
  the question.

### SEARCH
* Shows the **total current inventory** (`GET /api/stock`), colour-coded by expiry.
* **Two-tier category filter:** row 1 has major categories (`All`, `Dairy`, `Meat`, …).
  Tapping one opens row 2 with its subcategories (`All Dairy`, `Butter`, `Milk`, …).
* **Location filter:** one chip per location that currently holds stock
  (`All locations`, `📍 Fridge`, `📍 Pantry`, …), based on where entries actually are –
  moved/opened items count at their new location.
* An optional name filter has a ✕ clear button.
* Scan a label or a manufacturer barcode, or tap a product, to see its stock entries
  (expiry, quantity, location). A scanned label's entry is highlighted. Each entry has
  **Consume Now** (uses the undo countdown), **Open 1**, **Reprint Label** and
  **Move Location** (moves the whole entry). **Open 1** marks a single unit as opened
  and – if the product is set to move when opened – moves just that unit (e.g. one
  long-life milk from the cupboard to the fridge); the rest stays put.
  The product header has **✎ Category** to recategorise a product that was entered
  incorrectly (`POST /api/products/{id}/category`) and **⚙ Settings** for per-product
  behaviour: what happens **when one is opened** (stays in place / move to a location),
  the long-life **use by days after opened** (3–14, `POST /api/products/{id}/use-by-days`)
  and the **minimum quantity** (`POST /api/products/{id}/min-stock`) – when stock falls
  below it after a consume, the kiosk asks Grocy to put the product on the shopping
  list (`POST /api/stock/shoppinglist/add-missing-products`).

### ADD
1. **Product.** Scan a manufacturer barcode (`GET /api/stock/products/by-barcode/…`),
   or tap **Major → Subcategory → Product**. An unknown barcode opens a quick
   *create product* dialog with category and location buttons (each with a **＋ New…**
   option that creates the Grocy product group / location in place). It calls
   `POST /api/objects/products` and links the barcode with
   `POST /api/objects/product_barcodes`. The dialog also sets an optional
   **minimum quantity** for the shopping list.
2. **Expiry.** Quick buttons: `+3 Days`, `+5 Days`, `+1 Week`, `+2 Weeks`, `+1 Month`,
   `+3 Months`, `+6 Months`, `No Expiry`. There is also a date picker. The pre-selected
   date is the product's *default due days*, or `GROCY_DEFAULT_BEST_BEFORE_DAYS` if
   the product has none. Pick a **quantity**, whether labels are printed **1 per item**
   or **1 for the box**, and whether to print at all — **Use OEM barcode** skips the
   label and you consume by scanning the product's manufacturer barcode instead.
   **When one is opened later** picks where a single opened unit should move
   (e.g. *Move to Fridge* for long-life milk) or *Stays in place*. This is stored on
   the Grocy product (`move_on_open` + default consume location), so it sticks.
3. **ADD & PRINT LABEL.**
   * Calls `POST /api/stock/products/{id}/add` with `amount`, `best_before_date`
     and `location_id`.
   * Reads the new `stock_id` from the response.
   * Prints a 62 mm label with the product name in large bold text, `EXP: YYYY-MM-DD`
     in bold (omitted when the item has no expiry), and a Code 128 barcode of
     `${BARCODE_PREFIX}${stock_id}`. The barcode has quiet zones and the
     human-readable code printed below it. **Long-life products** print
     `Use by: S M T W T F S` instead of the EXP line, so the day it must be used by
     can be circled with a pen when the item is opened.
   * A toast confirms the print and offers **Reprint**. If the printer fails, the stock
     is still saved and you can reprint. The result screen also offers **Open one now**
     to immediately open (and move) one of the items just added.

## Organising Grocy product groups for the category buttons

The category buttons come from Grocy's **Product groups**
(*Master data → Product groups*). Name each group `Major/Subcategory`:

| Product group name | Row 1 button | Row 2 button |
|--------------------|--------------|--------------|
| `Dairy/Butter`     | Dairy        | Butter       |
| `Dairy/Milk`       | Dairy        | Milk         |
| `Dairy/Cheese`     | Dairy        | Cheese       |
| `Meat/Chicken`     | Meat         | Chicken      |
| `Canned/Beans`     | Canned       | Beans        |
| `Canned/Tomatoes`  | Canned       | Tomatoes     |
| `Dry/Pasta`        | Dry          | Pasta        |
| `Long Life/Sauces` | Long Life    | Sauces       |
| `Leftovers`        | Leftovers    | *(none)*     |

* Only the **first** delimiter splits the name, so `Dry/Pasta/Long` becomes
  `Dry` → `Pasta/Long`.
* Spaces around the delimiter are ignored.
* Products without a group appear under **Uncategorized**.
* Change the delimiter with `CATEGORY_DELIMITER`, for example `CATEGORY_DELIMITER=" > "`
  for `Dairy > Butter`.
* Give each product a group, and a **Default due days** value if you can, so the
  expiry date is pre-filled.
* The major category **`Long-Life`** (also matches `Long Life`/`long-life`) is special:
  its products get circle-a-day `Use by: S M T W T F S` labels and the consume-or-open
  question described above.
* **Default tree:** the kiosk seeds a ready-made kitchen tree (Bakery, Condiments,
  Cooking, Drinks, Frozen, Meat, Long-Life, Pantry, Snacks, …) into Grocy at startup
  (disable with `SEED_DEFAULT_CATEGORIES=false`). It only creates groups that don't
  exist yet, so it is safe on a populated Grocy; it never modifies or deletes your
  own groups. The status dialog (tap the Grocy/Printer indicator) also has a
  **📂 Create default categories** button (`POST /api/categories/seed-defaults`).

## Setup

### 1. Configure

```bash
cp .env.example .env
# edit GROCY_URL, GROCY_API_KEY, GROCY_DEFAULT_LOCATION_ID …
```

| Variable | Default | Description |
|----------|---------|-------------|
| `GROCY_URL` | `http://grocy:80` | Grocy base URL (without `/api`) |
| `GROCY_API_KEY` | – | API key, sent as the `GROCY-API-KEY` header |
| `GROCY_DEFAULT_LOCATION_ID` | *(empty)* | Location for new stock. If empty, the product's default location is used |
| `GROCY_DEFAULT_BEST_BEFORE_DAYS` | `7` | Fallback expiry offset when the product has no default due days |
| `GROCY_DEFAULT_QUANTITY_UNIT_ID` | *(empty)* | Quantity unit for products created at the kiosk. If empty, the first unit is used |
| `CATEGORY_DELIMITER` | `/` | Splits product group names into Major → Subcategory |
| `SEED_DEFAULT_CATEGORIES` | `true` | Create the suggested category tree in Grocy at startup (idempotent) |
| `PRINTER_MODEL` | `QL-800` | Any model `brother_ql` supports |
| `PRINTER_BACKEND` | `linux_kernel` | `linux_kernel` or `pyusb` (`network` also works) |
| `PRINTER_IDENTIFIER` | `file:///dev/usb/lp0` | Use `usb://0x04f9:0x209b` with the `pyusb` backend |
| `PRINTER_LABEL_SIZE` | `62` | Continuous roll width in mm (DK-22205 = `62`) |
| `PRINTER_AUTO_CUT` | `true` | Cut after each label |
| `PRINTER_RED` | `false` | Set `true` for two-colour Black/Red rolls (DK-22251) |
| `BARCODE_PREFIX` | `grcy:s:` | Labels encode `<prefix><stock_id>`, which native Grocy also understands |
| `CONSUME_COUNTDOWN_SECONDS` | `5` | Undo window before an item is consumed (1–60) |
| `UI_IDLE_RETURN_SECONDS` | `300` | Return to the CONSUME screen after this much inactivity (0 disables) |
| `APP_PORT` | `8080` | HTTP port |

Labels printed by Grocy itself (`grcy:s:<stock_id>`, `grcy:p:<id>`,
`grcy:p:<id>:<stock_id>`) are always recognised, even with a custom prefix.

### 2. Printer (Brother QL-800)

* Load a **62 mm continuous** roll (DK-22205).
* Turn **Editor Lite off**. The green LED must be off, otherwise the printer acts as a
  USB drive and ignores raster data.
* On the host, `ls /dev/usb/lp0` should show the device. `lsusb` shows `04f9:209b`.
* `docker-compose.yml` mounts the whole `/dev` directory and adds cgroup rules, so a
  printer that is power-cycled or re-plugged keeps working – even when the kernel
  renumbers it (e.g. `/dev/usb/lp0` → `/dev/usb/lp1`). The kiosk automatically
  switches to the new device node when the configured one disappears. Mounting only
  `/dev/usb` (or a single node via `devices:`) does **not** survive a power cycle:
  the kernel deletes and recreates `/dev/usb`, leaving the container with a stale
  mount until it is restarted.

### 3. Scanner (Zebra DS2278)
Use the default **USB HID keyboard** mode with an **Enter** suffix (factory default).
Code 128 must be enabled (it is by default). No drivers are needed.

### 4. Run

```bash
docker compose up -d --build
# open http://<host>:8080 on the kiosk tablet
```

To run the kiosk fullscreen without an address bar, open it in Chrome or Edge and
choose **Install app / Add to Home screen**. `manifest.json` uses `display: fullscreen`,
and the service worker caches the app shell. Kiosk browsers such as Fully Kiosk Browser
also work.

Check the setup:

```bash
curl http://localhost:8080/api/health        # Grocy + printer status
curl -X POST http://localhost:8080/api/print/test   # prints a test label
```

You can also tap the **● Grocy ● Printer** indicator in the top bar to see status
and print a test label.

## API (served by FastAPI, docs at `/docs`)

| Method & path | Purpose |
|---------------|---------|
| `GET /api/health` | Grocy reachability/version + printer USB status |
| `POST /api/print/test` | Print a test label |
| `GET /api/config` | Client settings (countdown, prefix, …) |
| `GET /api/catalog` | Category tree, products, product groups, locations |
| `GET /api/inventory` | Current stock (per product) with categories |
| `GET /api/lookup?code=` | Resolve a scan (stock label, raw stock id, Grocy product code, OEM barcode) |
| `GET /api/products/{id}/stock` | Stock entries of a product |
| `POST /api/products` | Create a product (+ optional barcode link, category, location) |
| `POST /api/products/{id}/category` | Change a product's category (product group) |
| `POST /api/products/{id}/open-behavior` | Set where one opened unit moves to (`location_id`, `null` = stays in place) |
| `POST /api/categories` | Create a product group (category) |
| `POST /api/locations` | Create a location |
| `POST /api/consume` | Start a pending (undo-able) consumption for a scanned code |
| `POST /api/consume/{id}/commit` | Commit now (countdown reached 0) |
| `POST /api/consume/{id}/cancel` | Undo (409 if already removed) |
| `POST /api/stock/add` | Add items and print labels (`label_mode`: `individual`/`box`, `print_label: false` for OEM-barcode mode) |
| `POST /api/stock/{stock_id}/move` | Move a stock entry (all of it) to another location |
| `POST /api/stock/{stock_id}/open` | Open one unit; moves it per the product's open behavior |
| `POST /api/print/stock/{stock_id}` | Reprint a label |

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest                         # Grocy is mocked; no printer needed
cp .env.example .env && python -m app   # serves on APP_PORT
```

Project layout:

```
app/
  config.py      settings (pydantic-settings, .env)
  grocy.py       async Grocy REST client
  service.py     kiosk logic (lookups, add, move, create product)
  consume.py     pending consumption / undo manager
  categories.py  Major/Subcategory parsing
  scanning.py    scan-code normalisation
  labels.py      label + Code 128 rendering (Pillow, python-barcode)
  printer.py     brother_ql raster printing + USB status
  main.py        FastAPI routes and PWA assets
  static/        index.html, app.js, style.css, manifest.json, service-worker.js, icons
tests/           pytest suite with an in-memory fake Grocy
```
