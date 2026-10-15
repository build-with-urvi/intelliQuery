"""
Simple, password-protected admin website for an ordering tenant. Visit
/admin/<tenant> in a browser, e.g. http://localhost:8000/admin/dominos —
your browser will prompt for the username/password set in .env.

Two pages per tenant:
  - Menu            /admin/<tenant>            pizzas, toppings, sides/desserts/drinks
  - Knowledge base  /admin/<tenant>/document   the text document the chatbot answers from

Menu edits are read live by orders.py (via menu_store.py). Document edits are
saved to disk and re-indexed into ChromaDB inside the running server (see
reindex.py), so the bot uses them without any restart or VM access.

Wired into main.py via: app.include_router(admin_router)
"""

import html
import os
import secrets
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

import menu_store
import reindex

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


def esc(value) -> str:
    """HTML-escape anything that came from the menu/database/user before
    putting it in a page (names like "Chef's Special" would otherwise break
    the forms)."""
    return html.escape(str(value), quote=True)


PAGE_CSS = """
  :root { color-scheme: light; }
  body { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; max-width: 900px;
         margin: 30px auto; padding: 0 15px; color: #1d2430; line-height: 1.5; }
  nav { display: flex; gap: 6px; margin-bottom: 22px; border-bottom: 1px solid #d5d9e0; }
  nav a { padding: 8px 14px; text-decoration: none; color: #3b4658; border-bottom: 3px solid transparent; }
  nav a[aria-current="page"] { color: #0b3d91; border-bottom-color: #0b3d91; font-weight: 600; }
  nav a:focus-visible, button:focus-visible, input:focus-visible,
  select:focus-visible, textarea:focus-visible, summary:focus-visible { outline: 3px solid #7aa7ff; outline-offset: 1px; }
  table { border-collapse: collapse; width: 100%; margin-bottom: 15px; }
  td, th { border: 1px solid #ccc; padding: 6px 10px; text-align: left; }
  form.inline-form { margin-bottom: 30px; background: #f5f5f5; padding: 12px; border-radius: 6px; }
  input { margin: 4px 4px 4px 0; padding: 4px; }
  h2 { margin-top: 40px; }
  .banner { padding: 10px 14px; border-radius: 6px; margin: 0 0 16px; border: 1px solid; }
  .banner.info, .status.running { background: #eef4ff; border-color: #9db8ee; color: #0b3d91; }
  .banner.ok, .status.done { background: #eaf7ee; border-color: #8fcaa1; color: #14532d; }
  .banner.warn, .status.error { background: #fff4e5; border-color: #f0b873; color: #7a3e00; }
  .status { padding: 10px 14px; border-radius: 6px; margin: 0 0 16px; border: 1px solid; }
  .doc-bar { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; margin-bottom: 10px; }
  textarea { width: 100%; box-sizing: border-box; min-height: 480px; padding: 12px; font: 15px/1.55 system-ui, sans-serif;
             border: 1px solid #b9c0cc; border-radius: 6px; resize: vertical; }
  button { padding: 8px 16px; border-radius: 6px; border: 1px solid #0b3d91; background: #0b3d91; color: #fff; cursor: pointer; font-size: 15px; }
  button.secondary { background: #fff; color: #0b3d91; }
  button:disabled { opacity: .5; cursor: not-allowed; }
  .hint { color: #4a5568; margin-top: 0; }
  .chunk { border-left: 3px solid #d5d9e0; padding: 2px 12px; margin: 14px 0; }
  .chunk pre { white-space: pre-wrap; margin: 4px 0 0; font: 14px/1.5 system-ui, sans-serif; }
  .chunk small { color: #4a5568; }
  details { margin-top: 28px; }
"""


def _nav(tenant: str, active: str) -> str:
    links = [("menu", f"/admin/{esc(tenant)}", "Menu")]
    if reindex.is_editable(tenant):
        links.append(("document", f"/admin/{esc(tenant)}/document", "Knowledge base"))
    parts = []
    for key, href, label in links:
        current = ' aria-current="page"' if key == active else ""
        parts.append('<a href="' + href + '"' + current + ">" + label + "</a>")
    return "<nav>" + "".join(parts) + "</nav>"


# ---------------------------------------------------------------------------
# Menu page (unchanged behaviour; values are now HTML-escaped)
# ---------------------------------------------------------------------------

def render_admin_page(tenant: str) -> str:
    config = menu_store.get_menu_config(tenant)
    pizzas = config.get("pizzas", {})
    toppings = config.get("toppings", {})
    items = config.get("items", {})
    t = esc(tenant)

    pizza_rows = ""
    for name, info in pizzas.items():
        sizes = info.get("sizes", {})
        base_toppings = ", ".join(info.get("base_toppings", []))
        size_str = ", ".join(f"{s}: Rs.{p}" for s, p in sizes.items())
        pizza_rows += (
            f"<tr><td>{esc(name)}</td><td>{esc(base_toppings)}</td><td>{esc(size_str)}</td>"
            f"<td><form method='post' action='/admin/{t}/pizza/delete' style='display:inline'>"
            f"<input type='hidden' name='name' value='{esc(name)}'>"
            f"<button type='submit' class='secondary'>Delete</button></form></td></tr>"
        )

    topping_rows = ""
    for name, price in toppings.items():
        topping_rows += (
            f"<tr><td>{esc(name)}</td><td>Rs.{esc(price)}</td>"
            f"<td><form method='post' action='/admin/{t}/topping/delete' style='display:inline'>"
            f"<input type='hidden' name='name' value='{esc(name)}'>"
            f"<button type='submit' class='secondary'>Delete</button></form></td></tr>"
        )

    item_rows = ""
    for category, cat_items in items.items():
        for name, price in cat_items.items():
            item_rows += (
                f"<tr><td>{esc(category)}</td><td>{esc(name)}</td><td>Rs.{esc(price)}</td>"
                f"<td><form method='post' action='/admin/{t}/item/delete' style='display:inline'>"
                f"<input type='hidden' name='category' value='{esc(category)}'>"
                f"<input type='hidden' name='name' value='{esc(name)}'>"
                f"<button type='submit' class='secondary'>Delete</button></form></td></tr>"
            )

    return f"""
    <html>
    <head>
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>{t} Menu Admin</title>
      <style>{PAGE_CSS}</style>
    </head>
    <body>
      {_nav(tenant, "menu")}
      <h1>{esc(tenant.title())} Menu Admin</h1>
      <p>Adding an item with a name that already exists updates it (acts as edit). Prices are in Rs. Menu changes apply to the bot immediately.</p>

      <h2>Pizzas</h2>
      <table>
        <tr><th>Name</th><th>Base Toppings</th><th>Sizes</th><th></th></tr>
        {pizza_rows}
      </table>
      <form class="inline-form" method="post" action="/admin/{t}/pizza/add">
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
      <form class="inline-form" method="post" action="/admin/{t}/topping/add">
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
      <form class="inline-form" method="post" action="/admin/{t}/item/add">
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


# ---------------------------------------------------------------------------
# Knowledge base page — edit the document the chatbot answers from
# ---------------------------------------------------------------------------

# notice key -> (banner style, message)
NOTICES = {
    "saved": ("ok", "Saved. The bot is being updated now. This takes about a minute."),
    "unchanged": ("info", "Nothing was changed, so there was nothing to save."),
    "busy": ("warn", "An update is already running. Wait for it to finish, then save again."),
    "saved_busy": ("warn", "Saved, but another update was still running. Click \"Rebuild bot knowledge\" once it finishes."),
    "empty": ("warn", "The document can't be empty. Nothing was saved."),
    "toolong": ("warn", f"That document is too long (the limit is {reindex.MAX_DOCUMENT_CHARS:,} characters). Nothing was saved."),
    "badname": ("warn", "File names can use letters, numbers, spaces, dots, dashes, underscores and brackets, and must end in .txt. Nothing was saved."),
    "rebuilding": ("info", "Rebuilding the bot's knowledge from the saved files. This takes about a minute."),
}


def _require_editable(tenant: str):
    if not reindex.is_editable(tenant):
        raise HTTPException(status_code=404, detail="Document editing is not enabled for this tenant.")


def _current_chunks(tenant: str):
    """What the bot currently has indexed, as (text, metadata) pairs, or None
    if it can't be read. Lets the admin confirm an update really landed."""
    try:
        import rag

        collection = rag.client.get_or_create_collection(rag.TENANT_CONFIGS[tenant]["collection_name"])
        data = collection.get(include=["documents", "metadatas"])
        pairs = list(zip(data["documents"], data["metadatas"]))

        def sort_key(pair):
            meta = pair[1] or {}
            try:
                number = int(meta.get("chunk_number", 0))
            except (TypeError, ValueError):
                number = 0
            return (str(meta.get("source", "")), number)

        return sorted(pairs, key=sort_key)
    except Exception:
        return None


def _doc_redirect(tenant: str, notice: str, filename: str = "") -> RedirectResponse:
    url = f"/admin/{tenant}/document?notice={quote(notice)}"
    if filename:
        url += f"&file={quote(filename)}"
    return RedirectResponse(url=url, status_code=303)


def render_document_page(tenant: str, selected_file: str, version_id: str, notice: str) -> str:
    t = esc(tenant)
    files = reindex.list_documents(tenant)
    if selected_file not in files:
        selected_file = files[0] if files else f"{tenant}.txt"

    banners = ""
    if notice in NOTICES:
        style, message = NOTICES[notice]
        banners += f'<p class="banner {style}" role="status">{esc(message)}</p>'

    content = reindex.read_document(tenant, selected_file) if selected_file in files else ""
    if version_id:
        loaded = reindex.read_backup(tenant, selected_file, version_id)
        if loaded:
            content, label = loaded
            banners += (
                f'<p class="banner info" role="status">Showing the version saved on {esc(label)}. '
                f"It isn't live yet. Click \"Save and update bot\" to restore it.</p>"
            )

    file_options = "".join(
        f'<option value="{esc(f)}"{" selected" if f == selected_file else ""}>{esc(f)}</option>' for f in files
    )
    file_picker = ""
    if len(files) > 1:
        file_picker = (
            '<form method="get" class="doc-bar">'
            '<label for="file">Document</label>'
            f'<select id="file" name="file" onchange="this.form.submit()">{file_options}</select>'
            "</form>"
        )

    backups = reindex.list_backups(tenant, selected_file)
    if backups:
        items = "".join(
            f'<li><a href="/admin/{t}/document?file={quote(selected_file)}&version={quote(b["id"])}">{esc(b["label"])}</a></li>'
            for b in backups
        )
        versions_html = (
            "<h2>Earlier versions</h2>"
            '<p class="hint">Every save keeps the version it replaced. Open one to load it into the editor, then save to restore it.</p>'
            f"<ul>{items}</ul>"
        )
    else:
        versions_html = ""

    chunks = _current_chunks(tenant)
    if chunks is None:
        chunks_html = ""
    else:
        blocks = "".join(
            f'<div class="chunk"><small>{esc((meta or {}).get("source", "unknown source"))}, '
            f'section {esc((meta or {}).get("chunk_number", "?"))}</small><pre>{esc(text)}</pre></div>'
            for text, meta in chunks
        )
        empty_note = '<p class="hint">Nothing is indexed yet.</p>'
        chunks_html = (
            f"<details><summary>What the bot currently knows ({len(chunks)} sections)</summary>"
            f"{blocks or empty_note}</details>"
        )

    return f"""
    <html>
    <head>
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>{t} Knowledge Base</title>
      <style>{PAGE_CSS}</style>
    </head>
    <body>
      {_nav(tenant, "document")}
      <h1>{esc(tenant.title())} Knowledge Base</h1>
      <p class="hint">This is the text the chatbot uses to answer questions about delivery, payments, refunds, offers and customer care. Menu items and prices are not here. Change those on the Menu page.</p>

      {banners}
      <div id="status" class="status" hidden></div>

      {file_picker}
      <form method="post" action="/admin/{t}/document/save">
        <div class="doc-bar">
          <label for="filename">File name</label>
          <input id="filename" name="filename" value="{esc(selected_file)}" size="28" required>
        </div>
        <textarea name="content" aria-label="Document text" spellcheck="true">
{esc(content)}</textarea>
        <p>
          <button type="submit" id="save-btn">Save and update bot</button>
        </p>
      </form>

      <form method="post" action="/admin/{t}/document/reindex">
        <p class="hint">Not seeing your changes? Rebuild the bot's knowledge from the files that are already saved.</p>
        <button type="submit" class="secondary" id="rebuild-btn">Rebuild bot knowledge</button>
      </form>

      {versions_html}
      {chunks_html}

      <script>
        const box = document.getElementById("status");
        const buttons = [document.getElementById("save-btn"), document.getElementById("rebuild-btn")];
        async function poll() {{
          try {{
            const r = await fetch("/admin/{t}/document/status", {{ credentials: "same-origin" }});
            const s = await r.json();
            box.hidden = !s.message;
            box.className = "status " + s.state;
            box.textContent = s.message || "";
            buttons.forEach(b => b.disabled = s.state === "running");
            setTimeout(poll, s.state === "running" ? 2000 : 15000);
          }} catch (e) {{
            setTimeout(poll, 5000);
          }}
        }}
        poll();
      </script>
    </body>
    </html>
    """


@router.get("/admin/{tenant}/document", response_class=HTMLResponse)
def document_page(
    tenant: str,
    file: str = "",
    version: str = "",
    notice: str = "",
    username: str = Depends(verify_admin),
):
    _require_editable(tenant)
    return render_document_page(tenant, file, version, notice)


@router.post("/admin/{tenant}/document/save")
def save_document_route(
    tenant: str,
    filename: str = Form(...),
    content: str = Form(""),
    username: str = Depends(verify_admin),
):
    _require_editable(tenant)

    name = reindex.clean_filename(filename)
    if name is None:
        return _doc_redirect(tenant, "badname")
    if not content.strip():
        return _doc_redirect(tenant, "empty", name)
    if len(content) > reindex.MAX_DOCUMENT_CHARS:
        return _doc_redirect(tenant, "toolong", name)
    if reindex.is_running(tenant):
        return _doc_redirect(tenant, "busy", name)

    if not reindex.save_document(tenant, name, content):
        return _doc_redirect(tenant, "unchanged", name)

    if not reindex.start_reindex(tenant):
        return _doc_redirect(tenant, "saved_busy", name)
    return _doc_redirect(tenant, "saved", name)


@router.post("/admin/{tenant}/document/reindex")
def rebuild_route(tenant: str, username: str = Depends(verify_admin)):
    _require_editable(tenant)
    if reindex.start_reindex(tenant):
        return _doc_redirect(tenant, "rebuilding")
    return _doc_redirect(tenant, "busy")


@router.get("/admin/{tenant}/document/status")
def document_status(tenant: str, username: str = Depends(verify_admin)):
    _require_editable(tenant)
    return JSONResponse(reindex.get_status(tenant))