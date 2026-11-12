import sqlite3

conn = sqlite3.connect('/home/administrator/starfall/data/starfall_state.db')
cursor = conn.cursor()
cursor.execute('SELECT season_id, title_romaji, season_number, season_state FROM anime_seasons')
for row in cursor.fetchall():
    print(row)

# Reset FAILED state to QUEUED so pipeline can retry cleanly
cursor.execute(" UPDATE anime_seasons SET season_state = QUEUED WHERE season_state = FAILED \)
conn.commit()
print('Reset FAILED seasons to QUEUED')
