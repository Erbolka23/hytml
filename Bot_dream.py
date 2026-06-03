import telebot
import json
import os
import requests
from urllib.parse import quote
from datetime import datetime, date
from groq import Groq
from collections import defaultdict
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
load_dotenv()
# ─── Config ──────────────────────────────────────────────────────────────────

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY   = os.getenv("GROQ_API_KEY")

client = Groq(api_key=GROQ_API_KEY)
bot    = telebot.TeleBot(TELEGRAM_TOKEN)

# Dr. Jung — персонаж с характером
SYSTEM_PROMPT = """You are Dr. Jung — a warm, poetic, and deeply insightful Jungian dream analyst.
You have a distinct character: curious, empathetic, occasionally philosophical, never clinical or cold.
You explore dreams *together* with the user, not lecture them.

Rules:
1. Always analyze using Jungian concepts (archetypes, Shadow, Anima/Animus, collective unconscious, individuation).
2. After your analysis, always end with ONE thoughtful follow-up question about a specific symbol or emotion.
   Format it on a new line starting with "💭 *Question:*"
3. If you have the user's psychological profile, weave it naturally into your analysis
   ("I notice water appears in your dreams again — last time it was...").
4. Respond in the user's language.
"""

MAX_HISTORY    = 20
JOURNAL_FILE   = "journal.json"
PROFILES_FILE  = "profiles.json"
REMINDERS_FILE = "reminders.json"

# ─── In-memory state ─────────────────────────────────────────────────────────

conversation_history = defaultdict(list)

# "idle"     → ждём новый сон
# "followup" → ждём ответ пользователя на вопрос доктора
user_state     = defaultdict(lambda: "idle")
last_dream     = {}   # chat_id → текст последнего сна
last_analysis  = {}   # chat_id → текст последнего анализа

# ─── APScheduler ─────────────────────────────────────────────────────────────

scheduler = BackgroundScheduler(timezone="Asia/Bishkek")
scheduler.start()

# ─── Journal ─────────────────────────────────────────────────────────────────

def load_journal():
    if os.path.exists(JOURNAL_FILE):
        with open(JOURNAL_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_journal(journal):
    with open(JOURNAL_FILE, "w", encoding="utf-8") as f:
        json.dump(journal, f, ensure_ascii=False, indent=2)

def add_to_journal(chat_id, dream, analysis):
    journal = load_journal()
    key = str(chat_id)
    if key not in journal:
        journal[key] = []
    journal[key].append({
        "date":     datetime.now().strftime("%Y-%m-%d %H:%M"),
        "dream":    dream,
        "analysis": analysis
    })
    save_journal(journal)

# ─── Psychological profile ────────────────────────────────────────────────────

def load_profiles():
    if os.path.exists(PROFILES_FILE):
        with open(PROFILES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_profiles(profiles):
    with open(PROFILES_FILE, "w", encoding="utf-8") as f:
        json.dump(profiles, f, ensure_ascii=False, indent=2)

def load_profile(chat_id) -> dict:
    return load_profiles().get(str(chat_id), {
        "archetypes":        [],
        "recurring_symbols": [],
        "emotional_patterns":[],
        "fears":             [],
        "themes":            [],
        "last_updated":      None
    })

def update_profile(chat_id, dream_text: str, analysis: str, followup_answer: str = None):
    """
    Asks Groq to merge new session data into the user's Jungian profile.
    Returns the updated profile dict.
    """
    current = load_profile(chat_id)

    followup_section = (
        f"\nFollow-up answer from user: {followup_answer}" if followup_answer else ""
    )

    prompt = f"""Analyze this dream session and update the user's Jungian psychological profile.

Dream: {dream_text}
Analysis: {analysis}{followup_section}

Current profile:
{json.dumps(current, ensure_ascii=False, indent=2)}

Instructions:
- Merge new findings with existing profile (don't erase old entries, add new ones).
- Keep lists concise (max 7 items each, remove duplicates).
- Set "last_updated" to today's date: {date.today().isoformat()}.

Return ONLY a valid JSON object, no markdown, no explanation:
{{
  "archetypes":         ["..."],
  "recurring_symbols":  ["..."],
  "emotional_patterns": ["..."],
  "fears":              ["..."],
  "themes":             ["..."],
  "last_updated":       "YYYY-MM-DD"
}}"""

    try:
        response = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=600
        )
        raw = response.choices[0].message.content.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        updated = json.loads(raw)

        profiles = load_profiles()
        profiles[str(chat_id)] = updated
        save_profiles(profiles)
        return updated

    except Exception as e:
        print(f"Profile update error: {e}")
        return current

def format_profile(profile: dict) -> str:
    if not any(profile.get(k) for k in ["archetypes", "recurring_symbols", "themes"]):
        return "_(profile is empty — share more dreams to build it)_"

    lines = ["🧠 *Your Jungian Psychological Profile*\n"]
    if profile.get("archetypes"):
        lines.append("⚡ *Archetypes:* " + ", ".join(profile["archetypes"]))
    if profile.get("recurring_symbols"):
        lines.append("🔁 *Recurring symbols:* " + ", ".join(profile["recurring_symbols"]))
    if profile.get("emotional_patterns"):
        lines.append("💫 *Emotional patterns:* " + ", ".join(profile["emotional_patterns"]))
    if profile.get("fears"):
        lines.append("🌑 *Fears / shadows:* " + ", ".join(profile["fears"]))
    if profile.get("themes"):
        lines.append("🌊 *Life themes:* " + ", ".join(profile["themes"]))
    if profile.get("last_updated"):
        lines.append(f"\n_Last updated: {profile['last_updated']}_")
    return "\n".join(lines)

# ─── Image generation ─────────────────────────────────────────────────────────

def generate_image_prompt(dream_text: str) -> str:
    response = client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[{
            "role": "user",
            "content": (
                "Write a short vivid visual image generation prompt (max 20 words, English only, "
                f"surreal dream-like style) based on this dream: {dream_text}"
            )
        }],
        max_tokens=60
    )
    return response.choices[0].message.content.strip()

def fetch_dream_image(prompt: str):
    encoded = quote(prompt)
    url = f"https://image.pollinations.ai/prompt/{encoded}?width=768&height=512&nologo=true"
    response = requests.get(url, timeout=30)
    return response.content if response.status_code == 200 else None

# ─── Audio transcription ──────────────────────────────────────────────────────

def transcribe_audio(file_path: str) -> str:
    with open(file_path, "rb") as f:
        result = client.audio.transcriptions.create(
            model="whisper-large-v3",
            file=f,
            response_format="text"
        )
    return result.strip()

def download_telegram_file(file_id: str, dest: str):
    info = bot.get_file(file_id)
    url  = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{info.file_path}"
    r    = requests.get(url, timeout=30)
    r.raise_for_status()
    with open(dest, "wb") as f:
        f.write(r.content)

# ─── Reminders ────────────────────────────────────────────────────────────────

def load_reminders() -> dict:
    if os.path.exists(REMINDERS_FILE):
        with open(REMINDERS_FILE, "r") as f:
            return json.load(f)
    return {}

def save_reminders(reminders: dict):
    with open(REMINDERS_FILE, "w") as f:
        json.dump(reminders, f)

def send_reminder(chat_id: int):
    bot.send_message(
        chat_id,
        "🌅 Доброе утро!\n\n"
        "Пока воспоминания о сне ещё свежи — расскажи мне его. "
        "Я готов слушать 🌙"
    )

def schedule_reminder(chat_id: int, time_str: str):
    hour, minute = map(int, time_str.split(":"))
    job_id = f"remind_{chat_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    scheduler.add_job(
        send_reminder,
        CronTrigger(hour=hour, minute=minute),
        id=job_id,
        args=[chat_id]
    )

def restore_reminders():
    """Re-schedule all reminders from disk on startup."""
    for chat_id, time_str in load_reminders().items():
        schedule_reminder(int(chat_id), time_str)
        print(f"  Restored reminder for {chat_id} at {time_str}")

# ─── Core analysis ────────────────────────────────────────────────────────────

def run_analysis(message, dream_text: str):
    chat_id = message.chat.id

    # Inject psychological profile into context so Dr. Jung "remembers"
    profile = load_profile(chat_id)
    profile_context = ""
    if any(profile.get(k) for k in ["archetypes", "recurring_symbols"]):
        profile_context = (
            f"\n\n[User's psychological profile so far: {json.dumps(profile, ensure_ascii=False)}]"
        )

    conversation_history[chat_id].append({"role": "user", "content": dream_text})
    if len(conversation_history[chat_id]) > MAX_HISTORY:
        conversation_history[chat_id] = conversation_history[chat_id][-MAX_HISTORY:]

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT + profile_context}
    ] + conversation_history[chat_id]

    response = client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=messages,
        max_tokens=1000
    )
    analysis = response.choices[0].message.content
    conversation_history[chat_id].append({"role": "assistant", "content": analysis})

    # Send analysis (Dr. Jung includes a follow-up question automatically)
    bot.reply_to(message, analysis, parse_mode="Markdown")

    # Save dream & analysis, remember state
    add_to_journal(chat_id, dream_text, analysis)
    last_dream[chat_id]    = dream_text
    last_analysis[chat_id] = analysis
    user_state[chat_id]    = "followup"

    # Background: update profile from this dream alone
    update_profile(chat_id, dream_text, analysis)

    # Generate image
    try:
        prompt   = generate_image_prompt(dream_text)
        img_data = fetch_dream_image(prompt)
        if img_data:
            bot.send_photo(chat_id, img_data, caption=f"🎨 _{prompt}_", parse_mode="Markdown")
    except Exception as e:
        print(f"Image error: {e}")


def handle_followup_answer(message):
    """
    User answered Dr. Jung's follow-up question.
    Do a deeper reflection and update the profile with the new insight.
    """
    chat_id = message.chat.id
    answer  = message.text

    conversation_history[chat_id].append({"role": "user", "content": answer})

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT}
    ] + conversation_history[chat_id]

    response = client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=messages,
        max_tokens=700
    )
    deeper = response.choices[0].message.content
    conversation_history[chat_id].append({"role": "assistant", "content": deeper})

    bot.reply_to(message, deeper, parse_mode="Markdown")

    # Now update profile with richer data (dream + answer)
    update_profile(
        chat_id,
        last_dream.get(chat_id, ""),
        last_analysis.get(chat_id, ""),
        followup_answer=answer
    )

    # Back to idle — next message is a new dream
    user_state[chat_id] = "idle"
    bot.send_message(
        chat_id,
        "✨ _Profile updated. Tell me your next dream whenever you're ready._",
        parse_mode="Markdown"
    )

# ─── Commands ─────────────────────────────────────────────────────────────────

@bot.message_handler(commands=["start", "reset"])
def cmd_start(message):
    conversation_history[message.chat.id].clear()
    user_state[message.chat.id] = "idle"
    bot.reply_to(
        message,
        "🌙 *Dr. Jung at your service.*\n\n"
        "Tell me your dream — in text or voice.\n\n"
        "Commands:\n"
        "/profile — your Jungian psychological profile\n"
        "/remind 07:30 — daily morning reminder\n"
        "/remind off — cancel reminder\n"
        "/journal — last 10 saved dreams\n"
        "/clearjournal — delete all saved dreams\n"
        "/history — session summary",
        parse_mode="Markdown"
    )

@bot.message_handler(commands=["remind"])
def cmd_remind(message):
    chat_id = message.chat.id
    parts   = message.text.strip().split()

    if len(parts) < 2:
        bot.reply_to(message, "Usage: /remind 08:00  or  /remind off")
        return

    arg = parts[1].lower()

    if arg == "off":
        job_id = f"remind_{chat_id}"
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
        reminders = load_reminders()
        reminders.pop(str(chat_id), None)
        save_reminders(reminders)
        bot.reply_to(message, "🔕 Reminder cancelled.")
        return

    # Validate HH:MM
    try:
        hour, minute = map(int, arg.split(":"))
        assert 0 <= hour <= 23 and 0 <= minute <= 59
    except Exception:
        bot.reply_to(message, "❌ Invalid time format. Use HH:MM (e.g. /remind 08:30)")
        return

    schedule_reminder(chat_id, arg)

    reminders = load_reminders()
    reminders[str(chat_id)] = arg
    save_reminders(reminders)

    bot.reply_to(message, f"⏰ Reminder set for *{arg}* every morning.", parse_mode="Markdown")

@bot.message_handler(commands=["profile"])
def cmd_profile(message):
    profile = load_profile(message.chat.id)
    bot.reply_to(message, format_profile(profile), parse_mode="Markdown")

@bot.message_handler(commands=["history"])
def cmd_history(message):
    chat_id = message.chat.id
    history = conversation_history.get(chat_id, [])
    dreams  = [m["content"] for m in history if m["role"] == "user"]
    if not dreams:
        bot.reply_to(message, "No dreams in this session yet.")
        return
    resp = client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[{"role": "user", "content":
            "Summarize these dreams in terms of key Jungian archetypes and recurring themes:\n"
            + "\n".join(f"- {d}" for d in dreams)
        }],
        max_tokens=500
    )
    summary = resp.choices[0].message.content
    text = "📖 *Session Dreams:*\n" + "\n".join(f"{i+1}. {d}" for i, d in enumerate(dreams))
    text += f"\n\n🔮 *Jungian Summary:*\n{summary}"
    bot.reply_to(message, text, parse_mode="Markdown")

@bot.message_handler(commands=["journal"])
def cmd_journal(message):
    entries = load_journal().get(str(message.chat.id), [])
    if not entries:
        bot.reply_to(message, "Your journal is empty.")
        return
    text = "📔 *Last 10 saved dreams:*\n\n"
    for i, e in enumerate(entries[-10:], 1):
        text += f"*{i}. {e['date']}*\n_{e['dream'][:120]}..._\n\n"
    bot.reply_to(message, text, parse_mode="Markdown")

@bot.message_handler(commands=["clearjournal"])
def cmd_clearjournal(message):
    journal = load_journal()
    journal[str(message.chat.id)] = []
    save_journal(journal)
    bot.reply_to(message, "🗑️ Journal cleared.")

# ─── Text handler ─────────────────────────────────────────────────────────────

@bot.message_handler(content_types=["text"])
def handle_text(message):
    if user_state[message.chat.id] == "followup":
        handle_followup_answer(message)
    else:
        run_analysis(message, message.text)

# ─── Voice & audio ────────────────────────────────────────────────────────────

@bot.message_handler(content_types=["voice"])
def handle_voice(message):
    chat_id  = message.chat.id
    tmp_path = f"/tmp/voice_{chat_id}.ogg"
    try:
        bot.send_chat_action(chat_id, "typing")
        download_telegram_file(message.voice.file_id, tmp_path)
        bot.send_message(chat_id, "🎙️ Transcribing...")
        dream_text = transcribe_audio(tmp_path)
        bot.send_message(chat_id, f"📝 *Transcribed:* _{dream_text}_", parse_mode="Markdown")

        if user_state[chat_id] == "followup":
            message.text = dream_text
            handle_followup_answer(message)
        else:
            run_analysis(message, dream_text)

    except Exception as e:
        bot.reply_to(message, f"❌ Voice error: {e}")
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

@bot.message_handler(content_types=["audio"])
def handle_audio(message):
    chat_id = message.chat.id
    ext     = "mp3"
    if message.audio.mime_type:
        ext = message.audio.mime_type.split("/")[-1].replace("mpeg", "mp3")
    tmp_path = f"/tmp/audio_{chat_id}.{ext}"
    try:
        bot.send_chat_action(chat_id, "typing")
        download_telegram_file(message.audio.file_id, tmp_path)
        bot.send_message(chat_id, "🎵 Transcribing audio...")
        dream_text = transcribe_audio(tmp_path)
        bot.send_message(chat_id, f"📝 *Transcribed:* _{dream_text}_", parse_mode="Markdown")
        run_analysis(message, dream_text)
    except Exception as e:
        bot.reply_to(message, f"❌ Audio error: {e}")
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

# ─── Run ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("🌙 Restoring reminders...")
    restore_reminders()
    print("🌙 Dr. Jung is ready.")
    bot.infinity_polling()