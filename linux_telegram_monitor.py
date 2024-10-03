import asyncio
import csv
import datetime
import os
import socket
from io import StringIO
from typing import List
from typing import NamedTuple

import dotenv
import psutil
from async_tail import atail
from telegram import Update, Bot
from telegram.ext import Application, ContextTypes, CommandHandler, CallbackContext

dotenv.load_dotenv()


CHECK_EVERY_SEC = float(os.environ.get("CHECK_EVERY_SEC", 5))
CPU_USAGE_PERC_THRESHOLD = float(os.environ.get("CPU_USAGE_PERC_THRESHOLD", 80))
MEM_USAGE_PERC_THRESHOLD = float(os.environ.get("MEM_USAGE_PERC_THRESHOLD", 80))
TAIL_LOG_FILES = os.environ.get("TAIL_LOG_FILES", "")

TELEGRAM_BOT_TOKEN = str(os.environ.get("TELEGRAM_BOT_TOKEN"))
TELEGRAM_BOT_CHAT_ID = int(os.environ.get("TELEGRAM_BOT_CHAT_ID") or 0)


class ProcInfo(NamedTuple):
    name: str
    cmdline: str
    cpu_percent: float
    mem_percent: float
    mem_used: float


async def read_proc_info() -> List[ProcInfo]:
    proc: psutil.Process
    res = []
    processes = list(psutil.process_iter())
    for proc in processes:
        try:
            proc.cpu_percent(interval=None)
        except psutil.NoSuchProcess:
            continue
    await asyncio.sleep(1)

    for proc in processes:
        try:
            res.append(
                ProcInfo(
                    name=proc.name(),
                    cmdline=" ".join(proc.cmdline()),
                    cpu_percent=round(proc.cpu_percent(interval=None), 2),
                    mem_percent=round(proc.memory_percent(), 2),
                    mem_used=round(proc.memory_info().rss / 1024 / 1024, 2),
                )
            )
        except psutil.NoSuchProcess:
            continue
    return res


async def report_cpu_mem_usage(bot: Bot, title: str = "CPU and Memory Usage"):
    cpu_percent: List[float] = psutil.cpu_percent(interval=None, percpu=True)
    cpu_count = psutil.cpu_count()
    cpu_percent_avg = sum(cpu_percent) / cpu_count
    virtual_mem = psutil.virtual_memory()
    msg = (
        f"{title}\n"
        f"System time: {datetime.datetime.now().isoformat()}\n"
        f"CPU Usage: {cpu_percent} ({round(cpu_percent_avg)}%)\n"
        f"Mem Usage: {round(virtual_mem.used / 1024 / 1024)}MB "
        f"of {round(virtual_mem.total / 1024 / 1024)}MB ({round(virtual_mem.percent)}%)\n"
    )
    await bot.send_message(TELEGRAM_BOT_CHAT_ID, msg)

    proc_info = await read_proc_info()

    def render_csv(proc_infos: List[ProcInfo]) -> StringIO:
        res = StringIO()
        csv_writer = csv.writer(res, dialect="excel")
        csv_writer.writerow(
            ("Name", "CPU Usage, %", "Mem Usage, %", "Mem Usage, MB", "Command Line")
        )
        csv_writer.writerows(
            (
                (p.name, p.cpu_percent, p.mem_percent, p.mem_used, p.cmdline)
                for p in proc_infos
            )
        )
        res.seek(0)
        return res

    await bot.send_document(
        TELEGRAM_BOT_CHAT_ID,
        document=render_csv(
            sorted(proc_info, key=lambda p: p.cpu_percent, reverse=True)[:20]
        ),
        filename=f"top_cpu_usage_processes_{datetime.datetime.now().strftime('%Y-%m-%d-%H-%M-%S')}.csv",
    )
    await bot.send_document(
        TELEGRAM_BOT_CHAT_ID,
        document=render_csv(
            sorted(proc_info, key=lambda p: p.mem_percent, reverse=True)[:20]
        ),
        filename=f"top_mem_usage_processes_{datetime.datetime.now().strftime('%Y-%m-%d-%H-%M-%S')}.csv",
    )


async def check_cpu_mem_usage_thresholds(context: CallbackContext):
    cpu_number = psutil.cpu_count()
    cpu_percent: List[float] = psutil.cpu_percent(interval=None, percpu=True)
    virtual_mem = psutil.virtual_memory()
    if (
        sum(cpu_percent) / cpu_number > CPU_USAGE_PERC_THRESHOLD
        or virtual_mem.percent > MEM_USAGE_PERC_THRESHOLD
    ):
        await report_cpu_mem_usage(
            context.bot, title="🚨 CPU or Memory Usage Threshold Exceeded 🚨"
        )


async def tg_cmd_handler_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    chat_id = update.message.chat_id
    if chat_id != TELEGRAM_BOT_CHAT_ID:
        await update.message.reply_text(f"Your chat ID: {chat_id}")
    else:
        await update.message.reply_text(
            f"Linux Monitoring Service: {socket.gethostname()}\n"
            f"CPU Usage Threshold: {CPU_USAGE_PERC_THRESHOLD}\n"
            f"Mem Usage Threshold: {MEM_USAGE_PERC_THRESHOLD}"
        )


async def tg_cmd_handler_cpu_mem_usage(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if update.message.chat_id == TELEGRAM_BOT_CHAT_ID:
        await report_cpu_mem_usage(context.bot)


async def tail_f(tg_app: Application):
    paths = TAIL_LOG_FILES.split(";")
    print(f"Monitoring log file(s): {paths}")
    async for line, fn in atail(*paths):
        if tg_app.running:
            await tg_app.bot.send_message(TELEGRAM_BOT_CHAT_ID, f"{fn}\n{line}")


def run():
    print("Started simple linux -> telegram monitoring service.")
    print(f"CPU Usage Threshold: {CPU_USAGE_PERC_THRESHOLD}")
    print(f"Mem Usage Threshold: {MEM_USAGE_PERC_THRESHOLD}")
    print(f"Check interval in seconds: {CHECK_EVERY_SEC}")

    if not TELEGRAM_BOT_TOKEN:
        print(
            f"TELEGRAM_BOT_TOKEN is not set. "
            f"Please setup a Telegram bot with @BotFather, set the env var and restart this service."
        )
        exit(1)
    if not TELEGRAM_BOT_CHAT_ID:
        print(
            f"TELEGRAM_BOT_CHAT_ID is not set. After creating a new bot, contact it and type /start.\n"
            f"It will respond with your chat id. Please fill it in the config and restart this service."
        )

    # Init cpu_percent() to call it in non-blocking way next time
    psutil.cpu_percent(interval=None, percpu=True)

    tg_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    tg_app.add_handler(CommandHandler("start", tg_cmd_handler_start))
    tg_app.add_handler(CommandHandler("cpu_mem_usage", tg_cmd_handler_cpu_mem_usage))
    if TELEGRAM_BOT_CHAT_ID:
        tg_app.job_queue.run_repeating(
            check_cpu_mem_usage_thresholds, interval=CHECK_EVERY_SEC
        )

        if TAIL_LOG_FILES:
            asyncio.ensure_future(tail_f(tg_app))

    tg_app.run_polling()


if __name__ == "__main__":
    run()
