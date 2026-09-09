import logging
import os
import re
import threading
from datetime import datetime, date, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests
from bs4 import BeautifulSoup
from openai import OpenAI
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
)
import icalendar
import recurring_ical_events

# --- CONFIGURATION (ENVIRONMENT VARIABLES) ---
WILMA_BASE_URL = os.environ.get("WILMA_BASE_URL", "https://espoo.inschool.fi/")
WILMA_USERNAME = os.environ.get("WILMA_USERNAME", "")
WILMA_PASSWORD = os.environ.get("WILMA_PASSWORD", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
OPENROUTER_TOKEN = os.environ.get("OPENROUTER_TOKEN", "")

# Relative path for local & cloud flexibility (keep Wilma.ics in the bot's root folder)
ICS_FILE_PATH = os.environ.get("ICS_FILE_PATH", "Wilma.ics")

# School lunch menu web source
CUSTOM_MENU_URL = "https://kouluruoka.fi/menu/espoo_maininginkoulu/"

PE_SCHEDULE = """
--- EXTRA PHYSICAL EDUCATION SCHEDULE (Syksy 2026 liikuntatunnit) ---
Note: Wednesday (ke) PE lesson takes the first activity; Friday (pe) PE lesson takes the second activity.
Vko 35: Aitajuoksu + keihäänheitto (ke), Koripallo (pe)
Vko 36: Pituushyppy ja 60m juoksu (ke), Ultimate (pe)
Vko 37: Cooper + kuulantyöntö (ke), Salibandy (pe)
Vko 38: Suunnistus (ke), Suunnistus (pe)
Vko 39: Lentopallo (ke), Suunnistus (pe)
Vko 40: TET
Vko 41: TET
Vko 42: Syysloma
Vko 43: Lentopallo
Vko 44: Salibandy
Vko 45: Uinti, Matinkylä
Vko 46: Uinti, Matinkylä
Vko 47: Koripallo
Vko 48: Polttopalloja
Vko 49: Jää Espoonlahden Forum
Vko 50: Jää Espoonlahden Forum
Vko 51: Kuntosali
"""

DATA_SOURCES = {
    "GRADES": [
        "https://espoo.inschool.fi/grades",
        "https://espoo.inschool.fi/choices",
    ],
    "MESSAGES": [
        "https://espoo.inschool.fi/messages",
        "https://espoo.inschool.fi/messages/inbox",
        "https://espoo.inschool.fi/messages/?page=1",
    ],
    "EXAMS": [
        "https://espoo.inschool.fi/exams/calendar/past",
        "https://espoo.inschool.fi/exams/calendar",
    ],
    "ATTENDANCE": ["https://espoo.inschool.fi/attendance/view"],
}

logging.basicConfig(level=logging.INFO)

ai_client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_TOKEN,
    default_headers={
        "HTTP-Referer": "https://github.com/wilma-telegram-bot",
        "X-Title": "Wilma Telegram Bot",
    },
)


class HealthCheckHandler(BaseHTTPRequestHandler):
    """Dummy web server to satisfy Render Free Web Service port checks."""
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Wilma Telegram Bot is active!")

    def log_message(self, format, *args):
        return  # Suppress default HTTP logs


def start_dummy_web_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()


def fetch_school_menu() -> str:
    """Scrapes the menu directly from Kouluruoka.fi."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }

    try:
        r = requests.get(CUSTOM_MENU_URL, headers=headers, timeout=8)
        if r.ok:
            soup = BeautifulSoup(r.text, "html.parser")

            for tag in soup(["script", "style", "header", "footer", "nav"]):
                tag.extract()

            menu_container = (
                soup.find("div", class_=re.compile(r"(menu|week|content)"))
                or soup.find("main")
                or soup.body
            )

            if menu_container:
                raw_text = menu_container.get_text(separator="\n", strip=True)
                lines = [
                    line.strip()
                    for line in raw_text.splitlines()
                    if len(line.strip()) > 1
                ]
                clean_text = "\n".join(lines)

                if len(clean_text) > 30:
                    return f"--- SCHOOL LUNCH MENU (Mainingin koulu / Kouluruoka.fi) ---\n{clean_text[:3000]}"
    except Exception as e:
        logging.warning(f"Failed to scrape menu from {CUSTOM_MENU_URL}: {e}")

    return "⚠️ Note: Could not fetch lunch menu from Kouluruoka.fi."


def load_ics_weekly_schedule(ics_path: str, query_date: date) -> str:
    """Parses Wilma.ics and expands recurring events for the full current week."""
    try:
        with open(ics_path, "rb") as f:
            cal = icalendar.Calendar.from_ical(f.read())

        start_of_week = query_date - timedelta(days=query_date.weekday())
        end_of_week = start_of_week + timedelta(days=6)

        events = recurring_ical_events.of(cal).between(start_of_week, end_of_week)

        if not events:
            return "No lessons scheduled in Wilma.ics for this week."

        day_names = {
            0: "Maanantai",
            1: "Tiistai",
            2: "Keskiviikko",
            3: "Torstai",
            4: "Perjantai",
            5: "Lauantai",
            6: "Sunnuntai",
        }

        events_sorted = sorted(events, key=lambda e: e.get("DTSTART").dt)

        schedule_lines = []
        for event in events_sorted:
            dt_start = event.get("DTSTART").dt
            dt_end = event.get("DTEND").dt

            if isinstance(dt_start, datetime):
                day_str = day_names[dt_start.weekday()]
                date_str = dt_start.strftime("%d.%m.%Y")
                time_str = f"{dt_start.strftime('%H:%M')}–{dt_end.strftime('%H:%M')}"
            else:
                day_str = day_names[dt_start.weekday()]
                date_str = dt_start.strftime("%d.%m.%Y")
                time_str = "Koko päivä"

            summary = str(event.get("SUMMARY", ""))
            location = str(event.get("LOCATION", ""))

            entry = f"{day_str} {date_str} [{time_str}]: {summary}"
            if location and location not in summary:
                entry += f" (Luokka: {location})"
            schedule_lines.append(entry)

        week_num = query_date.isocalendar()[1]
        return f"--- BASE TIMETABLE FROM {ics_path} (Week {week_num}) ---\n" + "\n".join(
            schedule_lines
        )
    except FileNotFoundError:
        return f"⚠️ Warning: Schedule file '{ics_path}' not found."
    except Exception as e:
        return f"⚠️ Error parsing '{ics_path}': {e}"


def get_active_free_models() -> list[str]:
    """Fetches active free models dynamically from OpenRouter."""
    fallback_models = [
        "google/gemini-2.0-flash-exp:free",
        "google/gemini-2.0-pro-exp-02-05:free",
        "deepseek/deepseek-r1:free",
        "meta-llama/llama-3.3-70b-instruct:free",
        "qwen/qwen-2.5-7b-instruct:free",
    ]
    try:
        response = requests.get("https://openrouter.ai/api/v1/models", timeout=5)
        if response.ok:
            data = response.json().get("data", [])
            live_free = [
                m.get("id")
                for m in data
                if str(m.get("id", "")).endswith(":free")
            ]
            if live_free:
                return live_free
    except Exception as e:
        logging.warning(f"Could not fetch live OpenRouter models: {e}")

    return fallback_models


def clean_ai_response(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    # Strip asterisks to prevent italic/cursive text output in Telegram
    text = text.replace("*", "")
    if "Let me analyze" in text or "First, let me look" in text:
        parts = re.split(r"\n\n+", text)
        filtered_parts = [
            p
            for p in parts
            if not p.strip().startswith(("Let me", "First,", "Now for", "Looking at"))
        ]
        if filtered_parts:
            text = "\n\n".join(filtered_parts)
    return text.strip()


def convert_scores_to_finnish_scale(text: str) -> str:
    def replace_fraction(match):
        score = float(match.group(1))
        max_score = float(match.group(2))
        if max_score <= 0 or score > max_score:
            return match.group(0)
        raw_grade = 4.0 + (6.0 * (score / max_score))
        step_grade = round(raw_grade * 4) / 4
        base_int = int(step_grade)
        decimal_part = round(step_grade - base_int, 2)
        if decimal_part == 0.0:
            formatted_grade = f"{base_int}"
        elif decimal_part == 0.25:
            formatted_grade = f"{base_int}+"
        elif decimal_part == 0.50:
            formatted_grade = f"{base_int}½ ({base_int}.5)"
        elif decimal_part == 0.75:
            formatted_grade = f"{base_int + 1}-"
        else:
            formatted_grade = f"{round(raw_grade, 1)}"
        return f"{match.group(0)} (Finnish grade: {formatted_grade})"

    pattern = r"\b(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\b"
    return re.sub(pattern, replace_fraction, text)


def determine_required_data_fast(user_question: str) -> list[str]:
    q = user_question.lower()
    categories = []

    if any(k in q for k in ["grade", "mark", "score", "report", "arvosana", "todistus", "pisteet", "numero"]):
        categories.append("GRADES")

    if any(k in q for k in ["message", "inbox", "announcement", "viesti", "tiedote", "opettaja"]):
        categories.append("MESSAGES")

    if any(k in q for k in ["exam", "test", "quiz", "koe", "kokeet", "tentti"]):
        categories.append("EXAMS")

    if any(k in q for k in ["absence", "tardy", "attendance", "poissaolo", "poissaolot", "myöhästyminen"]):
        categories.append("ATTENDANCE")

    if any(k in q for k in ["menu", "food", "lunch", "ruoka", "ruokalista", "lounas", "syödään"]):
        categories.append("MENU")

    return list(set(categories))


def find_role_prefix(session, response, base_url):
    all_urls = [response.url] + [h.url for h in response.history]
    for url in all_urls:
        match = re.search(r"(![a-zA-Z0-9_-]+)", url)
        if match:
            return match.group(1)
    match = re.search(r'href=["\']/?(![a-zA-Z0-9_-]+)', response.text)
    if match:
        return match.group(1)
    return None


def get_wilma_session():
    if not WILMA_USERNAME or not WILMA_PASSWORD:
        logging.warning("Wilma credentials are not configured.")
        return None, None

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        }
    )
    base = WILMA_BASE_URL.rstrip("/").replace("/login", "")
    login_url = f"{base}/login"

    try:
        initial_res = session.get(login_url, timeout=10)
        if not initial_res.ok:
            return None, None
        soup = BeautifulSoup(initial_res.text, "html.parser")

        payload = {
            input_tag.get("name"): input_tag.get("value", "")
            for input_tag in soup.find_all("input")
            if input_tag.get("name")
        }
        payload["Login"] = WILMA_USERNAME
        payload["Password"] = WILMA_PASSWORD

        response = session.post(
            login_url, data=payload, allow_redirects=True, timeout=10
        )

        if (
            "kirjaudu-ulos" in response.text.lower()
            or "logout" in response.text.lower()
        ):
            role_id = find_role_prefix(session, response, base)
            if role_id:
                return session, f"{base}/{role_id}"
            return session, base
        return None, None
    except requests.exceptions.RequestException as e:
        logging.error(f"Wilma login error: {e}")
        return None, None


def fetch_school_context(
    session, base_user_url, required_categories: list[str]
) -> str:
    base = WILMA_BASE_URL.rstrip("/").replace("/login", "")
    combined = []
    visited_urls = set()

    role_match = re.search(r"(![a-zA-Z0-9_-]+)", base_user_url)
    role_prefix = role_match.group(1) if role_match else ""

    target_urls = []
    for cat in required_categories:
        if cat in DATA_SOURCES:
            target_urls.extend(DATA_SOURCES[cat])

    final_urls = []
    for d_url in target_urls:
        final_urls.append(d_url)
        if role_prefix and "espoo.inschool.fi" in d_url:
            path_only = d_url.replace("https://espoo.inschool.fi/", "").lstrip("/")
            role_variant = f"{base}/{role_prefix}/{path_only}"
            if role_variant not in final_urls:
                final_urls.append(role_variant)

    for target_url in final_urls:
        if target_url in visited_urls:
            continue
        visited_urls.add(target_url)

        try:
            r = session.get(target_url, timeout=10)
            path_label = target_url.replace("https://espoo.inschool.fi", "")

            if r.ok:
                page_soup = BeautifulSoup(r.text, "html.parser")
                for tag in page_soup(["script", "style", "nav", "header", "footer"]):
                    tag.extract()

                main_container = (
                    page_soup.find("div", {"id": "content"})
                    or page_soup.find("main")
                    or page_soup.find("body")
                    or page_soup
                )
                raw_text = main_container.get_text(separator=" ", strip=True)
                clean_text = re.sub(r"\s+", " ", raw_text).strip()

                if len(clean_text) > 50:
                    combined.append(
                        f"--- DATA FROM {path_label} ---\n{clean_text[:2500]}"
                    )
        except requests.exceptions.RequestException:
            continue

    full_context = "\n\n".join(combined)
    return (
        convert_scores_to_finnish_scale(full_context)
        if full_context.strip()
        else ""
    )


async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Resets conversation memory for the user."""
    context.user_data["history"] = []
    await update.message.reply_text("🧹 Conversation history cleared!")


async def send_hourly_wilma_summary(context: ContextTypes.DEFAULT_TYPE):
    """Background task: Fetches Wilma messages and sends a summary once per hour."""
    chat_id = context.bot_data.get("last_chat_id")
    if not chat_id:
        return

    session, base_user_url = get_wilma_session()
    if not session:
        return

    messages_context = fetch_school_context(session, base_user_url, ["MESSAGES"])
    if not messages_context.strip():
        return

    now_str = datetime.now().strftime("%A, %d.%m.%Y %H:%M")
    prompt = f"""
Summarize any recent Wilma messages into a quick bulleted summary for the student. If there are no new or important messages, reply strictly with 'NO_NEW_MESSAGES'.
CURRENT DATE & TIME: {now_str}

Wilma Messages Context:
{messages_context}
"""

    api_messages = [
        {
            "role": "system",
            "content": "You are a concise assistant. Output short plain text summaries without asterisks or markdown italics.",
        },
        {"role": "user", "content": prompt},
    ]

    summary_reply = ""
    for model_name in get_active_free_models():
        try:
            res = ai_client.chat.completions.create(
                model=model_name,
                messages=api_messages,
                max_tokens=600,
            )
            cleaned = clean_ai_response(res.choices[0].message.content or "")
            if cleaned:
                summary_reply = cleaned
                break
        except Exception:
            continue

    if summary_reply and "NO_NEW_MESSAGES" not in summary_reply:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"📩 Hourly Wilma Summary:\n\n{summary_reply}",
        )


async def handle_user_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_question = update.message.text
    status_msg = await update.message.reply_text("⏳ Processing calendar & Wilma data...")

    # Save active chat_id for hourly message background job
    context.bot_data["last_chat_id"] = update.effective_chat.id

    now = datetime.now()
    today = date.today()
    week_num = today.isocalendar()[1]

    # Initialize history list if absent
    if "history" not in context.user_data:
        context.user_data["history"] = []

    # Retain chat history within 1-hour window
    one_hour_ago = now - timedelta(hours=1)
    valid_history = [
        item for item in context.user_data["history"]
        if item.get("timestamp", now) >= one_hour_ago
    ]
    context.user_data["history"] = valid_history

    # 1. Load ICS timetable
    ics_schedule = load_ics_weekly_schedule(ICS_FILE_PATH, today)

    # 2. Fetch required categories or school menu
    required_data = determine_required_data_fast(user_question)

    if any(k in user_question.lower() for k in ["after that", "next", "then", "seuraava", "sitten", "what about"]):
        recent_text = " ".join([h["user"] for h in valid_history[-2:]]).lower()
        required_data.extend(determine_required_data_fast(recent_text))
        required_data = list(set(required_data))

    wilma_extra_context = ""
    if required_data:
        if "MENU" in required_data:
            wilma_extra_context += fetch_school_menu() + "\n\n"

        wilma_categories = [c for c in required_data if c != "MENU"]
        if wilma_categories:
            session, base_user_url = get_wilma_session()
            if session:
                wilma_extra_context += fetch_school_context(
                    session, base_user_url, wilma_categories
                )

    full_context = f"{ics_schedule}\n\n{PE_SCHEDULE}\n\n{wilma_extra_context}"[
        :10000
    ]
    now_str = now.strftime(f"%A, %d.%m.%Y %H:%M (Week {week_num})")

    system_instruction = f"""
You are an AI assistant helping a student with school schedules (using Wilma.ics calendar data and physical education plan) and school meal/grade details.
CURRENT DATE & TIME: {now_str}

CRITICAL RULES:
1. Output ONLY the direct final response.
2. DO NOT output internal reasoning or preamble.
3. Respond in the EXACT SAME LANGUAGE as the question.
4. Keep responses concise while including all requested subjects and schedule details.
5. Hide teacher names, room numbers, and course codes by default unless specifically asked.
6. Cross-reference the PE Schedule with the ICS calendar: match the specific PE activity to Wednesday (ke) or Friday (pe) based on the week number.
7. Use the conversation history to correctly resolve follow-up questions.
8. ABSOLUTELY DO NOT use asterisks (*) or markdown italics (like *this*). Write plain text only.
"""

    api_messages = [{"role": "system", "content": system_instruction.strip()}]
    for item in valid_history:
        api_messages.append({"role": "user", "content": item["user"]})
        api_messages.append({"role": "assistant", "content": item["bot"]})

    user_prompt = f"CONTEXT:\n{full_context}\n\nQUESTION:\n{user_question}"
    api_messages.append({"role": "user", "content": user_prompt})

    ai_reply = ""
    last_error_log = ""
    active_models = get_active_free_models()

    for model_name in active_models:
        try:
            res = ai_client.chat.completions.create(
                model=model_name,
                messages=api_messages,
                max_tokens=1200,
            )
            cleaned = clean_ai_response(res.choices[0].message.content or "")
            if cleaned:
                ai_reply = cleaned
                break
        except Exception as e:
            last_error_log = str(e)
            logging.warning(f"Model {model_name} failed: {e}")
            continue

    if not ai_reply:
        logging.error(f"All free AI models failed. Last error: {last_error_log}")
        ai_reply = f"❌ Request failed on free models.\nLog detail: {last_error_log[:150]}"

    context.user_data["history"].append({
        "timestamp": datetime.now(),
        "user": user_question,
        "bot": ai_reply
    })

    if len(ai_reply) > 4000:
        await status_msg.edit_text(ai_reply[:4000])
        for i in range(4000, len(ai_reply), 4000):
            await update.message.reply_text(ai_reply[i : i + 4000])
    else:
        await status_msg.edit_text(ai_reply)


def main():
    if not TELEGRAM_BOT_TOKEN:
        print("❌ ERROR: TELEGRAM_BOT_TOKEN is missing!")
        return

    # Start background web server to pass Render Free Web Service health checks
    threading.Thread(target=start_dummy_web_server, daemon=True).start()

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("clear", clear_history))
    app.add_handler(
        MessageHandler(filters.TEXT & (~filters.COMMAND), handle_user_question)
    )

    # Schedule hourly Wilma message summary task
    if app.job_queue:
        app.job_queue.run_repeating(
            send_hourly_wilma_summary,
            interval=3600,
            first=60,
        )

    print("🤖 Bot is running on Render Free Web Service...")
    app.run_polling()


if __name__ == "__main__":
    main()
