import subprocess

def gen_keys():
    private = subprocess.getoutput("wg genkey")
    public = subprocess.getoutput(f"echo {private} | wg pubkey")
    return private, public

def add_peer(public_key, ip):
    subprocess.call(f"wg set wg0 peer {public_key} allowed-ips {ip}/32", shell=True)

def remove_peer(public_key):
    subprocess.call(f"wg set wg0 peer {public_key} remove", shell=True)

def get_transfer():
    """Returns {public_key: (rx_bytes, tx_bytes)} for all peers."""
    output = subprocess.getoutput("wg show wg0 transfer")
    result = {}
    for line in output.strip().split('\n'):
        if not line:
            continue
        parts = line.split('\t')
        if len(parts) == 3:
            try:
                result[parts[0]] = (int(parts[1]), int(parts[2]))
            except ValueError:
                pass
    return result
