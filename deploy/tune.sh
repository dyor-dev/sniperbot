#!/usr/bin/env bash
# Oracle Cloud (Ubuntu 22.04/24.04) kechikish tuning'i.
#   sudo bash deploy/tune.sh
# Idempotent: bir necha marta ishlatsa ham bo'ladi.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ ${EUID} -ne 0 ]]; then
  echo "root kerak:  sudo bash deploy/tune.sh" >&2
  exit 1
fi

echo "== 1/6 sysctl: BBR + buferlar =="
install -m 0644 "$HERE/99-sniper.conf" /etc/sysctl.d/99-sniper.conf
sysctl --system >/dev/null
echo "   qdisc=$(sysctl -n net.core.default_qdisc)  cc=$(sysctl -n net.ipv4.tcp_congestion_control)"
if [[ "$(sysctl -n net.ipv4.tcp_congestion_control)" != "bbr" ]]; then
  echo "   OGOHLANTIRISH: BBR yoqilmadi (kernel moduli?). 'modprobe tcp_bbr' ni tekshiring."
fi

echo "== 2/6 CPU governor: performance =="
set_governor() {
  if command -v cpupower >/dev/null 2>&1; then
    cpupower frequency-set -g performance >/dev/null 2>&1 || true
  fi
  local f
  for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
    [[ -w "$f" ]] && echo performance >"$f" || true
  done
}
set_governor
echo "   governor=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || echo n/a)"
# Qayta yuklashdan keyin ham saqlansin
cat >/etc/systemd/system/cpu-performance.service <<'EOF'
[Unit]
Description=CPU governor = performance (latency tuning)
After=multi-user.target
[Service]
Type=oneshot
ExecStart=/bin/sh -c 'for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do echo performance > $f; done'
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now cpu-performance.service >/dev/null 2>&1 || true

echo "== 3/6 Transparent Huge Pages: madvise =="
if [[ -w /sys/kernel/mm/transparent_hugepage/enabled ]]; then
  echo madvise >/sys/kernel/mm/transparent_hugepage/enabled || true
fi
echo "   thp=$(cat /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null || echo n/a)"

echo "== 4/6 irqbalance =="
if ! command -v irqbalance >/dev/null 2>&1; then
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq irqbalance >/dev/null 2>&1 || true
fi
systemctl enable --now irqbalance >/dev/null 2>&1 || true
echo "   irqbalance=$(systemctl is-active irqbalance 2>/dev/null || echo n/a)"

echo "== 5/6 vaqt sinxronizatsiyasi (log/statistika uchun) =="
timedatectl set-ntp true || true
echo "   $(timedatectl show -p NTPSynchronized --value 2>/dev/null | sed 's/^/NTPSynchronized=/')"

echo "== 6/6 fayl limitlari =="
cat >/etc/security/limits.d/99-sniper.conf <<'EOF'
* soft nofile 1048576
* hard nofile 1048576
EOF
echo "   limits.d/99-sniper.conf yozildi"

echo
echo "Tayyor. Tekshirish:"
echo "  sysctl net.ipv4.tcp_congestion_control net.core.default_qdisc"
echo "  python -m sniper --probe 15    # real MTProto RTT"
echo
echo "Eslatma: Oracle'ning 'Security List' faqat INGRESS uchun; chiqish (egress)"
echo "odatda ochiq. Hech qanday port ochish shart emas - bot faqat tashqariga ulanadi."
