"""
Модуль платежей USDT TRC-20.
Хранилище: /opt/vpn-service/payments.json
Структура:
  {
    "sessions": {
      "<payment_id>": {
        "user_id": "...",
        "amount": 3.0,
        "wallet": "T...",
        "action": "buy" | "extend",
        "status": "pending" | "paid" | "expired",
        "txid": null | "TXID...",
        "tried_txids": [],
        "created_at": 1234567890,
        "expires_at": 1234569690
      }
    },
    "used_txids": ["TXID1", "TXID2", ...]
  }
"""

import base64 as _base64
import hashlib
import json
import logging
import re
import time
import uuid
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

PAYMENTS_FILE = "/opt/vpn-service/payments.json"
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"  # USDT TRC-20 mainnet
TRONGRID_BASE = "https://api.trongrid.io"

TXID_RE = re.compile(r'^[0-9a-fA-F]{64}$')

# TON USDT Jetton master contract (mainnet)
TON_USDT_JETTON_MASTER = "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs"
TONAPI_BASE = "https://tonapi.io"
# Accepts 64 hex chars (TON or TRX) OR base64url hash from TON explorer (~44 chars)
TON_TXID_RE = re.compile(r'^([0-9a-fA-F]{64}|[A-Za-z0-9+/_\-]{43,44}=?)$')

_B58_ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58decode(s: str) -> bytes:
    n = 0
    for c in s.encode():
        n = n * 58 + _B58_ALPHABET.index(c)
    result = n.to_bytes(25, "big")
    return result


def _tron_to_hex(address: str) -> str:
    """Конвертирует TRON base58 адрес (T...) в hex без префикса (нижний регистр)."""
    raw = _b58decode(address)
    return raw[1:21].hex()


def _normalize_address(addr: str) -> str:
    """Приводит любой TRON адрес к единому hex-виду (нижний регистр, без префикса)."""
    addr = addr.strip()
    if addr.startswith("0x") or addr.startswith("0X"):
        return addr[2:].lower()
    if addr.startswith("41") and len(addr) == 42:
        return addr[2:].lower()
    if addr.startswith("T") and len(addr) == 34:
        return _tron_to_hex(addr)
    return addr.lower()


# ─────────────────────────────────────────────
# TON address / txhash helpers
# ─────────────────────────────────────────────

def _ton_addr_hex(addr: str) -> str:
    """
    Нормализует любой формат TON-адреса к 64-символьному hex 32-байтного account_id.
    Поддерживает raw-формат "workchain:hex64" и friendly base64url (48 символов).
    """
    addr = addr.strip()
    if ':' in addr:
        # Raw-формат: "0:hexhex..." или "-1:hexhex..."
        return addr.split(':', 1)[1].lower()
    # Friendly base64url: 48 символов = 36 байт (1 флаг + 1 воркчейн + 32 адрес + 2 CRC)
    try:
        pad = (-len(addr)) % 4
        raw = _base64.urlsafe_b64decode(addr + '=' * pad)
        if len(raw) == 36:
            return raw[2:34].hex()
    except Exception:
        pass
    return addr.lower()


def _ton_txhash_to_hex(txid: str) -> str:
    """Конвертирует хэш TON-транзакции к lowercase hex (принимает base64url или hex)."""
    txid = txid.strip()
    if TXID_RE.match(txid):
        return txid.lower()
    # base64/base64url → hex
    b64 = txid.replace('-', '+').replace('_', '/')
    pad = (-len(b64)) % 4
    try:
        raw = _base64.b64decode(b64 + '=' * pad)
        if len(raw) == 32:
            return raw.hex()
    except Exception:
        pass
    return txid.lower()


# ─────────────────────────────────────────────
# DB helpers
# ─────────────────────────────────────────────

def _load() -> dict:
    p = Path(PAYMENTS_FILE)
    if not p.exists():
        return {"sessions": {}, "used_txids": []}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {"sessions": {}, "used_txids": []}


def _save(data: dict):
    Path(PAYMENTS_FILE).write_text(json.dumps(data, indent=2))


# ─────────────────────────────────────────────
# Создание платёжной сессии
# ─────────────────────────────────────────────

def create_payment_session(
    user_id: str,
    amount: float,
    wallet: str,
    action: str = "buy",
    timeout_minutes: int = 30,
    network: str = "tron",
    months: int = 1,
) -> dict:
    """
    Создаёт платёжную сессию для user_id.
    Если уже есть активная (не истёкшая) сессия для той же сети и срока — возвращает её.
    action: "buy" (новый VPN) | "extend" (продление).
    network: "tron" | "ton".
    months: срок подписки в месяцах (1, 2, 3).
    Возвращает {"payment_id": ..., "session": {...}, "existing": bool}.
    """
    data = _load()
    sessions = data["sessions"]
    now = int(time.time())

    for pid, s in sessions.items():
        if (s["user_id"] == user_id
                and s["status"] == "pending"
                and s.get("network", "tron") == network
                and s.get("months", 1) == months):
            if s["expires_at"] > now:
                logger.info("[PAYMENT] Existing active session %s for user %s network=%s months=%d", pid, user_id, network, months)
                return {"payment_id": pid, "session": s, "existing": True}
            else:
                sessions[pid]["status"] = "expired"
                logger.info("[PAYMENT] Session %s expired for user %s", pid, user_id)

    payment_id = str(uuid.uuid4())
    session = {
        "user_id": user_id,
        "amount": amount,
        "wallet": wallet,
        "action": action,
        "network": network,
        "months": months,
        "status": "pending",
        "txid": None,
        "tried_txids": [],
        "created_at": now,
        "expires_at": now + timeout_minutes * 60,
    }
    sessions[payment_id] = session
    _save(data)
    logger.info("[PAYMENT] Created session %s for user %s amount=%.6f USDT action=%s network=%s months=%d",
                payment_id, user_id, amount, action, network, months)
    return {"payment_id": payment_id, "session": session, "existing": False}


# ─────────────────────────────────────────────
# Получение активной сессии
# ─────────────────────────────────────────────

def get_active_session(user_id: str) -> tuple:
    """Возвращает (payment_id, session) или (None, None)."""
    data = _load()
    now = int(time.time())
    for pid, s in data["sessions"].items():
        if s["user_id"] == user_id and s["status"] == "pending":
            if s["expires_at"] > now:
                return pid, s
    return None, None


# ─────────────────────────────────────────────
# Валидация TXID
# ─────────────────────────────────────────────

def validate_txid_format(txid: str, network: str = "tron") -> bool:
    """Проверяет формат TXID: для tron — 64 hex, для ton — 64 hex или base64url ~44 символа."""
    if network == "ton":
        return bool(TON_TXID_RE.match(txid.strip()))
    return bool(TXID_RE.match(txid.strip()))


def is_txid_globally_used(txid: str) -> bool:
    """Проверяет, был ли TXID уже успешно использован в любом платеже."""
    data = _load()
    return txid.upper() in [t.upper() for t in data.get("used_txids", [])]


def is_txid_tried_in_session(payment_id: str, txid: str) -> bool:
    """Проверяет, отправлял ли пользователь этот TXID для данной сессии."""
    data = _load()
    session = data["sessions"].get(payment_id, {})
    tried = session.get("tried_txids", [])
    return txid.upper() in [t.upper() for t in tried]


# ─────────────────────────────────────────────
# Проверка транзакции в блокчейне TRON
# ─────────────────────────────────────────────

def check_tron_transaction(
    txid: str,
    expected_wallet: str,
    expected_amount_usdt: float,
    api_key: str = "",
) -> dict:
    """
    Проверяет транзакцию TRC-20 USDT через TronGrid API.
    Возвращает {"ok": True} или {"ok": False, "reason": "<текст ошибки>"}.

    Шаги проверки:
      1. gettransactionbyid  — существует ли TX и подтверждена ли?
      2. /v1/contracts/{txid}/events — Transfer к нашему адресу с нужной суммой?
    """
    headers = {"Accept": "application/json"}
    if api_key:
        headers["TRON-PRO-API-KEY"] = api_key

    # ── Шаг 1: базовая проверка транзакции ──────────────────────────────
    try:
        resp = requests.post(
            f"{TRONGRID_BASE}/wallet/gettransactionbyid",
            json={"value": txid},
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        tx = resp.json()
    except requests.RequestException as e:
        logger.error("[PAYMENT] gettransactionbyid error txid=%s: %s", txid, e)
        return {"ok": False, "reason": "Ошибка соединения с сетью TRON. Попробуйте через минуту."}

    # TronGrid возвращает пустой dict {} если TX не найдена или не подтверждена
    if not tx or "txID" not in tx:
        return {
            "ok": False,
            "reason": (
                "Транзакция не найдена или ещё не подтверждена в блокчейне.\n"
                "Подождите 1–2 минуты и отправьте TXID снова."
            ),
        }

    # Проверяем что смарт-контракт выполнился успешно
    ret_list = tx.get("ret", [{}])
    contract_ret = ret_list[0].get("contractRet", "UNKNOWN") if ret_list else "UNKNOWN"
    if contract_ret != "SUCCESS":
        logger.warning("[PAYMENT] txid=%s contractRet=%s", txid, contract_ret)
        return {"ok": False, "reason": f"Транзакция выполнена с ошибкой (статус: {contract_ret})."}

    # ── Шаг 2: проверяем солидификацию (финализацию) транзакции ─────────
    # /walletsolidity возвращает пустой dict если TX ещё не финализирована
    try:
        resp_sol = requests.post(
            f"{TRONGRID_BASE}/walletsolidity/gettransactionbyid",
            json={"value": txid},
            headers=headers,
            timeout=15,
        )
        resp_sol.raise_for_status()
        tx_sol = resp_sol.json()
    except requests.RequestException as e:
        logger.warning("[PAYMENT] walletsolidity check error txid=%s: %s", txid, e)
        tx_sol = {}

    if not tx_sol or "txID" not in tx_sol:
        logger.info("[PAYMENT] txid=%s not yet solidified", txid)
        return {
            "ok": False,
            "pending": True,
            "reason": (
                "Транзакция найдена в блокчейне, но ещё не получила достаточно подтверждений.\n"
                "Обычно это занимает 1–2 минуты — я проверю автоматически."
            ),
        }

    # ── Шаг 3: получаем события Transfer ────────────────────────────────
    try:
        resp = requests.get(
            f"{TRONGRID_BASE}/v1/transactions/{txid}/events",
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        events_data = resp.json()
    except requests.RequestException as e:
        logger.error("[PAYMENT] events error txid=%s: %s", txid, e)
        return {"ok": False, "reason": "Ошибка получения событий транзакции. Попробуйте через минуту."}

    events = events_data.get("data", [])

    # ── Шаг 3: ищем Transfer USDT на наш кошелёк ────────────────────────
    usdt_event_found = False
    for event in events:
        if event.get("event_name") != "Transfer":
            continue
        if event.get("contract_address", "").upper() != USDT_CONTRACT.upper():
            continue

        usdt_event_found = True
        result = event.get("result", {})
        to_addr = result.get("to", "")
        value_raw = result.get("value", "0")

        # Проверяем адрес получателя (нормализуем: hex vs base58)
        if _normalize_address(to_addr) != _normalize_address(expected_wallet):
            logger.warning("[PAYMENT] txid=%s: to=%s expected=%s", txid, to_addr, expected_wallet)
            return {
                "ok": False,
                "reason": (
                    f"Перевод отправлен не на тот адрес.\n"
                    f"Ожидался:  {expected_wallet}\n"
                    f"Получено:  {to_addr}"
                ),
            }

        # USDT TRC-20 имеет 6 знаков после запятой (1 USDT = 1 000 000)
        try:
            received_usdt = int(value_raw) / 1_000_000
        except (ValueError, TypeError):
            logger.error("[PAYMENT] txid=%s: cannot parse value=%s", txid, value_raw)
            return {"ok": False, "reason": "Не удалось прочитать сумму перевода."}

        # Сравниваем сумму с допуском 0.000001 USDT (1 sun)
        if abs(received_usdt - expected_amount_usdt) > 0.000001:
            logger.warning("[PAYMENT] txid=%s: amount=%.6f expected=%.6f",
                           txid, received_usdt, expected_amount_usdt)
            return {
                "ok": False,
                "reason": (
                    f"Неверная сумма перевода.\n"
                    f"Ожидалось: {expected_amount_usdt:.6f} USDT\n"
                    f"Получено:  {received_usdt:.6f} USDT"
                ),
            }

        logger.info("[PAYMENT] txid=%s verified OK: %.6f USDT → %s", txid, received_usdt, to_addr)
        return {"ok": True}

    if not usdt_event_found:
        return {
            "ok": False,
            "reason": (
                "В транзакции не найден перевод USDT (TRC-20).\n"
                "Убедитесь, что отправляете именно USDT в сети TRC-20 (TRON)."
            ),
        }

    return {
        "ok": False,
        "reason": f"Перевод USDT найден, но адрес получателя не совпадает с нашим кошельком.",
    }


# ─────────────────────────────────────────────
# Обновление статуса сессии
# ─────────────────────────────────────────────

def mark_payment_success(payment_id: str, txid: str):
    data = _load()
    if payment_id in data["sessions"]:
        data["sessions"][payment_id]["status"] = "paid"
        data["sessions"][payment_id]["txid"] = txid.upper()

    used = data.setdefault("used_txids", [])
    if txid.upper() not in [t.upper() for t in used]:
        used.append(txid.upper())

    _save(data)
    logger.info("[PAYMENT] Session %s marked paid, txid=%s", payment_id, txid)


def record_failed_attempt(payment_id: str, txid: str, reason: str):
    """Фиксирует неудачную попытку TXID в рамках сессии (без блокировки сессии)."""
    data = _load()
    if payment_id in data["sessions"]:
        tried = data["sessions"][payment_id].setdefault("tried_txids", [])
        if txid.upper() not in [t.upper() for t in tried]:
            tried.append(txid.upper())
        data["sessions"][payment_id]["last_fail_reason"] = reason
    _save(data)
    logger.warning("[PAYMENT] Session %s failed attempt txid=%s reason=%s",
                   payment_id, txid, reason)


# ─────────────────────────────────────────────
# Проверка транзакции в блокчейне TON (Jetton USDT)
# ─────────────────────────────────────────────

def _fetch_ton_jetton_wallet(main_wallet: str, api_key: str = "") -> str:
    """
    Возвращает hex Jetton-кошелька USDT для основного кошелька.
    В TON у каждого адреса есть отдельный контракт-кошелёк для каждого Jetton.
    TonAPI: GET /v2/accounts/{account}/jettons/{jetton_master}
    """
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        resp = requests.get(
            f"{TONAPI_BASE}/v2/accounts/{main_wallet}/jettons/{TON_USDT_JETTON_MASTER}",
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        jw_addr = resp.json().get("wallet_address", {}).get("address", "")
        if jw_addr:
            return _ton_addr_hex(jw_addr)
    except Exception as e:
        logger.warning("[PAYMENT TON] Could not fetch Jetton wallet for %s: %s", main_wallet, e)
    return ""


def check_ton_transaction(
    txid: str,
    expected_wallet: str,
    expected_amount_usdt: float,
    api_key: str = "",
) -> dict:
    """
    Проверяет перевод USDT Jetton в сети TON через TonAPI.
    Возвращает {"ok": True} или {"ok": False, "reason": "<текст ошибки>"}.

    Шаги проверки:
      1. GET /v2/events/{tx_hex}  — загружаем событие по хэшу транзакции
      2. Проверяем in_progress (ещё не подтверждено)
      3. Ищем действие JettonTransfer к нашему адресу с правильной суммой USDT
    """
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    tx_hex = _ton_txhash_to_hex(txid)
    expected_hex = _ton_addr_hex(expected_wallet)
    jetton_master_hex = _ton_addr_hex(TON_USDT_JETTON_MASTER)

    # ── Шаг 1: получаем наш Jetton-кошелёк USDT ─────────────────────────
    # В TON recipient.address может содержать адрес Jetton-кошелька, а не основного.
    our_jetton_wallet_hex = _fetch_ton_jetton_wallet(expected_wallet, api_key)
    logger.info("[PAYMENT TON] Our USDT Jetton wallet hex: %s", our_jetton_wallet_hex)

    # ── Шаг 2: получаем событие по хэшу транзакции ──────────────────────
    try:
        resp = requests.get(
            f"{TONAPI_BASE}/v2/events/{tx_hex}",
            headers=headers,
            timeout=15,
        )
        if resp.status_code == 404:
            return {
                "ok": False,
                "reason": (
                    "Транзакция не найдена в блокчейне TON.\n"
                    "Подождите 1–2 минуты и отправьте хэш снова."
                ),
            }
        resp.raise_for_status()
        event = resp.json()
    except requests.RequestException as e:
        logger.error("[PAYMENT TON] TonAPI error txid=%s: %s", txid, e)
        return {"ok": False, "reason": "Ошибка соединения с сетью TON. Попробуйте через минуту."}

    # ── Шаг 3: транзакция найдена, но ещё не финализирована ─────────────
    if event.get("in_progress"):
        logger.info("[PAYMENT TON] txid=%s still in_progress", txid)
        return {
            "ok": False,
            "pending": True,
            "reason": (
                "Транзакция найдена, но ещё не получила подтверждений сети.\n"
                "Обычно это занимает 1–2 минуты — проверю автоматически."
            ),
        }

    # ── Шаг 4: ищем JettonTransfer USDT на наш кошелёк ─────────────────
    actions = event.get("actions", [])
    found_jetton = False

    for action in actions:
        if action.get("type") != "JettonTransfer":
            continue
        if action.get("status") != "ok":
            continue

        jt = action.get("JettonTransfer", {})

        # Проверяем, что это именно USDT (по адресу Jetton master)
        jetton_addr = jt.get("jetton", {}).get("address", "")
        if _ton_addr_hex(jetton_addr) != jetton_master_hex:
            continue

        found_jetton = True

        # Проверяем адрес получателя.
        # TonAPI может вернуть в recipient.address либо основной кошелёк,
        # либо адрес Jetton-кошелька (контракт хранения USDT). Проверяем оба.
        recipient_hex = _ton_addr_hex(jt.get("recipient", {}).get("address", ""))
        rw_hex = _ton_addr_hex(jt.get("recipients_wallet", ""))

        is_our_wallet = (
            recipient_hex == expected_hex
            or (our_jetton_wallet_hex and recipient_hex == our_jetton_wallet_hex)
            or (our_jetton_wallet_hex and rw_hex == our_jetton_wallet_hex)
        )

        logger.info(
            "[PAYMENT TON] txid=%s recipient_hex=%s rw_hex=%s expected_hex=%s our_jetton=%s match=%s",
            txid, recipient_hex, rw_hex, expected_hex, our_jetton_wallet_hex, is_our_wallet,
        )

        if not is_our_wallet:
            # Этот перевод идёт не нам — в событии может быть несколько
            # JettonTransfer (DEX, свапы), продолжаем перебор
            continue

        # Нашли перевод на наш кошелёк — проверяем сумму (USDT TON = 6 знаков)
        try:
            received_usdt = int(jt.get("amount", "0")) / 1_000_000
        except (ValueError, TypeError):
            logger.error("[PAYMENT TON] txid=%s: cannot parse amount", txid)
            return {"ok": False, "reason": "Не удалось прочитать сумму перевода."}

        if abs(received_usdt - expected_amount_usdt) > 0.000001:
            logger.warning("[PAYMENT TON] txid=%s amount=%.6f expected=%.6f",
                           txid, received_usdt, expected_amount_usdt)
            return {
                "ok": False,
                "reason": (
                    f"Неверная сумма перевода.\n"
                    f"Ожидалось: {expected_amount_usdt:.6f} USDT\n"
                    f"Получено:  {received_usdt:.6f} USDT"
                ),
            }

        logger.info("[PAYMENT TON] txid=%s verified OK: %.6f USDT → %s",
                    txid, received_usdt, expected_wallet)
        return {"ok": True}

    if not found_jetton:
        return {
            "ok": False,
            "reason": (
                "В транзакции не найден перевод USDT (TON Jetton).\n"
                "Убедитесь, что отправляете именно USDT в сети TON."
            ),
        }

    return {
        "ok": False,
        "reason": "Перевод USDT найден, но адрес получателя не совпадает с нашим кошельком.",
    }
