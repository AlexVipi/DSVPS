WG_CONF = "/etc/wireguard/wg0.conf"

SERVER1_PING_HOST = "185.177.239.220"  # IP SE-сервера (текущий сервер)
SERVER2_PING_HOST = "45.150.34.188"  # IP NL-сервера (используется в set_location)
SERVER2_PING_IFACE = None
SERVER_PUBLIC = open("/etc/wireguard/keys/server_pub").read().strip()
NETWORK = "10.134.72."
PORT = 51820

# USDT TRC-20 Payment
USDT_WALLET = "TEbQ5sSsqaP5DadWmj1PZkzPpZeYKN7CEt"       # Замените на ваш адрес кошелька TRC-20
USDT_AMOUNT = 1.0                        # Цена подписки в USDT (30 дней)
TRONGRID_API_KEY = "ede3c338-d18d-48d0-b88f-017741bc0aa3"                    # Опционально: бесплатно на trongrid.io
PAYMENT_TIMEOUT_MINUTES = 30             # Таймаут платёжной сессии

# USDT TON (Jetton) Payment
TON_USDT_WALLET = "UQAXgbrR6j8l8jpWIKaLeRcXDmC7rrs2GJ0lYY7RGAFB6jRu"
TON_USDT_AMOUNT = 1.0                    # Цена подписки в USDT (30 дней)
TONAPI_API_KEY = ""                      # Опционально: бесплатно на tonapi.io

# Telegram Stars Payment
# Курс: сколько Stars стоит 1 USD (обновляйте при изменении курса Telegram)
# Telegram продаёт ~50 Stars за $1, но принимает оплату по ~67-80 Stars/$1
STARS_PER_USD = 60
