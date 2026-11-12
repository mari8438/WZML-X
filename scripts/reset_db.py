import sqlite3

conn = sqlite3.connect('/home/administrator/starfall/data/starfall_state.db')
cursor = conn.cursor()

# Check current seasons
print("Current seasons in DB:")
cursor.execute('SELECT season_id, title_romaji, season_number, season_state FROM anime_seasons')
for row in cursor.fetchall():
    print(row)

# Reset Cuckoos and Vigilantes
cursor.execute("UPDATE anime_seasons SET season_state = 'PENDING' WHERE season_state IN ('FAILED', 'COMPLETED')")
cursor.execute("DELETE FROM episode_jobs WHERE season_id IN (SELECT season_id FROM anime_seasons WHERE title_romaji LIKE '%Cuckoo%')")
conn.commit()
print("Reset seasons and cleared test jobs for sample run.")
