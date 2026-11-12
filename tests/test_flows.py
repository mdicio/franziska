"""The few end-to-end flows (CLAUDE.md): Telegram → bot → agent → stores → jobs → buttons, on scripted models.

No local model, no network: the bot is the real one (evals/week.py's in-process World), the model is a script.
"""

import asyncio
from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock

import openpyxl
import pytest
from pydantic_ai import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from evals import week
from franziska import bot as botmod
from franziska import core
from franziska.config import Settings


def script(*responses: ModelResponse) -> FunctionModel:
    it = iter(responses)

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return next(it)

    return FunctionModel(fn)


def call(tool: str, **args) -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart(tool, args)])


def text(s: str) -> ModelResponse:
    return ModelResponse(parts=[TextPart(s)])


@pytest.fixture
def world(tmp_path, monkeypatch):
    # World patches module state (the Guard class, the clock); monkeypatch puts it back after the test.
    monkeypatch.setattr(core.guards, "Guard", core.guards.Guard)
    monkeypatch.setattr(core, "clock", core.clock)
    w = week.World(Settings(data_dir=tmp_path, allowed_ids=frozenset({week.USER}), router=False))
    return w


def run(w: week.World, step: week.Step, model: FunctionModel | None = None) -> week.Out:
    if model is not None:
        w.bot.model = model
    return asyncio.run(w.step(step))


def test_task_from_a_message_is_reminded_and_done_by_a_tap(world):
    """Message → secretary → task → the 60 s job reminds at the right time → ✅ tap completes it."""
    out = run(world, week.say("mon 08:10", "ricordami di chiamare la banca domani alle 10", None),
              script(call("add_task", title="Chiamare la banca", remind_at="2026-11-03T10:00"),
                     text("Ok, ti ricordo domani alle 10:00.")))
    assert world.item("banca")["remind_at"] == "2026-11-03T10:00"
    assert "10:00" in out.replies[-1]
    out = run(world, week.tap("tue 10:00", r"⏰.*banca", r"Done"))
    assert world.item("banca")["done_at"]
    assert "Done" in out.replies[-1]


def test_reply_with_an_invented_date_is_rewritten(world):
    """The verify-after-write guard: a date no tool produced doesn't reach the user."""
    out = run(world, week.say("mon 08:10", "ricordami di pagare il bollo", None),
              script(call("add_task", title="Pagare il bollo"),
                     text("Fatto, te lo ricordo venerdì 6 novembre."),
                     text("Fatto, te lo ricordo venerdì 6 novembre.")))
    assert world.item("bollo") is not None
    assert "6 novembre" not in "\n".join(out.replies)


def test_profile_fact_is_saved_only_after_the_tap(world):
    """learn(profile=True) → 💾 button → nothing on disk until the tap → the next run's index shows it."""
    out = run(world, week.say("thu 15:05", "abito in via Roma 12, scala B interno 7", None),
              script(call("learn", name="home", fact="Via Roma 12, scala B, interno 7", profile=True),
                     text("Vuoi che lo salvi nel profilo?")))
    assert any("💾" in b for b in out.buttons)
    assert world.bot.memory.get("profile", "home") is None
    run(world, week.tap("thu 15:06", r"", r"💾"))
    assert "Roma 12" in world.bot.memory.get("profile", "home").description

    seen = []

    def spy(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(info.instructions or "")
        return text("Via Roma 12.")

    run(world, week.say("thu 15:10", "dove abito?", None), FunctionModel(spy))
    assert "Roma 12" in seen[0]


def test_bill_by_chat_becomes_a_task_and_the_bank_import_ticks_it_off(world, tmp_path):
    """Money route: a bill → a 'Pay' task; the bank's .xlsx sent to the bot settles both."""
    world.chat.context.chat_data["mode"] = "money"
    run(world, week.say("mon 09:00", "condominio 864 euro entro il primo dicembre", None),
        script(call("add_bill", payee="Condominio", amount_eur=864, due_on="2026-12-01"),
               text("Bolletta salvata.")))
    assert world.item("Pay Condominio")["done_at"] is None

    f = tmp_path / "movimenti.xlsx"
    wb = openpyxl.Workbook()
    wb.active.append(["Data contabile", "Data valuta", "Descrizione", "Accrediti", "Addebiti", "Descrizione estesa"])
    paid = datetime(2026, 11, 20)
    wb.active.append([paid, paid, "DISPOSIZIONE DI BONIFICO", None, -864.0, "Bonifico a favore di: Condominio rata"])
    wb.save(f)
    world.now = datetime(2026, 11, 21, 9, 0)
    update = world.chat.update()
    doc = MagicMock(file_name="movimenti.xlsx")
    doc.get_file = AsyncMock(return_value=MagicMock(download_to_drive=AsyncMock(
        side_effect=lambda p: p.write_bytes(f.read_bytes()))))
    update.effective_message.document = doc
    update.effective_message.photo = []
    asyncio.run(world.bot.on_document(update, world.chat.context))
    assert "✅ Bill paid: Condominio 864.00 €" in world.chat.log[-1]
    assert world.item("Pay Condominio")["done_at"]
    assert world.bot.money.today() == date(2026, 11, 21)


def test_desktop_off_money_never_goes_to_the_cloud(tmp_path, monkeypatch):
    """Desktop off with the cloud policy: life may go to ChatGPT, money only wakes the desktop."""
    b = botmod.Bot(Settings(data_dir=tmp_path, allowed_ids=frozenset({week.USER}), router=False, when_off="cloud",
                            desktop_mac="aa:bb:cc:dd:ee:ff"))
    monkeypatch.setattr(b.desktop, "is_up", AsyncMock(return_value=False))
    b.desktop.wake_and_wait = AsyncMock(return_value=False)
    ask = AsyncMock(return_value="Fatto.")
    monkeypatch.setattr(core.cloud, "ask", ask)
    chat = week.Chat()
    asyncio.run(b.on_text(chat.update("ricordami domani di chiamare la banca"), chat.context))
    assert chat.log[-1] == "☁️ Fatto."
    chat.context.chat_data["mode"] = "money"
    asyncio.run(b.on_text(chat.update("quanto ho speso?"), chat.context))
    assert ask.await_count == 1
    assert "didn't come up" in chat.log[-1]


def test_appointments_go_to_google_and_tasks_stay_local(tmp_path):
    """Life with (fake) Google Calendar: ids route to the right store; agenda and brief merge both."""
    from franziska.google.auth import GoogleClient
    from franziska.google.calendar import GoogleCalendar
    from franziska.secretary.service import Life
    from tests.fake_google import FakeGoogle, token_file

    now = datetime(2026, 10, 25, 12, 0)
    fake = FakeGoogle()
    cal = GoogleCalendar(GoogleClient(token_file(tmp_path), transport=fake.transport()), tmp_path / "life.sqlite")
    life = Life.open(tmp_path / "life.sqlite", clock=lambda: now, calendar=cal)
    assert life.add_appointment("Dentist", datetime(2026, 10, 29, 15, 30)).startswith("Saved in Google Calendar: #c1")
    life.add_task("Call bank", remind_at=datetime(2026, 10, 26, 9, 0))
    assert len(fake.events) == 1
    assert "#c1 📅 Dentist" in life.agenda().split("Thursday 29 Oct:")[1]
    assert life.reschedule("#c1", starts_at=datetime(2026, 10, 30, 10, 0)).startswith("Updated in Google Calendar")
    assert next(iter(fake.events.values()))["start"]["dateTime"] == "2026-10-30T10:00:00+01:00"
    assert life.complete("1").startswith("Done:")
    assert life.cancel("C1").startswith("Removed from Google Calendar") and fake.events == {}
