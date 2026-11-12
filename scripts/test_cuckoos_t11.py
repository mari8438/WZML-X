import sys
sys.path.insert(0, '/home/administrator/starfall')
from bot.helper.poster_engine.template_eleven import _template_eleven

poster = '/home/administrator/starfall/data/thumbnails/poster_A_Couple_of_Cuckoos_S01.jpg'
out = '/home/administrator/starfall/data/thumbnails/hero_card_A_Couple_of_Cuckoos_S01.jpg'
data = {
    'title': 'A Couple of Cuckoos',
    'year': 2022,
    'season': 1,
    'episodes': 24,
    'rating': '7.2',
    'genres': 'Comedy, Romance, Animation',
    'plot': '16-year-old super-studier Nagi Umino, high-school second-year student at the Meguro River Academy, was switched at birth. On his way to a dinner to meet his birth parents, he accidentally meets the brash, outspoken Erika Amano.',
    'resolution': '1080P ULTRA HD',
    'codec': 'HEVC x265',
    'bit': '10bit',
    'quality': 'BluRay',
    'audio': 'Tamil AAC 2.0',
}
img = _template_eleven(bg=poster, side=poster, data=data)
img.save(out, 'JPEG', quality=95)
print(f'Rendered Cuckoos Template 11: {out} size={img.size}')
