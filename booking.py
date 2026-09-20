"""
Appointment booking module — separate from the RAG chatbot.
Uses SQLite. Handles: booking, cancelling, rescheduling, slot conflict
resolution, auto-deleting past-date appointments, and a simple per-user
conversation state machine for WhatsApp's back-and-forth.

LIMITATIONS (read these before relying on this in production):
- Conversation state (_conversations) lives in memory only — resets if the
  server restarts mid-conversation.
- Reminders are sent as free-form text. WhatsApp only allows free-form text
  within 24 hours of the user's last message to you. If a reminder fires
  outside that window, Meta's API will likely reject it — you'd see this in
  the "WhatsApp API response" print in main.py. For guaranteed delivery
  regardless of timing, you'd need an approved WhatsApp Message Template.
- Assumes ONE active (future-dated) appointment per phone number per tenant
  at a time — if someone already has one, booking a second is blocked and
  they're pointed to reschedule/cancel instead.
"""

import os
import sqlite3
from datetime import datetime, timedelta
from dateutil import parser as dateparser

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "appointments.db")

BOOKING_START_HOUR = 9
BOOKING_END_HOUR = 16

TIME_FMT = "%H:%M"
DATE_FMT = "%Y-%m-%d"


# ---------------------------------------------------------------------------
# Database setup
# ---------------------------------------------------------------------------

def get_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS appointments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant TEXT NOT NULL,
            visitor_name TEXT NOT NULL,
            phone_number TEXT NOT NULL,
            appointment_date TEXT NOT NULL,
            time_slot TEXT NOT NULL,
            created_at TEXT NOT NULL,
            reminder_sent INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    # Safe to run repeatedly — adds the column only if it doesn't exist yet
    # (needed if you already had an appointments.db from before this update).
    try:
        conn.execute("ALTER TABLE appointments ADD COLUMN reminder_sent INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.commit()
    return conn


def cleanup_old_appointments():
    today_str = datetime.now().strftime(DATE_FMT)
    conn = get_connection()
    conn.execute("DELETE FROM appointments WHERE appointment_date < ?", (today_str,))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Slot logic
# ---------------------------------------------------------------------------

def _time_to_minutes(t: str) -> int:
    h, m = map(int, t.split(":"))
    return h * 60 + m


def _minutes_to_time(mins: int) -> str:
    h, m = divmod(mins, 60)
    return f"{h:02d}:{m:02d}"


def _within_booking_hours(t: str) -> bool:
    mins = _time_to_minutes(t)
    return (BOOKING_START_HOUR * 60) <= mins <= (BOOKING_END_HOUR * 60)


def get_booked_slots(tenant: str, date_str: str, exclude_id=None):
    conn = get_connection()
    if exclude_id is not None:
        rows = conn.execute(
            "SELECT time_slot FROM appointments WHERE tenant = ? AND appointment_date = ? AND id != ? ORDER BY time_slot",
            (tenant, date_str, exclude_id),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT time_slot FROM appointments WHERE tenant = ? AND appointment_date = ? ORDER BY time_slot",
            (tenant, date_str),
        ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def find_slot(tenant: str, date_str: str, requested_time: str, exclude_id=None):
    """exclude_id lets a reschedule ignore the user's OWN current booking
    when checking for conflicts on the new date/time."""
    booked = set(get_booked_slots(tenant, date_str, exclude_id))

    if requested_time not in booked and _within_booking_hours(requested_time):
        return requested_time, True

    plus_10 = _minutes_to_time(_time_to_minutes(requested_time) + 10)
    if plus_10 not in booked and _within_booking_hours(plus_10):
        return plus_10, False

    if booked:
        last_slot = max(booked, key=_time_to_minutes)
        candidate = _minutes_to_time(_time_to_minutes(last_slot) + 10)
        if _within_booking_hours(candidate):
            return candidate, False

    return None, False


def save_appointment(tenant, visitor_name, phone_number, date_str, time_slot):
    conn = get_connection()
    conn.execute(
        "INSERT INTO appointments (tenant, visitor_name, phone_number, appointment_date, time_slot, created_at, reminder_sent) "
        "VALUES (?, ?, ?, ?, ?, ?, 0)",
        (tenant, visitor_name, phone_number, date_str, time_slot, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def get_active_appointment(tenant: str, phone_number: str):
    """Returns the user's next upcoming appointment (today or later), or None."""
    today_str = datetime.now().strftime(DATE_FMT)
    conn = get_connection()
    row = conn.execute(
        "SELECT id, visitor_name, appointment_date, time_slot FROM appointments "
        "WHERE tenant = ? AND phone_number = ? AND appointment_date >= ? "
        "ORDER BY appointment_date, time_slot LIMIT 1",
        (tenant, phone_number, today_str),
    ).fetchone()
    conn.close()
    if row is None:
        return None
    return {"id": row[0], "visitor_name": row[1], "appointment_date": row[2], "time_slot": row[3]}


def cancel_appointment_by_id(appointment_id: int):
    conn = get_connection()
    conn.execute("DELETE FROM appointments WHERE id = ?", (appointment_id,))
    conn.commit()
    conn.close()


def update_appointment(appointment_id: int, new_date: str, new_time: str):
    conn = get_connection()
    conn.execute(
        "UPDATE appointments SET appointment_date = ?, time_slot = ?, reminder_sent = 0 WHERE id = ?",
        (new_date, new_time, appointment_id),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Reminder support (called by a scheduled job in main.py)
# ---------------------------------------------------------------------------

def get_appointments_needing_reminder():
    """
    Returns appointments starting within the next hour that haven't had a
    reminder sent yet. main.py's scheduler calls this every few minutes.
    """
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, tenant, visitor_name, phone_number, appointment_date, time_slot "
        "FROM appointments WHERE reminder_sent = 0"
    ).fetchall()
    conn.close()

    now = datetime.now()
    due = []
    for row in rows:
        appt_id, tenant, name, phone, date_str, time_str = row
        try:
            appt_dt = datetime.strptime(f"{date_str} {time_str}", f"{DATE_FMT} {TIME_FMT}")
        except ValueError:
            continue
        if now <= appt_dt <= now + timedelta(hours=1):
            due.append({
                "id": appt_id, "tenant": tenant, "visitor_name": name,
                "phone_number": phone, "appointment_date": date_str, "time_slot": time_str,
            })
    return due


def mark_reminder_sent(appointment_id: int):
    conn = get_connection()
    conn.execute("UPDATE appointments SET reminder_sent = 1 WHERE id = ?", (appointment_id,))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Conversation state machine (in-memory)
# ---------------------------------------------------------------------------

BOOKING_TRIGGERS = [
    "book an appointment", "book appointment", "book a slot", "book a visit",
    "schedule an appointment", "schedule a visit", "visit the college",
    "visit the campus", "reception", "book a meeting",
]
CANCEL_TRIGGERS = [
    "cancel my appointment", "cancel appointment", "cancel my booking",
    "cancel booking", "cancel my visit",
]
CHANGE_TRIGGERS = [
    "reschedule", "change my appointment", "change appointment",
    "change my booking", "change booking", "move my appointment",
]

STEP_NAME = "awaiting_name"
STEP_DATETIME = "awaiting_datetime"
STEP_CONFIRM = "awaiting_confirmation"
STEP_CANCEL_CONFIRM = "awaiting_cancel_confirmation"
STEP_CHANGE_DATETIME = "awaiting_change_datetime"
STEP_CHANGE_CONFIRM = "awaiting_change_confirmation"

_conversations = {}


def _matches_any(text: str, triggers) -> bool:
    lowered = text.lower()
    return any(t in lowered for t in triggers)


def _is_affirmative(text: str) -> bool:
    return text.strip().lower() in ("yes", "y", "confirm", "ok", "okay", "sure", "yes please")


def _is_negative(text: str) -> bool:
    return text.strip().lower() in ("no", "n", "cancel", "change", "no thanks")


def _parse_datetime_or_none(user_text: str):
    try:
        parsed = dateparser.parse(user_text, fuzzy=True, default=datetime.now())
    except (ValueError, OverflowError):
        return None
    return parsed


def handle_booking_message(tenant: str, phone_number: str, user_text: str):
    """
    Returns a reply string if this message was handled by the booking system
    (new booking, cancel, or reschedule flow), or None if it's unrelated —
    in which case the caller should fall through to the normal RAG chatbot.
    """
    cleanup_old_appointments()

    key = (tenant, phone_number)
    state = _conversations.get(key)

    # -------------------------------------------------------------
    # Not currently mid-flow — figure out which flow (if any) starts
    # -------------------------------------------------------------
    if state is None:
        if _matches_any(user_text, CANCEL_TRIGGERS):
            active = get_active_appointment(tenant, phone_number)
            if active is None:
                return "You don't have any upcoming appointment to cancel."
            _conversations[key] = {"step": STEP_CANCEL_CONFIRM, "data": {"appointment_id": active["id"]}}
            return (
                f"You have an appointment on {active['appointment_date']} at {active['time_slot']} "
                f"under {active['visitor_name']}. Reply 'yes' to cancel it, or 'no' to keep it."
            )

        if _matches_any(user_text, CHANGE_TRIGGERS):
            active = get_active_appointment(tenant, phone_number)
            if active is None:
                return "You don't have any upcoming appointment to reschedule. Would you like to book one instead?"
            _conversations[key] = {
                "step": STEP_CHANGE_DATETIME,
                "data": {"appointment_id": active["id"], "name": active["visitor_name"]},
            }
            return (
                f"You currently have an appointment on {active['appointment_date']} at {active['time_slot']}. "
                f"What new date and time would you like? (reception hours are 9 AM to 4 PM)"
            )

        if _matches_any(user_text, BOOKING_TRIGGERS):
            active = get_active_appointment(tenant, phone_number)
            if active is not None:
                return (
                    f"You already have an appointment on {active['appointment_date']} at {active['time_slot']}. "
                    f"Say 'reschedule' to change it, or 'cancel my appointment' to cancel it first."
                )
            _conversations[key] = {"step": STEP_NAME, "data": {}}
            return (
                "Sure, I can help you book an appointment at the reception desk. "
                "What name should I book it under?"
            )

        return None  # not booking-related at all

    step = state["step"]
    data = state["data"]

    # -------------------------------------------------------------
    # New booking flow
    # -------------------------------------------------------------
    if step == STEP_NAME:
        name = user_text.strip()
        if not name:
            return "Please tell me the name to book the appointment under."
        data["name"] = name
        state["step"] = STEP_DATETIME
        return (
            f"Thanks, {name}. What date and time would you like to visit? "
            f"(reception hours are 9 AM to 4 PM, e.g. '18 August, 2 PM')"
        )

    if step == STEP_DATETIME:
        parsed = _parse_datetime_or_none(user_text)
        if parsed is None:
            return "Sorry, I couldn't understand that date/time. Could you try again? (e.g. '18 August, 2 PM')"

        date_str = parsed.strftime(DATE_FMT)
        time_str = parsed.strftime(TIME_FMT)

        if parsed.date() < datetime.now().date():
            return "That date has already passed. Please give a future date."
        if not _within_booking_hours(time_str):
            return "Reception hours are 9 AM to 4 PM only. Please give a time within that range."

        offered_slot, was_exact = find_slot(tenant, date_str, time_str)
        if offered_slot is None:
            return f"Unfortunately there are no available slots on {date_str}. Could you try a different date?"

        data["date"] = date_str
        data["time"] = offered_slot
        state["step"] = STEP_CONFIRM

        if was_exact:
            return (
                f"Booking for {data['name']} on {date_str} at {offered_slot}. "
                f"Reply 'yes' to confirm or 'no' to choose a different time."
            )
        return (
            f"{time_str} is already booked. The nearest available slot is {offered_slot} "
            f"on {date_str}. Reply 'yes' to book this instead, or 'no' to choose a different time."
        )

    if step == STEP_CONFIRM:
        if _is_affirmative(user_text):
            save_appointment(tenant, data["name"], phone_number, data["date"], data["time"])
            del _conversations[key]
            return (
                f"Your appointment is confirmed for {data['date']} at {data['time']} "
                f"under the name {data['name']}. See you then!"
            )
        if _is_negative(user_text):
            state["step"] = STEP_DATETIME
            data.pop("date", None)
            data.pop("time", None)
            return "No problem — what date and time would you like instead?"
        return "Please reply 'yes' to confirm or 'no' to pick a different time."

    # -------------------------------------------------------------
    # Cancel flow
    # -------------------------------------------------------------
    if step == STEP_CANCEL_CONFIRM:
        if _is_affirmative(user_text):
            cancel_appointment_by_id(data["appointment_id"])
            del _conversations[key]
            return "Your appointment has been cancelled."
        if _is_negative(user_text):
            del _conversations[key]
            return "Okay, your appointment is unchanged."
        return "Please reply 'yes' to cancel or 'no' to keep your appointment."

    # -------------------------------------------------------------
    # Reschedule flow
    # -------------------------------------------------------------
    if step == STEP_CHANGE_DATETIME:
        parsed = _parse_datetime_or_none(user_text)
        if parsed is None:
            return "Sorry, I couldn't understand that date/time. Could you try again? (e.g. '18 August, 2 PM')"

        date_str = parsed.strftime(DATE_FMT)
        time_str = parsed.strftime(TIME_FMT)

        if parsed.date() < datetime.now().date():
            return "That date has already passed. Please give a future date."
        if not _within_booking_hours(time_str):
            return "Reception hours are 9 AM to 4 PM only. Please give a time within that range."

        offered_slot, was_exact = find_slot(tenant, date_str, time_str, exclude_id=data["appointment_id"])
        if offered_slot is None:
            return f"Unfortunately there are no available slots on {date_str}. Could you try a different date?"

        data["new_date"] = date_str
        data["new_time"] = offered_slot
        state["step"] = STEP_CHANGE_CONFIRM

        if was_exact:
            return (
                f"Reschedule to {date_str} at {offered_slot}? "
                f"Reply 'yes' to confirm or 'no' to choose a different time."
            )
        return (
            f"{time_str} is already booked. The nearest available slot is {offered_slot} "
            f"on {date_str}. Reply 'yes' to reschedule to this instead, or 'no' to choose a different time."
        )

    if step == STEP_CHANGE_CONFIRM:
        if _is_affirmative(user_text):
            update_appointment(data["appointment_id"], data["new_date"], data["new_time"])
            del _conversations[key]
            return f"Your appointment has been rescheduled to {data['new_date']} at {data['new_time']}."
        if _is_negative(user_text):
            state["step"] = STEP_CHANGE_DATETIME
            data.pop("new_date", None)
            data.pop("new_time", None)
            return "No problem — what date and time would you like instead?"
        return "Please reply 'yes' to confirm or 'no' to pick a different time."

    del _conversations[key]
    return None