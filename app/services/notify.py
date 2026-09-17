def fmt_money(amount: int) -> str:
    """Format rubles with a space as the thousands separator."""
    return f"{amount:,} ₽".replace(",", " ")


def _render_section(lines: list[str], emoji: str, title: str, entries: list[dict]) -> None:
    lines.append(f"{emoji} {title}")
    if not entries:
        lines.append("—")
    else:
        for entry in entries:
            due = entry["next_due"].strftime("%d.%m.%Y")
            lines.append(f"• {entry['name']} — {fmt_money(entry['amount_rub'])} — {due}")
    lines.append("")


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