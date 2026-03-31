import subprocess

def gen_keys():
    private = subprocess.getoutput("wg genkey")
    public = subprocess.getoutput(f"echo {private} | wg pubkey")
    return private, public

def add_peer(public_key, ip):
    subprocess.call(f"wg set wg0 peer {public_key} allowed-ips {ip}/32", shell=True)

def remove_peer(public_key):
    subprocess.call(f"wg set wg0 peer {public_key} remove", shell=True)
