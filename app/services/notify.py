from datetime import date


def fmt_money(amount: int) -> str:
    """Format rubles with a space as the thousands separator."""
    return f"{amount:,} ₽".replace(",", " ")


_RU_MONTHS = (
    "январь",
    "февраль",
    "март",
    "апрель",
    "май",
    "июнь",
    "июль",
    "август",
    "сентябрь",
    "октябрь",
    "ноябрь",
    "декабрь",
)


def _month_label(month: date) -> str:
    return f"{_RU_MONTHS[month.month - 1].capitalize()} {month.year}"


def render_monthly_summary(summary: dict) -> str:
    """Build the /stats message from a monthly_summary result."""
    lines = [f"📊 {_month_label(summary['month'])}"]
    lines.append("")
    lines.append(f"💸 Оплачено за месяц: {fmt_money(summary['total_paid'])}")
    lines.append(
        f"• серверы и подписки: {summary['recurring_count']} · "
        f"{fmt_money(summary['recurring_sum'])}"
    )
    lines.append(
        f"• разовые: {summary['one_time_count']} · "
        f"{fmt_money(summary['one_time_sum'])}"
    )
    if summary["top_categories"]:
        lines.append("")
        lines.append("🏷 Топ категорий:")
        for item in summary["top_categories"]:
            lines.append(f"• {item['category']} — {fmt_money(item['sum'])}")
    lines.append("")
    lines.append(f"📉 Прошлый месяц: {fmt_money(summary['prev_month_total'])}")
    if summary["due_remaining_count"]:
        lines.append(
            f"⏳ До конца месяца предстоит: {summary['due_remaining_count']} · "
            f"{fmt_money(summary['due_remaining_sum'])}"
        )
    return "\n".join(lines).rstrip()


def render_history(rows: list[dict], limit: int) -> str:
    """Build the /history message from payment rows."""
    lines = [f"📜 История (последние {limit})"]
    if not rows:
        lines.append("—")
    else:
        for row in rows:
            parts = [
                row["paid_date"].strftime("%d.%m.%Y"),
                row["name"],
                fmt_money(row["amount_rub"]),
            ]
            if row.get("paid_by"):
                parts.append(row["paid_by"])
            if row.get("notes"):
                parts.append(row["notes"])
            lines.append(" · ".join(parts))
    return "\n".join(lines).rstrip()


_TYPE_LABELS = {"server": "Сервер", "subscription": "Подписка", "expense": "Расход"}


def render_search(query: str, results: list[dict]) -> str:
    """Build the /find message from search_owned results."""
    lines = [f"🔍 Поиск: {query}"]
    if not results:
        lines.append("Ничего не найдено")
    else:
        for row in results:
            label = _TYPE_LABELS[row["target_type"]]
            if row["target_type"] == "expense":
                due = row["due"].strftime("%d.%m.%Y")
            else:
                due = f"до {row['due']:%d.%m.%Y}"
            lines.append(
                f"• {label} · {row['name']} · {fmt_money(row['amount_rub'])} · {due}"
            )
    return "\n".join(lines).rstrip()


def _render_section(lines: list[str], emoji: str, title: str, entries: list[dict]) -> None:
    lines.append(f"{emoji} {title}")
    if not entries:
        lines.append("—")
    else:
        for entry in entries:
            due = entry["next_due"].strftime("%d.%m.%Y")
            lines.append(f"• {entry['name']} — {fmt_money(entry['amount_rub'])} — {due}")
    lines.append("")


def render_entries(title: str, entries: list[dict], empty_text: str) -> str:
    """Render a titled list of entries as «name — amount — due» lines."""
    lines = [title, ""]
    if not entries:
        lines.append(empty_text)
    else:
        for entry in entries:
            due = entry["next_due"].strftime("%d.%m.%Y")
            lines.append(
                f"• {entry['name']} — {fmt_money(entry['amount_rub'])} — до {due}"
            )
    return "\n".join(lines).rstrip()


def render_digest(digest: dict, soon_days: int | None = None) -> str:
    """Build plain-text digest message from a collect_digest result."""
    lines: list[str] = []
    soon_title = f"Ближайшие {soon_days} дней" if soon_days else "Ближайшие"
    _render_section(lines, "💳", "Сегодня", digest["due_today"])
    _render_section(lines, "📅", soon_title, digest["due_soon"])
    _render_section(lines, "⚠️", "Просрочено", digest["overdue"])
    lines.append(
        f"Итого: 💳 {fmt_money(digest['total_today'])} · "
        f"📅 {fmt_money(digest['total_soon'])} · "
        f"⚠️ {fmt_money(digest['total_overdue'])}"
    )
    return "\n".join(lines).rstrip()