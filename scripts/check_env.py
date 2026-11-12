import sys
modules = ['pyrogram', 'tgcrypto', 'aiosqlite', 'aiohttp', 'qbittorrentapi', 'feedparser', 'PIL']
for m in modules:
    try:
        __import__(m)
        print(f'{m}: OK')
    except ImportError as e:
        print(f'{m}: MISSING ({e})')
