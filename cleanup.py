import time, json
from wg import remove_peer

DB_FILE = "/opt/vpn-service/db.json"

db = json.load(open(DB_FILE))
now = int(time.time())

new_db = {}

for user_id, user in db.items():
    if user["expires"] < now:
        remove_peer(user["public_key"])
    else:
        new_db[user_id] = user

json.dump(new_db, open(DB_FILE, "w"))
