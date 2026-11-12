#!/usr/bin/env python3
import pyrogram
import inspect

for modname in ["pyrogram.storage.storage", "pyrogram.storage.file_storage"]:
    try:
        mod = __import__(modname, fromlist=["*"])
        for name in dir(mod):
            obj = getattr(mod, name)
            if inspect.isclass(obj) and "Storage" in name:
                print(f"Class: {name} in {modname}")
                for method in ["export_session_string", "session_string", "__init__"]:
                    if hasattr(obj, method):
                        print(f"  --- {name}.{method} ---")
                        try:
                            print(inspect.getsource(getattr(obj, method))[:500])
                        except Exception as e:
                            print("  err:", e)
    except Exception as e:
        print(f"Mod err {modname}: {e}")
