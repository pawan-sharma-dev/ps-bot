from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BOT_NAME = os.getenv("BOT_NAME", "P.S").strip() or "P.S"
BOT_USERNAME = os.getenv("BOT_USERNAME", "arinnn_bot").strip() or "arinnn_bot"
DATABASE_PATH = os.getenv("DATABASE_PATH", "bot.db").strip() or "bot.db"
TIMEZONE_NAME = os.getenv("BOT_TIMEZONE", "UTC").strip() or "UTC"
DAILY_TODO_HOUR = int(os.getenv("DAILY_TODO_HOUR", "21"))
DAILY_PROGRESS_HOUR = int(os.getenv("DAILY_PROGRESS_HOUR", "22"))
ASSIGNMENT_REMINDER_HOUR = int(os.getenv("ASSIGNMENT_REMINDER_HOUR", "8"))
WEEKLY_REVIEW_HOUR = int(os.getenv("WEEKLY_REVIEW_HOUR", "20"))
WEEKLY_REVIEW_DAY = os.getenv("WEEKLY_REVIEW_DAY", "sun").strip().lower() or "sun"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

DB_LOCK = threading.Lock()
TZ = ZoneInfo(TIMEZONE_NAME)


def now_local() -> datetime:
    return datetime.now(TZ)


def today_local() -> date:
    return now_local().date()


def now_text() -> str:
    return now_local().strftime("%Y-%m-%d %H:%M:%S")


def today_text() -> str:
    return today_local().isoformat()


@contextmanager
def db_connection():
    with DB_LOCK:
        connection = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()


def init_db() -> None:
    with db_connection() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER PRIMARY KEY,
                username TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                description TEXT NOT NULL,
                due_date TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL,
                completed_at TEXT,
                deleted_at TEXT
            );

            CREATE TABLE IF NOT EXISTS assignments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                due_date TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL,
                completed_at TEXT,
                deleted_at TEXT
            );

            CREATE TABLE IF NOT EXISTS progress_updates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                update_date TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tomorrow_lists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                list_date TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS daily_scores (
                chat_id INTEGER NOT NULL,
                score_date TEXT NOT NULL,
                completed_count INTEGER NOT NULL,
                total_count INTEGER NOT NULL,
                score_percent INTEGER NOT NULL,
                rating TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (chat_id, score_date)
            );

            CREATE TABLE IF NOT EXISTS notification_log (
                kind TEXT NOT NULL,
                entity_key TEXT NOT NULL,
                schedule_date TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                PRIMARY KEY (kind, entity_key, schedule_date)
            );
            """
        )


def upsert_user(chat_id: int, username: str | None) -> None:
    timestamp = now_text()
    with db_connection() as connection:
        connection.execute(
            """
            INSERT INTO users (chat_id, username, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                username = excluded.username,
                last_seen_at = excluded.last_seen_at
            """,
            (chat_id, username or "", timestamp, timestamp),
        )


def fetch_all(query: str, params: tuple = ()) -> list[sqlite3.Row]:
    with db_connection() as connection:
        return list(connection.execute(query, params).fetchall())


def fetch_one(query: str, params: tuple = ()) -> sqlite3.Row | None:
    with db_connection() as connection:
        return connection.execute(query, params).fetchone()


def execute(query: str, params: tuple = ()) -> int:
    with db_connection() as connection:
        cursor = connection.execute(query, params)
        return cursor.rowcount


def parse_date(value: str) -> date | None:
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_command_payload(text: str) -> str:
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        return ""
    return parts[1].strip()


def parse_description_and_due_date(payload: str, require_due_date: bool = False) -> tuple[str, str | None, str | None]:
    if not payload:
        return "", None, ""

    description_part, separator, due_part = payload.partition("|")
    description = description_part.strip()
    due_text = due_part.strip() if separator else ""

    if not description:
        return "", None, "Description is required."

    if separator:
        due_value = parse_date(due_text)
        if due_value is None:
            return "", None, "Due date must use YYYY-MM-DD."
        return description, due_value.isoformat(), None

    if require_due_date:
        return "", None, "Due date is required."

    return description, None, None


def parse_ids(payload: str) -> list[int]:
    if not payload:
        return []
    tokens = [token for token in re.split(r"[\s,]+", payload.strip()) if token]
    identifiers: list[int] = []
    for token in tokens:
        if token.isdigit():
            identifiers.append(int(token))
    return identifiers


def format_due_date(value: str | None) -> str:
    if not value:
        return "no due date"
    return value


def score_label(score_percent: int | None) -> str:
    if score_percent is None:
        return "No tracked items today"
    if score_percent >= 85:
        return "Excellent"
    if score_percent >= 65:
        return "Satisfied"
    if score_percent >= 40:
        return "Average"
    return "Unsatisfied"


def task_score_rows(chat_id: int, score_date: str) -> list[sqlite3.Row]:
    return fetch_all(
        """
        SELECT id, status
        FROM tasks
        WHERE chat_id = ?
          AND deleted_at IS NULL
          AND (
                substr(created_at, 1, 10) = ?
                OR substr(completed_at, 1, 10) = ?
                OR due_date = ?
          )
        """,
        (chat_id, score_date, score_date, score_date),
    )


def assignment_score_rows(chat_id: int, score_date: str) -> list[sqlite3.Row]:
    return fetch_all(
        """
        SELECT id, status
        FROM assignments
        WHERE chat_id = ?
          AND deleted_at IS NULL
          AND (
                substr(created_at, 1, 10) = ?
                OR substr(completed_at, 1, 10) = ?
                OR due_date = ?
          )
        """,
        (chat_id, score_date, score_date, score_date),
    )


def calculate_productivity(chat_id: int, score_date: str | None = None) -> tuple[int, int, int | None, str]:
    target_date = score_date or today_text()
    rows = task_score_rows(chat_id, target_date) + assignment_score_rows(chat_id, target_date)
    total_count = len(rows)
    completed_count = sum(1 for row in rows if row["status"] == "done")

    if total_count == 0:
        return 0, 0, None, "No tracked items today"

    score_percent = round((completed_count / total_count) * 100)
    return completed_count, total_count, score_percent, score_label(score_percent)


def store_daily_score(chat_id: int, score_date: str, completed_count: int, total_count: int, score_percent: int, rating: str) -> None:
    with db_connection() as connection:
        connection.execute(
            """
            INSERT INTO daily_scores (chat_id, score_date, completed_count, total_count, score_percent, rating, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, score_date) DO UPDATE SET
                completed_count = excluded.completed_count,
                total_count = excluded.total_count,
                score_percent = excluded.score_percent,
                rating = excluded.rating,
                created_at = excluded.created_at
            """,
            (chat_id, score_date, completed_count, total_count, score_percent, rating, now_text()),
        )


def log_notification(kind: str, entity_key: str, schedule_date: str) -> bool:
    with db_connection() as connection:
        try:
            connection.execute(
                """
                INSERT INTO notification_log (kind, entity_key, schedule_date, sent_at)
                VALUES (?, ?, ?, ?)
                """,
                (kind, entity_key, schedule_date, now_text()),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def create_task(chat_id: int, description: str, due_date: str | None = None) -> int:
    with db_connection() as connection:
        cursor = connection.execute(
            """
            INSERT INTO tasks (chat_id, description, due_date, status, created_at)
            VALUES (?, ?, ?, 'open', ?)
            """,
            (chat_id, description, due_date, now_text()),
        )
        return int(cursor.lastrowid)


def create_assignment(chat_id: int, title: str, due_date: str) -> int:
    with db_connection() as connection:
        cursor = connection.execute(
            """
            INSERT INTO assignments (chat_id, title, due_date, status, created_at)
            VALUES (?, ?, ?, 'open', ?)
            """,
            (chat_id, title, due_date, now_text()),
        )
        return int(cursor.lastrowid)


def build_weekly_review(chat_id: int) -> str:
    end_day = today_local()
    start_day = end_day - timedelta(days=6)
    start_text = start_day.isoformat()
    end_text = end_day.isoformat()

    completed_tasks = fetch_one(
        """
        SELECT COUNT(*) AS count
        FROM tasks
        WHERE chat_id = ?
          AND deleted_at IS NULL
          AND status = 'done'
          AND substr(completed_at, 1, 10) BETWEEN ? AND ?
        """,
        (chat_id, start_text, end_text),
    )["count"]

    completed_assignments = fetch_one(
        """
        SELECT COUNT(*) AS count
        FROM assignments
        WHERE chat_id = ?
          AND deleted_at IS NULL
          AND status = 'done'
          AND substr(completed_at, 1, 10) BETWEEN ? AND ?
        """,
        (chat_id, start_text, end_text),
    )["count"]

    open_tasks = fetch_one(
        """
        SELECT COUNT(*) AS count
        FROM tasks
        WHERE chat_id = ? AND deleted_at IS NULL AND status = 'open'
        """,
        (chat_id,),
    )["count"]

    open_assignments = fetch_one(
        """
        SELECT COUNT(*) AS count
        FROM assignments
        WHERE chat_id = ? AND deleted_at IS NULL AND status = 'open'
        """,
        (chat_id,),
    )["count"]

    score_rows = fetch_all(
        """
        SELECT score_date, score_percent, rating
        FROM daily_scores
        WHERE chat_id = ? AND score_date BETWEEN ? AND ?
        ORDER BY score_date
        """,
        (chat_id, start_text, end_text),
    )

    average_score = round(sum(row["score_percent"] for row in score_rows) / len(score_rows)) if score_rows else 0

    lines = [
        f"Weekly review for {start_text} to {end_text}",
        f"Completed tasks: {completed_tasks}",
        f"Completed assignments: {completed_assignments}",
        f"Open tasks: {open_tasks}",
        f"Open assignments: {open_assignments}",
        f"Average recorded daily score: {average_score}%",
    ]

    if score_rows:
        lines.append("Daily scores:")
        for row in score_rows:
            lines.append(f"{row['score_date']}: {row['score_percent']}% - {row['rating']}")

    return "\n".join(lines)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_user or not update.message:
        return
    upsert_user(update.effective_chat.id, update.effective_user.username)
    await update.message.reply_text(
        f"{BOT_NAME} is ready.\n\n"
        f"Telegram username: @{BOT_USERNAME}\n\n"
        "Commands:\n"
        "/add task or /add task | YYYY-MM-DD\n"
        "/tasks\n"
        "/done id\n"
        "/delete id\n"
        "/assign assignment | YYYY-MM-DD\n"
        "/assignments\n"
        "/complete_assignment id\n"
        "/tomorrow your plan for tomorrow\n"
        "/progress today's update\n"
        "/score\n"
        "/review"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start_command(update, context)


async def add_task_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_user or not update.message:
        return
    upsert_user(update.effective_chat.id, update.effective_user.username)
    payload = parse_command_payload(update.message.text or "")
    description, due_date, error = parse_description_and_due_date(payload)
    if error is not None:
        await update.message.reply_text("Usage: /add task description or /add task description | YYYY-MM-DD")
        return

    task_id = create_task(update.effective_chat.id, description, due_date)
    response = f"Added task #{task_id}: {description}"
    if due_date:
        response += f" (due {due_date})"
    await update.message.reply_text(response)


async def list_tasks_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_user or not update.message:
        return
    upsert_user(update.effective_chat.id, update.effective_user.username)
    rows = fetch_all(
        """
        SELECT id, description, due_date
        FROM tasks
        WHERE chat_id = ? AND status = 'open' AND deleted_at IS NULL
        ORDER BY CASE WHEN due_date IS NULL THEN 1 ELSE 0 END, due_date, id DESC
        """,
        (update.effective_chat.id,),
    )
    if not rows:
        await update.message.reply_text("No open tasks right now.")
        return

    lines = ["Open tasks:"]
    for row in rows:
        lines.append(f"#{row['id']} - {row['description']} | due {format_due_date(row['due_date'])}")
    await update.message.reply_text("\n".join(lines))


async def complete_task_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_user or not update.message:
        return
    upsert_user(update.effective_chat.id, update.effective_user.username)
    payload = parse_command_payload(update.message.text or "")
    task_ids = parse_ids(payload)
    if not task_ids:
        await update.message.reply_text("Usage: /done 1 or /done 1,2,3")
        return

    completed: list[str] = []
    skipped: list[str] = []
    for task_id in task_ids:
        rowcount = execute(
            """
            UPDATE tasks
            SET status = 'done', completed_at = ?
            WHERE id = ? AND chat_id = ? AND deleted_at IS NULL AND status = 'open'
            """,
            (now_text(), task_id, update.effective_chat.id),
        )
        if rowcount:
            completed.append(str(task_id))
        else:
            skipped.append(str(task_id))

    response = []
    if completed:
        response.append(f"Completed tasks: {', '.join(completed)}")
    if skipped:
        response.append(f"Not found or already done: {', '.join(skipped)}")
    await update.message.reply_text("\n".join(response))


async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_user or not update.message:
        return
    upsert_user(update.effective_chat.id, update.effective_user.username)
    payload = parse_command_payload(update.message.text or "")
    item_ids = parse_ids(payload)
    if not item_ids:
        await update.message.reply_text("Usage: /delete 1 or /delete 1,2,3")
        return

    deleted: list[str] = []
    skipped: list[str] = []

    for item_id in item_ids:
        timestamp = now_text()
        task_deleted = execute(
            """
            UPDATE tasks
            SET status = 'deleted', deleted_at = ?
            WHERE id = ? AND chat_id = ? AND deleted_at IS NULL
            """,
            (timestamp, item_id, update.effective_chat.id),
        )
        assignment_deleted = 0 if task_deleted else execute(
            """
            UPDATE assignments
            SET status = 'deleted', deleted_at = ?
            WHERE id = ? AND chat_id = ? AND deleted_at IS NULL
            """,
            (timestamp, item_id, update.effective_chat.id),
        )
        if task_deleted or assignment_deleted:
            deleted.append(str(item_id))
        else:
            skipped.append(str(item_id))

    response = []
    if deleted:
        response.append(f"Deleted items: {', '.join(deleted)}")
    if skipped:
        response.append(f"Not found: {', '.join(skipped)}")
    await update.message.reply_text("\n".join(response))


async def add_assignment_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_user or not update.message:
        return
    upsert_user(update.effective_chat.id, update.effective_user.username)
    payload = parse_command_payload(update.message.text or "")
    title, due_date, error = parse_description_and_due_date(payload, require_due_date=True)
    if error is not None or due_date is None:
        await update.message.reply_text("Usage: /assign assignment title | YYYY-MM-DD")
        return

    assignment_id = create_assignment(update.effective_chat.id, title, due_date)
    await update.message.reply_text(f"Added assignment #{assignment_id}: {title} (due {due_date})")


async def list_assignments_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_user or not update.message:
        return
    upsert_user(update.effective_chat.id, update.effective_user.username)
    rows = fetch_all(
        """
        SELECT id, title, due_date
        FROM assignments
        WHERE chat_id = ? AND status = 'open' AND deleted_at IS NULL
        ORDER BY due_date, id DESC
        """,
        (update.effective_chat.id,),
    )
    if not rows:
        await update.message.reply_text("No open assignments right now.")
        return

    lines = ["Open assignments:"]
    for row in rows:
        lines.append(f"#{row['id']} - {row['title']} | due {row['due_date']}")
    await update.message.reply_text("\n".join(lines))


async def complete_assignment_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_user or not update.message:
        return
    upsert_user(update.effective_chat.id, update.effective_user.username)
    payload = parse_command_payload(update.message.text or "")
    assignment_ids = parse_ids(payload)
    if not assignment_ids:
        await update.message.reply_text("Usage: /complete_assignment 1 or /complete_assignment 1,2,3")
        return

    completed: list[str] = []
    skipped: list[str] = []
    for assignment_id in assignment_ids:
        rowcount = execute(
            """
            UPDATE assignments
            SET status = 'done', completed_at = ?
            WHERE id = ? AND chat_id = ? AND deleted_at IS NULL AND status = 'open'
            """,
            (now_text(), assignment_id, update.effective_chat.id),
        )
        if rowcount:
            completed.append(str(assignment_id))
        else:
            skipped.append(str(assignment_id))

    response = []
    if completed:
        response.append(f"Completed assignments: {', '.join(completed)}")
    if skipped:
        response.append(f"Not found or already done: {', '.join(skipped)}")
    await update.message.reply_text("\n".join(response))


async def tomorrow_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_user or not update.message:
        return
    upsert_user(update.effective_chat.id, update.effective_user.username)
    payload = parse_command_payload(update.message.text or "")
    if not payload:
        await update.message.reply_text("Usage: /tomorrow your plan for tomorrow")
        return

    with db_connection() as connection:
        connection.execute(
            """
            INSERT INTO tomorrow_lists (chat_id, list_date, content, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (update.effective_chat.id, (today_local() + timedelta(days=1)).isoformat(), payload, now_text()),
        )

    await update.message.reply_text("Tomorrow's to-do list saved.")


async def progress_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_user or not update.message:
        return
    upsert_user(update.effective_chat.id, update.effective_user.username)
    payload = parse_command_payload(update.message.text or "")
    if not payload:
        await update.message.reply_text("Usage: /progress today's update")
        return

    with db_connection() as connection:
        connection.execute(
            """
            INSERT INTO progress_updates (chat_id, update_date, content, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (update.effective_chat.id, today_text(), payload, now_text()),
        )

    await update.message.reply_text("Progress update saved.")


async def score_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_user or not update.message:
        return
    upsert_user(update.effective_chat.id, update.effective_user.username)
    completed_count, total_count, score_percent, rating = calculate_productivity(update.effective_chat.id)
    if score_percent is None:
        await update.message.reply_text("No tracked tasks or assignments for today yet.")
        return

    store_daily_score(update.effective_chat.id, today_text(), completed_count, total_count, score_percent, rating)
    await update.message.reply_text(
        f"Today's productivity score: {score_percent}% - {rating}\n"
        f"Completed: {completed_count}/{total_count}"
    )


async def review_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_user or not update.message:
        return
    upsert_user(update.effective_chat.id, update.effective_user.username)
    await update.message.reply_text(build_weekly_review(update.effective_chat.id))


async def send_daily_todo_reminders(bot) -> None:
    reminder_date = today_text()
    rows = fetch_all("SELECT chat_id FROM users ORDER BY chat_id")
    for row in rows:
        chat_id = row["chat_id"]
        if not log_notification("daily_todo", str(chat_id), reminder_date):
            continue
        await bot.send_message(
            chat_id=chat_id,
            text=(
                "9 PM check-in: please send tomorrow's to-do list with /tomorrow.\n"
                "Example: /tomorrow finish slide deck, study 2 chapters, clean desk"
            ),
        )


async def send_daily_progress_reminders(bot) -> None:
    reminder_date = today_text()
    rows = fetch_all("SELECT chat_id FROM users ORDER BY chat_id")
    for row in rows:
        chat_id = row["chat_id"]
        if not log_notification("daily_progress", str(chat_id), reminder_date):
            continue

        completed_count, total_count, score_percent, rating = calculate_productivity(chat_id)
        if score_percent is not None:
            store_daily_score(chat_id, reminder_date, completed_count, total_count, score_percent, rating)
            score_line = f"Today's productivity score: {score_percent}% - {rating} ({completed_count}/{total_count})"
        else:
            score_line = "No tracked tasks or assignments for today yet."

        await bot.send_message(
            chat_id=chat_id,
            text=(
                "10 PM check-in: please share today's progress with /progress.\n"
                f"{score_line}"
            ),
        )


async def send_assignment_reminders(bot) -> None:
    reminder_date = today_text()
    today = today_local().isoformat()
    tomorrow = (today_local() + timedelta(days=1)).isoformat()
    rows = fetch_all(
        """
        SELECT id, chat_id, title, due_date
        FROM assignments
        WHERE status = 'open' AND deleted_at IS NULL
          AND due_date IN (?, ?)
        ORDER BY due_date, id
        """,
        (today, tomorrow),
    )

    for row in rows:
        if row["due_date"] == today:
            kind = "assignment_due_today"
            message = f"Assignment due today: #{row['id']} - {row['title']}"
        else:
            kind = "assignment_due_tomorrow"
            message = f"Assignment due tomorrow: #{row['id']} - {row['title']}"

        if not log_notification(kind, str(row["id"]), reminder_date):
            continue

        await bot.send_message(
            chat_id=row["chat_id"],
            text=message + f"\nDue date: {row['due_date']}\nMark it with /complete_assignment {row['id']} when finished.",
        )


async def send_weekly_review(bot) -> None:
    reminder_date = today_text()
    rows = fetch_all("SELECT chat_id FROM users ORDER BY chat_id")
    for row in rows:
        chat_id = row["chat_id"]
        if not log_notification("weekly_review", str(chat_id), reminder_date):
            continue
        await bot.send_message(
            chat_id=chat_id,
            text=(
                "Weekly review time.\n"
                f"{build_weekly_review(chat_id)}\n\n"
                "Reply with /review anytime to regenerate this summary."
            ),
        )


def build_scheduler(application: Application) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=TZ)
    scheduler.add_job(
        send_assignment_reminders,
        CronTrigger(hour=ASSIGNMENT_REMINDER_HOUR, minute=0, timezone=TZ),
        kwargs={"bot": application.bot},
        id="assignment_reminders",
        replace_existing=True,
    )
    scheduler.add_job(
        send_daily_todo_reminders,
        CronTrigger(hour=DAILY_TODO_HOUR, minute=0, timezone=TZ),
        kwargs={"bot": application.bot},
        id="daily_todo_reminders",
        replace_existing=True,
    )
    scheduler.add_job(
        send_daily_progress_reminders,
        CronTrigger(hour=DAILY_PROGRESS_HOUR, minute=0, timezone=TZ),
        kwargs={"bot": application.bot},
        id="daily_progress_reminders",
        replace_existing=True,
    )
    scheduler.add_job(
        send_weekly_review,
        CronTrigger(day_of_week=WEEKLY_REVIEW_DAY, hour=WEEKLY_REVIEW_HOUR, minute=0, timezone=TZ),
        kwargs={"bot": application.bot},
        id="weekly_review",
        replace_existing=True,
    )
    return scheduler


async def post_init(application: Application) -> None:
    application.bot_data["scheduler"] = build_scheduler(application)
    application.bot_data["scheduler"].start()
    logger.info("%s started in %s", BOT_NAME, TIMEZONE_NAME)


async def post_shutdown(application: Application) -> None:
    scheduler = application.bot_data.get("scheduler")
    if scheduler:
        scheduler.shutdown(wait=False)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled error: %s", context.error)


def build_application() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing. Add it to your .env file.")
    return (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )


def register_handlers(application: Application) -> None:
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("add", add_task_command))
    application.add_handler(CommandHandler("tasks", list_tasks_command))
    application.add_handler(CommandHandler("done", complete_task_command))
    application.add_handler(CommandHandler("delete", delete_command))
    application.add_handler(CommandHandler("assign", add_assignment_command))
    application.add_handler(CommandHandler("assignments", list_assignments_command))
    application.add_handler(CommandHandler("complete_assignment", complete_assignment_command))
    application.add_handler(CommandHandler("tomorrow", tomorrow_command))
    application.add_handler(CommandHandler("progress", progress_command))
    application.add_handler(CommandHandler("score", score_command))
    application.add_handler(CommandHandler("review", review_command))
    application.add_error_handler(error_handler)


def main() -> None:
    init_db()
    application = build_application()
    register_handlers(application)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
