#!/bin/bash
# Восстанавливает ip rules для пользователей с location=Server2 после перезагрузки
python3 -c "
import json, subprocess
db = json.load(open('/opt/vpn-service/db.json'))
for user_id, data in db.items():
    if not isinstance(data, dict):
        continue
    if data.get('location') == 'Server2':
        ip = data['ip']
        subprocess.run(f'ip rule add from {ip} table 100 2>/dev/null', shell=True)
        print(f'Restored rule for {user_id} ({ip})')
"
