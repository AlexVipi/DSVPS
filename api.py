from fastapi import FastAPI, Query
from contextlib import asynccontextmanager
import asyncio
import json, time, subprocess, os, base64, logging
from wg import gen_keys, add_peer, remove_peer, get_transfer
import config
import payment as pay_module

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def _expiry_checker():
    """Каждые 60 секунд: обновляет трафик, удаляет пиры при превышении лимита или истечении подписки."""
    while True:
        await asyncio.sleep(60)
        try:
            db = load_db()
            now = int(time.time())
            changed = False
            transfer = get_transfer()

            for uid, data in list(db.items()):
                if not isinstance(data, dict) or "expires" not in data:
                    continue
                if not data.get("active", True):
                    continue

                # Обновляем счётчик трафика для пользователей с лимитом
                traffic_used = data.get("traffic_used_bytes", 0)
                if "traffic_limit_bytes" in data:
                    pubkey = data.get("public_key")
                    if pubkey and pubkey in transfer:
                        rx, tx = transfer[pubkey]
                        current_total = rx + tx
                        snapshot = data.get("traffic_wg_snapshot", 0)
                        delta = current_total - snapshot if current_total >= snapshot else current_total
                        if delta > 0:
                            traffic_used += delta
                            db[uid]["traffic_used_bytes"] = traffic_used
                            db[uid]["traffic_wg_snapshot"] = current_total
                            changed = True

                    # Проверяем превышение лимита
                    if traffic_used >= data["traffic_limit_bytes"]:
                        remove_peer(data["public_key"])
                        db[uid]["active"] = False
                        changed = True
                        logger.info("[TRAFFIC] Limit reached for user %s (used %d bytes)", uid, traffic_used)
                        continue

                # Проверяем истечение подписки
                if data["expires"] <= now:
                    remove_peer(data["public_key"])
                    db[uid]["active"] = False
                    changed = True
                    logger.info("[EXPIRY] Removed WireGuard peer for user %s", uid)

            if changed:
                save_db(db)
        except Exception as e:
            logger.error("[EXPIRY] Error in expiry checker: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(_expiry_checker())
    yield

app = FastAPI(lifespan=lifespan)
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
    used = [int(v["ip"].split(".")[-1]) for v in db.values() if isinstance(v, dict) and "ip" in v]
    return f"{config.NETWORK}{max(used)+1 if used else 2}"



@app.get("/status/{user_id}")
def get_status(user_id: str):
    db = load_db()
    if user_id not in db:
        return {"active": False}

    user = db[user_id]
    now = int(time.time())
    is_active = user["expires"] > now and user.get("active", True)

    result = {
        "active": is_active,
        "ip": user["ip"],
        "expires": user["expires"],
        "location": user.get("location", "Server1"),
    }
    if "traffic_limit_bytes" in user:
        result["traffic_limit_bytes"] = user["traffic_limit_bytes"]
        result["traffic_used_bytes"] = user.get("traffic_used_bytes", 0)
    return result



# ===== CREATE TRIAL =====
@app.post("/trial/{user_id}")
def trial(user_id: str):
    db = load_db()

    trial_used = db.get("trial_used", [])
    if user_id in trial_used or user_id in db:
        return {"error": "trial already used"}

    private, public = gen_keys()
    ip = get_next_ip(db)

    add_peer(public, ip)

    expires = int(time.time()) + 24 * 3600  # 24 часа

    db[user_id] = {
        "ip": ip,
        "public_key": public,
        "private_key": private,
        "expires": expires,
        "subscribed_at": int(time.time()),
        "is_trial": True,
        "traffic_limit_bytes": 10 * 1024 * 1024 * 1024,  # 10 ГБ
        "traffic_used_bytes": 0,
        "traffic_wg_snapshot": 0,
    }

    db.setdefault("trial_used", []).append(user_id)
    save_db(db)

    endpoint = subprocess.getoutput("curl -s ifconfig.me")

    # ===== CONFIG TEXT =====
    config_text = f"""[Interface]
PrivateKey = {private}
Address = {ip}/24
DNS = 1.1.1.1
MTU = 1280

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

    if user_id not in db or not isinstance(db.get(user_id), dict):
        return {"error": "user not found"}

    user = db[user_id]

    # удаляем из WireGuard
    remove_peer(user["public_key"])

    # удаляем основную запись
    del db[user_id]

    # удаляем из trial_used
    if user_id in db.get("trial_used", []):
        db["trial_used"].remove(user_id)

    save_db(db)

    # удаляем все платёжные сессии и связанные txid из payments.json
    pdata = pay_module._load()
    user_txids = set()
    sessions_to_delete = [pid for pid, s in pdata["sessions"].items() if s.get("user_id") == user_id]
    for pid in sessions_to_delete:
        txid = pdata["sessions"][pid].get("txid")
        if txid:
            user_txids.add(txid.upper())
        del pdata["sessions"][pid]

    pdata["used_txids"] = [t for t in pdata.get("used_txids", []) if t.upper() not in user_txids]
    pay_module._save(pdata)

    return {"status": "deleted", "user": user_id}






@app.post("/buy/{user_id}")
def buy(user_id: str, months: int = Query(default=1)):
    db = load_db()
    conf_path = f"{CONF_DIR}/{user_id}.conf"

    # Пользователь уже существует — продлеваем, а не создаём нового пира
    if user_id in db and isinstance(db.get(user_id), dict) and os.path.exists(conf_path):
        return _do_extend(user_id, months=months)

    private, public = gen_keys()
    ip = get_next_ip(db)

    add_peer(public, ip)

    expires = int(time.time()) + months * 30 * 24 * 3600

    now = int(time.time())
    db[user_id] = {
        "ip": ip,
        "public_key": public,
        "private_key": private,
        "expires": expires,
        "subscribed_at": now,
    }

    save_db(db)

    endpoint = subprocess.getoutput("curl -s ifconfig.me")

    config_text = f"""[Interface]
PrivateKey = {private}
Address = {ip}/24
DNS = 1.1.1.1
MTU = 1280

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



@app.post("/set_location/{user_id}/{location}")
def set_location(user_id: str, location: str):
    db = load_db()
    user_ip = db[user_id]["ip"]

    if location == "Server2":
        subprocess.run(f"ip rule add from {user_ip} table 100", shell=True)
    else:
        # Удаляем правило, трафик идет через основной интерфейс Сервера 1
        subprocess.run(f"ip rule del from {user_ip} table 100", shell=True)

    db[user_id]["location"] = location
    save_db(db)
    return {"status": "ok", "location": location}


# ══════════════════════════════════════════════════
# USDT PAYMENT ENDPOINTS
# ══════════════════════════════════════════════════

@app.post("/payment/create/{user_id}")
def payment_create(
    user_id: str,
    action: str = Query(default="buy"),
    network: str = Query(default="tron"),
    months: int = Query(default=1),
):
    """
    Создаёт (или возвращает существующую) платёжную сессию.
    action: "buy" — новый VPN, "extend" — продление.
    network: "tron" — USDT TRC-20, "ton" — USDT TON Jetton.
    months: срок подписки (1, 2, 3).
    """
    months = max(1, min(months, 12))
    if network == "ton":
        wallet = config.TON_USDT_WALLET
        amount = config.TON_USDT_AMOUNT * months
    else:
        wallet = config.USDT_WALLET
        amount = config.USDT_AMOUNT * months

    result = pay_module.create_payment_session(
        user_id=user_id,
        amount=amount,
        wallet=wallet,
        action=action,
        timeout_minutes=config.PAYMENT_TIMEOUT_MINUTES,
        network=network,
        months=months,
    )
    return result


@app.get("/payment/active/{user_id}")
def payment_active(user_id: str):
    """Возвращает активную сессию пользователя или пустой dict."""
    pid, session = pay_module.get_active_session(user_id)
    if not pid:
        return {}
    return {"payment_id": pid, "session": session}


@app.post("/payment/verify/{user_id}/{txid}")
def payment_verify(user_id: str, txid: str):
    """
    Верифицирует TXID, проверяет транзакцию в блокчейне.
    При успехе — выдаёт/продлевает VPN и возвращает пути к конфигу.
    """
    txid = txid.strip()

    # 1. Формат TXID — определяем сеть из активной сессии для правильной проверки формата
    _pid_check, _sess_check = pay_module.get_active_session(user_id)
    _net_check = _sess_check.get("network", "tron") if _sess_check else "tron"
    if not pay_module.validate_txid_format(txid, network=_net_check):
        return {"error": "invalid_format", "reason": "Неверный формат TXID. Для TRC-20 — 64 hex-символа, для TON — хэш транзакции из обозревателя блокчейна."}

    # 2. TXID уже был успешно использован глобально
    if pay_module.is_txid_globally_used(txid):
        return {"error": "txid_already_used", "reason": "Этот TXID уже был использован для другого платежа."}

    # 3. Получаем активную сессию
    pid, session = pay_module.get_active_session(user_id)
    if not pid:
        return {"error": "no_session", "reason": "Платёжная сессия не найдена или истекла. Нажми «Купить» снова."}

    # 4. TXID уже пробовали для этой сессии (временно отключено — может блокировать повтор при сетевой ошибке)
    # if pay_module.is_txid_tried_in_session(pid, txid):
    #     return {
    #         "error": "txid_already_tried",
    #         "reason": "Этот TXID уже был отправлен для текущего платежа. Отправь другой TXID.",
    #     }

    # 5. Проверяем транзакцию в блокчейне (выбираем функцию по сети)
    network = session.get("network", "tron")
    if network == "ton":
        check = pay_module.check_ton_transaction(
            txid=txid,
            expected_wallet=session["wallet"],
            expected_amount_usdt=session["amount"],
            api_key=config.TONAPI_API_KEY,
        )
    else:
        check = pay_module.check_tron_transaction(
            txid=txid,
            expected_wallet=session["wallet"],
            expected_amount_usdt=session["amount"],
            api_key=config.TRONGRID_API_KEY,
        )

    if not check["ok"]:
        if check.get("pending"):
            return {"pending": True, "reason": check["reason"], "expires_at": session["expires_at"]}
        pay_module.record_failed_attempt(pid, txid, check["reason"])
        return {"error": "verification_failed", "reason": check["reason"]}

    # 6. Платёж подтверждён — помечаем сессию и выдаём VPN
    pay_module.mark_payment_success(pid, txid)

    action = session.get("action", "buy")
    months = session.get("months", 1)
    if action == "extend":
        return _do_extend(user_id, months=months)
    else:
        return buy(user_id, months=months)


@app.post("/extend/{user_id}")
def extend_user(user_id: str, months: int = Query(default=1)):
    """Продлевает подписку без создания нового WireGuard-пира."""
    return _do_extend(user_id, months=months)


@app.post("/grant/{user_id}")
def grant_access(
    user_id: str,
    days: int = Query(default=0),
    hours: int = Query(default=0),
    minutes: int = Query(default=0),
):
    """Выдаёт или продлевает VPN-доступ на указанное время (только для admin)."""
    db = load_db()
    conf_path = f"{CONF_DIR}/{user_id}.conf"
    qr_path = f"{CONF_DIR}/{user_id}.png"

    now = int(time.time())
    duration = days * 24 * 3600 + hours * 3600 + minutes * 60
    if duration <= 0:
        return {"error": "duration must be greater than zero"}

    # Пользователь уже существует — продлеваем или восстанавливаем
    if user_id in db and isinstance(db[user_id], dict) and os.path.exists(conf_path):
        current_expires = db[user_id]["expires"]
        db[user_id]["expires"] = max(current_expires, now) + duration
        db[user_id]["subscribed_at"] = now
        db[user_id]["notified"] = {}  # сброс уведомлений для нового периода
        # Если пир был деактивирован — реактивируем
        if not db[user_id].get("active", True):
            add_peer(db[user_id]["public_key"], db[user_id]["ip"])
            db[user_id]["active"] = True
        save_db(db)
        return {
            "config_file": conf_path,
            "qr_file": qr_path,
            "expires": db[user_id]["expires"],
        }

    # Новый пользователь — создаём пир
    private, public = gen_keys()
    ip = get_next_ip(db)
    add_peer(public, ip)
    expires = now + duration

    db[user_id] = {
        "ip": ip,
        "public_key": public,
        "private_key": private,
        "expires": expires,
        "subscribed_at": now,
    }
    save_db(db)

    endpoint = subprocess.getoutput("curl -s ifconfig.me")
    config_text = f"""[Interface]
PrivateKey = {private}
Address = {ip}/24
DNS = 1.1.1.1
MTU = 1280

[Peer]
PublicKey = {config.SERVER_PUBLIC}
Endpoint = {endpoint}:{config.PORT}
AllowedIPs = 0.0.0.0/0
PersistentKeepalive = 25
"""

    with open(conf_path, "w") as f:
        f.write(config_text)

    subprocess.run(
        ["qrencode", "-o", qr_path, "-t", "png"],
        input=config_text.encode(),
        check=True,
    )

    return {
        "config_file": conf_path,
        "qr_file": qr_path,
        "expires": expires,
    }


def _do_extend(user_id: str, months: int = 1) -> dict:
    """Внутренняя функция продления — если нет пира, создаёт нового."""
    db = load_db()
    conf_path = f"{CONF_DIR}/{user_id}.conf"
    qr_path = f"{CONF_DIR}/{user_id}.png"

    # Нет подписки вообще — создаём как при покупке
    if user_id not in db or not os.path.exists(conf_path):
        return buy(user_id, months=months)

    # Продлеваем: если подписка ещё активна — считаем от текущего expires
    now = int(time.time())
    current_expires = db[user_id]["expires"]
    db[user_id]["expires"] = max(current_expires, now) + months * 30 * 24 * 3600
    db[user_id]["subscribed_at"] = now
    db[user_id]["notified"] = {}  # сброс уведомлений для нового периода

    # Если пир был деактивирован по истечении — реактивируем
    if not db[user_id].get("active", True):
        add_peer(db[user_id]["public_key"], db[user_id]["ip"])
        db[user_id]["active"] = True

    save_db(db)

    return {
        "config_file": conf_path,
        "qr_file": qr_path,
        "expires": db[user_id]["expires"],
    }


