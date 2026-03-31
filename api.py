from fastapi import FastAPI
import json, time, subprocess, os, base64
from wg import gen_keys, add_peer, remove_peer
import config

app = FastAPI()
DB_FILE = "db.json"
CONF_DIR = "/opt/vpn-service/configs"
os.makedirs(CONF_DIR, exist_ok=True)


# ===== DB =====
def load_db():
    if not os.path.exists(DB_FILE):
        return {}
    return json.load(open(DB_FILE))


def save_db(db):
    json.dump(db, open(DB_FILE, "w"))


def get_next_ip(db):
    used = [int(v["ip"].split(".")[-1]) for v in db.values()]
    return f"{config.NETWORK}{max(used)+1 if used else 2}"


# ===== CREATE TRIAL =====
@app.post("/trial/{user_id}")
def trial(user_id: str):
    db = load_db()

    if user_id in db:
        return {"error": "trial already used"}

    private, public = gen_keys()
    ip = get_next_ip(db)

    add_peer(public, ip)

    expires = int(time.time()) + 24 * 3600

    db[user_id] = {
        "ip": ip,
        "public_key": public,
        "private_key": private,
        "expires": expires
    }

    save_db(db)

    endpoint = subprocess.getoutput("curl -s ifconfig.me")

    # ===== CONFIG TEXT =====
    config_text = f"""[Interface]
PrivateKey = {private}
Address = {ip}/24
DNS = 1.1.1.1

[Peer]
PublicKey = {config.SERVER_PUBLIC}
Endpoint = {endpoint}:{config.PORT}
AllowedIPs = 0.0.0.0/0
PersistentKeepalive = 25
"""

    # ===== FILE PATHS =====
    conf_path = f"{CONF_DIR}/{user_id}.conf"
    qr_path = f"{CONF_DIR}/{user_id}.png"

    # ===== SAVE CONFIG =====
    with open(conf_path, "w") as f:
        f.write(config_text)

    # ===== GENERATE QR =====
    subprocess.run(
    ["qrencode", "-o", qr_path, "-t", "png"],
    input=config_text.encode(),
    check=True
    )
    # ===== QR BASE64 =====
    with open(qr_path, "rb") as f:
        qr_base64 = base64.b64encode(f.read()).decode()

    return {
        "config_file": conf_path,
        "qr_file": qr_path,
        "qr_base64": qr_base64,
        "config": config_text,
        "expires": expires
    }
@app.delete("/delete/{user_id}")
def delete_user(user_id: str):
    db = load_db()

    if user_id not in db:
        return {"error": "user not found"}

    user = db[user_id]

    # удаляем из WireGuard
    remove_peer(user["public_key"])

    # удаляем из базы
    del db[user_id]
    save_db(db)

    return {"status": "deleted", "user": user_id}






@app.post("/buy/{user_id}")
def buy(user_id: str):
    db = load_db()

    private, public = gen_keys()
    ip = get_next_ip(db)

    add_peer(public, ip)

    expires = int(time.time()) + 30 * 24 * 3600  # 30 дней

    db[user_id] = {
        "ip": ip,
        "public_key": public,
        "private_key": private,
        "expires": expires
    }

    save_db(db)

    endpoint = subprocess.getoutput("curl -s ifconfig.me")

    config_text = f"""[Interface]
PrivateKey = {private}
Address = {ip}/24
DNS = 1.1.1.1

[Peer]
PublicKey = {config.SERVER_PUBLIC}
Endpoint = {endpoint}:{config.PORT}
AllowedIPs = 0.0.0.0/0
PersistentKeepalive = 25
"""

    conf_path = f"{CONF_DIR}/{user_id}.conf"
    qr_path = f"{CONF_DIR}/{user_id}.png"

    with open(conf_path, "w") as f:
        f.write(config_text)

    subprocess.run(
        ["qrencode", "-o", qr_path, "-t", "png"],
        input=config_text.encode(),
        check=True
    )

    return {
        "config_file": conf_path,
        "qr_file": qr_path
    }



@app.get("/config/{user_id}")
def get_config(user_id: str):
    db = load_db()

    if user_id not in db:
        return {"error": "not found"}

    conf_path = f"{CONF_DIR}/{user_id}.conf"
    qr_path = f"{CONF_DIR}/{user_id}.png"

    if not os.path.exists(conf_path):
        return {"error": "config not found"}

    return {
        "config_file": conf_path,
        "qr_file": qr_path
    }




