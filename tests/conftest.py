"""Тесты вне .claude/ видят ту же настройку, что и тесты под .claude: очистка RPV_*/ALPHA_*, spool во временном каталоге,
метка endurance (TK-103: стенд выносливости вынесен из поставки в tests/)."""
import importlib.util
from pathlib import Path

_p = Path(__file__).resolve().parent.parent / ".claude" / "conftest.py"
_spec = importlib.util.spec_from_file_location("rpv_claude_conftest", _p)
_m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_m)
pytest_sessionstart = _m.pytest_sessionstart
pytest_configure = _m.pytest_configure
pytest_collection_modifyitems = _m.pytest_collection_modifyitems
