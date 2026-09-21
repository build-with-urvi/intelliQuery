"""
Generic order-taking module — handles ANY number of ordering-style tenants
(Domino's, and any future restaurant/shop). Separate from both the RAG
chatbot and the appointment booking system.

Pizzas get a customization sub-flow (toppings, then size) before landing in
the cart. Non-pizza items (sides, desserts, drinks) are added directly, same
as before.

Pizzas can be ordered with a quantity ("2 veggie paradise") in a single
fresh-order message. Each instance is tracked separately through the
toppings/size sub-flow, so per-instance customizations ("remove onion in
both and add paneer in one and pepperoni in second", "one small and one
large") apply to the right pizza and are checked out as separate cart
lines with their own pricing.

MENU DATA — this module used to hold the menu as hardcoded dicts
(MENUS_BY_TENANT / PIZZAS_BY_TENANT / TOPPINGS_BY_TENANT). Those have been
replaced with live reads from menu_store.py, which is backed by the
menu.db SQLite database that admin.py's web page edits. That means any
change made in the admin GUI (add/edit/delete a pizza, topping, or item)
is picked up immediately here — no code change or restart needed. See
menu_store.py for the DEFAULT_MENUS seed data and the get_pizzas() /
get_toppings() / get_items() read accessors used throughout this file.

LIMITATIONS (read before relying on this):
- Conversation state (_conversations) lives in memory only — resets if the
  server restarts mid-order.
- Item/topping matching and the free-text "add/remove" parsing are simple
  keyword/substring matching — not true natural-language understanding.
- Quantity parsing (_parse_quantity) just looks for the first digit or
  number-word (one, two, three, ...) anywhere in the message. It doesn't
  understand more complex phrasing.
- Per-instance targeting for toppings understands "both/all/each" and
  ordinal words ("first"/"1st"/"one", "second"/"2nd"/"two", etc. up to
  "fifth"). Per-instance targeting for sizes understands "both/all/each"
  and explicit ordinals ("first", "second", ... — NOT bare "one"/"two",
  since those are needed as quantity words there, e.g. "one small and one
  large"). More than 5 of the same pizza in one message isn't supported
  for individual targeting (only "both/all" still works).
- Menu content (pizzas, toppings, sides/desserts/drinks and their prices)
  now lives in the database via menu_store.py and is editable live from
  the admin GUI — it is NOT hardcoded here anymore. The DEFAULT_MENUS seed
  in menu_store.py is placeholder data (Domino's doesn't publish exact
  topping-by-topping pricing publicly), used only the very first time a
  tenant is accessed.
- Repeating a past order that included a customized pizza repeats the
  EXACT same configuration as-is. If you ADD a new pizza while MODIFYING
  a repeated order (not a fresh order), it's added at Medium size with
  default toppings only, and quantity words are ignored (a repeat-order
  modification is a single free-text message, not a multi-turn
  conversation) — the full interactive topping/size flow, including
  multi-instance quantity support, only runs for fresh orders.
- Generic category words ("pizza", "drinks", "sides", "desserts" — see
  GENERIC_CATEGORY_WORDS) are matched by a fixed keyword list, not true
  language understanding. A phrasing that isn't in that list (e.g. "got
  any pies?") won't be recognized as a category browse request and will
  fall through to the normal item-matching / "not on the menu" path.
- A handler in this file can return either a plain string, or a
  (text, buttons) tuple where `buttons` is a list of up to 3 (id, title)
  pairs — see the "WhatsApp clickable reply buttons" section below. Only
  main.py's WhatsApp-sending code needs to know about the tuple form; a
  button's title is always chosen to be a word the existing free-text
  parsing already accepts (e.g. "Yes", "Small", "Done"), so tapping a
  button and typing the same word behave identically.
"""

import os
import re
import json
import sqlite3
from datetime import datetime

import menu_store

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "orders.db")

DATETIME_FMT = "%Y-%m-%d %H:%M:%S"


# ---------------------------------------------------------------------------
# Menu lookup helpers — thin wrappers around menu_store's read accessors so
# the rest of this file doesn't need to know the DB is involved. Every call
# re-reads the tenant's current config, so admin GUI edits show up on the
# very next message with no caching/staleness to worry about.
# ---------------------------------------------------------------------------

def _pizzas(tenant: str) -> dict:
    return menu_store.get_pizzas(tenant)


def _toppings(tenant: str) -> dict:
    return menu_store.get_toppings(tenant)


def _items(tenant: str) -> dict:
    return menu_store.get_items(tenant)


CATEGORY_EMOJI = {
    "Sides": "🍟",
    "Desserts": "🍰",
    "Drinks": "🥤",
}


def format_menu(tenant: str) -> str:
    pizzas = _pizzas(tenant)
    other = _items(tenant)

    lines = ["🍕 *Domino's Menu* 🍕", ""]

    if pizzas:
        lines.append("*Pizzas*")
        for name, info in pizzas.items():
            sizes = info["sizes"]
            # Abbreviate Small/Medium/Large to S/M/L so each line stays
            # short and scannable on a phone screen instead of one long
            # wrapped line per pizza.
            size_str = " | ".join(f"{size[0]} Rs.{price}" for size, price in sizes.items())
            lines.append(f"🔸 *{name}* — {size_str}")
        lines.append("")

    for category, items in other.items():
        if not items:
            continue
        emoji = CATEGORY_EMOJI.get(category, "🍽️")
        lines.append(f"*{emoji} {category}*")
        for name, price in items.items():
            lines.append(f"• {name} — Rs.{price}")
        lines.append("")

    lines.append(
        "Just tell me what you'd like! Say a pizza name (e.g. \"farmhouse\" or "
        "\"2 veggie paradise\") and I'll ask about toppings & size, or say any "
        "side/dessert/drink name to add it straight away 🙂"
    )
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# Generic category requests — e.g. the customer just says "pizza" or
# "drinks" with no specific item name. Rather than either (a) dumping the
# ENTIRE menu (noisy, and not what they asked for) or (b) wrongly saying
# "that's not on the menu" (the bug this was added to fix — see the
# WhatsApp screenshot this was reported from: after adding a Coke, saying
# "pizza" mid-order returned a flat "not on the menu" reply instead of
# showing the pizza list), we detect the category word and show just that
# section of the menu.
#
# NOTE: this is a fixed keyword list, not language understanding — see the
# module docstring's LIMITATIONS note. It only catches the words listed in
# GENERIC_CATEGORY_WORDS.
# ---------------------------------------------------------------------------

GENERIC_CATEGORY_WORDS = {
    "pizza": "Pizzas", "pizzas": "Pizzas", "pie": "Pizzas", "pies": "Pizzas",
    "side": "Sides", "sides": "Sides", "starter": "Sides", "starters": "Sides",
    "dessert": "Desserts", "desserts": "Desserts", "sweet": "Desserts", "sweets": "Desserts",
    "drink": "Drinks", "drinks": "Drinks", "beverage": "Drinks", "beverages": "Drinks",
}


def _detect_generic_category(text: str):
    """Returns a category key ('Pizzas', 'Sides', 'Desserts', 'Drinks') if
    the message is a bare category word/near-synonym, else None. Deliberately
    narrow — see GENERIC_CATEGORY_WORDS."""
    words = set(re.findall(r"[a-zA-Z']+", text.lower()))
    for w in words:
        if w in GENERIC_CATEGORY_WORDS:
            return GENERIC_CATEGORY_WORDS[w]
    return None


# Bare "menu" / "order" (or any of the MENU_ONLY_TRIGGERS phrases) typed
# while ALREADY mid-order (STEP_COLLECTING). The state-is-None (first
# message) path already handles this via MENU_ONLY_TRIGGERS / ORDER_TRIGGERS
# / _looks_like_casual_order_intent, but none of those checks run once a
# conversation is underway — so previously a mid-order "menu"/"order" fell
# straight through to find_pizza/find_menu_item (no match, since no item is
# literally named "menu" or "order"), then to _generate_smart_unavailable_reply,
# wrongly reporting "that's not something we currently have on the menu."
MENU_REQUEST_WORDS = {"menu", "order"}


def _is_menu_request(text: str) -> bool:
    lowered = text.strip().lower()
    if any(t in lowered for t in MENU_ONLY_TRIGGERS):
        return True
    words = set(re.findall(r"[a-zA-Z']+", lowered))
    return bool(words & MENU_REQUEST_WORDS)


def format_category_list(tenant: str, category: str) -> str:
    """Render just one section of the menu (pizzas, or one of the
    _items() categories) — used when the customer names a category
    generically instead of a specific item."""
    if category == "Pizzas":
        pizzas = _pizzas(tenant)
        if not pizzas:
            return "Sorry, we don't have any pizzas available right now."
        lines = ["🍕 *Our Pizzas* 🍕", ""]
        for name, info in pizzas.items():
            sizes = info["sizes"]
            size_str = " | ".join(f"{size[0]} Rs.{price}" for size, price in sizes.items())
            lines.append(f"🔸 *{name}* — {size_str}")
        lines.append("")
        lines.append(
            "Which one would you like? Just say the name (e.g. \"farmhouse\" "
            "or \"2 veggie paradise\")."
        )
        return "\n".join(lines)

    items = _items(tenant).get(category, {})
    if not items:
        return f"Sorry, we don't have any {category.lower()} available right now."

    emoji = CATEGORY_EMOJI.get(category, "🍽️")
    lines = [f"*{emoji} {category}*", ""]
    for name, price in items.items():
        lines.append(f"• {name} — Rs.{price}")
    lines.append("")
    singular = category[:-1] if category.endswith("s") else category
    lines.append(f"Say the name of any {singular.lower()} to add it to your order.")
    return "\n".join(lines)


_STOPWORDS = {
    "do", "you", "have", "has", "a", "an", "the", "is", "are", "any", "some",
    "for", "of", "to", "please", "can", "i", "get", "order", "one", "got",
    "we", "want", "would", "like", "me",
}


def _tokenize(text: str):
    return [w for w in re.findall(r"[a-zA-Z']+", text.lower()) if len(w) >= 3 and w not in _STOPWORDS]


def _tokens_confidently_match(subject: str, name: str) -> bool:
    """True only when one of subject/name's significant word-sets fully
    contains the other — e.g. 'pasta' is a genuine match for 'Veg Pasta'
    (subset), but 'white sauce pasta' is NOT a confident match for
    'Veg Pasta' (they only share the single word 'pasta'; 'white' and
    'sauce' describe a different, unlisted variant). Used to distinguish
    "yes, we have exactly that" from "we don't have that specific thing,
    but here's something related" — see _detect_food_inquiry and the
    substitution-confirmation flow in handle_order_message."""
    subject_tokens = set(_tokenize(_apply_aliases(subject)))
    name_tokens = set(_tokenize(name))
    if not subject_tokens or not name_tokens:
        return False
    return subject_tokens <= name_tokens or name_tokens <= subject_tokens


COMMON_ITEM_ALIASES = {
    # Common abbreviations/alternate wording customers actually type,
    # normalized to how the item is spelled on the menu. This only helps
    # find a REAL menu item under a different name — it never invents
    # availability (e.g. "pepsi" is deliberately NOT mapped to
    # "coca-cola": it's a different product we don't carry, so that
    # should correctly fall through to the "not on the menu" reply).
    "coke": "Coca-Cola",
    "coca cola": "Coca-Cola",
    "cola": "Coca-Cola",
    "coldrink": "Coca-Cola",
    "cold drink": "Coca-Cola",
}


def _apply_aliases(text: str) -> str:
    lowered = text.lower()
    for alias, canonical in COMMON_ITEM_ALIASES.items():
        if alias in lowered:
            lowered = lowered.replace(alias, canonical.lower())
    return lowered


def _candidate_strings(text: str):
    """All normalized variants of `text` worth testing against the menu:
    raw lowercase text, text with a leading quantity word stripped, and
    each of those with common aliases/abbreviations substituted (e.g.
    'coke' -> 'coca-cola'). Deduplicated, empties dropped."""
    raw = text.lower().strip()
    stripped = _strip_leading_quantity(text).lower().strip()
    variants = {raw, stripped}
    variants |= {_apply_aliases(v) for v in list(variants)}
    return [v for v in variants if v]


def _matches(lowered_input: str, name: str) -> bool:
    name_lower = name.lower()
    if name_lower in lowered_input:
        return True
    if len(lowered_input) >= 3 and lowered_input in name_lower:
        return True
    # Word-level fuzzy match: any significant word from the item's name
    # appears as a whole word in the input. Handles natural phrasing like
    # "do you have pasta?" matching the menu item "Veg Pasta", without
    # needing the full name or an exact substring.
    name_tokens = set(_tokenize(name_lower))
    input_tokens = set(_tokenize(lowered_input))
    if name_tokens and (name_tokens & input_tokens):
        return True
    return False


def _strip_leading_quantity(text: str) -> str:
    """Remove a leading quantity token ('2', 'two', 'a', 'an', optionally
    followed by 'x') so that pizza/item-name matching is never thrown off
    by the quantity word sitting in front of the name (e.g. 'one
    margherita' -> 'margherita'). Falls back to the original text
    unchanged if there's no recognizable leading quantity token."""
    t = text.strip()
    m = re.match(r"^\s*(\d+)\s*[xX]?\s*(.+)$", t)
    if m and m.group(2).strip():
        return m.group(2).strip()
    parts = t.split(None, 1)
    if len(parts) == 2:
        first = parts[0].lower().strip(".,!")
        if first in NUMBER_WORDS or first in ("a", "an"):
            return parts[1].strip()
    return t


def find_pizza(tenant: str, text: str):
    candidates = _candidate_strings(text)
    if not candidates:
        return None
    for name in _pizzas(tenant):
        for candidate in candidates:
            if _matches(candidate, name):
                return name
    return None


def find_menu_item(tenant: str, text: str):
    candidates = _candidate_strings(text)
    if not candidates:
        return None
    for items in _items(tenant).values():
        for name, price in items.items():
            for candidate in candidates:
                if _matches(candidate, name):
                    return name, price
    return None


def find_topping(tenant: str, text: str):
    candidates = _candidate_strings(text)
    if not candidates:
        return None
    for name, price in _toppings(tenant).items():
        for candidate in candidates:
            if _matches(candidate, name):
                return name, price
    return None


# ---------------------------------------------------------------------------
# "Smart" reply for when a message is clearly asking about a specific food
# item's availability but nothing on the menu matches it — e.g. "do you
# have pasta?" (should have matched but for some phrasing reason didn't)
# or "got pepsi?" (genuinely not on the menu). orders.py's job here is
# just to know WHAT to ground the reply in (the tenant's real, current
# menu, read live from menu_store) and WHEN to ask for it — the actual
# LLM call and its system prompt live in rag.py, alongside every other
# tenant prompt, so this file stays pure ordering logic. Falls back to a
# plain canned message if that call fails for any reason (missing API
# key, network issue, rag.py's ML dependencies not installed, etc.) —
# ordering must never break because of this.
# ---------------------------------------------------------------------------

def _fallback_unavailable_reply(tenant: str) -> str:
    return (
        "Sorry, that's not something we currently have on the menu. "
        "Say 'menu' if you'd like to see everything we offer."
    )


def _build_menu_text(tenant: str) -> str:
    pizzas = _pizzas(tenant)
    items = _items(tenant)

    lines = ["Pizzas: " + ", ".join(pizzas.keys())] if pizzas else []
    for category, cat_items in items.items():
        if cat_items:
            lines.append(f"{category}: " + ", ".join(cat_items.keys()))
    return "\n".join(lines) if lines else "(menu is currently empty)"


def _generate_smart_unavailable_reply(tenant: str, user_text: str) -> str:
    try:
        import rag
        menu_text = _build_menu_text(tenant)
        reply = rag.generate_menu_unavailable_reply(user_text, menu_text)
        return reply if reply else _fallback_unavailable_reply(tenant)
    except Exception:
        return _fallback_unavailable_reply(tenant)


# ---------------------------------------------------------------------------
# Detecting "do you have X?" / "is X available?" style questions so they
# can be answered directly (with the real price if we DO have it, or the
# smart grounded reply above if we don't) instead of either (a) silently
# falling through to the general RAG chatbot, which has no idea what's
# actually on today's menu, or (b) worse — being misread as a plain order
# statement and jumping straight into building an order.
#
# Two-tier detection, on purpose:
#   - FOOD_INQUIRY_PREFIXES: a strict, unambiguous set of "do you have/
#     sell/serve ___" openers. This is the ONLY trigger allowed to fire
#     the LLM-grounded "sorry, not on the menu, but here's what we have"
#     reply when NOTHING on the menu matches at all — kept narrow so an
#     unrelated question ("are you open right now?") never gets told
#     "that's not on our menu".
#   - _looks_like_inquiry: a broader signal (question mark, a leading
#     question word, or words like "available"/"have"/"any"/"sell"
#     anywhere in the message) used only to recognize that the message IS
#     a question at all — so that if it also happens to name a real menu
#     item (in any phrasing — "does it have veggie paradise pizza
#     available", "is veggie paradise a thing you do", etc.), we answer
#     informationally instead of falling through to plain order-starting
#     logic, which is exactly the bug this broader check fixes. A denylist
#     of policy-topic words still protects against misclassifying a
#     genuine FAQ question in either tier.
# ---------------------------------------------------------------------------

FOOD_INQUIRY_PREFIXES = (
    "do you have ", "do u have ", "does it have ", "do you sell ",
    "do you serve ", "you have ", "you got ", "got any ", "got ",
)

FOOD_INQUIRY_INDICATOR_WORDS = {
    "have", "having", "has", "available", "avail", "any", "got",
    "sell", "sells", "selling", "serve", "serves", "serving",
    "stock", "stocked",
}

QUESTION_STARTER_WORDS = (
    "is ", "are ", "does ", "do ", "can ", "what ", "how ",
    "will ", "should ", "why ", "did ",
)

NON_FOOD_INQUIRY_WORDS = {
    "delivery", "refund", "refunds", "policy", "cancellation",
    "cancellations", "payment", "payments", "offer", "offers", "discount",
    "discounts", "coupon", "coupons", "tracking", "status", "custom",
    "customer", "care", "allergen", "allergens", "gluten",
}


def _looks_like_inquiry(user_text: str) -> bool:
    """Broad 'is this a question' signal — used only to keep a clearly
    interrogative message (in any common phrasing) from being mistaken
    for a plain order statement. NOT sufficient on its own to trigger the
    'sorry, not on the menu' LLM reply when nothing matches — see
    FOOD_INQUIRY_PREFIXES for that stricter gate."""
    lowered = user_text.strip().lower()
    if "?" in user_text:
        return True
    if any(lowered.startswith(qw) for qw in QUESTION_STARTER_WORDS):
        return True
    words = set(re.findall(r"[a-zA-Z']+", lowered))
    return bool(words & FOOD_INQUIRY_INDICATOR_WORDS)


def _detect_food_inquiry(tenant: str, user_text: str):
    """Returns (handled, reply). handled=False means: not recognized as
    a menu-item question — caller should proceed with the normal
    order-taking flow (which may eventually fall through to RAG)."""
    lowered = user_text.strip().lower().rstrip("?").strip()

    strict_subject = None
    for prefix in FOOD_INQUIRY_PREFIXES:
        if lowered.startswith(prefix):
            strict_subject = lowered[len(prefix):].strip()
            break

    if strict_subject is None and not _looks_like_inquiry(user_text):
        return False, None

    words = set(re.findall(r"[a-zA-Z']+", lowered))
    if words & NON_FOOD_INQUIRY_WORDS:
        return False, None

    subject = (strict_subject if strict_subject is not None else lowered).replace(" available", "").strip()
    if not subject:
        return False, None

    pizza_match = find_pizza(tenant, subject)
    if pizza_match:
        sizes = _pizzas(tenant)[pizza_match]["sizes"]
        price_str = " / ".join(f"{s}: Rs.{p}" for s, p in sizes.items())
        if _tokens_confidently_match(subject, pizza_match):
            return True, (
                f"Yes, we have {pizza_match}! ({price_str}) "
                f"Just say the name to add it to your order."
            )
        # Loose match only (e.g. shares one word with a longer, more
        # specific query) — don't falsely confirm; offer the real item.
        return True, (
            f"We don't have that exact pizza, but we do have {pizza_match} "
            f"({price_str}) — would you like that instead?"
        )

    item_match = find_menu_item(tenant, subject)
    if item_match:
        name, price = item_match
        if _tokens_confidently_match(subject, name):
            return True, (
                f"Yes, we have {name} for Rs.{price}! "
                f"Just say the name to add it to your order."
            )
        return True, (
            f"We don't have that exact item, but we do have {name} for Rs.{price} "
            f"— would you like that instead?"
        )

    # Nothing on the menu matched at all. Only fire the LLM-grounded
    # "sorry, not on the menu, but here's what we have" reply for the
    # STRICT trigger set — the broader inquiry signal alone (e.g. "are you
    # open right now?") isn't reliable evidence this was about a specific
    # food item, and we don't want to wrongly tell someone an unrelated
    # question "isn't on our menu". For a broad-only match with nothing
    # found, just let it fall through to the normal flow (-> RAG).
    if strict_subject is not None:
        return True, _generate_smart_unavailable_reply(tenant, user_text)

    return False, None


def _parse_size(text: str):
    t = text.strip().lower()
    if t in ("s", "small"):
        return "Small"
    if t in ("m", "medium"):
        return "Medium"
    if t in ("l", "large"):
        return "Large"
    if "small" in t:
        return "Small"
    if "medium" in t:
        return "Medium"
    if "large" in t:
        return "Large"
    return None


# ---------------------------------------------------------------------------
# Quantity / multi-instance parsing helpers
# ---------------------------------------------------------------------------

NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}

# Used when figuring out which pizza INSTANCE a topping instruction targets.
# "one"/"two"/... are safe to treat as ordinals here since there's no
# competing "quantity" meaning inside a topping-change sentence.
TOPPING_ORDINAL_MAP = {
    "first": 0, "1st": 0, "one": 0,
    "second": 1, "2nd": 1, "two": 1,
    "third": 2, "3rd": 2, "three": 2,
    "fourth": 3, "4th": 3, "four": 3,
    "fifth": 4, "5th": 4, "five": 4,
}

# Used for size assignment. Deliberately excludes bare "one"/"two"/... —
# those are needed as QUANTITY words in phrases like "one small and one
# large", so only unambiguous ordinals count as explicit instance targets
# here.
SIZE_ORDINAL_MAP = {
    "first": 0, "1st": 0,
    "second": 1, "2nd": 1,
    "third": 2, "3rd": 2,
    "fourth": 3, "4th": 3,
    "fifth": 4, "5th": 4,
}

INSTANCE_KEYWORDS_ALL = ("both", "all", "each", "every")


def _parse_quantity(text: str) -> int:
    """Find a quantity (digit or number-word) anywhere in the text. Defaults to 1."""
    t = text.strip().lower()
    m = re.search(r"\b(\d+)\b", t)
    if m:
        return max(1, int(m.group(1)))
    for word, val in NUMBER_WORDS.items():
        if re.search(rf"\b{word}\b", t):
            return val
    return 1


def _extract_instance_targets(clause_lower: str, count: int, ordinal_map: dict):
    """
    Figure out which pizza instance(s) (0-based indices) a clause refers to.
    Returns a list of indices, or None if the clause doesn't specify (caller
    decides the default — usually "apply to all").
    """
    if count <= 1:
        return [0]
    for kw in INSTANCE_KEYWORDS_ALL:
        if re.search(rf"\b{re.escape(kw)}\b", clause_lower):
            return list(range(count))
    for word, idx in ordinal_map.items():
        if idx < count and re.search(rf"\b{re.escape(word)}\b", clause_lower):
            return [idx]
    return None


def get_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant TEXT NOT NULL,
            phone_number TEXT NOT NULL,
            items TEXT NOT NULL,
            total_price REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'placed',
            created_at TEXT NOT NULL
        )
        """
    )
    # Migration: add delivery-detail columns for DBs created before this
    # feature existed. SQLite has no "ADD COLUMN IF NOT EXISTS", so we
    # just attempt each ALTER and ignore the "duplicate column" error if
    # it's already there — safe to run on every connection.
    for column_def in ("customer_name TEXT", "contact_number TEXT", "delivery_address TEXT"):
        try:
            conn.execute(f"ALTER TABLE orders ADD COLUMN {column_def}")
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.commit()
    return conn


def save_order(
    tenant: str,
    phone_number: str,
    cart: dict,
    customer_name: str = None,
    contact_number: str = None,
    delivery_address: str = None,
):
    items_list = [
        {"name": label, "price": info["price"], "qty": info["qty"]}
        for label, info in cart.items()
    ]
    total = sum(i["price"] * i["qty"] for i in items_list)

    conn = get_connection()
    conn.execute(
        "INSERT INTO orders (tenant, phone_number, items, total_price, status, "
        "customer_name, contact_number, delivery_address, created_at) "
        "VALUES (?, ?, ?, ?, 'placed', ?, ?, ?, ?)",
        (
            tenant, phone_number, json.dumps(items_list), total,
            customer_name, contact_number, delivery_address,
            datetime.now().strftime(DATETIME_FMT),
        ),
    )
    conn.commit()
    conn.close()
    return items_list, total


def get_last_order(tenant: str, phone_number: str):
    conn = get_connection()
    row = conn.execute(
        "SELECT items, total_price FROM orders WHERE tenant = ? AND phone_number = ? "
        "ORDER BY created_at DESC LIMIT 1",
        (tenant, phone_number),
    ).fetchone()
    conn.close()
    if row is None:
        return None
    items = json.loads(row[0])
    return {"items": items, "total_price": row[1]}


def _format_order_summary(items_list, total) -> str:
    lines = []
    for item in items_list:
        qty_part = f" x{item['qty']}" if item["qty"] > 1 else ""
        lines.append(f"  - {item['name']}{qty_part}: Rs.{item['price'] * item['qty']}")
    lines.append(f"\nTotal: Rs.{total}")
    return "\n".join(lines)


def _add_to_cart(cart: dict, label: str, price):
    if label in cart:
        cart[label]["qty"] += 1
    else:
        cart[label] = {"price": price, "qty": 1}


def _current_cart_summary(cart: dict) -> str:
    """Human-readable running summary of everything added so far, shown
    after every add so the customer always sees the full order-in-progress
    rather than just the item that was just added."""
    if not cart:
        return "Your order is currently empty."
    lines = ["Here's your order so far:"]
    total = 0
    for label, info in cart.items():
        price = info["price"]
        qty = info["qty"]
        line_total = price * qty
        total += line_total
        qty_part = f" x{qty}" if qty > 1 else ""
        lines.append(f"  - {label}{qty_part}: Rs.{line_total}")
    lines.append(f"\nRunning total: Rs.{total}")
    return "\n".join(lines)


ANYTHING_ELSE_PROMPT = (
    "Anything else? Say the name of another item to add it, "
    "or say 'no' / 'done' / 'that's all' when you're finished."
)


# ---------------------------------------------------------------------------
# WhatsApp "clickable" reply buttons
#
# Any return statement below can be either a plain string (sent as normal
# text, unchanged from before) or a (text, buttons) tuple, where `buttons`
# is a list of up to 3 (id, title) pairs. main.py's send_whatsapp_reply()
# renders the tuple form as tappable WhatsApp reply buttons; the plain
# string form is unaffected.
#
# Every button title below is chosen to be exactly the word the existing
# free-text parsing already accepts ("Yes"/"No" for _is_affirmative /
# _is_negative, "Small"/"Medium"/"Large" for _parse_size, "Done" for
# _is_done_adding, "No changes" for _is_no_changes) — so tapping a button
# and typing the same word by hand produce identical behaviour. That also
# means a customer who prefers to type never loses anything; the buttons
# are purely a tappable shortcut on top of the existing text flow.
#
# "add_more" and "done_order" (used on the "anything else?" prompt) and
# the two greeting buttons ("ask_query"/"place_order", sent by main.py
# itself before any order conversation has started) are the only ids
# main.py special-cases rather than piping straight through as text — see
# the comments in main.py's receive_message.
# ---------------------------------------------------------------------------

BUTTONS_YES_NO = [("confirm_yes", "Yes"), ("confirm_no", "No")]
BUTTONS_ADD_MORE_DONE = [("add_more", "Add More"), ("done_order", "Done")]
BUTTONS_SIZE = [("size_small", "Small"), ("size_medium", "Medium"), ("size_large", "Large")]
BUTTONS_NO_TOPPING_CHANGE = [("no_toppings", "No changes")]


def _with_buttons(text: str, buttons):
    return (text, buttons)


# "What's my order so far?" / "what have I ordered?" style questions —
# checked with HIGH priority (before menu/order-trigger/casual-intent
# checks) in both the first-message and mid-order paths, since these
# phrases contain the word "order" and would otherwise be misread as a
# request to start ordering or see the menu, rather than a request to see
# what's already in the cart.
CART_STATUS_TRIGGERS = [
    "what is my order", "what's my order", "whats my order",
    "what have i ordered", "what did i order", "my order so far",
    "order so far", "what's in my cart", "whats in my cart",
    "what is in my cart", "current order", "my cart",
    "show my order", "show my cart", "what have i added",
    "what did i add", "what's in my order", "whats in my order",
    "what is in my order", "what's my cart", "whats my cart",
]

NO_CART_YET_REPLY = (
    "You haven't added anything to your order yet. "
    "Say 'menu' to see what we offer, or just tell me what you'd like!"
)


def _is_cart_status_request(text: str) -> bool:
    lowered = text.strip().lower()
    return any(t in lowered for t in CART_STATUS_TRIGGERS)


MENU_ONLY_TRIGGERS = ["menu", "show me the menu", "what's on the menu", "see menu", "show menu"]
ORDER_TRIGGERS = [
    "place an order", "place my order", "want to place an order",
    "i want to order", "i'd like to order", "id like to order",
    "i would like to order", "would like to order", "wanna order",
    "want to order", "can i order", "may i order",
    "order a pizza", "order pizza", "order food", "start an order",
    "new order", "make an order", "order me",
]
REPEAT_TRIGGERS = [
    "same as last order", "repeat my last order", "same as before",
    "repeat last order", "same order as last time", "order the same as last time",
    "same as last time",
]

# Broader, best-effort signal that a message is casually expressing "I want
# to order/eat something" even when it doesn't match any of the specific
# ORDER_TRIGGERS phrases above — e.g. "i'm hungry", "give me a pizza",
# "lets order". Deliberately guarded (see _looks_like_casual_order_intent)
# so it never hijacks a genuine question (which should reach the RAG agent
# instead) or a message about order cancellation/refunds/tracking/etc.
# (which is a real topic the RAG agent already handles, not a request to
# start a NEW order).
ORDER_WORD_PATTERN = re.compile(r"\border\b")

HUNGER_FOOD_WORDS = {"hungry", "eat", "food", "snack", "meal", "bite", "pizza", "pizzas", "starving"}

ORDER_POLICY_DENYLIST_WORDS = NON_FOOD_INQUIRY_WORDS | {
    "cancel", "cancelled", "canceled", "complaint", "complaints",
    "issue", "wrong", "delayed", "late", "problem", "charge", "charged",
    "track", "history", "previous", "last", "deliver", "delivered",
    "delivering", "arrive", "arrived", "arriving", "eta", "when",
}

def _looks_like_casual_order_intent(lowered: str) -> bool:
    words = set(re.findall(r"[a-zA-Z']+", lowered))
    if words & ORDER_POLICY_DENYLIST_WORDS:
        return False
    if ORDER_WORD_PATTERN.search(lowered):
        return True
    if words & HUNGER_FOOD_WORDS:
        return True
    return False


# Exact-match phrases (checked first) plus a looser substring check below
# for natural free text like "no that's it" or "nope, done here" — see
# _is_done_adding. Kept long/specific phrases only in the substring pass
# so short ones like "no" can't accidentally match inside an unrelated word.
DONE_ADDING_PHRASES = (
    "no", "n", "nope", "nothing else", "that's all", "thats all",
    "that is all", "that's it", "thats it", "done", "no thanks",
    "no more", "i'm done", "im done", "finish", "finish order",
    "checkout", "place my order", "place the order", "complete order",
    "that's everything", "thats everything",
)
NO_CHANGES_PHRASES = ("none", "no changes", "no change", "keep as is", "as is", "n", "no")

STEP_COLLECTING = "collecting_items"
STEP_PIZZA_TOPPINGS = "awaiting_pizza_toppings"
STEP_PIZZA_SIZE = "awaiting_pizza_size"
STEP_REPEAT_CONFIRM = "awaiting_repeat_confirmation"
STEP_REPEAT_MODIFY = "awaiting_repeat_modification"
STEP_CONFIRM_SUBSTITUTE = "awaiting_substitute_confirmation"
STEP_AWAITING_NAME = "awaiting_customer_name"
STEP_AWAITING_CONTACT = "awaiting_contact_number"
STEP_AWAITING_ADDRESS = "awaiting_delivery_address"

_conversations = {}


def _is_affirmative(text: str) -> bool:
    return text.strip().lower() in ("yes", "y", "confirm", "ok", "okay", "sure", "yes please")


def _is_negative(text: str) -> bool:
    return text.strip().lower() in ("no", "n", "cancel", "change", "no thanks")


FILLER_PHRASES = ("thanks", "thank you", "thankyou", "ty", "ok", "okay", "cool", "great", "nice")


def _is_filler(text: str) -> bool:
    return text.strip().lower() in FILLER_PHRASES


def _is_done_adding(text: str) -> bool:
    lowered = text.strip().lower().strip(".!")
    if lowered in DONE_ADDING_PHRASES:
        return True
    # Looser check for natural phrasing like "no that's it" — only for
    # phrases long enough (>2 chars) that they can't accidentally match
    # inside an unrelated item name.
    return any(len(phrase) > 2 and phrase in lowered for phrase in DONE_ADDING_PHRASES)


def _is_no_changes(text: str) -> bool:
    return text.strip().lower() in NO_CHANGES_PHRASES


class PizzaInstance:
    """One physical pizza within an order — its own toppings, size, and price.
    Having this be a real object (instead of parallel index-matched lists)
    is what fixes the class of bug where one pizza's customization could
    bleed into or overwrite another's.

    Menu data (base toppings, per-size prices, topping charges) is looked
    up live from menu_store on each call rather than cached on the
    instance, so an admin edit made mid-conversation (e.g. changing a
    topping's price) is reflected immediately."""

    def __init__(self, tenant: str, pizza_name: str):
        self.tenant = tenant
        self.pizza_name = pizza_name
        self.toppings = list(_pizzas(tenant)[pizza_name]["base_toppings"])
        self.added_toppings = []  # toppings added beyond the base — these carry a price
        self.size = None

    def add_topping(self, name: str):
        if name not in self.toppings:
            self.toppings.append(name)
            self.added_toppings.append(name)

    def remove_topping(self, name: str):
        if name in self.toppings:
            self.toppings.remove(name)
        if name in self.added_toppings:
            # covers "add mushroom ... remove mushroom" in the same message —
            # it shouldn't still be charged for once it's been taken back off.
            self.added_toppings.remove(name)

    def set_size(self, size: str):
        self.size = size

    def price(self) -> int:
        base = _pizzas(self.tenant)[self.pizza_name]["sizes"][self.size]
        extra = sum(_toppings(self.tenant).get(t, 0) for t in self.added_toppings)
        return base + extra

    def label(self) -> str:
        return f"{self.pizza_name} ({self.size}, {', '.join(self.toppings)})"


class PendingPizzaOrder:
    """The group of PizzaInstance objects created from a single 'add pizza'
    message (e.g. '2 veggie paradise' -> two PizzaInstance objects). Owns
    the topping/size customization sub-flow for that group."""

    def __init__(self, tenant: str, pizza_name: str, quantity: int):
        self.tenant = tenant
        self.pizza_name = pizza_name
        self.instances = [PizzaInstance(tenant, pizza_name) for _ in range(quantity)]

    @property
    def count(self) -> int:
        return len(self.instances)

    def topping_prompt(self) -> str:
        # All instances start identical (base toppings), so instance 0 is
        # representative for the prompt text.
        return _topping_prompt(self.tenant, self.pizza_name, self.instances[0].toppings, self.count)

    def size_prompt(self) -> str:
        return _size_prompt(self.tenant, self.pizza_name, self.count)

    def apply_topping_changes(self, text: str):
        """Parse a free-text topping instruction and route each clause to
        the instance(s) it targets ('both', 'first'/'one', 'second'/'two', ...
        or unspecified -> applies to all)."""
        count = self.count
        pieces = text.replace(" and ", ",").split(",")
        for piece in pieces:
            piece = piece.strip()
            if not piece:
                continue
            lowered = piece.lower()
            is_removal = any(lowered.startswith(p) for p in ("remove ", "no ", "without "))

            targets = _extract_instance_targets(lowered, count, TOPPING_ORDINAL_MAP)
            if targets is None:
                targets = list(range(count))

            if is_removal:
                remainder = piece
                for prefix in ("remove ", "no ", "without "):
                    if lowered.startswith(prefix):
                        remainder = piece[len(prefix):]
                        break
                match = find_topping(self.tenant, remainder)
                if match:
                    name, _ = match
                    for idx in targets:
                        self.instances[idx].remove_topping(name)
            else:
                remainder = piece
                if lowered.startswith("add "):
                    remainder = piece[4:]
                match = find_topping(self.tenant, remainder)
                if match:
                    name, _ = match
                    for idx in targets:
                        self.instances[idx].add_topping(name)

    def apply_sizes(self, text: str) -> bool:
        """Parse a free-text size instruction ('both medium', 'one small and
        one large', 'first large, second medium', or a plain size when
        count == 1). Returns False if a size couldn't be determined for
        every instance (caller should re-prompt)."""
        sizes = _parse_sizes_multi(text, self.count)
        if sizes is None:
            return False
        for instance, size in zip(self.instances, sizes):
            instance.set_size(size)
        return True

    def checkout(self, cart: dict) -> list:
        """Add every instance to the cart as its own line and return the
        'label — Rs.price' strings for the confirmation message."""
        added_labels = []
        for instance in self.instances:
            label = instance.label()
            price = instance.price()
            _add_to_cart(cart, label, price)
            added_labels.append(f"{label} — Rs.{price}")
        return added_labels


def _topping_prompt(tenant: str, pizza_name: str, current_toppings: list, count: int = 1) -> str:
    available = _toppings(tenant)
    avail_lines = "\n".join(f"  - {n}: +Rs.{p}" for n, p in available.items())

    if count > 1:
        intro = (
            f"You ordered {count} {pizza_name} pizzas. Each one comes with: "
            f"{', '.join(current_toppings)}.\n\n"
            f"Would you like to add or remove any toppings? You can apply a change to "
            f"all of them (e.g. 'add mushroom to both') or to individual ones "
            f"(e.g. 'remove onion in both and add paneer in one and pepperoni in second')."
        )
    else:
        intro = (
            f"{pizza_name} comes with: {', '.join(current_toppings)}.\n\n"
            f"Would you like to add or remove any toppings?"
        )

    return (
        f"{intro} Available extra toppings:\n"
        f"{avail_lines}\n\n"
        f"Say what to add/remove (e.g. 'add mushroom, remove onion'), or 'none' to keep it as is."
    )


def _size_prompt(tenant: str, pizza_name: str, count: int = 1) -> str:
    sizes = _pizzas(tenant)[pizza_name]["sizes"]
    size_lines = " / ".join(f"{s}: Rs.{p}" for s, p in sizes.items())
    if count > 1:
        return (
            f"What size would you like for each of the {count} {pizza_name} pizzas? "
            f"({size_lines})\n\n"
            f"You can say the same size for all (e.g. 'both medium') or different sizes "
            f"per pizza (e.g. 'one small and one large')."
        )
    return f"What size would you like? ({size_lines})"


def _parse_sizes_multi(text: str, count: int):
    """
    Parse a size for each of `count` pizza instances from one free-text
    reply, e.g. "both medium", "one small and one large", or
    "first large, second medium". Returns a list of `count` size strings,
    or None if a size couldn't be determined for every instance.
    """
    if count <= 1:
        size = _parse_size(text)
        return [size] if size else None

    sizes = [None] * count
    clauses = [c.strip() for c in text.replace(" and ", ",").split(",") if c.strip()]

    implicit = []  # (size, quantity) clauses with no explicit single-instance target
    for clause in clauses:
        lowered = clause.lower()
        size = _parse_size(clause)
        if size is None:
            continue

        targets = _extract_instance_targets(lowered, count, SIZE_ORDINAL_MAP)
        if targets is not None and len(targets) == 1:
            sizes[targets[0]] = size
        elif targets == list(range(count)):
            for i in range(count):
                if sizes[i] is None:
                    sizes[i] = size
        else:
            qty = _parse_quantity(clause)
            implicit.append((size, qty))

    # Fill remaining empty slots in the order the implicit clauses appeared,
    # e.g. "one small and one large" -> instance 0 = Small, instance 1 = Large.
    for size, qty in implicit:
        assigned = 0
        for i in range(count):
            if assigned >= qty:
                break
            if sizes[i] is None:
                sizes[i] = size
                assigned += 1

    if all(s is not None for s in sizes):
        return sizes
    return None


def _apply_order_modifications(tenant: str, base_items_list, modification_text: str):
    cart = {item["name"]: {"price": item["price"], "qty": item["qty"]} for item in base_items_list}

    pieces = modification_text.replace(" and ", ",").split(",")
    for piece in pieces:
        piece = piece.strip()
        if not piece:
            continue

        lowered = piece.lower()
        is_removal = any(lowered.startswith(p) for p in ("remove ", "no ", "without "))

        if is_removal:
            remainder = piece
            for prefix in ("remove ", "no ", "without "):
                if lowered.startswith(prefix):
                    remainder = piece[len(prefix):]
                    break
            match_name = None
            pizza_match = find_pizza(tenant, remainder)
            item_match = find_menu_item(tenant, remainder)
            if pizza_match:
                match_name = pizza_match
            elif item_match:
                match_name = item_match[0]
            if match_name:
                for label in list(cart.keys()):
                    if match_name.lower() in label.lower():
                        del cart[label]
        else:
            remainder = piece
            if lowered.startswith("add "):
                remainder = piece[4:]

            pizza_match = find_pizza(tenant, remainder)
            if pizza_match:
                sizes = _pizzas(tenant)[pizza_match]["sizes"]
                price = sizes["Medium"]
                label = f"{pizza_match} (Medium, default toppings)"
                _add_to_cart(cart, label, price)
                continue

            item_match = find_menu_item(tenant, remainder)
            if item_match:
                name, price = item_match
                _add_to_cart(cart, name, price)

    return cart


def _begin_checkout(key, cart: dict) -> str:
    """Called once the customer is done adding items (fresh order, or a
    repeat order confirmed/modified) — starts the delivery-details
    sub-flow (name -> contact -> address) before the order is actually
    saved. The order isn't placed until STEP_AWAITING_ADDRESS completes."""
    _conversations[key] = {"step": STEP_AWAITING_NAME, "data": {"cart": cart}}
    return (
        f"{_current_cart_summary(cart)}\n\n"
        f"That's your order! To get it delivered, could you share your name?"
    )


def handle_order_message(tenant: str, phone_number: str, user_text: str):
    key = (tenant, phone_number)
    state = _conversations.get(key)

    lowered = user_text.lower().strip()

    if state is None:
        # Checked first — before REPEAT/ORDER/MENU triggers — since a
        # status question like "what is my order so far" contains the
        # word "order" and would otherwise be misread as one of those.
        # No conversation exists yet, so there's nothing in the cart.
        if _is_cart_status_request(user_text):
            return NO_CART_YET_REPLY

        if any(t in lowered for t in REPEAT_TRIGGERS):
            last_order = get_last_order(tenant, phone_number)
            if last_order is None:
                return "You don't have any previous orders with us yet. Say 'menu' to see what we offer, or 'order' to start a new one."

            summary = _format_order_summary(last_order["items"], last_order["total_price"])
            _conversations[key] = {
                "step": STEP_REPEAT_CONFIRM,
                "data": {"last_order_items": last_order["items"]},
            }
            return _with_buttons(
                f"Here's your last order:\n{summary}\n\n"
                f"Reply 'yes' to place the same order again, or 'no' if you'd like to change something.",
                BUTTONS_YES_NO,
            )

        if any(t in lowered for t in ORDER_TRIGGERS):
            _conversations[key] = {"step": STEP_COLLECTING, "data": {"cart": {}}}
            return format_menu(tenant)

        if any(t in lowered for t in MENU_ONLY_TRIGGERS):
            return format_menu(tenant)

        # "do you have X?" / "do you sell X?" etc. — answer directly from
        # the real menu (with price, if we have it) instead of silently
        # falling through to the general chatbot, which has no idea what's
        # actually on today's menu. Deliberately narrow trigger set — see
        # _detect_food_inquiry's docstring for why.
        handled, food_reply = _detect_food_inquiry(tenant, user_text)
        if handled:
            return food_reply

        # A bare category word ("pizza", "drinks", "desserts", "sides" —
        # see GENERIC_CATEGORY_WORDS) with no specific item name: show just
        # that section of the menu and start collecting, rather than
        # dumping the whole menu (too noisy) via the broader casual-intent
        # fallback below. Checked before that fallback so this narrower,
        # more useful response wins for these specific words.
        category = _detect_generic_category(user_text)
        if category:
            _conversations[key] = {"step": STEP_COLLECTING, "data": {"cart": {}}}
            return format_category_list(tenant, category)

        looks_like_a_question = "?" in user_text or any(
            lowered.startswith(qw) for qw in
            ("is ", "are ", "does ", "do ", "can ", "what ", "how ", "will ", "should ", "why ")
        )

        # Casual, non-exact-phrase expressions of wanting to order/eat
        # (e.g. "i'm hungry", "give me a pizza", "lets order", "can i get
        # a pizza"). Not blocked by the blanket question-guard below
        # (unlike pizza-name matching) because a policy/complaint-style
        # question ("can i get a refund", "why was my order delayed")
        # is already excluded by its own denylist check — see
        # _looks_like_casual_order_intent.
        if _looks_like_casual_order_intent(lowered):
            _conversations[key] = {"step": STEP_COLLECTING, "data": {"cart": {}}}
            return format_menu(tenant)

        if not looks_like_a_question:
            pizza_match = find_pizza(tenant, user_text)
            if pizza_match:
                quantity = _parse_quantity(user_text)
                if _tokens_confidently_match(user_text, pizza_match):
                    pending = PendingPizzaOrder(tenant, pizza_match, quantity)
                    _conversations[key] = {
                        "step": STEP_PIZZA_TOPPINGS,
                        "data": {"cart": {}, "pending_pizza": pending},
                    }
                    return _with_buttons(pending.topping_prompt(), BUTTONS_NO_TOPPING_CHANGE)
                # Loose match only — e.g. a query with extra descriptive
                # words the actual pizza name doesn't have. Confirm before
                # starting the customization flow for a possibly wrong item.
                _conversations[key] = {
                    "step": STEP_CONFIRM_SUBSTITUTE,
                    "data": {
                        "cart": {},
                        "substitute_candidate": {"kind": "pizza", "name": pizza_match, "quantity": quantity},
                    },
                }
                return _with_buttons(
                    f"We don't have that exact pizza, but we do have {pizza_match} — "
                    f"would you like that instead? (yes/no)",
                    BUTTONS_YES_NO,
                )

            item_match = find_menu_item(tenant, user_text)
            if item_match:
                name, price = item_match
                if _tokens_confidently_match(user_text, name):
                    cart = {}
                    _add_to_cart(cart, name, price)
                    _conversations[key] = {"step": STEP_COLLECTING, "data": {"cart": cart}}
                    return _with_buttons(
                        f"Added {name} (Rs.{price}).\n\n"
                        f"{_current_cart_summary(cart)}\n\n"
                        f"{ANYTHING_ELSE_PROMPT}",
                        BUTTONS_ADD_MORE_DONE,
                    )
                _conversations[key] = {
                    "step": STEP_CONFIRM_SUBSTITUTE,
                    "data": {
                        "cart": {},
                        "substitute_candidate": {"kind": "item", "name": name, "price": price},
                    },
                }
                return _with_buttons(
                    f"We don't have that exact item, but we do have {name} for Rs.{price} "
                    f"— would you like that instead? (yes/no)",
                    BUTTONS_YES_NO,
                )

        return None

    step = state["step"]
    data = state["data"]

    if step == STEP_COLLECTING:
        cart = data["cart"]

        # Checked before done-adding/filler/item-matching — same reasoning
        # as the state-is-None check above: "what's my order so far" would
        # otherwise be caught by _is_menu_request (it contains "order") and
        # wrongly re-show the full menu instead of the actual cart.
        if _is_cart_status_request(user_text):
            if not cart:
                return NO_CART_YET_REPLY
            return _current_cart_summary(cart)

        if _is_done_adding(user_text):
            if not cart:
                return "You haven't added anything yet. What would you like to order?"
            return _begin_checkout(key, cart)

        if _is_filler(user_text):
            return "You're welcome! Let me know what else you'd like to add, or say 'no' if that's everything."

        pizza_match = find_pizza(tenant, user_text)
        if pizza_match:
            quantity = _parse_quantity(user_text)
            if _tokens_confidently_match(user_text, pizza_match):
                pending = PendingPizzaOrder(tenant, pizza_match, quantity)
                data["pending_pizza"] = pending
                state["step"] = STEP_PIZZA_TOPPINGS
                return _with_buttons(pending.topping_prompt(), BUTTONS_NO_TOPPING_CHANGE)
            data["substitute_candidate"] = {"kind": "pizza", "name": pizza_match, "quantity": quantity}
            state["step"] = STEP_CONFIRM_SUBSTITUTE
            return _with_buttons(
                f"We don't have that exact pizza, but we do have {pizza_match} — "
                f"would you like that instead? (yes/no)",
                BUTTONS_YES_NO,
            )

        match = find_menu_item(tenant, user_text)
        if match is None:
            # Bare category word mid-order ("pizza", "drinks", "sides",
            # "desserts") — show just that section instead of wrongly
            # saying "not on the menu" (the bug this was added to fix).
            category = _detect_generic_category(user_text)
            if category:
                return format_category_list(tenant, category)

            if _is_menu_request(user_text):
                cart_note = f"{_current_cart_summary(cart)}\n\n" if cart else ""
                return f"{cart_note}{format_menu(tenant)}"

            return _generate_smart_unavailable_reply(tenant, user_text)

        name, price = match
        if _tokens_confidently_match(user_text, name):
            _add_to_cart(cart, name, price)
            return _with_buttons(
                f"Added {name} (Rs.{price}).\n\n"
                f"{_current_cart_summary(cart)}\n\n"
                f"{ANYTHING_ELSE_PROMPT}",
                BUTTONS_ADD_MORE_DONE,
            )

        data["substitute_candidate"] = {"kind": "item", "name": name, "price": price}
        state["step"] = STEP_CONFIRM_SUBSTITUTE
        return _with_buttons(
            f"We don't have that exact item, but we do have {name} for Rs.{price} "
            f"— would you like that instead? (yes/no)",
            BUTTONS_YES_NO,
        )

    if step == STEP_CONFIRM_SUBSTITUTE:
        candidate = data.pop("substitute_candidate", None)
        cart = data["cart"]
        state["step"] = STEP_COLLECTING

        if candidate and _is_affirmative(user_text):
            if candidate["kind"] == "pizza":
                pending = PendingPizzaOrder(tenant, candidate["name"], candidate.get("quantity", 1))
                data["pending_pizza"] = pending
                state["step"] = STEP_PIZZA_TOPPINGS
                return _with_buttons(pending.topping_prompt(), BUTTONS_NO_TOPPING_CHANGE)

            _add_to_cart(cart, candidate["name"], candidate["price"])
            return _with_buttons(
                f"Added {candidate['name']} (Rs.{candidate['price']}).\n\n"
                f"{_current_cart_summary(cart)}\n\n"
                f"{ANYTHING_ELSE_PROMPT}",
                BUTTONS_ADD_MORE_DONE,
            )

        if not cart:
            return "No problem — what would you like instead? Say 'menu' to see everything we offer."
        return f"No problem — what would you like instead?\n\n{_current_cart_summary(cart)}"

    if step == STEP_PIZZA_TOPPINGS:
        pending = data["pending_pizza"]

        if not _is_no_changes(user_text):
            pending.apply_topping_changes(user_text)
        # if _is_no_changes: nothing to do, each PizzaInstance already
        # starts at its default base toppings.

        state["step"] = STEP_PIZZA_SIZE
        size_prompt = pending.size_prompt()
        if pending.count == 1:
            # Multi-pizza sizing needs free text ("one small and one
            # large") — only offer the tap-to-pick shortcut for a single
            # pizza, where one button unambiguously sets its size.
            return _with_buttons(size_prompt, BUTTONS_SIZE)
        return size_prompt

    if step == STEP_PIZZA_SIZE:
        pending = data["pending_pizza"]

        if not pending.apply_sizes(user_text):
            if pending.count == 1:
                return "Sorry, I didn't catch that. Please choose Small, Medium, or Large."
            return (
                f"Sorry, I didn't catch the size for all {pending.count} {pending.pizza_name} pizzas. "
                f"Please specify a size for each — e.g. 'both medium' or 'one small and one large'."
            )

        cart = data["cart"]
        added_labels = pending.checkout(cart)
        count = pending.count
        data.pop("pending_pizza", None)
        state["step"] = STEP_COLLECTING

        if count == 1:
            added_summary = added_labels[0]
        else:
            added_summary = "\n" + "\n".join(f"  - {lbl}" for lbl in added_labels)

        return _with_buttons(
            f"Added {added_summary}.\n\n"
            f"{_current_cart_summary(cart)}\n\n"
            f"{ANYTHING_ELSE_PROMPT}",
            BUTTONS_ADD_MORE_DONE,
        )

    if step == STEP_REPEAT_CONFIRM:
        if _is_affirmative(user_text):
            cart = {
                item["name"]: {"price": item["price"], "qty": item["qty"]}
                for item in data["last_order_items"]
            }
            return _begin_checkout(key, cart)

        if _is_negative(user_text):
            state["step"] = STEP_REPEAT_MODIFY
            return "No problem — what would you like to add or remove from this order?"

        return "Please reply 'yes' to place the same order, or 'no' to change something."

    if step == STEP_REPEAT_MODIFY:
        cart = _apply_order_modifications(tenant, data["last_order_items"], user_text)
        if not cart:
            return "That would leave your order empty. What would you like to order instead?"
        return _begin_checkout(key, cart)

    if step == STEP_AWAITING_NAME:
        name = user_text.strip()
        if not name:
            return "Please share your name for the delivery."
        data["customer_name"] = name
        state["step"] = STEP_AWAITING_CONTACT
        return f"Thanks, {name}! What's the best contact number to reach you on?"

    if step == STEP_AWAITING_CONTACT:
        contact = user_text.strip()
        digits_only = re.sub(r"\D", "", contact)
        if len(digits_only) < 7:
            return "That doesn't look like a valid phone number — could you share a valid contact number?"
        data["contact_number"] = contact
        state["step"] = STEP_AWAITING_ADDRESS
        return "Got it! And what's the delivery address?"

    if step == STEP_AWAITING_ADDRESS:
        address = user_text.strip()
        if not address:
            return "Please share the delivery address."

        cart = data["cart"]
        items_list, total = save_order(
            tenant, phone_number, cart,
            customer_name=data.get("customer_name"),
            contact_number=data.get("contact_number"),
            delivery_address=address,
        )
        summary = _format_order_summary(items_list, total)
        del _conversations[key]
        return (
            f"Your order has been placed!\n{summary}\n\n"
            f"Delivering to: {data.get('customer_name')} ({data.get('contact_number')})\n"
            f"Address: {address}\n\n"
            f"Thanks for ordering with us!"
        )

    del _conversations[key]
    return None