# Comment Sniper — Telegram kanal komment userboti

Kanalga yangi post tushgan zahoti uning **muhokama guruhiga birinchi komment**
yozadigan, serverda 24/7 ishlaydigan bot. Toza serverdan to'liq ishga tushirishgacha
bo'lgan yo'l quyida 0 dan yozilgan.

---

## 0. Avval halol raqamlar (buni o'qib chiqing)

| Nima | Vaqt | Izoh |
|---|---|---|
| `classify()` — har bir update uchun | **0.86 µs** (p50) | sof Python, tarmoqsiz |
| Komment matnini tanlash | **0.20 µs** | round-robin |
| MTProto so'rov obyektini yig'ish | **0.68 µs** | `messages.sendMessage` |
| **Kodning jami ishi** | **~2 µs** | shu uchdan iborat |
| Amsterdam ↔ Telegram DC RTT | **1–3 ms** | `.ping` bilan o'lchanadi |
| Post kelishi → server ACK | **5–30 ms** | real sharoitda, kanalga qarab |

**Mikrosekundli kechikish jismonan mumkin emas.** Yorug'lik tolada Amsterdamdan
Telegram serverigacha borib-qaytishi ham ~1 ms. Shuning uchun "0 ms" va'da qiladigan
botlar yolg'on. Biz qila oladigan maksimal narsa — **kodni hisobdan chiqarib tashlash**
(2 µs, ya'ni umumiy vaqtning %0.01'i) va **ortiqcha tarmoq aylanishlarini yo'q qilish**.
Aynan shu ikkinchisi raqobatchilarni ortda qoldiradi:

- ❌ Oddiy botlar: post → `getDiscussionMessage` (1 RTT) → `sendMessage` (1 RTT) = **2 RTT**
- ✅ Biz: post → `sendMessage` (1 RTT) = **1 RTT**

Birinchi RTT ni yo'q qilish uchun biz kanalni emas, uning **muhokama guruhini** tinglaymiz:
Telegram kanal postini guruhga avtomatik forward qilganda, o'sha update ichida
`reply_to` uchun kerakli message id **allaqachon bor**. Natijada post paytida hech qanday
qidiruv RPC bajarilmaydi.

---

## 1. Nega bu userbot, bot emas?

Telegram **Bot API** botlari kanal postlariga komment yozolmaydi — ular faqat o'z
nomidan yozadi, sizning akkauntingiz nomidan emas. Kanal izohi esa **user account**
talab qiladi. Shuning uchun bu loyiha MTProto (Telethon) ustida ishlaydi va sizning
session'ingiz bilan harakat qiladi.

Qo'shimcha yutuq: MTProto — xom TCP, HTTP/TLS qatlami yo'q. Bot API esa HTTPS orqali
`api.telegram.org` ga borib, u yerdan yana Telegram'ga uzatiladi (qo'shimcha hop).

---

## 2. Talablar

- Oracle Cloud'da **Amsterdam** regionida instance (Ubuntu 22.04 yoki 24.04).
  Ampere A1 (ARM, free tier) yoki AMD — ikkalasi ham bo'ladi, 1 OCPU yetarli.
- `my.telegram.org` dan **API_ID** va **API_HASH**.
- Sizda bor **session string** (`1B...` bilan boshlanadi) — yoki telefon raqam + kod.
- Termius (SSH) — serverga ulanish uchun.

> ⚠️ **Muhim:** telefon raqamingiz qaysi DC'ga bog'langan bo'lsa, eng past kechikish
> o'sha DC'ga eng yaqin serverda bo'ladi. Amsterdam odatda DC2/DC4 (ham Amsterdamda)
> bo'ladi — bu ideal. `.ping` buyrug'i qaysi DC ekanini ko'rsatadi.

---

## 3. Oracle Cloud'da server ochish

1. Oracle Cloud Console → **Compute → Instances → Create instance**.
2. **Image:** Ubuntu 24.04 (yoki 22.04), **Shape:** `VM.Standard.A1.Flex` (ARM, 1–2 OCPU).
3. **Region:** Germany Central (Frankfurt) emas — **Netherlands North-West (Amsterdam)**
   ni tanlang.
4. SSH kalit: "Generate a key pair" → **private key'ni yuklab oling** (Termius'ga import qilasiz).
5. **Networking:** public IP yetarli. **Security List'da hech qanday port ochish shart emas** —
   bot faqat tashqariga ulanadi; kirish uchun 22 (SSH) allaqachon ochiq.

Instance "Running" bo'lgach, public IP'ni yozib oling.

---

## 4. Termius bilan ulanish

1. Termius → **Hosts → New Host**.
2. **Address:** instance public IP. **Username:** `ubuntu` (Oracle Ubuntu image'da shu).
3. **Key:** yuklab olgan private key'ni tanlang (Termius → Keychain → Import).
4. Connect. Birinchi marta host key'ni tasdiqlaysiz.

Keyingi barcha buyruqlar shu SSH sessiyasida bajariladi.

---

## 5. Serverni tozalash va tayyorlash (5 daqiqa)

```bash
# 1) tizimni yangilash
sudo apt update && sudo apt -y upgrade

# 2) kerakli paketlar
sudo apt install -y python3 python3-venv python3-pip git ufw curl ca-certificates

# 3) vaqt mintaqasi (loglar va statistika uchun qulay)
sudo timedatectl set-timezone Europe/Amsterdam
sudo timedatectl set-ntp true

# 4) firewall: faqat SSH
sudo ufw allow 22/tcp
sudo ufw --force enable

# 5) bot uchun alohida tizim foydalanuvchisi
sudo adduser --system --group --home /opt/comment-sniper --shell /usr/sbin/nologin sniper
```

Python versiyasini tekshiring (3.10+ kerak):

```bash
python3 --version
```

---

## 6. Kechikish uchun server tuning'i

Loyiha papkasidagi skript BBR, `fq`, bufer o'lchamlari, CPU governor `performance`,
THP `madvise`, irqbalance va file limitlarini sozlaydi:

```bash
# kodni joylashtirgandan KEYIN (7-bo'lim) ishlatasiz:
sudo bash deploy/tune.sh
```

Tekshirish:

```bash
sysctl net.ipv4.tcp_congestion_control net.core.default_qdisc
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor
```

Chiqishi kerak: `bbr`, `fq`, `performance`.

> Nega bu muhim: `bbr + fq` yo'qolgan paketlarda navbatni to'g'ri boshqaradi, buferlar
> kichik bo'lsa "bufferbloat" kechikishi o'smaydi, `performance` governor CPU chastotasini
> pastga tushirmaydi (aks holda har bir update'da mikrosekundlar emas, **millisekundlar**
> yo'qotiladi).

---

## 7. Kodni serverga joylash

Loyihani serverga ikki usulda tashish mumkin.

**A) Git orqali (tavsiya etiladi)** — loyihani GitHub'ga qo'yib:

```bash
sudo mkdir -p /opt/comment-sniper
sudo chown "$USER":"$USER" /opt/comment-sniper
git clone <REPO_URL> /opt/comment-sniper
```

**B) Termius SFTP orqali** — Termius'da **SFTP** bo'limini ochib, loyiha papkasini
`/opt/comment-sniper` ga sudrab tashlang (`.venv` va `.env` ni tashlamang).

Keyin virtual muhit va paketlar:

```bash
cd /opt/comment-sniper
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

Tekshirish (tarmoqsiz, 2 soniya):

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/bench_hotpath.py 100000
```

Ikkinchi buyruq kodning o'z ishini mikrosekundlarda o'lchaydi.

---

## 8. API_ID / API_HASH

1. Brauzerda `my.telegram.org` → telefon raqamingiz bilan kirish.
2. **API development tools** → yangi ilova yarating (nomi ixtiyoriy).
3. `api_id` (raqam) va `api_hash` (32 belgi) ni yozib oling.

---

## 9. Session string

**Sizda session string bo'lsa** — hech narsa qilmaysiz, uni `.env` ga yozasiz (10-bo'lim).

**Bo'lmasa yoki muddati tugagan bo'lsa:**

```bash
cd /opt/comment-sniper
.venv/bin/python scripts/login.py
```

Telefon raqam (`+998...`), Telegram'dan kelgan kod va 2FA parolini kiritasiz. Oxirida
`1B...` bilan boshlanadigan uzun qator chiqadi — shuni `.env` ga yozing.

> Session string = **akkauntga to'liq kirish**. Uni hech kimga bermang, chatga
> yubormang, git'ga qo'shmang.

---

## 10. `.env` faylini yaratish

```bash
cd /opt/comment-sniper
cp .env.example .env
nano .env          # yoki: vim .env
sudo chown sniper:sniper .env
sudo chmod 600 .env     # faqat egasi o'qiy oladi
```

To'ldirish kerak bo'lgan minimal qismlar:

```env
API_ID=1234567
API_HASH=0123456789abcdef0123456789abcdef
TELEGRAM_SESSION=1BVtsOK...   # sizdagi session string
STATE_FILE=/opt/comment-sniper/state.json
WORDS=Salom!|Birinchi!|Qani, kim tezroq?
MIN_INTERVAL=0
MAX_PER_MINUTE=20
AUTO_JOIN=1
```

`WORDS` — boshlang'ich komment matnlari (`|` bilan ajratiladi). Keyin `.word` buyrug'i
bilan serverga tegmasdan o'zgartirasiz.

---

## 11. Birinchi ishga tushirish (foreground)

Systemd'ga o'tishdan oldin xatolarni ko'rish uchun qo'lda ishga tushiring:

```bash
cd /opt/comment-sniper
sudo -u sniper .venv/bin/python -m sniper
```

Ko'rinishi kerak:

```
INFO    event loop: uvloop | python 3.12.x
INFO    sniper 1.0.0 tayyor | akkaunt @username (id ...) | DC 4 | kanal 0 | matn 3 | tcp_tuned=True
```

`session yaroqsiz` desa — 9-bo'limga qayting. Boshqa xato bo'lsa, matnini o'qing:
u aniq nima yetishmayotganini aytadi.

Endi Telegram'da **Saved Messages** (o'zingizga yozish) chatini ochib:

```
.ping
.stats
.help
```

javob bersa — bot ishlayapti. `Ctrl+C` bilan to'xtating.

### Haqiqiy RTT o'lchash

```bash
cd /opt/comment-sniper
sudo -u sniper .venv/bin/python -m sniper --probe 20
```

```
MTProto RTT -> Telegram DC 4  (20 namuna)
  min     1.83 ms
  p50     2.10 ms
  p95     3.02 ms
```

Agar bu yerda 40–150 ms chiqsa, akkauntingiz uzoq DC'da (masalan DC5 Singapore) —
bu holda hech qanday kod yordam bermaydi, jismoniy chegara shu.

---

## 12. 24/7 fon rejimi (systemd, eng yuqori prioritet)

```bash
cd /opt/comment-sniper
sudo chown -R sniper:sniper /opt/comment-sniper
sudo cp deploy/comment-sniper.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now comment-sniper
```

Unit ichida `Nice=-20`, `IOSchedulingClass=realtime`, `OOMScoreAdjust=-500`,
`Restart=always` — ya'ni bot kernel darajasida eng yuqori ustuvorlikda ishlaydi va
yiqilsa 1 soniyada qayta ko'tariladi.

Holat va loglar:

```bash
systemctl status comment-sniper
journalctl -u comment-sniper -f            # jonli log
journalctl -u comment-sniper -n 100 --no-pager
systemctl restart comment-sniper
```

`ps -o ni,pid,cmd -C python3` bilan `NI` ustunida `-20` ko'rasiz.

---

## 13. Buyruqlar (Saved Messages'ga yozasiz)

| Buyruq | Vazifasi |
|---|---|
| `.ping` | Telegram DC'gacha real RTT (min/p50/p95/max + jitter) |
| `.stats` | update/post/komment soni, reaksiya vaqtlari, o'tkazib yuborilganlar |
| `.info` | rejim, kanallar, komment matnlari, flood kutish |
| `.word a\|b\|c` | matnlarni **restart qilmasdan** yangilash |
| `.auto` yoki `.resume` | komment yozishni yoqish |
| `.pause` | komment yozishni to'xtatish (kuzatish davom etadi) |
| `.channels` | kuzatilayotgan kanallar ro'yxati |
| `.add @kanal` | kanal + muhokama guruhini qo'shish (avtomatik bog'laydi) |
| `.remove @kanal` | kanalni ro'yxatdan chiqarish |
| `.help` | buyruqlar ro'yxati |

Ish tartibi:

```
.add @kanalingiz
.word Birinchi!|Salom|Zo'r
.auto
.stats
```

`.add` nima qiladi: kanalni va uning **muhokama guruhini** topadi, `access_hash`'larini
keshga yozadi (keyin post paytida hech qanday qidiruv bo'lmasligi uchun), kerak bo'lsa
kanal va guruhga qo'shiladi (Telegram yangilanishlarni faqat a'zolarga yuboradi).

Javobda `tez yo'l: bor (0 qo'shimcha RPC)` ko'rsangiz — bot maksimal tezlikda ishlaydi.
`guruh YO'Q` bo'lsa, kanalda muhokama guruhi yoqilmagan (kanal sozlamalarida
"Discussion" ni ulang).

---

## 14. Muammolar va yechimlari

| Alomat | Sabab / yechim |
|---|---|
| `FLOOD_WAIT 300s` logda | Telegram sizni tez yozayotgan deb hisobladi. Bot o'zi kutadi. `MAX_PER_MINUTE` ni kamaytiring (10–15), bir necha soat "tinchlaning". |
| `sekin rejim` (slow mode) | Guruhda slow mode bor — sizning kommentingiz jismonan bloklanadi. Bu kanal sozlamasi, kod aybi emas. |
| Komment kelmayapti, `.stats` da `unresolved` | Kanalda muhokama guruhi yo'q yoki guruh yopiq. `.remove` + qayta `.add`. |
| `.add` "guruh topilmadi" | Guruhga a'zo bo'ling yoki `AUTO_JOIN=1` ekanini tekshiring. |
| `Session yaroqsiz` | Session bekor qilingan (boshqa joydan logout). `scripts/login.py` bilan yangilang. |
| `PEER_ID_INVALID` | Kesh eskirgan: `.remove @kanal` → `.add @kanal`. |
| Update umuman kelmayapti | Akkaunt kanalga **obuna emas**. `.add` qayta ishlating (`AUTO_JOIN=1`). |
| `.ping` da 100+ ms | Akkaunt uzoq DC'da. Server joyini o'zgartirish yordam beradi, kod emas. |
| Bot o'chib qoladi | `journalctl -u comment-sniper -n 100 --no-pager` — `Restart=always` 1 soniyada qayta ko'taradi. |
| `state.json` buzilgan | Fayl atomik yoziladi, lekin buzilsa: o'chirib qayta `.add` qiling (kanallar yo'qoladi). |

---

## 15. Xavfsizlik va Telegram qoidalari (o'qib chiqing)

- **Bu akkauntingiz nomidan yozadi.** `session string` — akkaunt kaliti. `chmod 600`,
  hech qayerga nusxalamang.
- **Begona kanallarda avtomatik komment — Telegram uchun spam.** Bu qoidabuzarlik
  hisoblanadi va akkaunt **bloklanishi mumkin**. Tavsiya: shaxsiy/ish kanallaringizda
  yoki o'zingiz boshqaradigan kanallarda ishlating; kerak bo'lsa alohida (asosiy
  bo'lmagan) akkauntdan foydalaning.
- `MAX_PER_MINUTE` ni real ehtiyojdan oshirmang. 2–3 kanal uchun 10–20 yetarli.
- Katta kanallarda **slow mode** bo'lishi mumkin — u holda "birinchi komment" texnik
  jihatdan imkonsiz, chunki Telegram o'zi bloklaydi.
- Bot faqat **public** kanallarni qo'sha oladi (username orqali).

---

## 16. Yana tezroq qilish mumkinmi?

Ha, lekin foyda kamayib boradi (chunki vaqt tarmoqda):

1. **Bir nechta akkaunt** — har biri alohida jarayon, bir xil kanallarda. Qaysi biri
   birinchi yetib borsa, o'sha yozadi (hozirgi kod bitta akkaunt uchun).
2. **Telethon o'rniga Rust/Go MTProto** — kod ishini 2 µs dan ~0.2 µs ga tushirardi,
   ya'ni umumiy 20 ms dan 0.002 ms tejash. Amalda sezilmaydi.
3. **Bir xil shahar/peering** — Amsterdamdagi instance allaqachon DC2/DC4 bilan
   ~1–3 ms. Bu eng katta yutuq va u allaqachon qilingan.
4. **Kanalni emas, guruhni tinglash** — qilingan (asosiy optimizatsiya).

---

## 17. Loyiha tuzilishi

```
sniper/
  classify.py   # har update uchun 1 µs: nima qilish kerakligini aniqlaydi (tarmoqsiz)
  hotpath.py    # post -> bitta sendMessage; dedupe, flood himoyasi
  commands.py   # .ping .stats .info .word .auto .pause .channels .add .remove
  store.py      # kanallar + matnlar (atomik JSON)
  metrics.py    # hisoblagichlar va kechikish namunalari
  ratelimit.py  # per-channel / global limit
  words.py      # matn rotatsiyasi
  config.py     # .env o'qish
  app.py        # client, handlerlar, socket tuning, --probe
scripts/
  login.py      # session string olish
  bench_hotpath.py  # kod ishini µs da o'lchash
deploy/
  comment-sniper.service  # systemd (Nice=-20)
  99-sniper.conf          # sysctl (BBR, fq, buferlar)
  tune.sh                 # hammasini qo'llaydi
tests/
  test_core.py  # 33 test: klassifikatsiya, rotatsiya, limit, saqlash, yuborish
```

Ichki ishlash:

```
Telegram ──TCP/MTProto──▶ Telethon ──▶ on_update (har paket)
                                          │
                                     classify()  ~0.9 µs
                                          │
                        ┌─────────────────┴──────────────────┐
                   guruh update'i                      kanal update'i
                (reply_to tayyor)                  (1 RPC: getDiscussionMessage)
                        │                                   │
                        └──────────▶ SendMessageRequest ◀───┘
                                          │
                                   1 RTT → ACK → .stats
```

---

## 18. Tez ma'lumotnoma

```bash
# holat
systemctl status comment-sniper
journalctl -u comment-sniper -f

# qayta ishga tushirish / to'xtatish
sudo systemctl restart comment-sniper
sudo systemctl stop comment-sniper

# RTT o'lchash
cd /opt/comment-sniper && sudo -u sniper .venv/bin/python -m sniper --probe 20

# kod ishini o'lchash (tarmoqsiz)
.venv/bin/python scripts/bench_hotpath.py 200000

# testlar
.venv/bin/python -m unittest discover -s tests -v
```
