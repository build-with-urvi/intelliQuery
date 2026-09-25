import os
import traceback
from datetime import datetime, timedelta

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from rag import generate_answer
from booking import (
    handle_booking_message,
    get_appointments_needing_reminder,
    mark_reminder_sent,
)
import orders
from orders import handle_order_message
from admin import router as admin_router

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(admin_router)


class ChatRequest(BaseModel):
    message: str
    tenant: str = "ggsipu"


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/chat")
def chat(request: ChatRequest):
    try:
        result = generate_answer(request.message, tenant=request.tenant)
        return result
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# WhatsApp multi-tenant webhook
# ---------------------------------------------------------------------------

VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN")

GGSIPU_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN")
GGSIPU_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID")

NSUT_ACCESS_TOKEN = os.getenv("NSUT_ACCESS_TOKEN")
NSUT_PHONE_NUMBER_ID = os.getenv("NSUT_PHONE_NUMBER_ID")

# NEW — fill these in .env once you've claimed Domino's test number and
# generated its access token in the Meta dashboard.
DOMINOS_ACCESS_TOKEN = os.getenv("DOMINOS_ACCESS_TOKEN")
DOMINOS_PHONE_NUMBER_ID = os.getenv("DOMINOS_PHONE_NUMBER_ID")

# Every tenant is RAG-capable (see rag.py's TENANT_CONFIGS). On top of that,
# some tenants ALSO have a domain-specific "flow" that gets first crack at
# each message — "booking" for the education tenants, "ordering" for
# Domino's. If that flow handler returns None (message wasn't related to
# it), we fall through to the RAG chatbot. A tenant with flow=None would be
# pure RAG with no special flow at all.
TENANT_BY_PHONE_ID = {
    GGSIPU_PHONE_NUMBER_ID: {
        "tenant": "ggsipu",
        "access_token": GGSIPU_ACCESS_TOKEN,
        "phone_number_id": GGSIPU_PHONE_NUMBER_ID,
        "flow": "booking",
    },
    NSUT_PHONE_NUMBER_ID: {
        "tenant": "nsut",
        "access_token": NSUT_ACCESS_TOKEN,
        "phone_number_id": NSUT_PHONE_NUMBER_ID,
        "flow": "booking",
    },
    DOMINOS_PHONE_NUMBER_ID: {
        "tenant": "dominos",
        "access_token": DOMINOS_ACCESS_TOKEN,
        "phone_number_id": DOMINOS_PHONE_NUMBER_ID,
        "flow": "ordering",
    },
}

TENANT_CREDENTIALS_BY_NAME = {
    cfg["tenant"]: cfg for cfg in TENANT_BY_PHONE_ID.values()
}


# ---------------------------------------------------------------------------
# WhatsApp "clickable" buttons — greeting / session state
#
# For ordering-flow tenants (Domino's), the first message of a fresh
# "session" is answered with two tappable buttons — "Ask a Query" and
# "Place Order" — instead of guessing what a plain first message meant.
#
# What counts as a fresh "session": NOT "has this contact ever completed
# a full order" (the previous version's rule) — that meant a contact who
# said "hey" once and never finished an order got permanently skipped for
# the greeting until the server itself restarted, since _greeted_users
# only ever grew and almost never shrank. Instead, a session is fresh
# whenever it's been more than SESSION_TIMEOUT since this contact's last
# message — an ordinary "it's been a while, so start over" rule, tracked
# via `_last_seen`.
#
#   - _last_seen: (tenant, phone_number) -> datetime of their last
#     message. Checked/updated on every inbound message for an ordering
#     tenant via _touch_and_check_fresh() below.
#   - _query_mode_users: contacts who tapped "Ask a Query" and haven't
#     started a fresh session since — their messages skip the order flow
#     and go straight to RAG. Cleared automatically once their session
#     goes stale, so nobody gets stuck in query mode forever either.
#
# Same in-memory caveat as orders._conversations: this resets if the
# server restarts mid-conversation.
# ---------------------------------------------------------------------------

SESSION_TIMEOUT = timedelta(minutes=30)

_last_seen = {}
_query_mode_users = set()

GREETING_TEXT = "Hi! 👋 What would you like to do?"
GREETING_BUTTONS = [("ask_query", "Ask a Query"), ("place_order", "Place Order")]


def _touch_and_check_fresh(key) -> bool:
    """Records `key`'s message as happening right now, and returns True
    if this counts as the start of a NEW session for them — i.e. their
    previous message (if any) was more than SESSION_TIMEOUT ago, or this
    is their first message ever. A stale session also clears any
    leftover "ask a query" mode, so a contact who tapped that long ago
    gets the greeting again on their next visit rather than being stuck
    in query mode indefinitely."""
    now = datetime.now()
    last = _last_seen.get(key)
    is_fresh = last is None or (now - last) > SESSION_TIMEOUT
    _last_seen[key] = now
    if is_fresh:
        _query_mode_users.discard(key)
    return is_fresh


@app.get("/webhook")
async def verify_webhook(request: Request):
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        return int(challenge)
    return {"error": "verification failed"}, 403


@app.post("/webhook")
async def receive_message(request: Request):
    body = await request.json()

    try:
        entry = body["entry"][0]
        changes = entry["changes"][0]
        value = changes["value"]

        if "messages" not in value:
            return {"status": "ignored"}

        receiving_phone_id = value.get("metadata", {}).get("phone_number_id")
        tenant_config = TENANT_BY_PHONE_ID.get(receiving_phone_id)

        if tenant_config is None:
            print(f"WARNING: received message for unknown phone_number_id: {receiving_phone_id}")
            return {"status": "ignored_unknown_number"}

        message = value["messages"][0]
        from_number = message["from"]
        msg_type = message.get("type")

        # Every inbound WhatsApp message is either plain text, or — when
        # the customer taps one of our buttons — an "interactive" message
        # carrying a button_reply. We normalize both into a single
        # `user_text` string, using the tapped button's TITLE as the text,
        # so every existing keyword/phrase-matching function in orders.py
        # and booking.py (e.g. _is_affirmative, _parse_size, _is_done_adding)
        # keeps working completely unchanged — tapping "Yes" is
        # indistinguishable from typing "yes". `button_id` is kept
        # alongside for the couple of buttons (the initial greeting,
        # "Add More") that need to be special-cased below rather than
        # treated as ordinary free text.
        button_id = None
        if msg_type == "text":
            user_text = message["text"]["body"]
        elif msg_type == "interactive":
            interactive = message.get("interactive", {})
            interactive_type = interactive.get("type")
            if interactive_type == "button_reply":
                button_reply = interactive["button_reply"]
                button_id = button_reply.get("id")
                user_text = button_reply.get("title", "")
            elif interactive_type == "list_reply":
                # Not used by any flow yet, but handled the same way in
                # case a list-style message is added later.
                list_reply = interactive["list_reply"]
                button_id = list_reply.get("id")
                user_text = list_reply.get("title", "")
            else:
                return {"status": "ignored_unsupported_interactive"}
        else:
            # Images, audio, location, etc. — nothing for the text-based
            # flows below to do with these yet.
            return {"status": "ignored_unsupported_message_type"}

        tenant = tenant_config["tenant"]
        flow = tenant_config.get("flow")
        key = (tenant, from_number)

        special_reply = None
        if flow == "booking":
            special_reply = handle_booking_message(tenant, from_number, user_text)

        elif flow == "ordering":
            is_new_session = _touch_and_check_fresh(key)

            if button_id == "ask_query":
                # Customer wants to talk to the chatbot rather than order —
                # nothing to answer yet, just confirm and switch modes so
                # their next message goes straight to RAG.
                _query_mode_users.add(key)
                special_reply = "Sure! Go ahead and ask me anything 🙂"

            elif button_id == "add_more":
                # A pure UI nudge from the "anything else?" prompt — there's
                # no new item text to parse yet, so don't feed the button's
                # own title into the order parser.
                special_reply = "Sure! What would you like to add?"

            else:
                if button_id == "place_order":
                    # Route exactly like a customer typing "order" would —
                    # orders.py's casual-intent detection already matches
                    # the word "order" and starts the collecting flow.
                    user_text = "order"
                    _query_mode_users.discard(key)

                had_state_before = key in orders._conversations

                if (
                    not had_state_before
                    and key not in _query_mode_users
                    and is_new_session
                ):
                    # Fresh session, nothing in progress yet — lead with
                    # the two clickable options instead of guessing what
                    # a plain first message meant.
                    special_reply = (GREETING_TEXT, GREETING_BUTTONS)
                else:
                    special_reply = handle_order_message(tenant, from_number, user_text)
            # flow == None (or any other value) simply skips straight to RAG

        if special_reply is not None:
            answer = special_reply
        else:
            answer = await get_rag_answer(user_text, tenant)

        await send_whatsapp_reply(
            to_number=from_number,
            reply=answer,
            access_token=tenant_config["access_token"],
            phone_number_id=tenant_config["phone_number_id"],
        )

    except (KeyError, IndexError):
        pass

    return {"status": "ok"}


async def get_rag_answer(user_text: str, tenant: str) -> str:
    result = generate_answer(user_text, tenant=tenant)
    return result["answer"]


async def send_whatsapp_message(
    to_number: str, text: str, access_token: str, phone_number_id: str
):
    url = f"https://graph.facebook.com/v21.0/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {"body": text},
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers=headers, json=payload)
        print("WhatsApp API response:", resp.status_code, resp.text)
        resp.raise_for_status()
        return resp.json()


async def send_whatsapp_buttons(
    to_number: str,
    body_text: str,
    buttons,
    access_token: str,
    phone_number_id: str,
):
    """Send a WhatsApp interactive "reply button" message — up to 3
    tappable buttons, each an (id, title) pair. `title` is what the
    customer sees, and it's what comes back verbatim as the text of their
    next message if they tap it (see the button_reply handling in
    receive_message above). WhatsApp caps button titles at 20 characters;
    they're truncated here as a safety net so a too-long title never
    causes the whole send to fail."""
    url = f"https://graph.facebook.com/v21.0/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body_text},
            "action": {
                "buttons": [
                    {
                        "type": "reply",
                        "reply": {"id": btn_id, "title": btn_title[:20]},
                    }
                    for btn_id, btn_title in buttons[:3]
                ]
            },
        },
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers=headers, json=payload)
        print("WhatsApp API response:", resp.status_code, resp.text)
        resp.raise_for_status()
        return resp.json()


async def send_whatsapp_reply(
    to_number: str, reply, access_token: str, phone_number_id: str
):
    """Send whatever a flow handler (or the greeting logic above) produced.
    `reply` is either a plain string (sent as normal text, same as always),
    or a (text, buttons) tuple — as returned by orders.py at its yes/no
    confirmations, size choice, and "anything else?" prompts, and by the
    initial greeting above — which is sent as tappable WhatsApp reply
    buttons instead."""
    if isinstance(reply, tuple):
        text, buttons = reply
        if buttons:
            await send_whatsapp_buttons(to_number, text, buttons, access_token, phone_number_id)
            return
        reply = text
    await send_whatsapp_message(to_number, reply, access_token, phone_number_id)


# ---------------------------------------------------------------------------
# Reminder scheduler (appointments only — Domino's orders have no reminder)
# ---------------------------------------------------------------------------

scheduler = AsyncIOScheduler()


async def send_due_reminders():
    due = get_appointments_needing_reminder()
    for appt in due:
        tenant_creds = TENANT_CREDENTIALS_BY_NAME.get(appt["tenant"])
        if tenant_creds is None:
            print(f"WARNING: no credentials found for tenant '{appt['tenant']}', skipping reminder")
            continue

        reminder_text = (
            f"Reminder: you have an appointment today at {appt['time_slot']} "
            f"under the name {appt['visitor_name']}. See you soon!"
        )
        try:
            await send_whatsapp_message(
                to_number=appt["phone_number"],
                text=reminder_text,
                access_token=tenant_creds["access_token"],
                phone_number_id=tenant_creds["phone_number_id"],
            )
            mark_reminder_sent(appt["id"])
        except Exception:
            traceback.print_exc()


@app.on_event("startup")
async def start_scheduler():
    scheduler.add_job(send_due_reminders, "interval", minutes=5)
    scheduler.start()