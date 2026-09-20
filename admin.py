"""
Simple, password-protected admin webpage for editing an ordering tenant's
menu (pizzas, toppings, and other items). Visit /admin/<tenant> in a
browser, e.g. http://localhost:8000/admin/dominos — your browser will
prompt for the username/password set in .env.

Wired into main.py via: app.include_router(admin_router)
"""

import os
import secrets

from fastapi import APIRouter, Depends, HTTPException, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

import menu_store

router = APIRouter()
security = HTTPBasic()

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")


def verify_admin(credentials: HTTPBasicCredentials = Depends(security)):
    if not ADMIN_PASSWORD:
        raise HTTPException(status_code=500, detail="ADMIN_PASSWORD is not set in .env")

    correct_username = secrets.compare_digest(credentials.username, ADMIN_USERNAME)
    correct_password = secrets.compare_digest(credentials.password, ADMIN_PASSWORD)
    if not (correct_username and correct_password):
        raise HTTPException(
            status_code=401,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


def render_admin_page(tenant: str) -> str:
    config = menu_store.get_menu_config(tenant)
    pizzas = config.get("pizzas", {})
    toppings = config.get("toppings", {})
    items = config.get("items", {})

    pizza_rows = ""
    for name, info in pizzas.items():
        sizes = info.get("sizes", {})
        base_toppings = ", ".join(info.get("base_toppings", []))
        size_str = ", ".join(f"{s}: Rs.{p}" for s, p in sizes.items())
        pizza_rows += (
            f"<tr><td>{name}</td><td>{base_toppings}</td><td>{size_str}</td>"
            f"<td><form method='post' action='/admin/{tenant}/pizza/delete' style='display:inline'>"
            f"<input type='hidden' name='name' value='{name}'>"
            f"<button type='submit'>Delete</button></form></td></tr>"
        )

    topping_rows = ""
    for name, price in toppings.items():
        topping_rows += (
            f"<tr><td>{name}</td><td>Rs.{price}</td>"
            f"<td><form method='post' action='/admin/{tenant}/topping/delete' style='display:inline'>"
            f"<input type='hidden' name='name' value='{name}'>"
            f"<button type='submit'>Delete</button></form></td></tr>"
        )

    item_rows = ""
    for category, cat_items in items.items():
        for name, price in cat_items.items():
            item_rows += (
                f"<tr><td>{category}</td><td>{name}</td><td>Rs.{price}</td>"
                f"<td><form method='post' action='/admin/{tenant}/item/delete' style='display:inline'>"
                f"<input type='hidden' name='category' value='{category}'>"
                f"<input type='hidden' name='name' value='{name}'>"
                f"<button type='submit'>Delete</button></form></td></tr>"
            )

    return f"""
    <html>
    <head>
      <title>{tenant} Menu Admin</title>
      <style>
        body {{ font-family: sans-serif; max-width: 900px; margin: 30px auto; padding: 0 15px; }}
        table {{ border-collapse: collapse; width: 100%; margin-bottom: 15px; }}
        td, th {{ border: 1px solid #ccc; padding: 6px 10px; text-align: left; }}
        form.inline-form {{ margin-bottom: 30px; background: #f5f5f5; padding: 12px; border-radius: 6px; }}
        input {{ margin: 4px 4px 4px 0; padding: 4px; }}
        h2 {{ margin-top: 40px; }}
      </style>
    </head>
    <body>
      <h1>{tenant.title()} Menu Admin</h1>
      <p>Adding an item with a name that already exists updates it (acts as edit). Prices are in Rs.</p>

      <h2>Pizzas</h2>
      <table>
        <tr><th>Name</th><th>Base Toppings</th><th>Sizes</th><th></th></tr>
        {pizza_rows}
      </table>
      <form class="inline-form" method="post" action="/admin/{tenant}/pizza/add">
        <b>Add / Update Pizza</b><br>
        Name: <input name="name" required><br>
        Base toppings (comma-separated): <input name="base_toppings" style="width:350px"><br>
        Small price: <input name="small" type="number" required>
        Medium price: <input name="medium" type="number" required>
        Large price: <input name="large" type="number" required><br>
        <button type="submit">Save Pizza</button>
      </form>

      <h2>Toppings</h2>
      <table>
        <tr><th>Name</th><th>Extra Charge</th><th></th></tr>
        {topping_rows}
      </table>
      <form class="inline-form" method="post" action="/admin/{tenant}/topping/add">
        <b>Add / Update Topping</b><br>
        Name: <input name="name" required>
        Price: <input name="price" type="number" required><br>
        <button type="submit">Save Topping</button>
      </form>

      <h2>Other Menu Items (Sides / Desserts / Drinks)</h2>
      <table>
        <tr><th>Category</th><th>Name</th><th>Price</th><th></th></tr>
        {item_rows}
      </table>
      <form class="inline-form" method="post" action="/admin/{tenant}/item/add">
        <b>Add / Update Item</b><br>
        Category (e.g. Sides, Desserts, Drinks): <input name="category" required><br>
        Name: <input name="name" required>
        Price: <input name="price" type="number" required><br>
        <button type="submit">Save Item</button>
      </form>
    </body>
    </html>
    """


@router.get("/admin/{tenant}", response_class=HTMLResponse)
async def admin_page(tenant: str, username: str = Depends(verify_admin)):
    return render_admin_page(tenant)


@router.post("/admin/{tenant}/pizza/add")
async def add_pizza(
    tenant: str,
    name: str = Form(...),
    base_toppings: str = Form(""),
    small: int = Form(...),
    medium: int = Form(...),
    large: int = Form(...),
    username: str = Depends(verify_admin),
):
    toppings_list = [t.strip() for t in base_toppings.split(",") if t.strip()]
    menu_store.upsert_pizza(tenant, name, toppings_list, {"Small": small, "Medium": medium, "Large": large})
    return RedirectResponse(url=f"/admin/{tenant}", status_code=303)


@router.post("/admin/{tenant}/pizza/delete")
async def delete_pizza_route(tenant: str, name: str = Form(...), username: str = Depends(verify_admin)):
    menu_store.delete_pizza(tenant, name)
    return RedirectResponse(url=f"/admin/{tenant}", status_code=303)


@router.post("/admin/{tenant}/topping/add")
async def add_topping(tenant: str, name: str = Form(...), price: int = Form(...), username: str = Depends(verify_admin)):
    menu_store.upsert_topping(tenant, name, price)
    return RedirectResponse(url=f"/admin/{tenant}", status_code=303)


@router.post("/admin/{tenant}/topping/delete")
async def delete_topping_route(tenant: str, name: str = Form(...), username: str = Depends(verify_admin)):
    menu_store.delete_topping(tenant, name)
    return RedirectResponse(url=f"/admin/{tenant}", status_code=303)


@router.post("/admin/{tenant}/item/add")
async def add_item(
    tenant: str,
    category: str = Form(...),
    name: str = Form(...),
    price: int = Form(...),
    username: str = Depends(verify_admin),
):
    menu_store.upsert_item(tenant, category, name, price)
    return RedirectResponse(url=f"/admin/{tenant}", status_code=303)


@router.post("/admin/{tenant}/item/delete")
async def delete_item_route(
    tenant: str, category: str = Form(...), name: str = Form(...), username: str = Depends(verify_admin)
):
    menu_store.delete_item(tenant, category, name)
    return RedirectResponse(url=f"/admin/{tenant}", status_code=303)