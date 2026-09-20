"""
Database-backed menu storage — replaces the hardcoded MENUS_BY_TENANT /
PIZZAS_BY_TENANT / TOPPINGS_BY_TENANT dictionaries that used to live in
orders.py. Storing the menu here means it can be edited live (via
admin.py's web page) without touching code or restarting the server.

Each tenant's ENTIRE menu is stored as one JSON blob in a single table row
— simpler than fully normalized tables, and plenty for this scale.
"""

import os
import json
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "menu.db")

# Seed data used ONLY the first time a tenant's menu is accessed and no
# database row exists yet for it — after that, the database is the single
# source of truth, and this dict is never read again.
DEFAULT_MENUS = {
    "dominos": {
        "pizzas": {
            "Margherita": {
                "base_toppings": ["Cheese"],
                "sizes": {"Small": 199, "Medium": 349, "Large": 499},
            },
            "Farmhouse": {
                "base_toppings": ["Onion", "Capsicum", "Tomato", "Cheese"],
                "sizes": {"Small": 299, "Medium": 499, "Large": 699},
            },
            "Peppy Paneer": {
                "base_toppings": ["Paneer", "Capsicum", "Red Paprika", "Cheese"],
                "sizes": {"Small": 319, "Medium": 519, "Large": 719},
            },
            "Veggie Paradise": {
                "base_toppings": ["Onion", "Capsicum", "Tomato", "Black Olives", "Cheese"],
                "sizes": {"Small": 299, "Medium": 499, "Large": 699},
            },
            "Pepperoni": {
                "base_toppings": ["Pepperoni", "Cheese"],
                "sizes": {"Small": 349, "Medium": 549, "Large": 749},
            },
            "Chicken Dominator": {
                "base_toppings": ["Chicken Tikka", "Peri Peri Chicken", "Grilled Chicken", "Cheese"],
                "sizes": {"Small": 379, "Medium": 579, "Large": 779},
            },
        },
        "toppings": {
            "Extra Cheese": 40,
            "Onion": 20,
            "Capsicum": 20,
            "Tomato": 20,
            "Mushroom": 30,
            "Black Olives": 30,
            "Jalapeno": 20,
            "Paneer": 50,
            "Chicken Tikka": 60,
            "Pepperoni": 60,
            "Grilled Chicken": 60,
            "Peri Peri Chicken": 60,
            "Red Paprika": 20,
        },
        "items": {
            "Sides": {
                "Garlic Bread": 89,
                "Stuffed Garlic Bread": 129,
                "Chicken Wings": 199,
                "Veg Pasta": 149,
            },
            "Desserts": {
                "Choco Lava Cake": 99,
                "Brownie": 89,
                "Choco Sundae": 79,
            },
            "Drinks": {
                "Coca-Cola": 49,
                "Sprite": 49,
                "Fanta": 49,
            },
        },
    },
}


def get_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS menu_config (
            tenant TEXT PRIMARY KEY,
            config_json TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def get_menu_config(tenant: str) -> dict:
    conn = get_connection()
    row = conn.execute("SELECT config_json FROM menu_config WHERE tenant = ?", (tenant,)).fetchone()
    if row is None:
        default = DEFAULT_MENUS.get(tenant, {"pizzas": {}, "toppings": {}, "items": {}})
        conn.execute(
            "INSERT INTO menu_config (tenant, config_json) VALUES (?, ?)",
            (tenant, json.dumps(default)),
        )
        conn.commit()
        conn.close()
        return default
    conn.close()
    return json.loads(row[0])


def save_menu_config(tenant: str, config: dict):
    conn = get_connection()
    conn.execute(
        "INSERT INTO menu_config (tenant, config_json) VALUES (?, ?) "
        "ON CONFLICT(tenant) DO UPDATE SET config_json = excluded.config_json",
        (tenant, json.dumps(config)),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Read accessors — used by orders.py (replace the old hardcoded dicts)
# ---------------------------------------------------------------------------

def get_pizzas(tenant: str) -> dict:
    return get_menu_config(tenant).get("pizzas", {})


def get_toppings(tenant: str) -> dict:
    return get_menu_config(tenant).get("toppings", {})


def get_items(tenant: str) -> dict:
    return get_menu_config(tenant).get("items", {})


# ---------------------------------------------------------------------------
# Write accessors — used by admin.py's web forms
# ---------------------------------------------------------------------------

def upsert_pizza(tenant: str, name: str, base_toppings: list, sizes: dict):
    config = get_menu_config(tenant)
    config.setdefault("pizzas", {})[name] = {"base_toppings": base_toppings, "sizes": sizes}
    save_menu_config(tenant, config)


def delete_pizza(tenant: str, name: str):
    config = get_menu_config(tenant)
    config.get("pizzas", {}).pop(name, None)
    save_menu_config(tenant, config)


def upsert_topping(tenant: str, name: str, price):
    config = get_menu_config(tenant)
    config.setdefault("toppings", {})[name] = price
    save_menu_config(tenant, config)


def delete_topping(tenant: str, name: str):
    config = get_menu_config(tenant)
    config.get("toppings", {}).pop(name, None)
    save_menu_config(tenant, config)


def upsert_item(tenant: str, category: str, name: str, price):
    config = get_menu_config(tenant)
    config.setdefault("items", {}).setdefault(category, {})[name] = price
    save_menu_config(tenant, config)


def delete_item(tenant: str, category: str, name: str):
    config = get_menu_config(tenant)
    if category in config.get("items", {}):
        config["items"][category].pop(name, None)
    save_menu_config(tenant, config)