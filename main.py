import os
import traceback

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
        user_text = message["text"]["body"]

        tenant = tenant_config["tenant"]
        flow = tenant_config.get("flow")

        special_reply = None
        if flow == "booking":
            special_reply = handle_booking_message(tenant, from_number, user_text)
        elif flow == "ordering":
            special_reply = handle_order_message(tenant, from_number, user_text)
        # flow == None (or any other value) simply skips straight to RAG

        if special_reply is not None:
            answer = special_reply
        else:
            answer = await get_rag_answer(user_text, tenant)

        await send_whatsapp_message(
            to_number=from_number,
            text=answer,
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