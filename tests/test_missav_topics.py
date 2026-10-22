import asyncio
import importlib.util
from pathlib import Path
import sys

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Create dummy bot package if not loaded
if "bot" not in sys.modules:
    import types
    bot_mod = types.ModuleType("bot")
    bot_mod.LOGGER = types.SimpleNamespace(info=lambda *a: None, warning=lambda *a: None, error=lambda *a: None)
    bot_mod.DOWNLOAD_DIR = "downloads"
    sys.modules["bot"] = bot_mod

_ARCH_SPEC = importlib.util.spec_from_file_location(
    "missav_topic_archive",
    _REPO_ROOT / "bot" / "helper" / "ext_utils" / "missav_topic_archive.py"
)
_ARCH_MOD = importlib.util.module_from_spec(_ARCH_SPEC)
_ARCH_SPEC.loader.exec_module(_ARCH_MOD)
MissAVTopicArchive = _ARCH_MOD.MissAVTopicArchive


def test_missav_topic_archive():
    async def run_test():
        await MissAVTopicArchive.init()
        chat_id = -1004486729792
        topic_name = "Catwoman"
        thread_id = 5755

        # Set topic
        await MissAVTopicArchive.set(chat_id, topic_name, thread_id)

        # Fast get with matching and case variations
        res1 = await MissAVTopicArchive.get(chat_id, "Catwoman")
        res2 = await MissAVTopicArchive.get(chat_id, "catwoman")
        res3 = await MissAVTopicArchive.get(chat_id, "  CATWOMAN  ")

        assert res1 == 5755
        assert res2 == 5755
        assert res3 == 5755

        # Non-existent topic
        res_none = await MissAVTopicArchive.get(chat_id, "NonExistentTopic123")
        assert res_none is None

    asyncio.run(run_test())
