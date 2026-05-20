# P.S Telegram Productivity Bot

P.S is a Telegram productivity bot built with Python, python-telegram-bot, SQLite, APScheduler, and python-dotenv.

## Features

- Task management with add, list, complete, and delete commands.
- Assignment tracking with due dates and reminder notifications.
- Daily reminders at 9 PM and 10 PM.
- Daily productivity score with Excellent, Satisfied, Average, and Unsatisfied ratings.
- Weekly review every Sunday.

## Setup

1. Create and activate a virtual environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Create a `.env` file from `.env.example` and set your Telegram bot token.

4. Run the bot:

```bash
python main.py
```

## Environment Variables

- `BOT_TOKEN` - Telegram bot token from BotFather.
- `BOT_NAME` - display name for the bot.
- `BOT_USERNAME` - Telegram username, for example `arinnn_bot`.
- `DATABASE_PATH` - SQLite database file path.
- `BOT_TIMEZONE` - timezone for reminders, for example `UTC` or `Asia/Jakarta`.
- `DAILY_TODO_HOUR` - hour for the 9 PM reminder.
- `DAILY_PROGRESS_HOUR` - hour for the 10 PM reminder.
- `ASSIGNMENT_REMINDER_HOUR` - hour for assignment reminders.
- `WEEKLY_REVIEW_HOUR` - hour for the Sunday review.
- `WEEKLY_REVIEW_DAY` - day of week for the weekly review, default `sun`.

## Commands

### Tasks

- `/add Finish report` - add a task.
- `/add Finish report | 2026-05-21` - add a task with an optional due date.
- `/tasks` - list open tasks.
- `/done 3` - mark task 3 as done.
- `/delete 3` - delete task 3.

### Assignments

- `/assign Math homework | 2026-05-25` - add an assignment with a due date.
- `/assignments` - list open assignments.
- `/complete_assignment 2` - mark assignment 2 as complete.

### Daily check-ins

- `/tomorrow call dentist, prepare slides` - save tomorrow's to-do list.
- `/progress finished report, started project draft` - save today's progress update.
- `/score` - view today's productivity score.
- `/review` - generate the weekly review manually.

## Scoring

The productivity score is based on the tracked items for the day. The bot calculates a completion percentage and maps it to one of four labels:

- Excellent - 85% or higher
- Satisfied - 65% to 84%
- Average - 40% to 64%
- Unsatisfied - below 40%

## Scheduling

The bot uses APScheduler to send scheduled reminders in the timezone defined by BOT_TIMEZONE.

- 9 PM - ask for tomorrow's to-do list.
- 10 PM - ask for today's progress update and store the daily score.
- Daily assignment reminders - prompt for assignments due today or tomorrow.
- Sunday - send a weekly review summary.

## Notes

- SQLite data is stored in bot.db by default.
- The bot starts recording users after they send a command.
- If you want reminder times in your local timezone, set BOT_TIMEZONE accordingly.
