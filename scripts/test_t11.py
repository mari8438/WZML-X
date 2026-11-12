import os
import sys
from PIL import Image

sys.path.insert(0, "/home/administrator/starfall")

from bot.helper.poster_engine.template_eleven import _template_eleven

poster_path = "/home/administrator/starfall/data/thumbnails/poster_My_Hero_Academia_Vigilantes_S01.jpg"
out_card = "/home/administrator/starfall/data/thumbnails/hero_card_My_Hero_Academia_Vigilantes_S01.jpg"

data = {
    "title": "My Hero Academia: Vigilantes",
    "year": 2025,
    "season": 1,
    "episodes": 13,
    "rating": "7.6",
    "genres": "Action, Adventure, Animation, Sci-Fi",
    "plot": "Kouichi Haimawari is a dull college student who aspires to be a hero but has given up on his dreams. He uses his modest Quirk to do good deeds in his neighborhood until one night an encounter with the terrifying villain Knuckleduster changes everything.",
    "resolution": "1080P ULTRA HD",
    "codec": "HEVC x265",
    "bit": "10bit",
    "quality": "BluRay",
    "audio": "Tamil AAC 2.0",
}

img = _template_eleven(bg=poster_path, side=poster_path, data=data)
img.save(out_card, "JPEG", quality=95)
print(f"Template 11 Hero Card generated successfully: {out_card} ({os.path.getsize(out_card)} bytes, size={img.size})")
