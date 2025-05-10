import asyncio
import csv
import datetime
import os
import re
import socket
import time
from io import StringIO
from typing import List, Optional, Dict
from typing import NamedTuple

import dotenv
import psutil
from async_tail import atail
from telegram import Update, Bot
from telegram.constants import ParseMode
from telegram.ext import Application, ContextTypes, CommandHandler, CallbackContext

dotenv.load_dotenv()

TELEGRAM_BOT_TOKEN = str(os.environ.get("TELEGRAM_BOT_TOKEN"))
TELEGRAM_BOT_CHAT_ID = int(os.environ.get("TELEGRAM_BOT_CHAT_ID") or 0)
TELEGRAM_BOT_THREAD_ID = int(os.environ.get("TELEGRAM_BOT_THREAD_ID") or 0)

CHECK_EVERY_SEC = float(os.environ.get("CHECK_EVERY_SEC", 5))
CPU_USAGE_PERC_THRESHOLD = float(os.environ.get("CPU_USAGE_PERC_THRESHOLD", 80))
MEM_USAGE_PERC_THRESHOLD = float(os.environ.get("MEM_USAGE_PERC_THRESHOLD", 80))
TAIL_LOG_FILES = os.environ.get("TAIL_LOG_FILES", "")
TAIL_LOG_FILES_LINE_EXCLUDE_REGEXP = os.environ.get(
    "TAIL_LOG_FILES_LINE_EXCLUDE_REGEXP"
)


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


def fmt_bytes_speed(bytes_sec: int) -> str:
    bt = bytes_sec * 8
    if bt < 1024:
        return str(round(bt)) + "Bit/s"
    elif bt < 1024**2:
        return str(round(bt / 1024, 1)) + "Kbit/s"
    elif bt < 1024**3:
        return str(round(bt / 1024**2, 1)) + "Mbit/s"
    elif bt < 1024**4:
        return str(round(bt / 1024**3, 1)) + "Gbit/s"
    else:
        return str(round(bt / 1024**4, 1)) + "Tbit/s"


def fmt_datetime(dt: datetime.datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def fmt_mem_bytes(bt: float) -> str:
    if bt < 1024:
        return str(round(bt)) + "B"
    elif bt < 1024**2:
        return str(round(bt / 1024, 1)) + "KB"
    elif bt < 1024**3:
        return str(round(bt / 1024**2, 1)) + "MB"
    elif bt < 1024**4:
        return str(round(bt / 1024**3, 1)) + "GB"
    else:
        return str(round(bt / 1024**4, 1)) + "TB"


async def report_status(bot: Bot, title: str = "🌿 ️System Status 🌿"):
    cpu_percent: List[float] = psutil.cpu_percent(interval=None, percpu=True)
    cpu_count = psutil.cpu_count()
    cpu_percent_avg = sum(cpu_percent) / cpu_count
    virtual_mem = psutil.virtual_memory()
    users = psutil.users()

    net_counters_start: Dict[str, NamedTuple] = psutil.net_io_counters(
        pernic=True, nowrap=True
    )
    net_counters_start_time = time.time()

    # read_proc_info() makes ~1 sec pause to measure per-process cpu usage
    proc_info = await read_proc_info()

    net_counters_end: Dict[str, NamedTuple] = psutil.net_io_counters(
        pernic=True, nowrap=True
    )
    net_counters_end_time = time.time()

    msg_net_speed = render_net_counters_per_nic(
        net_counters_end,
        net_counters_end_time,
        net_counters_start,
        net_counters_start_time,
    )

    cpu_usage_str = ("\n" if len(cpu_percent) > 2 else " ") + str(
        [round(f) for f in cpu_percent]
    )
    msg_users = render_logged_in_users(users)
    msg = (
        f"<b>{title}</b>\n"
        f"\n<b>System time:</b> {fmt_datetime(datetime.datetime.now())}\n"
        f"\n<b>CPU Core Usage %</b>:{cpu_usage_str}\n"
        f"\n<b>Mem Usage</b>: {fmt_mem_bytes(virtual_mem.used)} of {fmt_mem_bytes(virtual_mem.total)}\n"
        f"\n<b>Users:</b>\n{msg_users}\n"
        f"\n<b>Network Usage:</b>\n{msg_net_speed}"
    )
    await bot.send_message(
        chat_id=TELEGRAM_BOT_CHAT_ID,
        message_thread_id=TELEGRAM_BOT_THREAD_ID,
        text=msg,
        parse_mode=ParseMode.HTML,
    )

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
        chat_id=TELEGRAM_BOT_CHAT_ID,
        message_thread_id=TELEGRAM_BOT_THREAD_ID,
        document=render_csv(
            sorted(proc_info, key=lambda p: p.cpu_percent, reverse=True)[:20]
        ),
        filename=f"top_cpu_usage_processes_{datetime.datetime.now().strftime('%Y-%m-%d-%H-%M-%S')}.csv",
    )
    await bot.send_document(
        chat_id=TELEGRAM_BOT_CHAT_ID,
        message_thread_id=TELEGRAM_BOT_THREAD_ID,
        document=render_csv(
            sorted(proc_info, key=lambda p: p.mem_percent, reverse=True)[:20]
        ),
        filename=f"top_mem_usage_processes_{datetime.datetime.now().strftime('%Y-%m-%d-%H-%M-%S')}.csv",
    )


def render_logged_in_users(users):
    icons = "🐎🐑🐗🐘🐙🐚🐛🐜🐝🐞🐟🐠🐡🐢🐦🐧🐨🐩🐫🐬🐯🐳"

    def user_icn(name: str) -> str:
        return icons[hash(name) % len(icons)]

    msg_users = "\n".join(
        f"{user_icn(u.name)} {u.name} | {u.host} | {u.terminal} | {fmt_datetime(datetime.datetime.fromtimestamp(u.started))}"
        for u in users
    )
    return msg_users


def render_net_counters_per_nic(
    net_counters_end, net_counters_end_time, net_counters_start, net_counters_start_time
):
    time_passed_sec = net_counters_end_time - net_counters_start_time
    msg_net_speed = []
    for nic_name, counters_end in net_counters_end.items():
        counters_start = net_counters_start.get(nic_name)
        if counters_start:
            recv = (
                counters_end.bytes_recv - counters_start.bytes_recv
            ) / time_passed_sec
            sent = (
                counters_end.bytes_sent - counters_start.bytes_sent
            ) / time_passed_sec
            if recv > 0 or sent > 0:
                msg_net_speed.append(
                    f"<b>{nic_name}</b>\n"
                    f"⬇ {fmt_bytes_speed(recv)}\n"
                    f"⬆ {fmt_bytes_speed(sent)}"
                )
    msg_net_speed = "\n".join(msg_net_speed)
    return msg_net_speed


async def check_cpu_mem_usage_thresholds(context: CallbackContext):
    cpu_number = psutil.cpu_count()
    cpu_percent: List[float] = psutil.cpu_percent(interval=None, percpu=True)
    virtual_mem = psutil.virtual_memory()
    if (
        sum(cpu_percent) / cpu_number > CPU_USAGE_PERC_THRESHOLD
        or virtual_mem.percent > MEM_USAGE_PERC_THRESHOLD
    ):
        await report_status(
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


async def tg_cmd_handler_status(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if update.message.chat_id == TELEGRAM_BOT_CHAT_ID:
        await report_status(context.bot)


async def tail_f(
    tg_app: Application, paths: List[str], re_exclude_line: Optional[re.Pattern] = None
):
    print(f"Monitoring log file(s): {paths}")
    if re_exclude_line:
        print(f"Excluding log lines regexp: {re_exclude_line}")
    async for line, fn in atail(*paths):
        if tg_app.running and (not re_exclude_line or not re_exclude_line.search(line)):
            await tg_app.bot.send_message(
                chat_id=TELEGRAM_BOT_CHAT_ID,
                message_thread_id=TELEGRAM_BOT_THREAD_ID,
                text=f"{fn}\n{line}",
            )


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
    tg_app.add_handler(CommandHandler("status", tg_cmd_handler_status))
    if TELEGRAM_BOT_CHAT_ID:
        tg_app.job_queue.run_repeating(
            check_cpu_mem_usage_thresholds, interval=CHECK_EVERY_SEC
        )

        if TAIL_LOG_FILES:
            paths = TAIL_LOG_FILES.split(";")
            re_exclude_line = (
                re.compile(TAIL_LOG_FILES_LINE_EXCLUDE_REGEXP)
                if TAIL_LOG_FILES_LINE_EXCLUDE_REGEXP
                else None
            )
            asyncio.ensure_future(
                tail_f(tg_app, paths=paths, re_exclude_line=re_exclude_line)
            )

    tg_app.run_polling()


if __name__ == "__main__":
    run()
