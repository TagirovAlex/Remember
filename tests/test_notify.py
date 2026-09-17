from datetime import date

from app.services.notify import fmt_money, render_digest


def test_fmt_money():
    assert fmt_money(0) == "0 ₽"
    assert fmt_money(12000) == "12 000 ₽"
    assert fmt_money(12400) == "12 400 ₽"
    assert fmt_money(1234567) == "1 234 567 ₽"


def test_render_digest_empty():
    digest = {
        "due_today": [],
        "due_soon": [],
        "overdue": [],
        "total_today": 0,
        "total_soon": 0,
        "total_overdue": 0,
    }
    text = render_digest(digest, soon_days=7)
    assert "💳 Сегодня" in text
    assert "📅 Ближайшие 7 дней" in text
    assert "⚠️ Просрочено" in text
    assert "—" in text
    assert "Итого:" in text


def test_render_digest_with_entries():
    digest = {
        "due_today": [
            {"name": "gpu-a", "kind": "server", "amount_rub": 12000, "next_due": date(2026, 9, 16)},
        ],
        "due_soon": [
            {"name": "netflix", "kind": "subscription", "amount_rub": 900, "next_due": date(2026, 9, 20)},
        ],
        "overdue": [
            {"name": "gpu-overdue", "kind": "server", "amount_rub": 3000, "next_due": date(2026, 9, 1)},
        ],
        "total_today": 12000,
        "total_soon": 900,
        "total_overdue": 3000,
    }
    text = render_digest(digest, soon_days=7)
    assert "gpu-a" in text
    assert "12 000 ₽" in text
    assert "16.09.2026" in text
    assert "netflix" in text
    assert "900 ₽" in text
    assert "20.09.2026" in text
    assert "gpu-overdue" in text
    assert "01.09.2026" in text
    assert "Итого: 💳 12 000 ₽ · 📅 900 ₽ · ⚠️ 3 000 ₽" in text


def test_render_digest_without_soon_days():
    digest = {
        "due_today": [],
        "due_soon": [],
        "overdue": [],
        "total_today": 0,
        "total_soon": 0,
        "total_overdue": 0,
    }
    assert "📅 Ближайшие" in render_digest(digest)
    assert "📅 Ближайшие 7 дней" not in render_digest(digest)