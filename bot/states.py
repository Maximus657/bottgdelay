from aiogram.fsm.state import State, StatesGroup

class AddUser(StatesGroup):
    """Состояния для добавления пользователя."""
    tg_id = State()
    name = State()
    role = State()

class CreateArtist(StatesGroup):
    """Состояния для добавления артиста."""
    name = State()
    manager = State()
    date = State()

class CreateRelease(StatesGroup):
    """Состояния для создания релиза."""
    artist_str = State()
    title = State()
    rtype = State()
    has_cover = State()
    date = State()

class CreateTask(StatesGroup):
    """Состояния для создания задачи вручную."""
    title = State()
    desc = State()
    assignee = State()
    deadline = State()
    req_file = State()

class FinishTask(StatesGroup):
    """Состояния для завершения задачи."""
    file = State()
    comment = State()

class SMMReportState(StatesGroup):
    """Состояния для SMM отчета."""
    text = State()
