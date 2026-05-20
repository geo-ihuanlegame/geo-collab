import os
import socket
import sys

import paramiko

HOST = os.environ.get("GEO_DEPLOY_HOST")
USER = os.environ.get("GEO_DEPLOY_USER", "root")
PASS = os.environ.get("GEO_DEPLOY_PASS")

if not HOST or not PASS:
    sys.exit("Set GEO_DEPLOY_HOST and GEO_DEPLOY_PASS before running deploy_check.py")

def run(client, cmd, timeout=60):
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    exit_code = stdout.channel.recv_exit_status()
    out = stdout.read().decode(errors='replace').strip()
    err = stderr.read().decode(errors='replace').strip()
    return exit_code, (out + ('\n[stderr] ' + err if err else ''))

# 先测 TCP 连通
s = socket.create_connection((HOST, 22), timeout=10)
banner = s.recv(256)
print('SSH banner:', banner[:100])
s.close()

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())

# 尝试连接，允许 keyboard-interactive 密码认证
c.connect(
    HOST,
    username=USER,
    password=PASS,
    timeout=15,
    look_for_keys=False,
    allow_agent=False,
)
print('=== Connected ===\n')

_, o = run(c, 'cat /etc/os-release | grep PRETTY_NAME')
print('OS:', o)

_, o = run(c, 'docker --version 2>&1')
print('Docker:', o)

_, o = run(c, 'docker compose version 2>&1 || docker-compose --version 2>&1')
print('Compose:', o)

_, o = run(c, 'ls /root/ 2>&1')
print('\n/root/ 目录:\n', o)

_, o = run(c, 'docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" 2>&1')
print('\n容器状态:\n', o)

_, o = run(c, 'ls /root/geo 2>/dev/null || echo "NO GEO DIR"')
print('\n/root/geo:', o)

c.close()
print('\n=== Done ===')
